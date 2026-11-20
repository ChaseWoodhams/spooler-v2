"""
Base printer connection: shared state, status tracking, history, and broadcast.

Subclasses (CC1Connection, CC2Connection, …) must implement:
  - connect()   — establish the transport and run the receive loop
  - send_cmd()  — send a CMD_* command to the printer
"""

import asyncio
import random
import time

import jobs
import print_history
from gcode_meta import extrusion_share_through_layer
import state
from persistence import FILAMENT_DENSITY, filament_mm_to_grams, save_printers
from printers.protocol import decode_printinfo
from push import load_notif_settings, send_push_all
from spoolman import get_spool_density, spoolman_deduct, spoolman_deduct_spool

RECONNECT_BASE_S = 2    # delay after an established link drops
RECONNECT_MAX_S  = 60   # ceiling for repeated connection failures
STALE_LINK_S     = 65   # silence after which an open link is treated as dead


def reconnect_delay(
    attempts: int,
    base: float = RECONNECT_BASE_S,
    cap: float = RECONNECT_MAX_S,
    jitter: float = 0.2,
    rand=None,
) -> float:
    """Seconds to wait before the next connection attempt.

    `attempts` counts consecutive failed attempts; 0 means an established link
    just dropped and is retried quickly. Failures back off exponentially with
    ±jitter so several printers don't retry in lockstep.
    """
    if attempts <= 0:
        return base
    rand = rand or random.random
    delay = base * 2 ** min(attempts, 10)
    return min(cap, delay * (1 - jitter + 2 * jitter * rand()))


class PrinterConnection:
    def __init__(
        self,
        printer_id: str,
        ip: str,
        name: str,
        mainboard_id: str = "",
        printer_type: str = "cc1",
        access_code: str = "",
    ):
        self.id           = printer_id
        self.ip           = ip
        self.name         = name
        self.mainboard_id = mainboard_id
        self.printer_type = printer_type
        self.access_code  = access_code
        # Link telemetry for the health panel; the `connected` property keeps the timestamps current.
        self.last_message_at: float | None = None
        self.last_error: str | None = None
        self.reconnect_attempts = 0
        self.next_retry_at: float | None = None
        self._links_opened = 0
        self.connected    = False
        self.status: dict = {}
        self.attrs: dict  = {}
        self.camera_url: str | None = None
        self.filament_density: float = FILAMENT_DENSITY
        self._task: asyncio.Task | None = None
        self._last_print_status = None
        self._print_start_time: float | None = None
        self._spool_extrusion: dict  = {}   # spool_id -> mm used this print
        self._extrusion_snapshot: float = 0.0  # TotalExtrusion at last tray swap
        self._current_print_spool: int | None = None
        self._notif_state: dict = {
            "last_status":      None,
            "nozzle_idle_fired": False,
            "layer_fired":      False,
            "nozzle_hot_fired": False,
        }

    # ── Public interface ───────────────────────────────────────────────────────

    def _decoded_printinfo(self) -> dict:
        """Decode PrintInfo hex keys and normalise CC1 time fields.

        CC1 SDCP firmware exposes elapsed/total time as CurrentTicks/TotalTicks
        (both in seconds) instead of PrintTime/RemainTime.  Normalise here so the
        browser and history recording can use the same field names regardless of
        printer type.
        """
        pi = decode_printinfo(self.status.get("PrintInfo", {}))
        if self.printer_type == "cc1" and not pi.get("PrintTime"):
            ct = pi.get("CurrentTicks") or 0
            tt = pi.get("TotalTicks") or 0
            if ct:
                pi["PrintTime"]  = ct
                pi["RemainTime"] = max(0, tt - ct)
        return pi

    def to_dict(self) -> dict:
        pi = self._decoded_printinfo()
        filament_mm = pi.get("TotalExtrusion", 0) or 0
        # Replace raw PrintInfo (may have hex-encoded SDCP keys) with decoded version
        # so the browser can read plain field names like Filename directly.
        status = {**self.status, "PrintInfo": pi} if "PrintInfo" in self.status else self.status
        return {
            "id":              self.id,
            "ip":              self.ip,
            "name":            self.name,
            "printer_type":    self.printer_type,
            "has_access_code": bool(self.access_code),
            "mainboard_id":    self.mainboard_id,
            "connected":       self.connected,
            "status":          status,
            "attrs":           self.attrs,
            "camera_url":      self.camera_url,
            "filament_mm":     round(filament_mm, 1),
            "filament_g":      filament_mm_to_grams(filament_mm, self.filament_density),
        }

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        value = bool(value)
        if value == getattr(self, "_connected", None):
            return
        self._connected = value
        now = time.time()
        if value:
            self._links_opened     += 1
            self.connected_since    = now
            self.disconnected_since = None
            self.next_retry_at      = None
            self.last_error         = None
        else:
            self.connected_since    = None
            self.disconnected_since = now

    def _note_activity(self) -> None:
        """Record that the printer sent something (feeds the stale-link watchdog)."""
        self.last_message_at = time.time()

    def link_silence_s(self, now: float | None = None) -> float:
        """Seconds since the printer last sent anything on the current link."""
        now = time.time() if now is None else now
        return now - max(self.last_message_at or 0, self.connected_since or 0)

    def link_is_stale(self, now: float | None = None) -> bool:
        return self.connected and self.link_silence_s(now) > STALE_LINK_S

    def link_status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "id":                 self.id,
            "name":               self.name,
            "printer_type":       self.printer_type,
            "connected":          self.connected,
            "connected_since":    self.connected_since,
            "disconnected_since": self.disconnected_since,
            "last_message_age_s": round(now - self.last_message_at, 1) if self.last_message_at else None,
            "reconnect_attempts": self.reconnect_attempts,
            "next_retry_in_s":    (max(0, round(self.next_retry_at - now))
                                   if self.next_retry_at and not self.connected else None),
            "last_error":         self.last_error,
        }

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def start(self) -> None:
        try:
            while True:
                links_before = self._links_opened
                await self.connect()
                if self._links_opened != links_before:
                    self.reconnect_attempts = 0
                else:
                    self.reconnect_attempts += 1
                delay = reconnect_delay(self.reconnect_attempts)
                self.next_retry_at = time.time() + delay
                if self.reconnect_attempts:
                    print(f"[Printer {self.name}] Retrying in {delay:.0f} s (attempt {self.reconnect_attempts}) …")
                else:
                    print(f"[Printer {self.name}] Link closed — reconnecting in {delay:.0f} s …")
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass

    # ── Subclass contract ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        raise NotImplementedError

    async def send_cmd(self, cmd: int, data: dict) -> bool:
        raise NotImplementedError

    def _update_filament_density(self) -> None:
        self.filament_density = get_spool_density(self.id)

    def _on_tray_change(self, new_spool_id: int | None) -> None:
        """Record + immediately deduct filament used by the outgoing spool.

        Deducting at swap time (rather than waiting for print end) means partial
        prints are correctly accounted for if the server restarts or the printer
        disconnects mid-print.  Pass new_spool_id=None as a sentinel at print end
        to flush the last spool's usage.
        """
        current_mm = float(self._decoded_printinfo().get("TotalExtrusion", 0) or 0)
        delta = current_mm - self._extrusion_snapshot
        if self._current_print_spool is not None and delta > 0:
            self._spool_extrusion[self._current_print_spool] = (
                self._spool_extrusion.get(self._current_print_spool, 0) + delta
            )
            g = filament_mm_to_grams(delta, self.filament_density)
            if g > 0:
                try:
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(
                        None, spoolman_deduct_spool,
                        self._current_print_spool, g, self.id, loop,
                    )
                except RuntimeError:
                    pass
        self._extrusion_snapshot = current_mm
        self._current_print_spool = new_spool_id

    async def enable_camera(self) -> bool:
        """Ask the printer to (re)start its video feed. False if the printer has no such command."""
        return False

    async def request_file_list(self) -> bool:
        from printers.protocol import CMD_LIST_FILES
        return await self.send_cmd(CMD_LIST_FILES, {"Url": "/", "IsDir": True})

    async def start_print_file(self, filename: str, print_opts: dict | None = None) -> bool:
        from printers.protocol import CMD_START
        return await self.send_cmd(CMD_START, {"Filename": filename})

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _check_notifications(self) -> None:
        s = load_notif_settings()
        if not s:
            return
        ns = self._notif_state
        pi = self._decoded_printinfo()
        status      = pi.get("Status", 0)
        nozzle      = self.status.get("TempOfNozzle") or self.status.get("NozzleTemp") or 0
        layer       = pi.get("CurrentLayer", 0)
        is_idle     = status == 0
        is_printing = status in (3, 6)
        is_done     = status in (9, 8)
        last        = ns["last_status"]

        # CC2 typically goes 3→0 on completion (no 9/8 reported).
        # Fire "finished" on any transition away from an active print state.
        _was_printing = last in (2, 3, 4, 5, 6, 7)
        _print_ended  = (is_done or is_idle) and _was_printing
        if s.get("finished", {}).get("enabled") and _print_ended:
            if status == 8:
                send_push_all(f"{self.name} — Print cancelled", "Your print was cancelled.")
            else:
                send_push_all(f"{self.name} — Print complete", "Your print is complete.")

        if s.get("nozzle_idle", {}).get("enabled") and is_idle:
            thr = s["nozzle_idle"].get("threshold", 50)
            if nozzle > thr and not ns["nozzle_idle_fired"]:
                send_push_all(f"{self.name} — Nozzle hot", f"Nozzle is {round(nozzle)}°C while idle.")
                ns["nozzle_idle_fired"] = True
            elif nozzle <= thr:
                ns["nozzle_idle_fired"] = False
        elif not is_idle:
            ns["nozzle_idle_fired"] = False

        if s.get("layer", {}).get("enabled") and is_printing:
            target = s["layer"].get("layer", 1)
            if layer >= target and not ns["layer_fired"]:
                send_push_all(f"{self.name} — Layer {target} reached", f"Currently on layer {layer}.")
                ns["layer_fired"] = True
            if layer < target:
                ns["layer_fired"] = False
        if not is_printing:
            ns["layer_fired"] = False

        if s.get("nozzle_printing", {}).get("enabled") and is_printing:
            thr = s["nozzle_printing"].get("threshold", 260)
            if nozzle > thr and not ns["nozzle_hot_fired"]:
                send_push_all(f"{self.name} — Nozzle overheat", f"Nozzle is {round(nozzle)}°C during print.")
                ns["nozzle_hot_fired"] = True
            elif nozzle <= thr:
                ns["nozzle_hot_fired"] = False
        elif not is_printing:
            ns["nozzle_hot_fired"] = False

        ns["last_status"] = status

    async def _broadcast_state(self) -> None:
        self._check_notifications()
        await state.broadcast_to_browsers({
            "type":    "printer_update",
            "printer": self.to_dict(),
        })

    def _estimated_filament_mm(self, end_status: int | None) -> float:
        """Filament used by the job that just ended, from its slice estimate. Blocking (reads the G-code).

        The Centauri Carbons never report TotalExtrusion, so without this a finished
        print deducts nothing from its spool. A finished print uses the whole estimate;
        a stopped one uses the share of the G-code's extrusion up to the layer it reached."""
        entry = jobs.active_job_for(self.id)
        job = jobs.get_job(entry["id"]) if entry else None
        estimate_mm = float((job or {}).get("estimate_mm") or 0)
        if estimate_mm <= 0:
            return 0.0
        if end_status == 9:
            return estimate_mm
        layer, total = entry.get("layers") or (0, 0)
        if not layer:
            return 0.0
        path = jobs.stored_gcode_path(job)
        try:
            return estimate_mm * extrusion_share_through_layer(path, layer)
        except (OSError, TypeError):
            return estimate_mm * min(layer / total, 1.0) if total else 0.0  # file gone: assume even layers

    async def _check_print_transition(self) -> None:
        pi = self._decoded_printinfo()
        cur_status = pi.get("Status")

        ACTIVE   = {1, 2, 3, 4, 7, 9, 10, 12, 13, 15, 16, 18, 19, 20, 21}
        PRINTING = {2, 3, 4, 13}

        if cur_status in ACTIVE and self._last_print_status not in ACTIVE:
            self._print_start_time = time.time()
            # Initialise per-spool tracking for this print
            self._spool_extrusion = {}
            self._extrusion_snapshot = 0.0
            canvas = self.status.get("canvas_info", {})
            active_tray = canvas.get("active_tray_id", -1)
            self._current_print_spool = (
                (state.tray_map.get(self.id) or {}).get(str(active_tray))
                if active_tray >= 0 else None
            )

        if cur_status in (9, 8, 14, 0) and self._last_print_status in PRINTING | {5, 6}:
            filament_mm = pi.get("TotalExtrusion", 0) or 0
            filename    = pi.get("Filename", "")
            if filament_mm <= 0:
                filament_mm = await asyncio.get_running_loop().run_in_executor(
                    None, self._estimated_filament_mm, cur_status)
            if filament_mm > 0 or filename:
                loop = asyncio.get_running_loop()
                density    = await loop.run_in_executor(None, get_spool_density, self.id)
                self.filament_density = density
                filament_g = filament_mm_to_grams(filament_mm, density)
                print(f"[History] Print ended: {filename} – {filament_mm:.0f}mm / {filament_g}g"
                      f" (density {density} g/cm³)")
                spool_mm: dict = {}
                if filament_mm > 0:
                    # Flush + deduct the last active spool immediately
                    self._on_tray_change(None)
                    if self._spool_extrusion:
                        # Log per-spool breakdown (deduction already fired in _on_tray_change)
                        for spool_id, mm in self._spool_extrusion.items():
                            g = filament_mm_to_grams(mm, density)
                            print(f"[History] → spool {spool_id}: {mm:.0f}mm / {g}g")
                        spool_mm = dict(self._spool_extrusion)
                    else:
                        # No per-tray mapping: fall back to printer-location lookup
                        loop.run_in_executor(
                            None, spoolman_deduct, self.id, filament_g, loop,
                        )
                # Attached while the print's history record is still open (it closes below)
                await loop.run_in_executor(
                    None, print_history.record_usage, self.id, filament_mm, density, spool_mm,
                )

        jobs.on_printer_status(self)
        record = print_history.on_printer_status(self)
        if record is not None:
            await state.broadcast_to_browsers({"type": "print_recorded", "print": record})
        self._last_print_status = cur_status
