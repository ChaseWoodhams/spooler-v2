"""Staged Assistant Proposals: the one place anything the Assistant proposes is written, and where the app reads it back.

Standard library only (no jobs, printer or network code), so the read-only MCP server may import it. A Proposal lives in its own
folder, `<projects dir>/.assistant/<token>/proposal.json`, beside `.import-staging`. The MCP server writes it; Spooler's API reads
it, shows it, and applies it with `save_data` when the person accepts. Nothing here touches project data, so deleting staging
(sweeping) never changes a project.

A Proposal is kind-agnostic. What every kind carries:
  token, project_id, run (the Assistant run that made it), base_revision, kind ("orientation", "arrange", "plan", "settings"),
  title, scope (one line: what it covers), reason (one line), review ("placement" | "settings": which review sheet shows it),
  data (the resulting project data, applied as is on Accept), emptied (plate ids that lose their Slice on Accept),
  diff [{label, text}] (readable lines; `label` is user text such as an Object name), notes [str], pinned [{object, label}],
  problems [str], targets {objects: [ids], plates: [ids]}, created_at.
"""
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

STAGING_NAME = ".assistant"
FILE_NAME = "proposal.json"
TTL_S = 24 * 3600
MAX_PER_RUN = 3
TERMINAL_PREFIX = "terminal-"       # run ids of Proposals staged by a terminal-started server (no app run owns them)
TOKEN = re.compile(r"^[0-9a-f]{32}$")


def staging_root(projects_dir) -> Path:
    return Path(projects_dir) / STAGING_NAME


def new_token() -> str:
    return uuid.uuid4().hex


def valid_token(token) -> bool:
    return isinstance(token, str) and bool(TOKEN.match(token))


def _folder(projects_dir, token: str) -> Path:
    if not valid_token(token):
        raise ValueError("not a Proposal token")           # a path traversal guard: only 32 hex characters name a folder
    return staging_root(projects_dir) / token


def write(projects_dir, proposal: dict) -> dict:
    """Stage one Proposal (atomically: a reader never sees half of it). Fills in `token` and `created_at` and returns the record."""
    record = {**proposal, "token": proposal.get("token") or new_token(), "created_at": proposal.get("created_at") or time.time()}
    folder = _folder(projects_dir, record["token"])
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / (FILE_NAME + ".tmp")
    temp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temp, folder / FILE_NAME)
    return record


def read(projects_dir, token) -> dict | None:
    """The staged Proposal, or None when the token is unknown, swept or damaged."""
    if not valid_token(token):
        return None
    try:
        record = json.loads((_folder(projects_dir, token) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("token") == token else None


def remove(projects_dir, token) -> bool:
    """Delete one Proposal's folder. Only that token's own folder is touched; removing twice is harmless."""
    if not valid_token(token):
        return False
    folder = _folder(projects_dir, token)
    existed = folder.exists()
    shutil.rmtree(folder, ignore_errors=True)
    return existed


def listing(projects_dir, *, project_id=None, run=None) -> list:
    """Staged Proposals, oldest first, optionally only one project's or one run's."""
    root = staging_root(projects_dir)
    if not root.is_dir():
        return []
    found = []
    for entry in root.iterdir():
        if entry.is_dir() and valid_token(entry.name):
            record = read(projects_dir, entry.name)
            if record and (project_id is None or record.get("project_id") == int(project_id)) and (run is None or record.get("run") == run):
                found.append(record)
    return sorted(found, key=lambda r: (r.get("created_at") or 0, r["token"]))


def latest_of_each_kind(records: list) -> list:
    """Only the newest Proposal of each kind (the rest were replaced), in the order they were made."""
    newest = {}
    for record in records:
        newest[record.get("kind")] = record
    return [r for r in records if newest[r.get("kind")] is r]


def remove_run(projects_dir, run, *, keep=()) -> int:
    """Delete every Proposal a run staged except the tokens in `keep` (a cancelled or timed-out run keeps none)."""
    removed = 0
    for record in listing(projects_dir, run=run):
        if record["token"] not in keep:
            removed += remove(projects_dir, record["token"])
    return removed


def sweep(projects_dir, now: float | None = None, startup: bool = False) -> int:
    """Delete staging that is older than 24 hours or unreadable. At startup no run can still be working, so all of it goes
    (the panel's thread is gone too: it lives in the page). Folders that are not a token are never touched."""
    root = staging_root(projects_dir)
    if not root.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for entry in list(root.iterdir()):
        if not entry.is_dir() or not valid_token(entry.name):
            continue
        record = read(projects_dir, entry.name)
        if startup:
            expired = True
        elif record is not None:
            expired = (record.get("created_at") or 0) + TTL_S <= now
        else:
            try:
                expired = entry.stat().st_mtime + TTL_S <= now
            except OSError:
                expired = False
        if expired:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def summary(record: dict) -> dict:
    """What a card needs, never the resulting data."""
    keys = ("token", "kind", "title", "scope", "reason", "diff", "notes", "pinned", "problems", "review", "base_revision", "project_id", "created_at")
    return {key: record.get(key) for key in keys}


def terminal_pending(projects_dir, project_id) -> list:
    """The project's Proposals staged by terminal sessions (runs named `terminal-...`), newest of each kind per session; older ones
    of that kind are deleted. App-started runs are not listed: their Proposals reach the panel through the run."""
    by_run = {}
    for record in listing(projects_dir, project_id=project_id):
        if str(record.get("run") or "").startswith(TERMINAL_PREFIX):
            by_run.setdefault(record["run"], []).append(record)
    kept = []
    for records in by_run.values():
        latest = latest_of_each_kind(records)
        for record in records:
            if record not in latest:
                remove(projects_dir, record["token"])
        kept += latest
    return sorted(kept, key=lambda r: (r.get("created_at") or 0, r["token"]))
