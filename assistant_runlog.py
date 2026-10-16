"""The Assistant run log: one capped record per finished Assistant run, kept in a file in the data directory.

It holds project names, questions and the exact prompts, so it stays on this computer, is capped (`CAP` newest entries) and
`clear()` empties it. Nothing here is sent anywhere. The file is read on every call (no cache), so a restart changes nothing;
a missing or damaged file reads as empty and the next run starts a new one.

  record(run)  -> append the finished run (newest first); the runner calls it once from `_finish`
  entries()    -> newest first
  clear()      -> drop every entry
The tuning stored in an entry is the run's whole snapshot (`run.tuning`), whatever groups exist.
"""
import datetime
import json
import os
import threading

import assistant

CAP = 50
FILE = "assistant_run_log.json"
_OUTCOMES = {"answer": "answered", "proposal": "proposal", "error": "failed", "usage_limit": "failed", "cancelled": "cancelled", "timeout": "timed out"}
_lock = threading.Lock()


def log_file():
    return assistant.data_dir() / FILE


def _read() -> list:
    try:
        raw = json.loads(log_file().read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("entries") if isinstance(raw, dict) else None
    return [e for e in items if isinstance(e, dict)] if isinstance(items, list) else []


def _write(items: list) -> None:
    log_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = log_file().with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "entries": items}), encoding="utf-8")
    os.replace(tmp, log_file())


def entries() -> list:
    with _lock:
        return _read()


def clear() -> None:
    with _lock:
        _write([])


def entry_for(run) -> dict:
    ended = run.finished if run.finished is not None else run.started
    failed = run.state in ("error", "usage_limit", "cancelled", "timeout")
    return {"time": datetime.datetime.now().isoformat(timespec="seconds"), "project_id": run.project_id, "project_name": run.project_name,
            "question": run.question, "outcome": _OUTCOMES.get(run.state, run.state), "reason": run.error if failed else None,
            "duration_seconds": round(ended - run.started, 1), "model": run.model, "tool_calls": run.tool_calls,
            "steps": [s.get("label") for s in run.steps], "proposals": list(run.proposals), "prompt": run.prompt, "system_prompt": run.system_prompt, "tuning": run.tuning}


def record(run) -> None:
    with _lock:
        _write(([entry_for(run)] + _read())[:CAP])
