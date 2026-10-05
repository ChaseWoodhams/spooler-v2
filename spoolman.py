"""
Spoolman integration: filament database and spool deduction.
"""

import asyncio
import json
import time
import urllib.parse
import urllib.request

import os

import state as _state
from outbox import PermanentError, outbox, post_use
from persistence import FILAMENT_DENSITY
from push import load_notif_settings, send_push_all
from state import broadcast_to_browsers


def _printer_location(printer_id: str) -> str:
    """Return the Spoolman location string for a printer — its name, not its UUID."""
    p = _state.printers.get(printer_id)
    return p.name if p else printer_id

SPOOLMAN_URL    = os.getenv("SPOOLMAN_URL", "http://localhost:7912").rstrip("/")
SPOOLMAN_DB_URL = "https://donkie.github.io/SpoolmanDB/filaments.json"


def get_spoolman_url() -> str:
    return SPOOLMAN_URL
SPOOLMAN_DB_TTL  = 3600  # re-fetch at most once per hour

_spoolman_db: list | None = None
_spoolman_db_fetched: float = 0.0


def get_spoolman_db() -> list:
    global _spoolman_db, _spoolman_db_fetched
    if _spoolman_db is not None and time.time() - _spoolman_db_fetched < SPOOLMAN_DB_TTL:
        return _spoolman_db
    try:
        with urllib.request.urlopen(SPOOLMAN_DB_URL, timeout=10) as resp:
            _spoolman_db = json.loads(resp.read())
            _spoolman_db_fetched = time.time()
            print(f"[SpoolmanDB] Loaded {len(_spoolman_db)} filaments")
    except Exception as e:
        print(f"[SpoolmanDB] Fetch failed: {e}")
        if _spoolman_db is None:
            _spoolman_db = []
    return _spoolman_db


def get_spool_density(printer_id: str) -> float:
    """Return the filament density (g/cm³) for the spool assigned to this printer.

    Falls back to the PLA default if Spoolman is unreachable or no spool is assigned.
    Designed to run in a thread pool executor.
    """
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(loc)}"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
        if data:
            density = data[0].get("filament", {}).get("density")
            if density and float(density) > 0:
                return float(density)
    except Exception:
        pass
    return FILAMENT_DENSITY


def spoolman_set_location(spool_id: int, printer_id: str) -> None:
    """Set location on a single spool without touching any other spools."""
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        req = urllib.request.Request(
            f"{base}/api/v1/spool/{spool_id}",
            data=json.dumps({"location": loc}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        urllib.request.urlopen(req, timeout=3).close()
        print(f"[Spoolman] Spool {spool_id} location → {loc}")
    except Exception as e:
        print(f"[Spoolman] Set location skipped ({e})")


def spoolman_assign(printer_id: str, spool_id: int | None) -> None:
    """Assign a spool to a printer in Spoolman (blocking — run in executor).

    Clears the location on any spool currently assigned to this printer, then
    sets location=printer_name on the new spool (if given).
    """
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        # Find currently assigned spool and clear it
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(loc)}"
        with urllib.request.urlopen(url, timeout=3) as resp:
            current = json.loads(resp.read())
        for s in current:
            if spool_id is None or s["id"] != spool_id:
                req = urllib.request.Request(
                    f"{base}/api/v1/spool/{s['id']}",
                    data=json.dumps({"location": ""}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PATCH",
                )
                urllib.request.urlopen(req, timeout=3).close()
        # Assign the new spool
        if spool_id is not None:
            req = urllib.request.Request(
                f"{base}/api/v1/spool/{spool_id}",
                data=json.dumps({"location": loc}).encode(),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            urllib.request.urlopen(req, timeout=3).close()
            print(f"[Spoolman] Spool {spool_id} → {loc}")
    except Exception as e:
        print(f"[Spoolman] Assign skipped ({e})")


def _notify_spool_level(
    result: dict,
    printer_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Broadcast low/empty spool warnings from a thread-pool executor."""
    remaining = result.get("remaining_weight", 0)
    total     = result.get("initial_weight", 0)
    name      = result.get("filament", {}).get("name") or f"Spool {result.get('id', '?')}"

    if remaining <= 0:
        msg: dict | None = {"type": "spool_empty", "spool": result, "printer_id": printer_id}
    elif total > 0 and (remaining / total) < 0.1:
        msg = {"type": "spool_low", "spool": result, "printer_id": printer_id}
    else:
        msg = None
    if msg:
        asyncio.run_coroutine_threadsafe(broadcast_to_browsers(msg), loop)

    notif = load_notif_settings()
    spool_low_cfg = notif.get("spool_low", {})
    if spool_low_cfg.get("enabled") and remaining > 0:
        threshold = float(spool_low_cfg.get("threshold", 100))
        if remaining <= threshold:
            send_push_all(
                f"Spool almost empty — {name}",
                f"{round(remaining)}g remaining on {printer_id}.",
            )


def _report_deduction(
    result: dict,
    amount_g: float,
    printer_id: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    name = result.get("filament", {}).get("name") or f"Spool {result.get('id', '?')}"
    remaining = result.get("remaining_weight", 0)
    print(f"[Spoolman] {amount_g}g deducted from '{name}' → {remaining}g left")
    if loop is not None:
        _notify_spool_level(result, printer_id, loop)


def spoolman_deduct_spool(
    spool_id: int,
    amount_g: float,
    printer_id: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Deduct filament from a specific spool by ID. Runs in a thread-pool executor.

    If Spoolman cannot be reached the deduction is queued on disk and replayed
    by flush_spoolman_outbox() once Spoolman answers again.
    """
    try:
        result = post_use(get_spoolman_url(), spool_id, amount_g)
    except PermanentError as e:
        print(f"[Spoolman] Deduct rejected for spool {spool_id} ({e})")
        return
    except Exception as e:
        outbox.enqueue(amount_g, printer_id, str(e), spool_id=spool_id)
        print(f"[Spoolman] Unreachable — queued {round(amount_g, 1)}g for spool {spool_id} ({e})")
        return
    _report_deduction(result, amount_g, printer_id, loop)


def _spool_id_at_location(location: str) -> int | None:
    url = f"{get_spoolman_url()}/api/v1/spool?location={urllib.parse.quote(location)}"
    with urllib.request.urlopen(url, timeout=3) as resp:
        data = json.loads(resp.read())
    return data[0]["id"] if data else None


def spoolman_deduct(printer_id: str, amount_g: float, loop: asyncio.AbstractEventLoop | None) -> None:
    """Deduct filament from the spool assigned to this printer's location in Spoolman.

    Fallback for single-colour prints where no per-tray tracking is available.
    If Spoolman is unreachable the deduction is queued against the printer's
    location and resolved to a spool when it is replayed.
    Runs in a thread-pool executor.
    """
    loc = _printer_location(printer_id)
    try:
        spool_id = _spool_id_at_location(loc)
    except Exception as e:
        outbox.enqueue(amount_g, printer_id, str(e), location=loc)
        print(f"[Spoolman] Unreachable — queued {round(amount_g, 1)}g for the spool at '{loc}' ({e})")
        return
    if spool_id is None:
        return
    spoolman_deduct_spool(spool_id, amount_g, printer_id, loop)


def _replay_deduction(entry: dict) -> dict:
    spool_id = entry.get("spool_id")
    if spool_id is None:
        spool_id = _spool_id_at_location(entry.get("location") or "")
        if spool_id is None:
            raise PermanentError(f"No spool is assigned to '{entry.get('location')}'")
    return post_use(get_spoolman_url(), spool_id, entry["amount_g"])


def flush_spoolman_outbox(loop: asyncio.AbstractEventLoop | None = None) -> dict:
    """Replay deductions queued while Spoolman was unreachable. Blocking — run in an executor."""
    if not outbox.pending_count():
        return {"sent": [], "dropped": [], "remaining": 0}
    report = outbox.flush(_replay_deduction)
    for entry, result in report["sent"]:
        _report_deduction(result, entry["amount_g"], entry["printer_id"], loop)
    for entry in report["dropped"]:
        print(f"[Spoolman] Dropped queued deduction of {entry['amount_g']}g ({entry['last_error']})")
    if report["sent"]:
        print(f"[Spoolman] Replayed {len(report['sent'])} queued deduction(s), "
              f"{report['remaining']} still pending")
    return report


_spool_levels: dict = {}   # spool id → (fetched at, level)
SPOOL_LEVEL_TTL = 30.0


def spool_level(spool_id: int) -> dict | None:
    """{"name", "remaining_g"} of a Spoolman spool, or None when Spoolman can't say. Cached briefly, failures too,
    so a page that polls every few seconds never waits on an unreachable Spoolman."""
    cached = _spool_levels.get(spool_id)
    if cached and time.time() - cached[0] < SPOOL_LEVEL_TTL:
        return cached[1]
    level = None
    try:
        with urllib.request.urlopen(f"{get_spoolman_url()}/api/v1/spool/{int(spool_id)}", timeout=2) as resp:
            spool = json.loads(resp.read())
        filament = spool.get("filament") or {}
        name = " ".join(x for x in (filament.get("vendor", {}).get("name"), filament.get("name")) if x) or None
        remaining = spool.get("remaining_weight")
        level = {"name": name, "remaining_g": round(float(remaining), 1) if remaining is not None else None}
    except Exception:
        pass
    _spool_levels[spool_id] = (time.time(), level)
    return level
