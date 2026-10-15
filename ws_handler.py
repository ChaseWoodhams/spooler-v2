"""
Browser WebSocket handler: connection management and action dispatch.
"""

import asyncio
import ipaddress
import json
import uuid

import health
import jobs
import state
from discovery import discover_cc2_printers, discover_printers
from persistence import save_printers, save_tray_map
from printers import PRINTER_TYPES, make_printer
from spoolman import spoolman_assign, spoolman_set_location
from printers.protocol import (
    CMD_CAMERA, CMD_DELETE_FILES, CMD_LIGHT, CMD_LIST_FILES,
    CMD_HOME_AXES, CMD_MOVE_AXES, CMD_PAUSE, CMD_RESUME, CMD_STATUS, CMD_STOP,
)

MAX_WS_MSG = 1 * 1024 * 1024  # 1 MB

# One pending light-refresh task per printer — cancelled when a new light command arrives
_light_refresh_tasks: dict = {}


async def _run_light_refresh(printer) -> None:
    try:
        if printer.printer_type == "cc2":
            await asyncio.sleep(0.5)
            await printer.send_cmd(1002)
            await asyncio.sleep(1.5)
            await printer.send_cmd(1002)
        else:
            await asyncio.sleep(0.4)
            await printer.send_cmd(CMD_STATUS, {})
    except asyncio.CancelledError:
        pass
    finally:
        _light_refresh_tasks.pop(printer.id, None)


def _schedule_light_refresh(printer) -> None:
    old = _light_refresh_tasks.pop(printer.id, None)
    if old:
        old.cancel()
    _light_refresh_tasks[printer.id] = asyncio.create_task(_run_light_refresh(printer))


def _valid_ip_or_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    # Allow simple hostnames: letters, digits, hyphens, dots
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.")
    return bool(value) and all(c in allowed for c in value) and not value.startswith(".")


def _printer_is_idle(printer) -> bool:
    """Manual motion is permitted only when both print and machine state are idle."""
    status = printer.status or {}
    print_status = (status.get("PrintInfo") or {}).get("Status")
    machine_status = status.get("CurrentStatus") or []
    machine_code = machine_status[0] if machine_status else 0
    return print_status == 0 and machine_code == 0


async def _send_control_error(ws, message: str) -> None:
    await ws.send(json.dumps({"type": "error", "message": message}))


async def browser_handler(websocket) -> None:
    state.browser_clients.add(websocket)
    print(f"[Browser] Client connected ({len(state.browser_clients)} total)")

    for p in state.printers.values():
        await websocket.send(json.dumps({"type": "printer_update", "printer": p.to_dict()}))
    await websocket.send(json.dumps({"type": "printers_listed"}))  # the list is complete, even when empty: the page can say "No printers"
    await websocket.send(json.dumps({"type": "tray_map", "tray_map": state.tray_map}))
    await websocket.send(json.dumps({"type": "system_status", "status": health.snapshot()}))

    try:
        async for raw in websocket:
            if len(raw) > MAX_WS_MSG:
                await websocket.send(json.dumps({"type": "error", "message": "Message too large"}))
                continue
            await handle_browser_message(websocket, raw)
    except Exception:
        pass
    finally:
        state.browser_clients.discard(websocket)
        print(f"[Browser] Client disconnected ({len(state.browser_clients)} total)")


async def handle_browser_message(ws, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except Exception:
        return

    action     = msg.get("action")
    printer_id = msg.get("printer_id")
    printer    = state.printers.get(printer_id)
    loop       = asyncio.get_running_loop()

    if action == "list_printers":
        for p in state.printers.values():
            await ws.send(json.dumps({"type": "printer_update", "printer": p.to_dict()}))
        await ws.send(json.dumps({"type": "printers_listed"}))
        return

    if action == "discover":
        await ws.send(json.dumps({"type": "info", "message": "Scanning network…"}))
        known_ips = {p.ip for p in state.printers.values()}

        # CC1 (UDP broadcast) and CC2 (MQTT port scan) run in parallel
        cc1_found, cc2_ips = await asyncio.gather(
            loop.run_in_executor(None, discover_printers),
            loop.run_in_executor(None, discover_cc2_printers, known_ips),
        )

        new_count = 0
        added, known = [], 0
        for dev in cc1_found:
            pid = dev.get("MainboardID") or dev.get("SerialNumber") or dev.get("_ip")
            ip = dev.get("MainboardIP") or dev.get("_ip")
            if pid not in state.printers and not any(p.ip == ip for p in state.printers.values()):
                name = dev.get("Name") or dev.get("MachineName") or f"Printer {len(state.printers)+1}"
                pc   = make_printer("cc1", pid, ip, name, mainboard_id=pid)
                state.printers[pid] = pc
                pc._task = asyncio.create_task(pc.start())
                new_count += 1
                added.append({"id": pid, "name": name, "ip": ip})
            else:
                known += 1
        if new_count:
            await loop.run_in_executor(None, save_printers, state.printers)

        total = len(cc1_found) + len(cc2_ips)
        await ws.send(json.dumps({"type": "info", "message": f"Found {total} device(s)"}))
        if cc2_ips:
            await ws.send(json.dumps({"type": "cc2_discovered", "ips": cc2_ips}))
        # The scan is over: the page ends its Scanning state and lists what was found.
        await ws.send(json.dumps({"type": "discovery_done", "added": added, "known": known, "cc2": cc2_ips}))
        return

    if action == "add_printer":
        ip           = msg.get("ip", "").strip()
        name         = msg.get("name", f"Printer {len(state.printers)+1}").strip()
        printer_type = msg.get("printer_type", "cc1")
        access_code  = msg.get("access_code", "").strip()
        if not ip:
            await ws.send(json.dumps({"type": "error", "message": "IP address required"}))
            return
        if not _valid_ip_or_host(ip):
            await ws.send(json.dumps({"type": "error", "message": "Invalid IP address or hostname"}))
            return
        if printer_type not in PRINTER_TYPES:
            await ws.send(json.dumps({"type": "error", "message": f"Unknown printer type: {printer_type}"}))
            return
        if any(p.ip == ip for p in state.printers.values()):
            await ws.send(json.dumps({"type": "info", "message": "A printer with that IP is already added"}))
            return
        pid = uuid.uuid4().hex
        pc = make_printer(printer_type, pid, ip, name, access_code=access_code)
        state.printers[pid] = pc
        pc._task = asyncio.create_task(pc.start())
        await loop.run_in_executor(None, save_printers, state.printers)
        await ws.send(json.dumps({"type": "info", "message": f"Adding {name} ({ip})…"}))
        return

    if action == "remove_printer":
        p = state.printers.pop(printer_id, None)
        if p:
            p.stop()
        await loop.run_in_executor(None, save_printers, state.printers)
        await state.broadcast_to_browsers({"type": "printer_removed", "printer_id": printer_id})
        return

    if action == "update_printer":
        p = state.printers.get(printer_id)
        if not p:
            return
        new_name = msg.get("name", "").strip()
        if new_name:
            p.name = new_name
        needs_reconnect = False
        new_ip = msg.get("ip", "").strip()
        if new_ip and new_ip != p.ip:
            if any(q.ip == new_ip for q in state.printers.values() if q is not p):
                await ws.send(json.dumps({"type": "error", "message": "A printer with that IP is already added"}))
                return
            p.ip = new_ip
            needs_reconnect = True
        new_code = msg.get("access_code", "").strip()
        if new_code and new_code != p.access_code:
            p.access_code = new_code
            needs_reconnect = True
        if needs_reconnect:
            p.stop()
            p._task = asyncio.create_task(p.start())
        await loop.run_in_executor(None, save_printers, state.printers)
        await p._broadcast_state()
        return

    if action == "link_tray":
        pid      = printer_id
        tray_id  = msg.get("tray_id")
        spool_id = msg.get("spool_id")  # int or null
        if pid is None or tray_id is None:
            return
        key = str(tray_id)
        if pid not in state.tray_map:
            state.tray_map[pid] = {}
        if spool_id is None:
            state.tray_map[pid].pop(key, None)
        else:
            state.tray_map[pid][key] = spool_id
        await loop.run_in_executor(None, save_tray_map, state.tray_map)
        await state.broadcast_to_browsers({"type": "tray_map", "tray_map": state.tray_map})
        p = state.printers.get(pid)
        if p:
            loop.run_in_executor(None, p._update_filament_density)
            if spool_id is not None:
                loop.run_in_executor(None, spoolman_set_location, spool_id, pid)
        return

    if not printer:
        await ws.send(json.dumps({"type": "error", "message": "Printer not found"}))
        return

    cmd_map = {
        "pause":     (CMD_PAUSE,  {}),
        "resume":    (CMD_RESUME, {}),
        "stop":      (CMD_STOP,   {}),
        "status":    (CMD_STATUS, {}),
        "light_on":  (CMD_LIGHT, {"LightStatus": {"SecondLight": True,  "RgbLight": [0, 0, 0]}}),
        "light_off": (CMD_LIGHT, {"LightStatus": {"SecondLight": False, "RgbLight": [0, 0, 0]}}),
        "camera_on": (CMD_CAMERA, {"Enable": True}),
    }

    if action == "list_files":
        ok = await printer.request_file_list()
        if not ok:
            await ws.send(json.dumps({"type": "error", "message": "Could not retrieve file list"}))
        return

    if action == "start_print":
        active = jobs.active_job_for(printer.id)
        if active:
            await _send_control_error(ws, f"{printer.name} is running queued job #{active['id']} — wait for it to finish")
            return
        filename = msg.get("filename", "").strip()
        if not filename:
            await ws.send(json.dumps({"type": "error", "message": "No filename specified"}))
            return
        if ".." in filename.replace("\\", "/").split("/"):
            await ws.send(json.dumps({"type": "error", "message": "Invalid filename"}))
            return
        print_opts = msg.get("print_opts")
        if not isinstance(print_opts, (dict, type(None))):
            print_opts = None
        ok = await printer.start_print_file(filename, print_opts=print_opts)
        if not ok:
            await ws.send(json.dumps({"type": "error", "message": "Could not start print"}))
        return

    if action == "delete_files":
        files = msg.get("files", [])
        if (isinstance(files, list) and len(files) <= 50
                and all(isinstance(f, str) and f for f in files)):
            await printer.send_cmd(CMD_DELETE_FILES, {"FileList": files})
        return

    if action == "delete_file":
        filename = msg.get("filename", "").strip()
        parts = filename.replace("\\", "/").split("/")
        if not filename or ".." in parts:
            await ws.send(json.dumps({"type": "error", "message": "Invalid filename"}))
            return
        if printer.printer_type == "cc2":
            await printer.send_cmd(1047, {"filename": filename, "storage_media": "local"})
        elif printer.printer_type == "prusa":
            await printer.delete_file_prusa(filename)
        elif printer.printer_type == "moonraker":
            await printer.delete_file_moonraker(filename)
        else:
            await printer.send_cmd(CMD_DELETE_FILES, {"FileList": [filename]})
        return

    if action == "get_file_info":
        filename = msg.get("filename", "").strip()
        if filename and hasattr(printer, "fetch_file_info"):
            asyncio.create_task(printer.fetch_file_info(filename))
        return

    if action == "set_speed":
        speed = msg.get("speed")
        if isinstance(speed, (int, float)) and 10 <= int(speed) <= 200:
            if printer.printer_type == "cc2":
                await printer.send_cmd(1031, {"speed": int(speed)})
            else:
                await printer.send_cmd(CMD_LIGHT, {"PrintSpeedPct": int(speed)})
        return

    if action in ("move_axis", "home_axes"):
        if printer.printer_type != "cc1":
            await _send_control_error(ws, "Manual motion is currently available for CC1 printers only")
            return
        if not printer.connected:
            await _send_control_error(ws, "Printer is offline")
            return
        if not _printer_is_idle(printer):
            await _send_control_error(ws, "Manual motion is only available while the printer is idle")
            return

        axis = str(msg.get("axis", "")).upper()
        if action == "home_axes":
            if axis not in {"X", "Y", "Z", "XY", "XYZ"}:
                await _send_control_error(ws, "Invalid homing axis")
                return
            ok = await printer.send_cmd(CMD_HOME_AXES, {"Axis": axis})
        else:
            step = msg.get("step")
            if axis not in {"X", "Y", "Z"} or isinstance(step, bool) or not isinstance(step, (int, float)):
                await _send_control_error(ws, "Invalid movement request")
                return
            step = round(float(step), 1)
            if step == 0 or abs(step) not in {0.1, 1.0, 10.0}:
                await _send_control_error(ws, "Movement step must be 0.1, 1, or 10 mm")
                return
            ok = await printer.send_cmd(CMD_MOVE_AXES, {"Axis": axis, "Step": step})

        if not ok:
            await _send_control_error(ws, "Printer did not accept the motion command")
        return

    if action == "set_fan":
        if printer.printer_type != "cc1":
            await _send_control_error(ws, "Fan control from this dashboard is currently available for CC1 printers only")
            return
        fan_names = {"model": "ModelFan", "auxiliary": "AuxiliaryFan", "chamber": "BoxFan"}
        fan = fan_names.get(str(msg.get("fan", "")).lower())
        speed = msg.get("speed")
        if fan is None or isinstance(speed, bool) or not isinstance(speed, (int, float)):
            await _send_control_error(ws, "Invalid fan request")
            return
        speed = int(speed)
        if not 0 <= speed <= 100:
            await _send_control_error(ws, "Fan speed must be between 0 and 100 percent")
            return
        if not await printer.send_cmd(CMD_LIGHT, {"TargetFanSpeed": {fan: speed}}):
            await _send_control_error(ws, "Printer did not accept the fan command")
        return

    if action == "set_temperature":
        if printer.printer_type != "cc1":
            await _send_control_error(ws, "Temperature control from this dashboard is currently available for CC1 printers only")
            return
        target_name = str(msg.get("target", "")).lower()
        value = msg.get("value")
        limits = {"nozzle": ("TempTargetNozzle", 300), "bed": ("TempTargetHotbed", 110)}
        target = limits.get(target_name)
        if target is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            await _send_control_error(ws, "Invalid temperature request")
            return
        value = int(value)
        field, maximum = target
        if not 0 <= value <= maximum:
            await _send_control_error(ws, f"{target_name.title()} target must be between 0 and {maximum} °C")
            return
        if not await printer.send_cmd(CMD_LIGHT, {field: value}):
            await _send_control_error(ws, "Printer did not accept the temperature command")
        return

    if action in cmd_map:
        cmd, data = cmd_map[action]
        ok = await printer.send_cmd(cmd, data)
        if not ok:
            await ws.send(json.dumps({"type": "error", "message": "Printer not connected"}))
        elif action in ("light_on", "light_off"):
            _schedule_light_refresh(printer)
