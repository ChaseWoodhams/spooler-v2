"""What the dashboard leads with: Needs you, and a compact row per active Project.

Needs you is the printers waiting on the user (Bed cleared after a finished Print, or a Start once the bed is clear
and auto-start is off) plus Prints whose Outcome is still unconfirmed. A waiting printer carries its last Print, so
that Print is not repeated in the unconfirmed list.
"""

import database
import jobs
import print_history
import projects
import state
import bed_cleared


def _job_summary(job: dict) -> dict:
    return {"id": job["id"], "file_name": job.get("file_name"), "project_id": job.get("project_id"),
            "plate_id": job.get("plate_id")}


def needs_you() -> dict:
    queued = [j for j in jobs.list_jobs() if j["state"] == jobs.QUEUED and j.get("printer_id")]
    waiting, shown = [], set()
    for printer_id, printer in state.printers.items():
        if jobs.active_job_for(printer_id):
            continue
        with database.transaction() as conn:
            last = conn.execute("SELECT * FROM prints WHERE printer_id = ? ORDER BY id DESC LIMIT 1",
                                (printer_id,)).fetchone()
        if last is None or last["outcome"] is None:
            continue
        next_job = next((_job_summary(j) for j in queued if j["printer_id"] == printer_id), None)
        if bed_cleared.status(printer_id)["valid"]:
            if next_job is None:
                continue
            kind = "start_job"
        else:
            kind = "bed_cleared"
        record = print_history._to_print(last)
        shown.add(record["id"])
        waiting.append({"printer_id": printer_id, "printer_name": printer.name, "kind": kind,
                        "print": record, "next_job": next_job})
    unconfirmed = print_history.search(unconfirmed=True, limit=print_history.PAGE_LIMIT_MAX)["prints"]
    return {"printers": waiting, "prints": [p for p in unconfirmed if p["id"] not in shown and p["outcome"] is not None]}


def active_projects() -> list:
    """Projects with work under way: at least one plate past unsliced, and not every plate printed OK."""
    rows = []
    for project in projects.list_projects():
        progress = projects.plate_status(project["id"])["progress"]
        total, counts = progress["total"], progress["counts"]
        if total and progress["printed_ok"] < total and counts["unsliced"] < total:
            rows.append({"id": project["id"], "name": project["name"], "printed_ok": progress["printed_ok"],
                         "total": total, "percent": progress["percent"], "counts": counts})
    return rows


def snapshot() -> dict:
    return {"needs_you": needs_you(), "projects": active_projects()}
