"""
Outcome prompt: what the user is asked, and pushed about, when a print ends.

A Print that closes is pushed once (printer, project and plate in the title; the outcome the printer reported and
actual versus estimated time in the body; a deep link; on Android Chrome a Printed OK · Bed cleared button) and, where
the printer has a camera, a snapshot is stored on the Print. The server holds the set of Prints awaiting an answer: the
printer's latest closed Print while its Outcome is unconfirmed, or while a queued Job waits for Bed cleared. Reminders
repeat after REMINDER_DELAY_S for as long as a Print awaits; they never assume the bed is clear.
"""

import asyncio
import json
import os
import socket
import threading
import time
import urllib.parse
from pathlib import Path

import bed_cleared
import database
import jobs
import print_history
import push
import snapshots
import state

REMINDER_DELAY_S = int(os.getenv("REMINDER_DELAY_S", str(30 * 60)))
REMINDER_CHECK_S = 60
SNAPSHOT_TIMEOUT_S = 5

_reminded: dict = {}  # print id → when it was last pushed or reminded about (kept in memory; a restart re-reminds once)


def send_push(title: str, body: str, data: dict | None = None) -> None:
    push.send_push_all(title, body, data)


def run_in_background(fn, *args) -> None:
    """Pushing and fetching a snapshot block on the network, so they never run on the status handler's thread."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def init() -> None:
    _reminded.clear()
    if on_closed not in print_history.closed_hooks:
        print_history.closed_hooks.append(on_closed)


def shutdown() -> None:
    if on_closed in print_history.closed_hooks:
        print_history.closed_hooks.remove(on_closed)


# ── Push payload ───────────────────────────────────────────────────────────────

def _clock(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return "under 1m"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _names(record: dict) -> tuple:
    """(project name, plate name) for a Print made from a plate, else (None, None). A quick print's project is the
    model: it has a name (the model's) but no plate name, because nobody sees the hidden project or its plates."""
    if not record.get("project_id"):
        return None, None
    with database.transaction() as conn:
        row = conn.execute("SELECT name, data, quick FROM projects WHERE id = ?", (record["project_id"],)).fetchone()
    if row is None:
        return None, None
    if row["quick"]:
        return row["name"], None
    plate = next((p for p in json.loads(row["data"] or "{}").get("plates", []) if p.get("id") == record.get("plate_id")), None)
    plate_name = (plate or {}).get("name") or (f"Plate {record['plate_id']}" if record.get("plate_id") else None)
    return row["name"], plate_name


def _is_quick(record: dict) -> bool:
    if not record.get("project_id"):
        return False
    with database.transaction() as conn:
        row = conn.execute("SELECT quick FROM projects WHERE id = ?", (record["project_id"],)).fetchone()
    return bool(row and row["quick"])


def deep_link(record: dict) -> str:
    """The project with the printer's card focused, or the printer's own page when the Print has no project or its
    project is a hidden Quick print one (that page is never linked to)."""
    if record.get("project_id") and not _is_quick(record):
        return f"/#projects={record['project_id']}&printer={urllib.parse.quote(record['printer_id'], safe='')}"
    return f"/#printer={urllib.parse.quote(record['printer_id'], safe='')}"


def push_payload(record: dict, reminder: bool = False) -> tuple:
    """(title, body, data) for a closed Print. `data` is what the service worker reads."""
    project, plate = _names(record)
    what = f"{project} · {plate}" if project and plate else project or record.get("file_name") or "a print"
    title = f"{record['printer_name']} finished {what}"
    body = f"{record['outcome']} · {_clock(record.get('duration_s'))}"
    if record.get("estimate_s"):
        body += f" of {_clock(record['estimate_s'])} estimated"
    if record.get("outcome_unconfirmed"):
        body += " · tap to confirm"
    if reminder:
        title, body = f"Reminder: {title}", f"Still waiting for you. {body}"
    printer_id = record["printer_id"]
    ok = record["outcome"] == "completed"
    data = {"url": deep_link(record), "print_id": record["id"], "printer_id": printer_id, "tag": f"print-{record['id']}",
            "actions": [{"action": "bed-cleared", "title": "Printed OK · Bed cleared"}] if ok else [],
            "bed_cleared": {"url": f"/api/printers/{urllib.parse.quote(printer_id, safe='')}/bed-cleared",
                            "body": {"print_id": record["id"], "outcome": "completed"}}}
    return title, body, data


# ── Closing a Print ────────────────────────────────────────────────────────────

def on_closed(record: dict) -> None:
    run_in_background(notify, record)


def notify(record: dict) -> None:
    try:
        snapshot = take_snapshot(record)
        if snapshot:
            record = {**record, "end_snapshot": snapshot}
        _reminded[record["id"]] = record.get("ended_at") or time.time()
        send_push(*push_payload(record))
    except Exception as e:
        print(f"[Outcome] Could not notify about print {record.get('id')}: {e}")


def snapshot_path(relative: str) -> Path:
    return jobs._data_dir / relative


def take_snapshot(record: dict) -> str | None:
    """Store an end-of-print snapshot on the Print; returns its stored path, or None (no camera, or it failed)."""
    printer = state.printers.get(record["printer_id"])
    url = getattr(printer, "camera_url", None)
    if not url:
        return None
    try:
        frame = snapshots.grab_frame(url, timeout=SNAPSHOT_TIMEOUT_S)
    except (OSError, socket.error) as e:
        print(f"[Outcome] No snapshot from {record['printer_name']}: {e}")
        return None
    if not frame:
        return None
    relative = f"snapshots/print-{record['id']}.jpg"
    path = snapshot_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame)
    print_history.set_end_snapshot(record["id"], relative)
    return relative


def snapshot_file(print_id: int) -> Path:
    record = print_history.find_print(print_id)
    if record is None:
        raise jobs.JobError("Print not found", 404)
    path = snapshot_path(record["end_snapshot"]) if record["end_snapshot"] else None
    if path is None or not path.is_file():
        raise jobs.JobError("No snapshot for this print", 404)
    return path


# ── The set of Prints awaiting an answer ───────────────────────────────────────

def awaiting() -> list:
    """Each printer's latest closed Print while its Outcome is unconfirmed or a queued Job waits for Bed cleared."""
    queued = {j["printer_id"] for j in jobs.list_jobs() if j["state"] == jobs.QUEUED and j["printer_id"]}
    out = []
    for record in print_history.latest_closed_prints():
        if state.printers.get(record["printer_id"]) is None:
            continue
        needs_bed = record["printer_id"] in queued and not bed_cleared.status(record["printer_id"])["valid"]
        if not (record["outcome_unconfirmed"] or needs_bed):
            continue
        project, plate = _names(record)
        out.append({"print_id": record["id"], "printer_id": record["printer_id"], "printer_name": record["printer_name"],
                    "file_name": record["file_name"], "project_id": record["project_id"], "project_name": project,
                    "plate_id": record["plate_id"], "plate_name": plate, "quick": _is_quick(record), "reported_outcome": record["outcome"],
                    "outcome_unconfirmed": record["outcome_unconfirmed"], "bed_cleared_needed": needs_bed,
                    "ended_at": record["ended_at"], "duration_s": record["duration_s"], "estimate_s": record["estimate_s"],
                    "snapshot": f"/api/prints/{record['id']}/snapshot" if record["end_snapshot"] else None,
                    "url": deep_link(record)})
    return out


# ── Reminders ──────────────────────────────────────────────────────────────────

def send_due_reminders(now: float | None = None) -> int:
    """Push a reminder for every awaiting Print that has gone REMINDER_DELAY_S without one. Returns how many."""
    now = time.time() if now is None else now
    sent = 0
    for item in awaiting():
        if now - _reminded.get(item["print_id"], item["ended_at"] or now) < REMINDER_DELAY_S:
            continue
        _reminded[item["print_id"]] = now
        send_push(*push_payload(print_history.find_print(item["print_id"]), reminder=True))
        sent += 1
    return sent


async def reminder_loop() -> None:
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(REMINDER_CHECK_S)
        try:
            await loop.run_in_executor(None, send_due_reminders)
        except Exception as e:
            print(f"[Outcome] Reminder check failed: {e}")
