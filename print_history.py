"""
Print history: one record for every print on every printer, whether it was
started from the queue, the file browser or the printer's own screen.

A record opens when a printer starts working on a file and closes when its
status shows the print is over. printers/base.py attaches filament usage at the
end of a print; spool, material and cost come from Spoolman. A record that is
open when Spooler stops is picked up again and closed on the next status.
"""

import csv
import io
import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

import database
import jobs
import state  # `jobs` is imported as a module: printers/base.py imports this file while jobs is still loading
from persistence import HISTORY_FILE, filament_mm_to_grams
from printers.protocol import (
    PRINT_COMPLETE, PRINT_ENDED, PRINT_STOPPING, PRINT_WORKING, classify_print_end, decode_printinfo,
)
from spoolman import _printer_location, _spool_id_at_location, get_spoolman_url

OUTCOMES       = ("completed", "cancelled", "failed", "unknown")
# Failure reasons are data: ids stored on a print, labels live in the UI. Several per print are allowed.
FAILURE_REASONS = ("adhesion", "spaghetti", "warping", "layer_shift", "clog", "filament", "surface_quality",
                   "power_network", "other")
CANCEL_REASONS  = FAILURE_REASONS + ("cancelled_on_purpose",)
# What the UI shows for each reason id, served by GET /api/failure-reasons so the list lives only here.
FAILURE_REASON_LABELS = {
    "adhesion": "Adhesion / came loose", "spaghetti": "Spaghetti", "warping": "Warping", "layer_shift": "Layer shift",
    "clog": "Clog / under-extrusion", "filament": "Filament ran out or tangled", "surface_quality": "Surface quality",
    "power_network": "Power or network", "other": "Other", "cancelled_on_purpose": "Cancelled on purpose",
}
SEARCH_COLUMNS = ("file_name", "printer_name", "material", "filament_name", "vendor", "notes", "detail")
PAGE_LIMIT_MAX = 200
EXPORT_LIMIT   = 20000
USAGE_WINDOW_S = 600  # usage reported this long after a print closed still attaches to it

closed_hooks: list = []  # called with each Print that closes (outcome_prompt registers here)
_open: dict = {}  # printer_id → {"id", "file_name", "layers": (layer, total)}


def init() -> None:
    """Migrate, reload prints that were running at shutdown, and import the old history.json."""
    database.migrate()
    _open.clear()
    with database.transaction() as conn:
        rows = conn.execute(
            "SELECT id, printer_id, file_name, layer, total_layers FROM prints WHERE outcome IS NULL")
        for row in rows:
            _open[row["printer_id"]] = {"id": row["id"], "file_name": row["file_name"],
                                        "layers": (row["layer"] or 0, row["total_layers"] or 0)}
    imported = import_legacy_history()
    if imported:
        print(f"[History] Imported {imported} prints from history.json")


def _to_print(row) -> dict:
    record = dict(row)
    record["spools"] = json.loads(record["spools"] or "[]")
    record["failure_reasons"] = json.loads(record.get("failure_reasons") or "[]")
    record["outcome_unconfirmed"] = bool(record.get("outcome_unconfirmed"))
    record["was_quick"] = bool(record.get("was_quick"))
    return record


# ── Following prints ───────────────────────────────────────────────────────────

def _note_layers(entry: dict, pi: dict) -> None:
    layer, total = pi.get("CurrentLayer") or 0, pi.get("TotalLayer") or 0
    old_layer, old_total = entry["layers"]
    entry["layers"] = (max(layer, old_layer), total or old_total)


def on_printer_status(printer) -> dict | None:
    """Open or close the printer's print record from its latest status. Returns a record it closed."""
    pi = decode_printinfo((printer.status or {}).get("PrintInfo") or {})
    code = pi.get("Status")
    if code is None:
        return None
    filename = (pi.get("Filename") or "").rsplit("/", 1)[-1]
    entry = _open.get(printer.id)
    closed = None

    if entry is not None:
        same_file = filename == entry["file_name"]
        if code in PRINT_WORKING or code == PRINT_STOPPING:
            if not filename or same_file:
                if same_file:
                    _note_layers(entry, pi)
                return None
            closed = _close(printer.id, "unknown", f"The printer switched to another file ({filename})")
        elif code in PRINT_ENDED:
            if same_file:
                _note_layers(entry, pi)
            if code == PRINT_COMPLETE and filename and not same_file:
                outcome, detail = "unknown", f"The printer reported a different file ({filename})"
            else:
                outcome, detail = classify_print_end(code, *entry["layers"])
            closed = _close(printer.id, outcome, detail)
        else:
            return None  # a status code we don't know: wait for one we do

    if code in PRINT_WORKING and filename and printer.id not in _open:
        _open_record(printer, filename, pi)
    return closed


def _open_record(printer, filename: str, pi: dict) -> None:
    now = time.time()
    elapsed = pi.get("CurrentTicks") or pi.get("PrintTime") or 0
    started_at = now - elapsed if isinstance(elapsed, (int, float)) and 0 < elapsed < 7 * 86400 else now
    active = jobs.active_job_for(printer.id)
    job = jobs.get_job(active["id"]) if active and active["file_name"] == filename else None
    with database.transaction() as conn:
        # A record left open without our knowledge would block the unique index; close it first.
        conn.execute("UPDATE prints SET outcome = 'unknown', detail = 'Superseded by a newer print', "
                     "ended_at = ? WHERE printer_id = ? AND outcome IS NULL", (now, printer.id))
        record_id = conn.execute(
            "INSERT INTO prints (printer_id, printer_name, printer_type, job_id, project_id, plate_id, file_name, "
            "started_at, total_layers, estimate_s, estimate_g, was_quick) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "COALESCE((SELECT quick FROM projects WHERE id = ?), 0))",
            (printer.id, printer.name, getattr(printer, "printer_type", None), job["id"] if job else None,
             job.get("project_id") if job else None, job.get("plate_id") if job else None, filename, started_at, pi.get("TotalLayer") or None,
             job["estimate_s"] if job else None, job["estimate_g"] if job else None, job.get("project_id") if job else None),
        ).lastrowid
    _open[printer.id] = {"id": record_id, "file_name": filename,
                         "layers": (pi.get("CurrentLayer") or 0, pi.get("TotalLayer") or 0)}
    print(f"[History] {printer.name} started {filename}" + (f" (job {job['id']})" if job else ""))


def _close(printer_id: str, outcome: str, detail: str | None) -> dict | None:
    entry = _open.pop(printer_id, None)
    if entry is None:
        return None
    now = time.time()
    layer, total = entry["layers"]
    with database.transaction() as conn:
        conn.execute(
            "UPDATE prints SET outcome = ?, outcome_unconfirmed = 1, detail = ?, ended_at = ?, "
            "duration_s = CAST(? - started_at AS INTEGER), layer = ?, total_layers = COALESCE(NULLIF(?, 0), total_layers) WHERE id = ? AND outcome IS NULL",
            (outcome, detail, now, now, layer, total, entry["id"]),
        )
        row = conn.execute("SELECT * FROM prints WHERE id = ?", (entry["id"],)).fetchone()
    print(f"[History] {row['printer_name']}: {row['file_name']} {outcome}" + (f" — {detail}" if detail else ""))
    record = _to_print(row)
    for hook in list(closed_hooks):
        try:
            hook(record)
        except Exception as e:
            print(f"[History] closed hook failed: {e}")
    return record


# ── Filament, spools and cost ──────────────────────────────────────────────────

def fetch_spool(spool_id: int) -> dict:
    with urllib.request.urlopen(f"{get_spoolman_url()}/api/v1/spool/{spool_id}", timeout=3) as resp:
        return json.loads(resp.read())


def price_per_kg(spool: dict | None) -> float | None:
    """The spool's price over its net filament weight, else the filament's list price."""
    if not spool:
        return None
    filament = spool.get("filament") or {}
    net_weight = spool.get("initial_weight") or filament.get("weight")
    if spool.get("price") and net_weight:
        return spool["price"] / net_weight * 1000
    if filament.get("price") and filament.get("weight"):
        return filament["price"] / filament["weight"] * 1000
    return None


def spool_usage(spool_id: int, mm: float, spool: dict | None, fallback_density: float) -> dict:
    filament = (spool or {}).get("filament") or {}
    grams = filament_mm_to_grams(mm, filament.get("density") or fallback_density)
    per_kg = price_per_kg(spool)
    return {
        "spool_id":      spool_id,
        "mm":            round(mm, 1),
        "grams":         grams,
        "material":      filament.get("material"),
        "filament_name": filament.get("name"),
        "vendor":        (filament.get("vendor") or {}).get("name"),
        "color_hex":     filament.get("color_hex"),
        "price_per_kg":  round(per_kg, 2) if per_kg is not None else None,
        "cost":          round(grams / 1000 * per_kg, 4) if per_kg is not None else None,
    }


def _usage_target(printer_id: str) -> int | None:
    entry = _open.get(printer_id)
    if entry is not None:
        return entry["id"]
    with database.transaction() as conn:
        row = conn.execute(
            "SELECT id FROM prints WHERE printer_id = ? AND ended_at >= ? ORDER BY ended_at DESC LIMIT 1",
            (printer_id, time.time() - USAGE_WINDOW_S)).fetchone()
    return row["id"] if row else None


def record_usage(printer_id: str, filament_mm: float, density: float, spool_mm: dict | None = None,
                 lookup=None) -> dict | None:
    """Attach filament, spools, material and cost to the printer's current print. Blocking (Spoolman)."""
    record_id = _usage_target(printer_id)
    if record_id is None:
        return None
    lookup = lookup or fetch_spool
    usage = {int(k): float(v) for k, v in (spool_mm or {}).items() if v and v > 0}
    if not usage and filament_mm > 0:
        try:
            spool_id = _spool_id_at_location(_printer_location(printer_id))
        except Exception:
            spool_id = None
        if spool_id is not None:
            usage = {spool_id: float(filament_mm)}

    spools = []
    for spool_id, mm in usage.items():
        try:
            spool = lookup(spool_id)
        except Exception:
            spool = None
        spools.append(spool_usage(spool_id, mm, spool, density))

    grams = round(sum(s["grams"] for s in spools), 1) if spools else filament_mm_to_grams(filament_mm, density)
    priced = [s["cost"] for s in spools if s["cost"] is not None]
    cost = round(sum(priced), 2) if spools and len(priced) == len(spools) else None
    primary = max(spools, key=lambda s: s["grams"], default={})
    with database.transaction() as conn:
        conn.execute(
            "UPDATE prints SET filament_mm = ?, filament_g = ?, spools = ?, spool_id = ?, material = ?, "
            "filament_name = ?, vendor = ?, color_hex = ?, cost = ? WHERE id = ?",
            (round(filament_mm, 1), grams, json.dumps(spools), primary.get("spool_id"), primary.get("material"),
             primary.get("filament_name"), primary.get("vendor"), primary.get("color_hex"), cost, record_id),
        )
        row = conn.execute("SELECT * FROM prints WHERE id = ?", (record_id,)).fetchone()
    return _to_print(row)


# ── Reading, notes, export, repeat ─────────────────────────────────────────────

def _filters(q: str = "", printer_id: str | None = None, outcome: str | None = None,
             since: float | None = None, until: float | None = None, unconfirmed: bool | None = None) -> tuple:
    clauses, params = [], []
    q = (q or "").strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in SEARCH_COLUMNS) + ")")
        params += [like] * len(SEARCH_COLUMNS)
    if printer_id:
        clauses.append("printer_id = ?")
        params.append(printer_id)
    if outcome == "printing":
        clauses.append("outcome IS NULL")
    elif outcome:
        if outcome not in OUTCOMES:
            raise jobs.JobError(f"outcome must be one of: printing, {', '.join(OUTCOMES)}")
        clauses.append("outcome = ?")
        params.append(outcome)
    if unconfirmed is not None:
        clauses.append("outcome_unconfirmed = ?")
        params.append(1 if unconfirmed else 0)
    if since is not None:
        clauses.append("started_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("started_at < ?")
        params.append(until)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def search(q: str = "", printer_id: str | None = None, outcome: str | None = None,
           since: float | None = None, until: float | None = None, limit: int = 50, offset: int = 0,
           unconfirmed: bool | None = None) -> dict:
    where, params = _filters(q, printer_id, outcome, since, until, unconfirmed)
    limit = max(1, min(int(limit), PAGE_LIMIT_MAX))
    offset = max(0, int(offset))
    with database.transaction() as conn:
        rows = conn.execute(f"SELECT * FROM prints{where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                            (*params, limit, offset)).fetchall()
        totals = conn.execute(
            "SELECT count(*) AS prints, "
            "COALESCE(SUM(outcome = 'completed'), 0) AS completed, "
            "COALESCE(SUM(outcome IS NOT NULL), 0) AS finished, "
            "SUM(filament_g) AS filament_g, SUM(cost) AS cost, SUM(duration_s) AS duration_s "
            f"FROM prints{where}", params).fetchone()
    summary = dict(totals)
    summary["success_rate"] = round(summary["completed"] / summary["finished"], 3) if summary["finished"] else None
    return {"prints": _with_project([_to_print(r) for r in rows]), "total": summary["prints"], "summary": summary}


def _with_project(records: list) -> list:
    """Add what History needs about each Print's project: its name, whether it is a hidden Quick print one (`quick`),
    whether it still exists (`project_available`), a thumbnail of its plate and whether Reprint can work."""
    ids = sorted({r["project_id"] for r in records if r.get("project_id")})
    job_ids = sorted({r["job_id"] for r in records if r.get("job_id")})
    projects_by_id, jobs_by_id = {}, {}
    with database.transaction() as conn:
        if ids:
            marks = ",".join("?" * len(ids))
            projects_by_id = {r["id"]: r for r in conn.execute(f"SELECT id, name, quick FROM projects WHERE id IN ({marks})", ids)}
        if job_ids:
            marks = ",".join("?" * len(job_ids))
            jobs_by_id = {r["id"]: r for r in conn.execute(f"SELECT id, state, stored_file FROM jobs WHERE id IN ({marks})", job_ids)}
    for record in records:
        project = projects_by_id.get(record.get("project_id"))
        record["project_available"] = project is not None
        record["project_name"] = project["name"] if project else None
        record["quick"] = bool(project["quick"]) if project else record["was_quick"]   # promoted since, or deleted
        record["thumbnail"] = (f"/api/projects/{project['id']}/plates/{record['plate_id']}/preview.png"
                               if project and record.get("plate_id") else None)
        record["reprint_available"] = _reprint_problem(record, project, jobs_by_id.get(record.get("job_id"))) is None
    return records


def _reprint_problem(record: dict, project, job) -> tuple | None:
    """Why a Print can't be reprinted unchanged as (message, status), or None when it can."""
    if project is None and (record.get("project_id") or record.get("was_quick")):
        return "The project this print came from is gone", 409
    if job is None:
        return "This print's queue job no longer exists", 409
    if job["state"] not in jobs.FINAL:
        return "This print's job is already waiting in the queue", 409
    if not job["stored_file"] or not (jobs._data_dir / job["stored_file"]).is_file():
        return "The G-code file for this print is no longer stored", 410
    return None


def find_print(print_id: int) -> dict | None:
    """The Print's record without its Job and timeline, or None."""
    with database.transaction() as conn:
        row = conn.execute("SELECT * FROM prints WHERE id = ?", (print_id,)).fetchone()
    return _to_print(row) if row else None


def latest_print(printer_id: str) -> dict | None:
    """The printer's most recent Print (still printing or closed), or None when it has never printed."""
    with database.transaction() as conn:
        row = conn.execute("SELECT * FROM prints WHERE printer_id = ? ORDER BY id DESC LIMIT 1", (printer_id,)).fetchone()
    return _to_print(row) if row else None


def latest_closed_prints() -> list:
    """Each printer's latest Print, for printers whose latest Print has closed, oldest end first."""
    with database.transaction() as conn:
        rows = conn.execute("SELECT * FROM prints p WHERE outcome IS NOT NULL AND id = "
                            "(SELECT MAX(id) FROM prints WHERE printer_id = p.printer_id) ORDER BY ended_at, id").fetchall()
    return [_to_print(r) for r in rows]


def set_end_snapshot(print_id: int, relative_path: str) -> None:
    with database.transaction() as conn:
        conn.execute("UPDATE prints SET end_snapshot = ? WHERE id = ?", (relative_path, print_id))


def printer_standing(printer_id: str) -> dict:
    """`running` (a Print is open) and `last_ended_at` (end of the printer's latest finished Print, or None)."""
    with database.transaction() as conn:
        row = conn.execute("SELECT MAX(ended_at) AS at, COUNT(*) FILTER (WHERE outcome IS NULL) AS running "
                           "FROM prints WHERE printer_id = ?", (printer_id,)).fetchone()
    return {"running": bool(row["running"]), "last_ended_at": row["at"]}


def get_print(print_id: int) -> dict:
    record = find_print(print_id)
    if record is None:
        raise jobs.JobError("Print not found", 404)
    record["job"] = jobs.get_job(record["job_id"]) if record["job_id"] else None
    record["job_events"] = jobs.job_events(record["job_id"]) if record["job"] else []
    return record


def update_notes(print_id: int, notes) -> dict:
    with database.transaction() as conn:
        changed = conn.execute("UPDATE prints SET notes = ? WHERE id = ?",
                               (str(notes or "")[:4000], print_id)).rowcount
    if not changed:
        raise jobs.JobError("Print not found", 404)
    return get_print(print_id)


def set_outcome(print_id: int, outcome, failure_reasons=None, notes=None) -> dict:
    """Record the user's answer for a finished print: it overrides what the printer reported and confirms it.
    A failed print needs at least one failure reason; a cancelled one takes optional reasons, including
    cancelled on purpose. `notes`, when given, replaces the print's notes."""
    if outcome not in OUTCOMES:
        raise jobs.JobError(f"outcome must be one of: {', '.join(OUTCOMES)}")
    reasons = list(dict.fromkeys(failure_reasons or []))
    allowed = {"failed": FAILURE_REASONS, "cancelled": CANCEL_REASONS}.get(outcome, ())
    if any(r not in allowed for r in reasons):
        raise jobs.JobError(f"failure reasons for a {outcome} print must be among: {', '.join(allowed) or 'none'}")
    if outcome == "failed" and not reasons:
        raise jobs.JobError("A failed print needs at least one failure reason")
    with database.transaction() as conn:
        row = conn.execute("SELECT outcome FROM prints WHERE id = ?", (print_id,)).fetchone()
        if row is None:
            raise jobs.JobError("Print not found", 404)
        if row["outcome"] is None:
            raise jobs.JobError("The print is still running", 409)
        conn.execute("UPDATE prints SET outcome = ?, failure_reasons = ?, outcome_unconfirmed = 0, "
                     "notes = COALESCE(?, notes) WHERE id = ?",
                     (outcome, json.dumps(reasons), None if notes is None else str(notes)[:4000], print_id))
    return get_print(print_id)


CSV_COLUMNS = ("id", "started", "ended", "printer_name", "file_name", "outcome", "detail", "duration_s",
               "layer", "total_layers", "filament_g", "filament_mm", "material", "vendor", "filament_name",
               "spool_id", "cost", "estimate_s", "estimate_g", "job_id", "notes", "failure_reasons", "outcome_unconfirmed")


def export_csv(**filters) -> str:
    where, params = _filters(**filters)
    with database.transaction() as conn:
        rows = conn.execute(f"SELECT * FROM prints{where} ORDER BY started_at DESC, id DESC LIMIT ?",
                            (*params, EXPORT_LIMIT)).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
    for row in rows:
        record = dict(row, started=stamp(row["started_at"]), ended=stamp(row["ended_at"]),
                      failure_reasons=", ".join(json.loads(row["failure_reasons"] or "[]")),
                      outcome_unconfirmed="yes" if row["outcome_unconfirmed"] else "")
        writer.writerow([record[col] if record[col] is not None else "" for col in CSV_COLUMNS])
    return out.getvalue()


def reprint(print_id: int) -> dict:
    """Queue the Print's own job again, unchanged (the same slice), at the front of the queue."""
    record = find_print(print_id)
    if record is None:
        raise jobs.JobError("Print not found", 404)
    with database.transaction() as conn:
        project = conn.execute("SELECT id FROM projects WHERE id = ?", (record["project_id"],)).fetchone() if record["project_id"] else None
        job = conn.execute("SELECT id, state, stored_file FROM jobs WHERE id = ?", (record["job_id"],)).fetchone() if record["job_id"] else None
    problem = _reprint_problem(record, project, job)
    if problem:
        raise jobs.JobError(*problem)
    return jobs.requeue(record["job_id"])


def repeat_print(print_id: int) -> dict:
    """Queue the print again: a copy of its queue job, or the file on the printer it ran on."""
    record = get_print(print_id)
    if record["job"] is not None:
        return jobs.duplicate_job(record["job_id"])
    if record["printer_id"] not in state.printers:
        raise jobs.JobError("The printer this ran on is no longer configured", 409)
    return jobs.create_printer_file_job(record["printer_id"], f"/local/{record['file_name']}",
                                        {"print_time": record["duration_s"], "filament_mm": record["filament_mm"]})


# ── Legacy history.json ────────────────────────────────────────────────────────

def import_legacy_history(path: Path | None = None) -> int:
    """Copy entries from the old JSON history into the database once, then rename the file."""
    path = Path(path or HISTORY_FILE)
    if not path.is_file():
        return 0
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        print(f"[History] Could not read {path.name}: {e}")
        return 0
    rows = []
    for e in entries if isinstance(entries, list) else []:
        try:
            ended = time.mktime(time.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%S"))
        except (KeyError, TypeError, ValueError):
            continue
        duration = int(e.get("print_time_s") or 0)
        rows.append((e.get("printer_id") or "", e.get("printer_name") or "", e.get("filename") or "",
                     ended - duration, ended, "completed" if e.get("completed", True) else "cancelled",
                     duration, e.get("filament_mm"), e.get("filament_g")))
    try:
        with database.transaction() as conn:
            conn.executemany(
                "INSERT INTO prints (printer_id, printer_name, file_name, started_at, ended_at, outcome, "
                "duration_s, filament_mm, filament_g, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported')", rows)
    except sqlite3.Error as e:
        print(f"[History] Import of {path.name} failed: {e}")
        return 0
    os.replace(path, path.with_name(path.name + ".imported"))
    return len(rows)
