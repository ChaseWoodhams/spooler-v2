"""
Dependency holds: a queued Job waits until another Job, or a project plate, has finished with outcome completed.

A failed or cancelled print never releases a hold, and there are no clock-time holds. A hold names either
`after_job_id` or `after_project_id` + `after_plate_id` (a plate counts once its latest Print completed, the same rule
the per-plate status uses). Held Jobs cannot be started by hand or by Bed cleared auto-start.
"""

import time

import database
import jobs


def _satisfied(conn, row) -> bool:
    if row["after_job_id"] is not None:
        return conn.execute("SELECT 1 FROM prints WHERE job_id = ? AND outcome = 'completed'",
                            (row["after_job_id"],)).fetchone() is not None
    latest = conn.execute("SELECT outcome FROM prints WHERE project_id = ? AND plate_id = ? AND outcome IS NOT 'unknown' "
                          "ORDER BY started_at DESC, id DESC LIMIT 1",
                          (row["after_project_id"], row["after_plate_id"])).fetchone()
    return latest is not None and latest["outcome"] == "completed"


def _describe(conn, row) -> str:
    if row["after_job_id"] is not None:
        job = conn.execute("SELECT name FROM jobs WHERE id = ?", (row["after_job_id"],)).fetchone()
        return f"job {job['name'] if job else row['after_job_id']}"
    return f"plate {row['after_plate_id']}"


def for_jobs(job_ids: list) -> dict:
    """job id → its holds, each with `satisfied`."""
    if not job_ids:
        return {}
    result = {job_id: [] for job_id in job_ids}
    with database.transaction() as conn:
        rows = conn.execute(f"SELECT * FROM job_holds WHERE job_id IN ({', '.join('?' * len(job_ids))}) ORDER BY id",
                            job_ids).fetchall()
        for row in rows:
            result[row["job_id"]].append({
                "id": row["id"], "after_job_id": row["after_job_id"], "after_project_id": row["after_project_id"],
                "after_plate_id": row["after_plate_id"], "satisfied": _satisfied(conn, row)})
    return result


def waiting_on(job_id: int) -> list:
    """Descriptions of what the Job is still waiting for; empty when it is free to start."""
    with database.transaction() as conn:
        rows = conn.execute("SELECT * FROM job_holds WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        return [_describe(conn, row) for row in rows if not _satisfied(conn, row)]


def add(job_id: int, body: dict) -> dict:
    after_job = body.get("after_job_id")
    project, plate = body.get("after_project_id"), body.get("after_plate_id")
    if (after_job is None) == (project is None):
        raise jobs.JobError("Hold on either after_job_id or after_project_id with after_plate_id")
    with database.transaction() as conn:
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise jobs.JobError("Job not found", 404)
        if row["state"] != jobs.QUEUED:
            raise jobs.JobError("Only queued jobs can be held", 409)
        if after_job is not None:
            if not isinstance(after_job, int) or isinstance(after_job, bool):
                raise jobs.JobError("after_job_id must be a job number")
            if after_job == job_id:
                raise jobs.JobError("A job can't wait for itself")
            if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (after_job,)).fetchone() is None:
                raise jobs.JobError("Job to wait for not found", 404)
            if conn.execute("SELECT 1 FROM job_holds WHERE job_id = ? AND after_job_id = ?", (after_job, job_id)).fetchone():
                raise jobs.JobError("That job is already waiting for this one", 409)
            plate = None
        else:
            if plate is None or not all(isinstance(v, int) and not isinstance(v, bool) for v in (project, plate)):
                raise jobs.JobError("after_project_id and after_plate_id must be numbers")
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project,)).fetchone() is None:
                raise jobs.JobError("Project not found", 404)
        conn.execute("INSERT INTO job_holds (job_id, after_job_id, after_project_id, after_plate_id, created_at) "
                     "VALUES (?, ?, ?, ?, ?)", (job_id, after_job, project, plate, time.time()))
        jobs._log(conn, job_id, "held", f"waiting for {'job ' + str(after_job) if after_job is not None else f'plate {plate}'}")
    return jobs.get_job(job_id)


def remove(job_id: int, hold_id: int) -> dict:
    with database.transaction() as conn:
        if conn.execute("DELETE FROM job_holds WHERE id = ? AND job_id = ?", (hold_id, job_id)).rowcount == 0:
            raise jobs.JobError("Hold not found", 404)
        jobs._log(conn, job_id, "hold_removed", str(hold_id))
    return jobs.get_job(job_id)
