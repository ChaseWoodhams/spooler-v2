"""What the user last picked for a new project: printer, profile, spool, plate side and print options.

Kept in the database so every browser and device sees the same ones. Each default is its own row in
`remembered_defaults` (key, JSON value); a missing row means nothing is remembered yet.
"""
import json
import time

import database
import jobs
import slicer
import state

KEYS = ("printer", "profile", "spool", "plate_side", "options")
OPTION_KEYS = ("leveling", "timelapse")


def get() -> dict:
    with database.transaction() as conn:
        rows = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM remembered_defaults")}
    stored = {key: rows.get(key) for key in KEYS}
    stored["options"] = stored["options"] or {}
    return stored


def _clean(key: str, value):
    if value is None:
        return {} if key == "options" else None
    if key == "printer":
        if value not in state.printers:
            raise jobs.JobError("Unknown printer")
        return value
    if key == "profile":
        if not isinstance(value, str) or not any(b["id"] == value for b in slicer.list_bundles()):
            raise jobs.JobError("Unknown profile")
        return value
    if key == "spool":
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) < 1:
            raise jobs.JobError("spool must be a spool number")
        return int(value)
    if key == "plate_side":
        if value not in slicer.BED_TYPES:
            raise jobs.JobError(f"plate_side must be one of: {', '.join(slicer.BED_TYPES)}")
        return value
    if not isinstance(value, dict) or any(k not in OPTION_KEYS for k in value):
        raise jobs.JobError(f"options may only contain: {', '.join(OPTION_KEYS)}")
    return {k: bool(v) for k, v in value.items()}


def save(body) -> dict:
    """Merge the given defaults into the remembered ones; null forgets one. Nothing is saved if any value is refused."""
    if not isinstance(body, dict) or any(k not in KEYS for k in body):
        raise jobs.JobError(f"Defaults are: {', '.join(KEYS)}")
    cleaned = {key: _clean(key, value) for key, value in body.items()}
    now = time.time()
    with database.transaction() as conn:
        for key, value in cleaned.items():
            conn.execute("INSERT INTO remembered_defaults (key, value, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, json.dumps(value), now))
    return get()


def usable() -> dict:
    """The remembered defaults that still point at something that exists (a printer or profile may be gone)."""
    stored = get()
    if stored["printer"] not in state.printers:
        stored["printer"] = None
    if stored["profile"] not in {b["id"] for b in slicer.list_bundles()}:
        stored["profile"] = None
    return stored
