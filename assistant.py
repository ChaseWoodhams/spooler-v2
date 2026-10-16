"""Assistant: availability and Settings.

Everything here is optional and contained: nothing in core imports it at startup in a way that can wait on it,
the availability check runs on its own thread (never the request thread's own work) with a short timeout, and a
failure of any kind becomes "unavailable" with a reason instead of an error.
"""
import json
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from persistence import DATA_DIR

DEFAULT_JEV_URL = "http://127.0.0.1:3455"
SETTINGS_NAME = "assistant_settings.json"      # {"cli_path": str, "jev_url": str}; no API key is ever stored
CACHE_S = 5.0                                  # how long a check result is reused
PROBE_TIMEOUT_S = 2.5                          # Jev must answer ok within this
WAIT_S = 3.5                                   # the most a status request waits for a running check
MAX_FIELD = 500

_data_dir: Path = DATA_DIR
_lock = threading.Lock()
_cache: dict = {}        # {"key": (cli_path, jev_url), "at": monotonic, "result": dict}
_inflight: dict = {}     # {"key": ..., "done": threading.Event, "result": dict | None}


class AssistantError(ValueError):
    """A bad Settings value; the route answers 400 with the message."""


def configure(data_dir: Path | None = None) -> None:
    global _data_dir
    _data_dir = Path(data_dir or DATA_DIR)
    _clear_cache()


def data_dir() -> Path:
    """The data directory the Assistant reads and the MCP server is told to use."""
    return _data_dir


def _clear_cache() -> None:
    with _lock:
        _cache.clear()
        _inflight.clear()


# ── Settings ──────────────────────────────────────────────────────────────────

def _settings_file() -> Path:
    return _data_dir / SETTINGS_NAME


def read_raw_checked() -> tuple:
    """(the settings file as saved, a note or None). A missing file is {} with no note; one that is not JSON or not an object is {} with a note."""
    try:
        text = _settings_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except Exception:
        return {}, "The Assistant settings file could not be read, so the defaults are in use."
    try:
        raw = json.loads(text)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return {}, "The Assistant settings file is not a valid settings object, so the defaults are in use."
    return raw, None


def read_raw() -> dict:
    """The settings file as saved ({} when missing or damaged). Never raises. Connection settings and the tuning section share it."""
    return read_raw_checked()[0]


def write_raw(raw: dict) -> None:
    _settings_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = _settings_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(raw), encoding="utf-8")
    os.replace(tmp, _settings_file())
    _clear_cache()


def load_settings() -> dict:
    """The saved connection Settings (CLI path, Jev address) with working defaults filled in. Never raises."""
    raw = read_raw()
    cli = raw.get("cli_path") if isinstance(raw.get("cli_path"), str) else ""
    url = raw.get("jev_url") if isinstance(raw.get("jev_url"), str) else ""
    return {"cli_path": cli.strip(), "jev_url": url.strip().rstrip("/") or DEFAULT_JEV_URL}


def settings_view() -> dict:
    import assistant_tuning                      # lazy: it imports this module
    return {**load_settings(), "defaults": {"cli_path": "", "jev_url": DEFAULT_JEV_URL}, "cli_found": resolve_cli(load_settings()),
            "tuning": assistant_tuning.view()}


def save_settings(body) -> dict:
    """Merge the given keys into the saved Settings. A blank value goes back to its default. `tuning` ({group: {key: value}}) and
    `reset_tuning` (a group name or "all") change the tuning section; everything is checked before anything is written."""
    if not isinstance(body, dict):
        raise AssistantError("Settings must be an object")
    import assistant_tuning
    current = load_settings()
    if "cli_path" in body:
        value = body["cli_path"]
        if not isinstance(value, str) or len(value.strip()) > MAX_FIELD or "\n" in value or "\0" in value:
            raise AssistantError("The CLI path is a single line of text")
        current["cli_path"] = value.strip()
    if "jev_url" in body:
        value = body["jev_url"]
        if not isinstance(value, str) or len(value.strip()) > MAX_FIELD:
            raise AssistantError("The Jev address is a web address such as " + DEFAULT_JEV_URL)
        value = value.strip().rstrip("/")
        if value:
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise AssistantError("The Jev address must start with http:// or https://")
        current["jev_url"] = value or DEFAULT_JEV_URL
    if "tuning" in body or "reset_tuning" in body:
        assistant_tuning.save(body.get("tuning"), body.get("reset_tuning"))      # validates, then writes the file with the old connection keys
    write_raw({**read_raw(), **current})
    return settings_view()


# ── Availability ──────────────────────────────────────────────────────────────

def resolve_cli(settings: dict) -> str | None:
    """The CLI to run: the setting if it names a file, else claude on PATH. None when neither."""
    path = settings.get("cli_path") or ""
    if path:
        return path if os.path.isfile(path) else None
    return shutil.which("claude")


def probe_jev(base_url: str, timeout: float = PROBE_TIMEOUT_S) -> None:
    """Return when GET <base>/jev/status answers ok; raise with the reason otherwise."""
    handler = urllib.request.ProxyHandler({})      # a local address: never through a proxy
    opener = urllib.request.build_opener(handler)
    with opener.open(base_url.rstrip("/") + "/jev/status", timeout=timeout) as resp:
        text = resp.read(65536).decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = text
    value = (data.get("status", data.get("ok")) if isinstance(data, dict) else data)
    if value is True or (isinstance(value, str) and value.strip().lower() == "ok"):
        return
    raise RuntimeError(f"Jev status says {str(value)[:80]!r}")


def _check(settings: dict) -> dict:
    cli = resolve_cli(settings)
    if not cli:
        where = f"No CLI at {settings['cli_path']}" if settings["cli_path"] else "No claude CLI found on PATH"
        return {"available": False, "reason": where + ". Set the CLI path in Settings."}
    try:
        probe_jev(settings["jev_url"], PROBE_TIMEOUT_S)
    except Exception as e:                       # any failure is "unavailable", never an error
        return {"available": False, "reason": f"Jev did not answer at {settings['jev_url']}: {str(e) or type(e).__name__}"}
    return {"available": True, "reason": None}


def _run_check(job: dict, settings: dict) -> None:
    try:
        result = _check(settings)
    except Exception as e:
        result = {"available": False, "reason": f"The availability check failed: {str(e) or type(e).__name__}"}
    result = {**result, "checking": False}
    with _lock:
        if _inflight.get("done") is job["done"]:
            _cache.update(key=job["key"], at=time.monotonic(), result=result)
            _inflight.clear()
    job["result"] = result
    job["done"].set()


CHECKING = {"available": False, "reason": "Still checking the assistant.", "checking": True}


def status(fresh: bool = False, wait: bool = True) -> dict:
    """Availability: cached for CACHE_S, checked on a worker thread. If the check outlasts WAIT_S the answer is
    {"available": False, "checking": True} and the next call has the result. With wait=False nothing waits: the last result for
    these settings is returned even when it has expired (a new check starts behind it), else the checking answer."""
    settings = load_settings()
    key = (settings["cli_path"], settings["jev_url"])
    with _lock:
        have = _cache.get("key") == key
        if not fresh and have and time.monotonic() - _cache["at"] < CACHE_S:
            return _cache["result"]
        job = _inflight if _inflight.get("key") == key else None
        if job is None:
            _inflight.clear()
            _inflight.update(key=key, done=threading.Event(), result=None)
            job = _inflight
            threading.Thread(target=_run_check, args=(dict(_inflight), settings), daemon=True).start()
        done = job["done"]
        if not wait:
            return _cache["result"] if have else CHECKING
    if done.wait(WAIT_S):
        with _lock:
            result = _cache.get("result") if _cache.get("key") == key else None
        if result:
            return result
    return CHECKING


def purpose_summary(record: dict) -> dict:
    """The Purpose the assistant will get: the project default and each Model's effective Purpose."""
    import projects                              # lazy: keeps this module importable on its own
    data = record.get("data") or {}
    default = data.get("purpose") or {"value": "general", "note": ""}
    models = []
    for model in record.get("models") or []:
        p = projects.effective_purpose(data, model["id"])
        models.append({"id": model["id"], "name": model.get("name") or model.get("file") or f"Model {model['id']}",
                       "value": p["value"], "note": p["note"], "source": p["source"]})
    return {"default": {"value": default["value"], "note": default.get("note", "")}, "models": models}


def project_status(record: dict, fresh: bool = False) -> dict:
    return {**status(fresh), "purpose": purpose_summary(record), "jev_url": load_settings()["jev_url"]}
