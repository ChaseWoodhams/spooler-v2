"""
Print-job queue: G-code jobs, printer assignment, ordering, and the state
machine that starts a job and follows it on the printer.

States
  queued      waiting in the queue
  starting    Spooler holds the printer: uploading the file and sending the start command
  printing    the printer reports it is working on the job's file
  completed / cancelled / failed / unknown    final

A printer is controlled by at most one job in starting/printing — enforced by a
partial unique index in the database and checked before manual starts.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import database
import snapshots
import state
from gcode_meta import read_estimates, safe_gcode_name
from persistence import DATA_DIR
from printers.protocol import (
    CMD_STATUS, PRINT_COMPLETE, PRINT_ENDED, PRINT_STOPPING, PRINT_WORKING, classify_print_end, decode_printinfo,
)
from printers.sdcp_upload import upload_file

MAX_UPLOAD_BYTES = int(os.getenv("JOB_MAX_UPLOAD_MB", "512")) * 1024 * 1024
START_TIMEOUT_S  = 180
SETTLE_POLL_S    = 1.0
SETTLE_TIMEOUT_S = 60    # after an upload, how long to let the printer finish "receiving file" before calling it busy
RECENT_LIMIT     = 25

QUEUED, STARTING, PRINTING = "queued", "starting", "printing"
ACTIVE = (STARTING, PRINTING)
FINAL  = ("completed", "cancelled", "failed", "unknown")

QUEUE_PRINTER_TYPES = {"cc1"}
PRINT_OPT_KEYS      = ("timelapse", "leveling", "smooth_plate")

uploader = upload_file               # replaced in tests
snapshotter = snapshots.grab_frame   # replaced in tests

_data_dir: Path = DATA_DIR
_active: dict = {}                   # printer_id → {"id", "state", "file_name", "last_code", "layers"}
_progress: dict = {}                 # job_id → {"phase", "sent", "total"} while starting
_start_tasks: dict = {}              # job_id → asyncio.Task


class JobError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Setup ──────────────────────────────────────────────────────────────────────

def init(data_dir: Path | None = None) -> None:
    """Migrate the database and load jobs that were active when Spooler stopped."""
    global _data_dir
    _data_dir = Path(data_dir or DATA_DIR)
    database.migrate()
    _active.clear()
    _progress.clear()
    _start_tasks.clear()
    with database.transaction() as conn:
        rows = conn.execute("SELECT id, state, printer_id, file_name FROM jobs WHERE state IN (?, ?)", ACTIVE)
        for row in rows:
            _active[row["printer_id"]] = {"id": row["id"], "state": row["state"], "file_name": row["file_name"]}


def start_background() -> list:
    """Resume start-timeout watchers for jobs that were starting when Spooler stopped."""
    tasks = []
    for entry in _active.values():
        if entry["state"] == STARTING:
            job = get_job(entry["id"])
            remaining = max(0.0, job["updated_at"] + START_TIMEOUT_S - time.time())
            task = asyncio.create_task(_expire_start(job["id"], remaining))
            _start_tasks[job["id"]] = task
            tasks.append(task)
    return tasks


# ── Queries ────────────────────────────────────────────────────────────────────

def _jobs_dir() -> Path:
    return _data_dir / "jobs"


def _to_job(row) -> dict:
    job = dict(row)
    job["print_opts"] = json.loads(job["print_opts"] or "{}")
    job["progress"] = _progress.get(job["id"])
    return job


def _with_holds(listed: list) -> list:
    """Add each job's dependency holds and whether it is still waiting on one."""
    import holds  # imported here because holds imports this module
    found = holds.for_jobs([job["id"] for job in listed])
    for job in listed:
        job["holds"] = found[job["id"]]
        job["held"] = job["state"] == QUEUED and any(not h["satisfied"] for h in job["holds"])
    return listed


def get_job(job_id: int) -> dict | None:
    with database.transaction() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _with_holds([_to_job(row)])[0] if row else None


def list_jobs(recent: int = RECENT_LIMIT) -> list:
    """Active jobs, then the queue in order, then the most recently finished jobs."""
    with database.transaction() as conn:
        open_rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?, ?, ?) "
            "ORDER BY CASE state WHEN 'printing' THEN 0 WHEN 'starting' THEN 1 ELSE 2 END, position, id",
            (PRINTING, STARTING, QUEUED),
        ).fetchall()
        done_rows = conn.execute(
            f"SELECT * FROM jobs WHERE state IN ({', '.join('?' * len(FINAL))}) "
            "ORDER BY finished_at DESC, id DESC LIMIT ?",
            (*FINAL, recent),
        ).fetchall()
    return _with_holds([_to_job(r) for r in open_rows + done_rows])


def job_events(job_id: int) -> list:
    with database.transaction() as conn:
        rows = conn.execute("SELECT at, event, detail FROM job_events WHERE job_id = ? ORDER BY id", (job_id,))
        return [dict(r) for r in rows]


def active_job_for(printer_id: str) -> dict | None:
    entry = _active.get(printer_id)
    return dict(entry) if entry else None


def _log(conn, job_id: int, event: str, detail: str | None = None) -> None:
    conn.execute("INSERT INTO job_events (job_id, at, event, detail) VALUES (?, ?, ?, ?)",
                 (job_id, time.time(), event, detail))


def log_event(job_id: int, event: str, detail: str | None = None) -> None:
    with database.transaction() as conn:
        _log(conn, job_id, event, detail)


# ── Printers ───────────────────────────────────────────────────────────────────

def _require_printer(printer_id: str):
    printer = state.printers.get(printer_id)
    if printer is None:
        raise JobError("Printer not found", 404)
    if printer.printer_type not in QUEUE_PRINTER_TYPES:
        raise JobError("The queue currently supports CC1 printers only")
    return printer


def printer_block_reason(printer) -> str | None:
    """Why a new job can't start on this printer right now, or None if it can."""
    if printer is None:
        return "Printer not found"
    if printer.printer_type not in QUEUE_PRINTER_TYPES:
        return "The queue currently supports CC1 printers only"
    if not printer.connected:
        return f"{printer.name} is offline"
    if _active.get(printer.id):
        return f"{printer.name} is already running a queued job"
    status = printer.status or {}
    code = decode_printinfo(status.get("PrintInfo") or {}).get("Status")
    machine = (status.get("CurrentStatus") or [0])[0]
    if code is None:
        return f"{printer.name} hasn't reported its status yet"
    if code not in PRINT_ENDED or machine != 0:
        return f"{printer.name} is busy (print status {code}, machine status {machine})"
    return None


def _print_opts(value) -> dict:
    if not isinstance(value, dict):
        raise JobError("print_opts must be an object")
    return {key: bool(value.get(key)) for key in PRINT_OPT_KEYS}


# ── Creating and editing jobs (blocking; safe from HTTP threads) ───────────────

def create_upload_job(stream, length: int, filename: str, printer_id: str | None = None) -> dict:
    """Store an uploaded G-code file (streamed from `stream`) as a new queued job."""
    try:
        name = safe_gcode_name(filename)
    except ValueError as e:
        raise JobError(str(e)) from e
    if length <= 0:
        raise JobError("The file is empty")
    if length > MAX_UPLOAD_BYTES:
        raise JobError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    if printer_id:
        _require_printer(printer_id)

    jobs_dir = _jobs_dir()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".upload-", dir=jobs_dir))
    final_dir = None
    try:
        digest = hashlib.sha256()
        remaining = length
        with (staging / name).open("wb") as fh:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise JobError("The upload was interrupted")
                fh.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        estimates = read_estimates(staging / name)

        now = time.time()
        with database.transaction() as conn:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM jobs WHERE state = ?", (QUEUED,)).fetchone()[0]
            job_id = conn.execute(
                "INSERT INTO jobs (name, state, position, printer_id, source, file_name, file_size, file_sha256, "
                "estimate_s, estimate_g, estimate_mm, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'upload', ?, ?, ?, ?, ?, ?, ?, ?)",
                (Path(name).stem, QUEUED, position, printer_id or None, name, length, digest.hexdigest(),
                 estimates["estimate_s"], estimates["estimate_g"], estimates["estimate_mm"], now, now),
            ).lastrowid
            final_dir = jobs_dir / str(job_id)
            if final_dir.exists():  # left behind by a job whose id SQLite has reused
                shutil.rmtree(final_dir)
            os.replace(staging, final_dir)
            conn.execute("UPDATE jobs SET stored_file = ? WHERE id = ?", (f"jobs/{job_id}/{name}", job_id))
            _log(conn, job_id, "created", f"uploaded {name} ({length} bytes)")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if final_dir is not None:
            shutil.rmtree(final_dir, ignore_errors=True)
        raise
    return get_job(job_id)


def create_printer_file_job(printer_id: str, printer_file: str, meta: dict | None = None) -> dict:
    """Queue a G-code file that is already stored on a printer (runs only on that printer)."""
    printer = _require_printer(printer_id)
    printer_file = (printer_file or "").strip()
    if not printer_file or ".." in printer_file.replace("\\", "/").split("/"):
        raise JobError("Invalid printer file")
    try:
        name = safe_gcode_name(printer_file)
    except ValueError as e:
        raise JobError(str(e)) from e
    meta = meta or {}

    def number(key):
        value = meta.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None

    now = time.time()
    with database.transaction() as conn:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM jobs WHERE state = ?", (QUEUED,)).fetchone()[0]
        job_id = conn.execute(
            "INSERT INTO jobs (name, state, position, printer_id, source, file_name, printer_file, file_size, "
            "estimate_s, estimate_mm, created_at, updated_at) VALUES (?, ?, ?, ?, 'printer', ?, ?, ?, ?, ?, ?, ?)",
            (Path(name).stem, QUEUED, position, printer_id, name, printer_file, number("size"),
             number("print_time"), number("filament_mm"), now, now),
        ).lastrowid
        _log(conn, job_id, "created", f"{printer_file} on {printer.name}")
    return get_job(job_id)


def update_job(job_id: int, changes: dict) -> dict:
    with database.transaction() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobError("Job not found", 404)
        updates: dict = {}
        if "notes" in changes:
            updates["notes"] = str(changes["notes"] or "")[:2000]
        if "name" in changes:
            name = str(changes["name"] or "").strip()
            if not name:
                raise JobError("Name can't be empty")
            updates["name"] = name[:200]
        if "printer_id" in changes or "print_opts" in changes:
            if row["state"] != QUEUED:
                raise JobError("Only queued jobs can change printer or print options", 409)
            if "printer_id" in changes:
                printer_id = changes["printer_id"] or None
                if printer_id is not None:
                    _require_printer(printer_id)
                if row["source"] == "printer" and printer_id != row["printer_id"]:
                    raise JobError("This job prints a file stored on its printer, so it can't be moved", 409)
                updates["printer_id"] = printer_id
            if "print_opts" in changes:
                updates["print_opts"] = json.dumps(_print_opts(changes["print_opts"]))
        if updates:
            updates["updated_at"] = time.time()
            conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                         (*updates.values(), job_id))
            _log(conn, job_id, "edited", ", ".join(k for k in updates if k != "updated_at"))
    return get_job(job_id)


def assign_printers(job_ids: list, printer_ids: list) -> list:
    """Spread queued jobs over printers so they finish about together: longest estimated print first, each onto the printer
    with the least time assigned so far (jobs without an estimate count as the average). Jobs that aren't queued, or print a
    file stored on a printer, keep their printer."""
    if not isinstance(job_ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in job_ids):
        raise JobError("job_ids must be a list of job ids")
    if not isinstance(printer_ids, list) or not printer_ids or not all(isinstance(p, str) for p in printer_ids):
        raise JobError("Choose at least one printer")
    printers = [_require_printer(p) for p in dict.fromkeys(printer_ids)]
    with database.transaction() as conn:
        rows = conn.execute(f"SELECT id, state, source, estimate_s FROM jobs WHERE id IN ({', '.join('?' * len(job_ids))}) ORDER BY position, id",
                            job_ids).fetchall() if job_ids else []
        movable = [r for r in rows if r["state"] == QUEUED and r["source"] != "printer"]
        known = [r["estimate_s"] for r in movable if r["estimate_s"]]
        average = sum(known) / len(known) if known else 1
        load = {printer.id: 0.0 for printer in printers}
        now = time.time()
        for row in sorted(movable, key=lambda r: -(r["estimate_s"] or average)):  # stable: equal jobs keep queue order
            printer = min(printers, key=lambda p: load[p.id])
            load[printer.id] += row["estimate_s"] or average
            conn.execute("UPDATE jobs SET printer_id = ?, updated_at = ? WHERE id = ?", (printer.id, now, row["id"]))
            _log(conn, row["id"], "edited", f"printer_id → {printer.name} (balanced with {len(movable) - 1} other job{'s' if len(movable) != 2 else ''})")
    return [get_job(r["id"]) for r in rows]


def delete_job(job_id: int) -> None:
    with database.transaction() as conn:
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobError("Job not found", 404)
        if row["state"] in ACTIVE:
            raise JobError("This job is running — stop it or mark it finished first", 409)
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    shutil.rmtree(_jobs_dir() / str(job_id), ignore_errors=True)


def reorder(order: list) -> None:
    """Set the queue order. `order` must list every queued job id exactly once."""
    if not isinstance(order, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in order):
        raise JobError("order must be a list of job ids")
    with database.transaction() as conn:
        queued = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE state = ?", (QUEUED,))]
        if len(set(order)) != len(order) or set(order) != set(queued):
            raise JobError("The queue has changed — refresh and try again", 409)
        for position, job_id in enumerate(order, start=1):
            conn.execute("UPDATE jobs SET position = ? WHERE id = ?", (position, job_id))


def requeue(job_id: int) -> dict:
    """Put a finished job back at the front of the queue."""
    with database.transaction() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobError("Job not found", 404)
        if row["state"] not in FINAL:
            raise JobError("Only finished jobs can be queued again", 409)
        if row["source"] == "upload" and not (_data_dir / row["stored_file"]).is_file():
            raise JobError("The G-code file for this job is no longer stored", 410)
        position = conn.execute(
            "SELECT COALESCE(MIN(position), 1) - 1 FROM jobs WHERE state = ?", (QUEUED,)).fetchone()[0]
        conn.execute(
            "UPDATE jobs SET state = ?, position = ?, started_at = NULL, finished_at = NULL, error = NULL, "
            "updated_at = ? WHERE id = ?", (QUEUED, position, time.time(), job_id))
        _log(conn, job_id, "requeued", f"was {row['state']}")
    return get_job(job_id)


def duplicate_job(job_id: int) -> dict:
    """Queue a new copy of a job with the same file, printer, options and notes (for repeat prints)."""
    job = get_job(job_id)
    if job is None:
        raise JobError("Job not found", 404)
    if job["source"] == "printer":
        copy = create_printer_file_job(job["printer_id"], job["printer_file"], {
            "size": job["file_size"], "print_time": job["estimate_s"], "filament_mm": job["estimate_mm"]})
    else:
        path = _data_dir / (job["stored_file"] or "")
        if not job["stored_file"] or not path.is_file():
            raise JobError("The G-code file for this job is no longer stored", 410)
        printer_id = job["printer_id"] if job["printer_id"] in state.printers else None
        with path.open("rb") as fh:
            copy = create_upload_job(fh, path.stat().st_size, job["file_name"], printer_id)
    log_event(copy["id"], "copied", f"from job {job_id}")
    return update_job(copy["id"], {"name": job["name"], "notes": job["notes"], "print_opts": job["print_opts"]})


# ── State transitions (event loop) ─────────────────────────────────────────────

def _finish(job_id: int, outcome: str, detail: str | None = None) -> bool:
    """Move an active job to a final state. Returns False if it was no longer active."""
    now = time.time()
    with database.transaction() as conn:
        row = conn.execute("SELECT printer_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        changed = conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = ?, updated_at = ? WHERE id = ? AND state IN (?, ?)",
            (outcome, detail if outcome != "completed" else None, now, now, job_id, *ACTIVE),
        ).rowcount
        if changed:
            _log(conn, job_id, outcome, detail)
    if changed:
        if row and _active.get(row["printer_id"], {}).get("id") == job_id:
            del _active[row["printer_id"]]
        task = _start_tasks.pop(job_id, None)
        if task is not None and task is not _current_task():
            task.cancel()
        _progress.pop(job_id, None)
        print(f"[Jobs] Job {job_id} {outcome}" + (f": {detail}" if detail else ""))
    return bool(changed)


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def notifier(title: str, body: str) -> None:
    """Tell the user something (push notification). Replaced in tests."""
    try:
        import push
        push.send_push_all(title, body)
    except Exception as e:
        print(f"[Jobs] Notification failed: {e}")


def acknowledged_warnings(job_id: int) -> list:
    """The preflight warnings the user accepted when queueing the job (the latest acknowledgement)."""
    with database.transaction() as conn:
        row = conn.execute("SELECT detail FROM job_events WHERE job_id = ? AND event = 'warnings_acknowledged' "
                           "ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    try:
        return list(json.loads(row["detail"])) if row else []
    except ValueError:
        return []


def acknowledge_warnings(job_id: int, printer_id: str | None = None) -> dict:
    """Accept the job's current preflight warnings once, at queue time, so an automatic start can proceed on them."""
    import preflight  # imported here because preflight imports this module
    report = preflight.check_job_id(job_id, printer_id)
    if not report["can_start"]:
        raise JobError("Preflight failed: " + "; ".join(c["title"] for c in report["checks"] if c["level"] == "fail"), 409)
    titles = [c["title"] for c in report["checks"] if c["level"] == "warn"]
    log_event(job_id, "warnings_acknowledged", json.dumps(titles))
    return {"job_id": job_id, "acknowledged": titles}


async def start_job(job_id: int, printer_id: str | None = None, acknowledge_warnings: bool = False,
                    unattended: bool = False) -> dict:
    """Start a queued job. An unattended start (Bed cleared auto-start) that is refused leaves the job queued, records
    why on its timeline and notifies the user."""
    try:
        return await _start_job(job_id, printer_id, acknowledge_warnings, unattended)
    except JobError as e:
        if unattended and e.status != 404:
            log_event(job_id, "start_blocked", str(e))
            await asyncio.get_running_loop().run_in_executor(None, notifier, "Spooler: automatic start blocked", str(e))
        raise


async def _start_job(job_id: int, printer_id: str | None, acknowledge_warnings: bool, unattended: bool) -> dict:
    import holds  # imported here because holds imports this module
    job = get_job(job_id)
    if job is None:
        raise JobError("Job not found", 404)
    if job["state"] != QUEUED:
        raise JobError("Only queued jobs can be started", 409)
    waiting = holds.waiting_on(job_id)
    if waiting:
        raise JobError("Waiting for " + ", ".join(waiting) + " to finish", 409)
    printer_id = printer_id or job["printer_id"]
    if not printer_id:
        raise JobError("Choose a printer for this job first")
    if job["source"] == "printer" and printer_id != job["printer_id"]:
        raise JobError("This job prints a file stored on its printer, so it can't be moved", 409)
    printer = state.printers.get(printer_id)
    reason = printer_block_reason(printer)
    if reason:
        raise JobError(reason, 409)

    # Preflight: nothing reaches the printer with a failing check or warnings nobody acknowledged.
    import preflight  # imported here because preflight imports this module
    report = await asyncio.get_running_loop().run_in_executor(None, preflight.check_job, job, printer)
    failed = [c["title"] for c in report["checks"] if c["level"] == "fail"]
    warnings = [c["title"] for c in report["checks"] if c["level"] == "warn"]
    if failed:
        raise JobError("Preflight failed: " + "; ".join(failed), 409)
    new_warnings = [w for w in warnings if w not in acknowledged_warnings(job_id)]
    if new_warnings and not acknowledge_warnings:
        raise JobError(("New preflight warnings since this job was queued: " if unattended else
                        "Review the preflight warnings before starting: ") + "; ".join(new_warnings), 409)

    try:
        with database.transaction() as conn:
            claimed = conn.execute(
                "UPDATE jobs SET state = ?, printer_id = ?, error = NULL, updated_at = ? WHERE id = ? AND state = ?",
                (STARTING, printer_id, time.time(), job_id, QUEUED),
            ).rowcount
            if not claimed:
                raise JobError("This job is no longer queued", 409)
            _log(conn, job_id, "preflight",
                 "warnings acknowledged: " + "; ".join(warnings) if warnings else "all checks passed")
            _log(conn, job_id, "starting", f"on {printer.name}")
    except sqlite3.IntegrityError as e:
        raise JobError(f"{printer.name} is already running a queued job", 409) from e

    _active[printer_id] = {"id": job_id, "state": STARTING, "file_name": job["file_name"]}
    _progress[job_id] = {"phase": "preparing"}
    _start_tasks[job_id] = asyncio.create_task(_run_start(job_id, printer))
    await broadcast()
    return get_job(job_id)


async def _snapshot(job_id: int, printer) -> None:
    """Keep a camera snapshot of the bed on the job's timeline. Skipped without a camera; never blocks the start."""
    url = getattr(printer, "camera_url", None)
    if not url:
        return
    try:
        image = await asyncio.get_running_loop().run_in_executor(None, snapshotter, url)
        if not image:
            return
        path = _jobs_dir() / str(job_id) / "start.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        log_event(job_id, "snapshot", f"jobs/{job_id}/start.jpg")
    except Exception as e:
        print(f"[Jobs] Snapshot for job {job_id} skipped: {e}")


def snapshot_file(job_id: int) -> Path:
    path = _jobs_dir() / str(job_id) / "start.jpg"
    if not path.is_file():
        raise JobError("No snapshot for this job", 404)
    return path


async def _run_start(job_id: int, printer) -> None:
    loop = asyncio.get_running_loop()
    job = get_job(job_id)
    try:
        await _snapshot(job_id, printer)
        if job["source"] == "upload":
            path = _data_dir / job["stored_file"]
            if not path.is_file():
                raise JobError("The G-code file for this job is missing")
            _progress[job_id] = {"phase": "uploading", "sent": 0, "total": job["file_size"]}
            await broadcast()
            last_sent = [0.0]

            def on_progress(sent: int, total: int) -> None:
                _progress[job_id] = {"phase": "uploading", "sent": sent, "total": total}
                now = time.monotonic()
                if now - last_sent[0] >= 1 or sent >= total:
                    last_sent[0] = now
                    asyncio.run_coroutine_threadsafe(broadcast(), loop)

            await loop.run_in_executor(None, uploader, printer.ip, path, job["file_name"], on_progress)
            log_event(job_id, "uploaded", f"{job['file_size']} bytes to {printer.name}")
            remote_path = f"/local/{job['file_name']}"
        else:
            remote_path = job["printer_file"]

        reason = await _settle(printer, job_id)
        if reason:
            raise JobError(f"Printer became unavailable: {reason}")
        _progress[job_id] = {"phase": "sending"}
        if not await printer.start_print_file(remote_path, print_opts=job["print_opts"]):
            raise JobError("The printer did not accept the start command")
        log_event(job_id, "start_sent", remote_path)
        _progress[job_id] = {"phase": "waiting"}
        await broadcast()
        await _expire_start(job_id, START_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if _finish(job_id, "failed", str(e) or type(e).__name__):
            await broadcast()
    finally:
        if _start_tasks.get(job_id) is _current_task():
            _start_tasks.pop(job_id, None)


async def _settle(printer, job_id: int) -> str | None:
    """The reason the printer can't start `job_id`, after giving it time to finish taking the file we just uploaded: while
    it validates and stores an upload the printer reports a file-transfer state, then returns to idle by itself."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    reason = printer_block_reason_ignoring_job(printer, job_id)
    while reason and " is busy" in reason and time.monotonic() < deadline:
        ask = getattr(printer, "send_cmd", None)
        if ask is not None:
            try:
                await ask(CMD_STATUS, {})
            except Exception:
                pass
        await asyncio.sleep(SETTLE_POLL_S)
        reason = printer_block_reason_ignoring_job(printer, job_id)
    return reason


def printer_block_reason_ignoring_job(printer, job_id: int) -> str | None:
    """printer_block_reason() without counting the job that is being started."""
    entry = _active.get(getattr(printer, "id", None))
    if entry and entry["id"] == job_id:
        del _active[printer.id]
        try:
            return printer_block_reason(printer)
        finally:
            _active[printer.id] = entry
    return printer_block_reason(printer)


async def _expire_start(job_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    job = get_job(job_id)
    if job and job["state"] == STARTING:
        minutes = max(1, round(START_TIMEOUT_S / 60))
        if _finish(job_id, "failed", f"The printer didn't start printing within {minutes} min"):
            await broadcast()


async def finish_job(job_id: int, outcome: str) -> dict:
    """Manually close an active job — for a printer that went away or a start that hung."""
    if outcome not in FINAL:
        raise JobError(f"outcome must be one of: {', '.join(FINAL)}")
    job = get_job(job_id)
    if job is None:
        raise JobError("Job not found", 404)
    if job["state"] not in ACTIVE:
        raise JobError("This job isn't running", 409)
    printer = state.printers.get(job["printer_id"])
    if job["state"] == PRINTING and printer is not None and printer.connected:
        pi = decode_printinfo((printer.status or {}).get("PrintInfo") or {})
        on_printer = (pi.get("Filename") or "").rsplit("/", 1)[-1] == job["file_name"]
        if on_printer and (pi.get("Status") in PRINT_WORKING or pi.get("Status") == PRINT_STOPPING):
            raise JobError(f"{printer.name} is still printing this job — stop it on the printer first", 409)
    _finish(job_id, outcome, f"Marked {outcome} by user")
    await broadcast()
    return get_job(job_id)


def on_printer_status(printer) -> None:
    """Follow the active job on a printer from its status push. Called for every status update."""
    entry = _active.get(printer.id)
    if entry is None:
        return
    pi = decode_printinfo((printer.status or {}).get("PrintInfo") or {})
    code = pi.get("Status")
    if code is None:
        return
    filename = (pi.get("Filename") or "").rsplit("/", 1)[-1]
    matches = filename == entry["file_name"]
    if matches:
        entry["layers"] = (pi.get("CurrentLayer") or 0, pi.get("TotalLayer") or 0)
    previous = entry.get("last_code")
    entry["last_code"] = code
    job_id = entry["id"]
    if code != previous:
        layers = entry.get("layers") or (0, 0)
        log_event(job_id, "printer_status", f"{previous} → {code}, file={filename or '-'}, layer {layers[0]}/{layers[1]}")

    changed = False
    if entry["state"] == STARTING:
        if code in PRINT_WORKING and matches:
            with database.transaction() as conn:
                conn.execute("UPDATE jobs SET state = ?, started_at = ?, updated_at = ? WHERE id = ? AND state = ?",
                             (PRINTING, time.time(), time.time(), job_id, STARTING))
                _log(conn, job_id, PRINTING)
            entry["state"] = PRINTING
            task = _start_tasks.pop(job_id, None)
            if task is not None:
                task.cancel()
            _progress.pop(job_id, None)
            print(f"[Jobs] Job {job_id} printing on {printer.name}")
            changed = True
        elif code in PRINT_WORKING and filename:
            changed = _finish(job_id, "failed", f"The printer started a different file ({filename})")
    elif code in PRINT_WORKING or code == PRINT_STOPPING:
        if filename and not matches:
            changed = _finish(job_id, "unknown", f"The printer switched to another file ({filename})")
    elif code in PRINT_ENDED:
        if code == PRINT_COMPLETE and filename and not matches:
            changed = _finish(job_id, "unknown", f"The printer reported a different file ({filename})")
        else:
            outcome, detail = classify_print_end(code, *(entry.get("layers") or (0, 0)))
            changed = _finish(job_id, outcome, detail)

    if changed:
        _schedule_broadcast()


# ── Broadcast ──────────────────────────────────────────────────────────────────

async def broadcast() -> None:
    if state.browser_clients:
        await state.broadcast_to_browsers({"type": "jobs", "jobs": list_jobs()})


def _schedule_broadcast() -> None:
    try:
        asyncio.get_running_loop().create_task(broadcast())
    except RuntimeError:
        broadcast_threadsafe()


def broadcast_threadsafe() -> None:
    loop = state.loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast(), loop)
