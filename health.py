"""
Service health: Spoolman reachability, event-loop responsiveness, supervisor
state, and the system-status snapshot shown in the dashboard's health panel.
"""

import asyncio
import json
import os
import time
import urllib.request
from pathlib import Path

import backup
import state
from outbox import outbox
from persistence import DATA_DIR
from spoolman import flush_spoolman_outbox, get_spoolman_url

STARTED_AT             = time.time()
SPOOLMAN_CHECK_S       = 15
STATUS_BROADCAST_S     = 15
SUPERVISOR_STATUS_FILE = DATA_DIR / "supervisor_status.json"
SUPERVISOR_STALE_S     = 60

spoolman: dict = {
    "ok":         None,   # None until the first probe completes
    "checked_at": None,
    "latency_ms": None,
    "version":    None,
    "error":      None,
    "down_since": None,
}
loop_lag_ms: float = 0.0


def uptime_s() -> int:
    return int(time.time() - STARTED_AT)


# ── Spoolman ───────────────────────────────────────────────────────────────────

def probe_spoolman(base_url: str, want_version: bool, timeout: float = 3.0) -> dict:
    """Blocking health probe of Spoolman — run in an executor."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=timeout) as resp:
            resp.read()
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": str(e) or type(e).__name__}
    result = {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1), "error": None}
    if want_version:
        try:
            with urllib.request.urlopen(f"{base_url}/api/v1/info", timeout=timeout) as resp:
                result["version"] = json.loads(resp.read()).get("version")
        except Exception:
            pass
    return result


def apply_spoolman_probe(result: dict, now: float) -> tuple:
    """Record a probe result. Returns (was_ok, is_ok)."""
    was_ok = spoolman["ok"]
    spoolman.update(result)
    spoolman["checked_at"] = now
    if result["ok"]:
        spoolman["down_since"] = None
    elif was_ok is not False:
        spoolman["down_since"] = now
    return was_ok, result["ok"]


def _resync_spool_locations(loop: asyncio.AbstractEventLoop) -> None:
    for p in list(state.printers.values()):
        sync = getattr(p, "_sync_spoolman_locations", None)
        if sync is not None and p.connected:
            loop.run_in_executor(None, sync)


async def spoolman_monitor_loop() -> None:
    """Probe Spoolman; on recovery replay queued deductions and re-sync spool locations."""
    loop = asyncio.get_running_loop()
    while True:
        result = await loop.run_in_executor(
            None, probe_spoolman, get_spoolman_url(), spoolman["version"] is None,
        )
        was_ok, is_ok = apply_spoolman_probe(result, time.time())
        if was_ok is not is_ok:
            if is_ok:
                print(f"[Health] Spoolman reachable (v{spoolman['version']}, {spoolman['latency_ms']} ms)")
            else:
                print(f"[Health] Spoolman unreachable: {spoolman['error']}")
            await broadcast_status()
        if is_ok and was_ok is False:
            _resync_spool_locations(loop)
        if is_ok and outbox.pending_count():
            report = await loop.run_in_executor(None, flush_spoolman_outbox, loop)
            if report["sent"] or report["dropped"]:
                await broadcast_status()
        await asyncio.sleep(SPOOLMAN_CHECK_S)


# ── Spooler itself ─────────────────────────────────────────────────────────────

async def loop_lag_monitor() -> None:
    """Measure how late the event loop wakes up — a busy or blocked loop shows up here."""
    global loop_lag_ms
    while True:
        started = time.monotonic()
        await asyncio.sleep(1)
        loop_lag_ms = max(0.0, round((time.monotonic() - started - 1) * 1000, 1))


def event_loop_responsive(timeout: float = 2.0) -> bool:
    """True if the main event loop runs a trivial coroutine within `timeout`. Call from a thread."""
    loop = state.loop
    if loop is None or not loop.is_running():
        return False

    async def _ping() -> bool:
        return True

    try:
        return asyncio.run_coroutine_threadsafe(_ping(), loop).result(timeout)
    except Exception:
        return False


def read_supervisor_status(path: Path | None = None, now: float | None = None) -> dict | None:
    """Status written by supervisor.py, or None when Spooler isn't running under it."""
    path = path or SUPERVISOR_STATUS_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    now = time.time() if now is None else now
    data["stale"] = now - float(data.get("updated_at") or 0) > SUPERVISOR_STALE_S
    return data


# ── Snapshot + broadcast ───────────────────────────────────────────────────────

_time_models: dict = {"signature": None, "lines": []}


def time_model_lines() -> list:
    """The read-only "Time model, <bundle>: N slices, +/-E%" lines, refitted only when the calibration log has changed."""
    try:
        import calibration
        signature = calibration.signature()
        if signature != _time_models["signature"]:
            _time_models.update(signature=signature, lines=calibration.fit_models().status_lines())
        return _time_models["lines"]
    except Exception:
        return []


def snapshot() -> dict:
    now = time.time()
    return {
        "generated_at": now,
        "spooler": {
            "started_at":      STARTED_AT,
            "uptime_s":        int(now - STARTED_AT),
            "pid":             os.getpid(),
            "loop_lag_ms":     loop_lag_ms,
            "browser_clients": len(state.browser_clients),
        },
        "spoolman":   {**spoolman, "url": get_spoolman_url(), "pending_deductions": outbox.pending_count()},
        "printers":   [p.link_status(now) for p in list(state.printers.values())],
        "backups":    backup.status_snapshot(now),
        "supervisor": read_supervisor_status(now=now),
        "time_models": time_model_lines(),
    }


async def broadcast_status() -> None:
    if state.browser_clients:
        await state.broadcast_to_browsers({"type": "system_status", "status": snapshot()})


def broadcast_status_threadsafe() -> None:
    loop = state.loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_status(), loop)


async def status_broadcast_loop() -> None:
    while True:
        await asyncio.sleep(STATUS_BROADCAST_S)
        await broadcast_status()
