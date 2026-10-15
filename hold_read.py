"""Whether a queued Job still waits on a hold: the read side of `holds`, over a connection the caller supplies (pure; no jobs,
printer or network code), so a read-only connection will do."""


def satisfied(conn, row) -> bool:
    if row["after_job_id"] is not None:
        return conn.execute("SELECT 1 FROM prints WHERE job_id = ? AND outcome = 'completed'",
                            (row["after_job_id"],)).fetchone() is not None
    latest = conn.execute("SELECT outcome FROM prints WHERE project_id = ? AND plate_id = ? AND outcome IS NOT 'unknown' "
                          "ORDER BY started_at DESC, id DESC LIMIT 1",
                          (row["after_project_id"], row["after_plate_id"])).fetchone()
    return latest is not None and latest["outcome"] == "completed"


def describe(conn, row) -> str:
    if row["after_job_id"] is not None:
        job = conn.execute("SELECT name FROM jobs WHERE id = ?", (row["after_job_id"],)).fetchone()
        return f"job {job['name'] if job else row['after_job_id']}"
    return f"plate {row['after_plate_id']}"


def waiting_on(conn, job_id: int) -> list:
    """Descriptions of what the Job is still waiting for; empty when it is free to start."""
    rows = conn.execute("SELECT * FROM job_holds WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    return [describe(conn, row) for row in rows if not satisfied(conn, row)]
