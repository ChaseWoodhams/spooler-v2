"""
Bed cleared and per-printer auto-start.

The user confirms, from any logged-in session, that a printer's bed is clear. A
confirmation only counts if it is newer than the end of the printer's last
Print, so it expires when the next print finishes. With auto-start on (the
default; a printer with no settings row is on) confirming releases the next
queued Job through jobs.start_job, so one-job-per-printer protection and
preflight still apply. With it off the printer is only marked ready.
"""

import time

import database
import jobs
import print_history
import state


def _require_printer(printer_id: str) -> None:
    if state.printers.get(printer_id) is None:
        raise jobs.JobError("Printer not found", 404)


def auto_start(printer_id: str) -> bool:
    with database.transaction() as conn:
        row = conn.execute("SELECT auto_start FROM printer_settings WHERE printer_id = ?", (printer_id,)).fetchone()
    return True if row is None else bool(row["auto_start"])


def set_auto_start(printer_id: str, enabled) -> dict:
    _require_printer(printer_id)
    if not isinstance(enabled, bool):
        raise jobs.JobError("enabled must be true or false")
    with database.transaction() as conn:
        conn.execute("INSERT INTO printer_settings (printer_id, auto_start, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT (printer_id) DO UPDATE SET auto_start = excluded.auto_start, "
                     "updated_at = excluded.updated_at", (printer_id, int(enabled), time.time()))
    return {"printer_id": printer_id, "auto_start": enabled}


def status(printer_id: str) -> dict:
    """Whether a Bed cleared confirmation currently counts for this printer."""
    _require_printer(printer_id)
    with database.transaction() as conn:
        cleared = conn.execute("SELECT MAX(cleared_at) AS at FROM bed_cleared WHERE printer_id = ?",
                               (printer_id,)).fetchone()["at"]
        last = conn.execute("SELECT MAX(ended_at) AS at, COUNT(*) FILTER (WHERE outcome IS NULL) AS running "
                            "FROM prints WHERE printer_id = ?", (printer_id,)).fetchone()
    valid = cleared is not None and not last["running"] and (last["at"] is None or cleared > last["at"])
    return {"printer_id": printer_id, "auto_start": auto_start(printer_id), "cleared_at": cleared,
            "last_print_ended_at": last["at"], "valid": valid}


async def confirm(printer_id: str, print_id=None, outcome=None, failure_reasons=None, note=None) -> dict:
    """Record Bed cleared, optionally confirming the finished print's outcome in the same action."""
    _require_printer(printer_id)
    with database.transaction() as conn:
        if conn.execute("SELECT 1 FROM prints WHERE printer_id = ? AND outcome IS NULL", (printer_id,)).fetchone():
            raise jobs.JobError("The printer is still printing", 409)
        if print_id is None and outcome is not None:
            row = conn.execute("SELECT id FROM prints WHERE printer_id = ? ORDER BY id DESC LIMIT 1",
                               (printer_id,)).fetchone()
            print_id = row["id"] if row else None
        elif print_id is not None and not conn.execute(
                "SELECT 1 FROM prints WHERE id = ? AND printer_id = ?", (print_id, printer_id)).fetchone():
            raise jobs.JobError("Print not found for this printer", 404)
    record = print_history.set_outcome(print_id, outcome, failure_reasons, note) if outcome is not None else None
    with database.transaction() as conn:
        conn.execute("INSERT INTO bed_cleared (printer_id, print_id, cleared_at) VALUES (?, ?, ?)",
                     (printer_id, print_id, time.time()))

    result = {**status(printer_id), "print": record, "started": None, "start_error": None}
    if result["auto_start"]:
        queued = next((j for j in jobs.list_jobs() if j["state"] == jobs.QUEUED and j["printer_id"] == printer_id
                       and not j["held"]), None)
        if queued:
            try:
                result["started"] = await jobs.start_job(queued["id"], printer_id, unattended=True)
            except jobs.JobError as e:
                result["start_error"] = str(e)
    return result
