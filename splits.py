"""
Splitting models that are too big for the printer into parts with alignment holes.

The geometry lives in split_engine.py, which runs as a subprocess — one stage at
a time — in DATA_DIR/splits/<id>/. This module keeps the records, runs the stages
in the background, and turns finished parts into slices for the print queue.

States
  analyzing → planning → ready ⇄ checking → splitting → split → queued
  A failed stage leaves the record `failed` with `stage` and `error` set; retrying
  runs that stage again. A stage interrupted by a restart runs again on start.
"""

import asyncio
import hashlib
import importlib.util
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import assembly_edits as edits
import assembly_plan as planner
import database
import jobs  # used as a module: jobs.JobError, …
import slicer
import state
from datapaths import read_json as _read
from persistence import DATA_DIR

APP_DIR         = Path(__file__).parent
ENGINE          = APP_DIR / "split_engine.py"
STAGE_TIMEOUT_S = int(os.getenv("SPLIT_STAGE_TIMEOUT_S", "3600"))
MAX_UPLOAD_BYTES = int(os.getenv("SPLIT_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
MODEL_SUFFIXES  = (".stl", ".obj", ".3mf")
RECENT_LIMIT    = 30
NO_WINDOW       = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REQUIRED_MODULES = ("manifold3d", "trimesh", "numpy", "scipy", "networkx")
GUIDE_MODULES    = ("reportlab",)          # only the Assembly guide needs these; without them only Generate is off
PAPER_SIZES      = ("a4", "letter")

ANALYZING, PLANNING, READY, CHECKING = "analyzing", "planning", "ready", "checking"
SPLITTING, SPLIT, QUEUED, FAILED     = "splitting", "split", "queued", "failed"
STAGE_FOR  = {ANALYZING: "analyze", PLANNING: "plan", CHECKING: "check", SPLITTING: "split"}
STATE_FOR  = {stage: s for s, stage in STAGE_FOR.items()}
AFTER      = {ANALYZING: PLANNING, PLANNING: READY, CHECKING: READY, SPLITTING: SPLIT}

DEFAULT_SETTINGS = {
    "scale":      1.0,
    "up":         "z",
    "printer":    {"bed_x": 240.0, "bed_y": 240.0, "height": 250.0},
    "planner":    {"mode": "smart", "balanced": True},
    "connectors": {"kind": "rod", "diameter": 2.0, "fallback_diameter": 1.0, "clearance": 0.15, "depth": 6.5,
                   "spacing": 100.0, "max_per_joint": 8},
    "labels":     {"enabled": True},      # Part numbers debossed into each part
    "guide":      {"heavy_kg": 2.0, "heavy_mm": 400.0},   # Assembly guide: warn when a join moves more than this weight or length
}

_data_dir: Path = DATA_DIR
_wakeup: asyncio.Event | None = None
_progress: dict = {}     # split id → latest progress.json while a stage runs
_processes: dict = {}    # split id → running engine process
_process_lock = threading.Lock()


def engine_command(stage: str, work: Path) -> list:
    """The command that runs one engine stage (patched in tests)."""
    return [sys.executable, str(ENGINE), stage, str(work)]


# ── Setup ──────────────────────────────────────────────────────────────────────

def configure(data_dir: Path | None = None) -> None:
    global _data_dir
    _data_dir = Path(data_dir or DATA_DIR)


def splits_dir() -> Path:
    return _data_dir / "splits"


def work_dir(split_id: int) -> Path:
    return splits_dir() / str(int(split_id))


def missing_modules() -> list:
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def missing_guide_modules() -> list:
    return [name for name in GUIDE_MODULES if importlib.util.find_spec(name) is None]


def system_locale() -> str | None:
    """The machine's language and region, e.g. "en_US" or "English_United States" (patched in tests)."""
    return locale.getlocale()[0] or os.environ.get("LC_ALL") or os.environ.get("LANG")


def default_paper() -> str:
    """Letter for a US locale, A4 for everything else."""
    name = (system_locale() or "").replace("-", "_")
    return "letter" if name.startswith("en_US") or "United States" in name else "a4"


def init() -> None:
    database.migrate()
    missing = missing_modules()
    if missing:
        print(f"[Split] Model splitting unavailable — install {', '.join(missing)} (pip install -r requirements.txt)")


def status() -> dict:
    missing = missing_modules()
    guide_missing = missing_guide_modules()
    guide = {"available": not guide_missing, "missing": guide_missing, "default_paper": default_paper(), "papers": list(PAPER_SIZES),
             "hint": f"Install {', '.join(guide_missing)} to make the Assembly guide (pip install -r requirements.txt)" if guide_missing else ""}
    return {"available": not missing, "missing": missing, "guide": guide, "defaults": DEFAULT_SETTINGS,
            "model_types": list(MODEL_SUFFIXES), "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024)}


# ── Settings ───────────────────────────────────────────────────────────────────

def _number(value, name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise jobs.JobError(f"{name} must be a number")
    if not low <= number <= high:
        raise jobs.JobError(f"{name} must be between {low:g} and {high:g}")
    return number


def normalize_settings(raw: dict | None, base: dict | None = None) -> dict:
    """Merge user settings over `base` (or the defaults) and validate them."""
    base = json.loads(json.dumps(base or DEFAULT_SETTINGS))
    raw = raw or {}
    if not isinstance(raw, dict):
        raise jobs.JobError("Settings must be an object")
    out = base
    out.setdefault("labels", dict(DEFAULT_SETTINGS["labels"]))   # splits saved before part numbers existed
    out.setdefault("guide", dict(DEFAULT_SETTINGS["guide"]))     # ... or before the guide's warnings
    if "scale" in raw:
        out["scale"] = _number(raw["scale"], "Scale", 1e-4, 1e5)
    if "up" in raw:
        if raw["up"] not in ("x", "y", "z"):
            raise jobs.JobError("Up axis must be x, y or z")
        out["up"] = raw["up"]
    printer = raw.get("printer") or {}
    for key, label in (("bed_x", "Bed width"), ("bed_y", "Bed depth"), ("height", "Build height")):
        if key in printer:
            out["printer"][key] = _number(printer[key], label, 50, 2000)
    planner = raw.get("planner") or {}
    if "mode" in planner:
        if planner["mode"] not in ("smart", "grid"):
            raise jobs.JobError("Planner must be smart or grid")
        out["planner"]["mode"] = planner["mode"]
    if "balanced" in planner:
        out["planner"]["balanced"] = bool(planner["balanced"])
    connectors = raw.get("connectors") or {}
    if "kind" in connectors:
        if connectors["kind"] not in ("rod", "pin", "none"):
            raise jobs.JobError("Connector type must be rod, pin or none")
        out["connectors"]["kind"] = connectors["kind"]
    if "diameter" in connectors:
        out["connectors"]["diameter"] = _number(connectors["diameter"], "Diameter", 0.8, 12)
    if "fallback_diameter" in connectors:
        fallback = connectors["fallback_diameter"]
        out["connectors"]["fallback_diameter"] = None if fallback in (None, "", 0) else _number(fallback, "Fallback diameter", 0.8, 12)
    if "clearance" in connectors:
        out["connectors"]["clearance"] = _number(connectors["clearance"], "Clearance", 0, 0.6)
    if "depth" in connectors:
        out["connectors"]["depth"] = _number(connectors["depth"], "Hole depth", 2, 30)
    if "spacing" in connectors:
        out["connectors"]["spacing"] = _number(connectors["spacing"], "Spacing", 20, 1000)
    if "max_per_joint" in connectors:
        out["connectors"]["max_per_joint"] = int(_number(connectors["max_per_joint"], "Rods per joint", 1, 50))
    labels = raw.get("labels") or {}
    if "enabled" in labels:
        if not isinstance(labels["enabled"], bool):
            raise jobs.JobError("Part numbers must be on or off")
        out["labels"]["enabled"] = labels["enabled"]
    guide = raw.get("guide") or {}
    if "heavy_kg" in guide:
        out["guide"]["heavy_kg"] = _number(guide["heavy_kg"], "Heavy join weight", 0.1, 1000)
    if "heavy_mm" in guide:
        out["guide"]["heavy_mm"] = _number(guide["heavy_mm"], "Heavy join length", 10, 10000)
    return out


# ── Records ────────────────────────────────────────────────────────────────────

def _write_job(split_id: int, record: dict) -> None:
    work = work_dir(split_id)
    job = {"source": record["source_file"], **record["settings"]}
    tmp = work / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=1), encoding="utf-8")
    os.replace(tmp, work / "job.json")


def _queue_summary(conn, split_id: int) -> dict:
    rows = conn.execute("SELECT s.state AS slice_state, j.state AS job_state FROM slices s "
                        "LEFT JOIN jobs j ON j.slice_id = s.id WHERE s.split_id = ?", (split_id,)).fetchall()
    summary = {"slices": 0, "slicing": 0, "slice_failed": 0, "queued": 0, "printing": 0, "completed": 0, "failed": 0}
    counted = set()
    for row in rows:
        summary["slices"] += 1
        if row["slice_state"] in (slicer.QUEUED, slicer.SLICING):
            summary["slicing"] += 1
        elif row["slice_state"] == slicer.FAILED:
            summary["slice_failed"] += 1
        state_name = row["job_state"]
        if state_name == "queued":
            summary["queued"] += 1
        elif state_name in ("starting", "printing"):
            summary["printing"] += 1
        elif state_name == "completed":
            summary["completed"] += 1
        elif state_name in ("cancelled", "failed", "unknown"):
            summary["failed"] += 1
        counted.add(state_name)
    return summary


def _to_split(row, conn) -> dict:
    record = dict(row)
    record["settings"] = json.loads(record["settings"] or "{}")
    record["progress"] = _progress.get(record["id"]) if record["state"] in STAGE_FOR else None
    record["queue"] = _queue_summary(conn, record["id"])
    owner = conn.execute("SELECT id, quick, draft FROM projects WHERE split_id = ? "
                         "OR id IN (SELECT project_id FROM project_splits WHERE split_id = ?) ORDER BY id LIMIT 1",
                         (record["id"], record["id"])).fetchone()
    record["project_id"] = owner["id"] if owner else None  # the project that owns this split, quick or not
    record["project_quick"] = bool(owner and owner["quick"])  # a hidden quick project's: never offered as parts to add
    record["project_draft"] = bool(owner and owner["draft"])  # a hidden draft's: likewise
    return record


def get_split(split_id: int, detail: bool = False) -> dict:
    with database.transaction() as conn:
        row = conn.execute("SELECT * FROM splits WHERE id = ?", (split_id,)).fetchone()
        if row is None:
            raise jobs.JobError("Split not found", 404)
        record = _to_split(row, conn)
    if detail:
        work = work_dir(split_id)
        for name in ("analysis", "cuts", "check", "result"):
            record[name] = _read(work, f"{name}.json")
        record["has_preview"] = (work / "preview.bin").is_file()
        record["has_parts_preview"] = (work / "parts_preview.bin").is_file()
    return record


def list_splits(recent: int = RECENT_LIMIT) -> list:
    with database.transaction() as conn:
        rows = conn.execute("SELECT * FROM splits ORDER BY updated_at DESC, id DESC LIMIT ?", (recent,)).fetchall()
        return [_to_split(r, conn) for r in rows]


def _set_state(split_id: int, new_state: str, **fields) -> None:
    fields = {"state": new_state, "updated_at": time.time(), **fields}
    with database.transaction() as conn:
        conn.execute(f"UPDATE splits SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), split_id))


def _require(split_id: int, allowed: tuple, action: str) -> dict:
    record = get_split(split_id)
    if record["state"] not in allowed:
        busy = record["state"] in STAGE_FOR
        raise jobs.JobError(f"Can't {action} while the split is {record['state']}" if busy
                            else f"Can't {action} now (the split is {record['state']})", 409)
    return record


def safe_model_name(name: str) -> str:
    name = slicer._UNSAFE_CHARS.sub("_", (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip())
    stem, dot, suffix = name.rpartition(".")
    if not dot or f".{suffix.lower()}" not in MODEL_SUFFIXES:
        raise jobs.JobError("Upload an STL, OBJ or 3MF model to split")
    if not stem.strip("."):
        raise jobs.JobError("The model file needs a name")
    return f"{stem[:100]}.{suffix.lower()}"


def create_split(stream, length: int, filename: str, settings: dict | None = None, notes: str = "") -> dict:
    """Store an uploaded model and start analysing it."""
    missing = missing_modules()
    if missing:
        raise jobs.JobError(f"Model splitting needs {', '.join(missing)} installed on the Spooler computer", 503)
    name = safe_model_name(filename)
    settings = normalize_settings(settings)
    if length <= 0:
        raise jobs.JobError("The file is empty")
    if length > MAX_UPLOAD_BYTES:
        raise jobs.JobError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    root = splits_dir()
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".upload-", dir=root))
    final_dir = None
    try:
        (staging / "source").mkdir()
        digest, remaining = hashlib.sha256(), length
        with (staging / "source" / name).open("wb") as fh:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise jobs.JobError("The upload was interrupted")
                fh.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        now = time.time()
        with database.transaction() as conn:
            split_id = conn.execute(
                "INSERT INTO splits (state, stage, source_name, source_file, source_size, source_sha256, settings, notes, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ANALYZING, "analyze", name, f"source/{name}", length, digest.hexdigest(), json.dumps(settings),
                 str(notes or "")[:2000], now, now)).lastrowid
            final_dir = work_dir(split_id)
            if final_dir.exists():  # left behind by a split whose id SQLite has reused
                shutil.rmtree(final_dir)
            os.replace(staging, final_dir)
        _write_job(split_id, {"source_file": f"source/{name}", "settings": settings})
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if final_dir is not None:
            shutil.rmtree(final_dir, ignore_errors=True)
        raise
    notify_worker()
    return get_split(split_id)


def update_settings(split_id: int, body: dict) -> dict:
    """Change settings. A new scale or up axis analyses the model again; the rest apply to the next plan or split."""
    record = _require(split_id, (READY, SPLIT, QUEUED, FAILED), "change settings")
    settings = normalize_settings(body, record["settings"])
    reanalyze = settings["scale"] != record["settings"].get("scale") or settings["up"] != record["settings"].get("up")
    with database.transaction() as conn:
        conn.execute("UPDATE splits SET settings = ?, updated_at = ? WHERE id = ?", (json.dumps(settings), time.time(), split_id))
    _write_job(split_id, {"source_file": record["source_file"], "settings": settings})
    if reanalyze:
        for name in ("analysis.json", "cuts.json", "check.json", "result.json", "preview.bin", "parts_preview.bin"):
            (work_dir(split_id) / name).unlink(missing_ok=True)
        _set_state(split_id, ANALYZING, stage="analyze", error=None)
        notify_worker()
    return get_split(split_id, detail=True)


def request_plan(split_id: int, body: dict) -> dict:
    """Plan cuts automatically. `keep_cuts` (smart only) keeps the current cuts and only splits pieces that don't fit."""
    record = _require(split_id, (READY, SPLIT, QUEUED, FAILED), "plan cuts")
    if not (work_dir(split_id) / "work.npz").is_file():
        raise jobs.JobError("The model hasn't been analysed yet", 409)
    planner = {k: body[k] for k in ("mode", "balanced") if k in body}
    settings = normalize_settings({"planner": planner, **{k: body[k] for k in ("printer", "connectors") if k in body}},
                                  record["settings"])
    settings["planner"]["keep_cuts"] = bool(body.get("keep_cuts"))
    if settings["planner"]["keep_cuts"]:  # Auto-fix: only smart cuts can keep the current cuts and add to them
        settings["planner"]["mode"] = "smart"
    with database.transaction() as conn:
        conn.execute("UPDATE splits SET settings = ?, updated_at = ? WHERE id = ?", (json.dumps(settings), time.time(), split_id))
    _write_job(split_id, {"source_file": record["source_file"], "settings": settings})
    _set_state(split_id, PLANNING, stage="plan", error=None)
    notify_worker()
    return get_split(split_id)


def save_cuts(split_id: int, body: dict) -> dict:
    """Store edited cuts and check which pieces they make."""
    _require(split_id, (READY, SPLIT, QUEUED, FAILED), "edit cuts")
    cuts = body.get("cuts")
    if not isinstance(cuts, list) or len(cuts) > 200:
        raise jobs.JobError("Cuts must be a list of at most 200 cuts")
    work = work_dir(split_id)
    previous = _read(work, "cuts.json") or {}
    tmp = work / "cuts.json.tmp"
    tmp.write_text(json.dumps({"mode": previous.get("mode", "edited"), "cuts": cuts}, indent=1), encoding="utf-8")
    os.replace(tmp, work / "cuts.json")
    _set_state(split_id, CHECKING, stage="check", error=None)
    notify_worker()
    return get_split(split_id)


def request_split(split_id: int) -> dict:
    record = _require(split_id, (READY, SPLIT, QUEUED), "split")
    check = _read(work_dir(split_id), "check.json")
    if not check:
        raise jobs.JobError("Plan the cuts first", 409)
    tiny = sum(bool(p.get("tiny")) for p in check["pieces"])
    if tiny:
        raise jobs.JobError(f"{tiny} part{'s are' if tiny != 1 else ' is'} too small to print — move the cut that makes {'them' if tiny != 1 else 'it'} or remove it", 409)
    oversize = sum(not p["fits"] for p in check["pieces"])
    if oversize:
        raise jobs.JobError(f"{oversize} part{'s are' if oversize != 1 else ' is'} still too big — use Auto-fix or move the cuts", 409)
    _set_state(split_id, SPLITTING, stage="split", error=None)
    notify_worker()
    return get_split(split_id)


def retry(split_id: int) -> dict:
    record = _require(split_id, (FAILED,), "retry")
    stage = record["stage"] or "analyze"
    _set_state(split_id, STATE_FOR.get(stage, ANALYZING), error=None)
    notify_worker()
    return get_split(split_id)


def delete_split(split_id: int) -> None:
    get_split(split_id)
    with _process_lock:
        proc = _processes.get(split_id)
    if proc is not None and proc.poll() is None:
        proc.kill()
    with database.transaction() as conn:
        conn.execute("DELETE FROM splits WHERE id = ?", (split_id,))
    shutil.rmtree(work_dir(split_id), ignore_errors=True)
    _progress.pop(split_id, None)
    _guide_progress.pop(split_id, None)


def file_path(split_id: int, kind: str) -> Path:
    get_split(split_id)
    names = {"preview": "preview.bin", "parts-preview": "parts_preview.bin"}
    path = work_dir(split_id) / names[kind]
    if not path.is_file():
        raise jobs.JobError("Preview not available yet", 404)
    return path


def part_path(split_id: int, number: int) -> tuple:
    record = get_split(split_id, detail=True)
    part = next((p for p in (record.get("result") or {}).get("parts", []) if p["number"] == number), None)
    path = work_dir(split_id) / "parts" / part["file"] if part else None
    if not path or not path.is_file():
        raise jobs.JobError("Part not found", 404)
    return path, f"{Path(record['source_name']).stem}_part{number:02d}.stl"


# ── The assembly plan ──────────────────────────────────────────────────────────

_plans: dict = {}   # split id → (files' modification times, planning record)


def _planning(split_id: int) -> dict:
    """The model, the automatic plan and the plan to use (edits applied when they are valid). Cached; read-only."""
    get_split(split_id)
    work = work_dir(split_id)
    result = _read(work, "result.json")
    if not result:
        raise jobs.JobError("Split the model first", 409)
    limits = {**DEFAULT_SETTINGS["guide"], **(get_split(split_id)["settings"].get("guide") or {})}
    stamp = (limits["heavy_kg"], limits["heavy_mm"]) + tuple((work / name).stat().st_mtime_ns if (work / name).is_file() else 0
                                                              for name in ("result.json", "cuts.json", "parts_preview.bin", edits.FILE))
    cached = _plans.get(split_id)
    if cached and cached[0] == stamp:
        return cached[1]
    cuts = result.get("cuts") or (_read(work, "cuts.json") or {}).get("cuts") or []
    model = planner.SplitModel(result, cuts, planner.read_preview(work / "parts_preview.bin"))
    auto = planner.build_plan(model, heavy_kg=limits["heavy_kg"], heavy_mm=limits["heavy_mm"])
    record = {"model": model, "auto": auto, "plan": auto.to_dict(), "edits": [], "state": "none", "reason": "", "failed": None}
    saved = edits.load(work)
    if saved and saved["edits"]:
        record["edits"] = saved["edits"]
        record["state"], record["reason"] = "outdated", "The cuts or parts have changed since these edits were made."
        if auto.ok and saved.get("fingerprint") == model.fingerprint:
            done = edits.replay(model, auto.plan, saved["edits"])
            if done.ok:
                record["state"], record["reason"] = "applied", ""
                record["plan"] = planner.add_warnings(model, planner.PlanResult(plan=done.plan), limits["heavy_kg"], limits["heavy_mm"]).to_dict()
            else:
                index, _edit, refused = done.dropped[0]
                record["failed"], record["reason"] = index, f"Edit {index + 1} no longer works: {refused}"
    if auto.ok:
        record["plan"]["edits"] = {"state": record["state"], "count": len(record["edits"]), "reason": record["reason"], "failed": record["failed"]}
    _plans[split_id] = (stamp, record)
    return record


def assembly_plan(split_id: int) -> dict:
    """How the split goes together (Sections, steps), or why it cannot be assembled as cut. Read-only and cached.
    Saved edits are applied when they still match the split and pass the validator; otherwise the automatic plan is
    served and `edits.state` says "outdated"."""
    return _planning(split_id)["plan"]


def assembly_edits(split_id: int) -> dict:
    """The saved edit list, whether it applies, and the plan it gives."""
    record = _planning(split_id)
    return {"state": record["state"], "count": len(record["edits"]), "edits": record["edits"], "reason": record["reason"],
            "failed": record["failed"], "plan": record["plan"]}


def _edit_base(split_id: int) -> dict:
    _require(split_id, (SPLIT, QUEUED), "edit the plan")
    record = _planning(split_id)
    if not record["auto"].ok:
        raise jobs.JobError("The plan has to work as cut before it can be edited.", 409)
    if record["state"] == "outdated":
        raise jobs.JobError("Your edits no longer match this split. Discard them or try to keep them first.", 409)
    return record


def edit_plan(split_id: int, body: dict | None = None) -> dict:
    """Apply one edit ({"op": "move"|"reorder"|"reset"}) and save the edit list with the split, or refuse it with the reason."""
    body = body or {}
    work = work_dir(split_id)
    if body.get("op") == "reset":
        get_split(split_id)
        edits.reset(work)
        _plans.pop(split_id, None)
        return assembly_edits(split_id)
    if body.get("op") in ("discard", "keep"):
        return _settle_outdated(split_id, body["op"])
    record = _edit_base(split_id)
    edit = body
    if body.get("op") == "reorder" and "order" not in body:
        edit = edits.reorder_for_step(record["model"], record["auto"].plan, record["edits"], body.get("parts") or [], str(body.get("direction")))
    _plan, stored = edits.apply_edit(record["model"], record["auto"].plan, record["edits"], edit)
    edits.save(work, record["model"].fingerprint, record["edits"] + [stored])
    _plans.pop(split_id, None)
    return assembly_edits(split_id)


def _settle_outdated(split_id: int, op: str) -> dict:
    """Discard (delete the override) or Try to keep (replay each edit, keep those that still pass, stamp the split's
    fingerprint) for edits marked outdated. `dropped` lists what Try to keep could not keep."""
    _require(split_id, (SPLIT, QUEUED), "settle the edits")
    record = _planning(split_id)
    if record["state"] != "outdated":
        raise jobs.JobError("There are no outdated edits to settle.", 409)
    work = work_dir(split_id)
    dropped = []
    if op == "discard":
        edits.reset(work)
    else:
        if not record["auto"].ok:
            raise jobs.JobError("The plan has to work as cut before edits can be kept.", 409)
        done = edits.replay(record["model"], record["auto"].plan, record["edits"], keep_going=True)
        dropped = [{"index": i, "edit": edit, "parts": list(refused.parts), "kind": refused.kind, "reason": str(refused)}
                   for i, edit, refused in done.dropped]
        if done.kept:
            edits.save(work, record["model"].fingerprint, done.kept)
        else:
            edits.reset(work)
    _plans.pop(split_id, None)
    return {**assembly_edits(split_id), "dropped": dropped}


def edit_receivers(split_id: int, parts) -> dict:
    """Where a part or finished Section can be moved to; only receivers the step validator accepts are listed."""
    record = _edit_base(split_id)
    try:
        parts = sorted({int(p) for p in parts if str(p).strip()})
    except ValueError:
        raise jobs.JobError("Part numbers must be numbers") from None
    found = edits.valid_receivers(record["model"], record["auto"].plan, record["edits"], parts)
    return {"parts": parts, "receivers": found}


# ── The Assembly guide ─────────────────────────────────────────────────────────
#
# A guide is a background job tracked apart from the split's own state: guide.json holds a small status record
# (generating, ready or failed, with the stage text), guide.pdf the finished PDF, guide_meta.json when it was made, for
# which plan code and paper size, and guide_plan.json the plan it was made from. A new try replaces them only once it
# has succeeded; a failed try keeps the PDF from before.

_guide_progress: dict = {}   # split id → {"stage": text, "percent": n} while a guide is being made


def _guide_file(split_id: int, name: str) -> Path:
    return work_dir(split_id) / name


def _write_json(path: Path, record: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _guide_blocker(split_id: int) -> str:
    """Why a guide cannot be made right now ("" when it can)."""
    missing = missing_guide_modules()
    if missing:
        return f"The PDF library is not installed. Install {', '.join(missing)} (pip install -r requirements.txt)."
    if get_split(split_id)["state"] not in (SPLIT, QUEUED):
        return "Split the model first."
    try:
        plan = assembly_plan(split_id)
    except jobs.JobError as e:
        return str(e)
    if not plan["ok"]:
        return f"{plan['headline']}: {plan['failure']['message']}"
    if not planner.read_preview(work_dir(split_id) / "parts_preview.bin"):
        return "The part previews are missing, so the guide cannot be drawn. Split the model again."
    return ""


def guide_status(split_id: int) -> dict:
    """The guide job's state, the PDF on disk (date, plan code, paper, out of date) and why Generate is off, if it is."""
    get_split(split_id)
    work = work_dir(split_id)
    record = _read(work, "guide.json") or {}
    meta = _read(work, "guide_meta.json")
    has_pdf = bool(meta) and (work / "guide.pdf").is_file()
    state_name = record.get("state") or ("ready" if has_pdf else "none")
    status = {"state": state_name, "stage": None, "percent": None, "error": record.get("error"),
              "has_pdf": has_pdf, "paper": None, "generated_at": None, "plan_code": None,
              "out_of_date": False, "blocked": _guide_blocker(split_id)}
    if state_name == "generating":
        progress = _guide_progress.get(split_id) or {}
        status["stage"] = progress.get("stage") or record.get("stage") or "Waiting to start"
        status["percent"] = progress.get("percent", 0)
        status["paper"] = record.get("paper")
    if has_pdf:
        status.update(paper=meta["paper"], generated_at=meta["generated_at"], plan_code=meta["plan_code"])
        try:
            current = assembly_plan(split_id)
            status["out_of_date"] = not current["ok"] or current["code"] != meta["plan_code"]
        except jobs.JobError:
            status["out_of_date"] = True
    return status


def start_guide(split_id: int, body: dict | None = None) -> dict:
    """Queue the Assembly guide for a finished split. Never changes the split's own state."""
    body = body or {}
    _require(split_id, (SPLIT, QUEUED), "make the Assembly guide")
    paper = str(body.get("paper") or default_paper()).lower()
    if paper not in PAPER_SIZES:
        raise jobs.JobError("Paper must be A4 or Letter")
    blocked = _guide_blocker(split_id)
    if blocked:
        raise jobs.JobError(blocked, 409)
    if (_read(work_dir(split_id), "guide.json") or {}).get("state") == "generating":
        raise jobs.JobError("The guide is already being made", 409)
    _write_json(_guide_file(split_id, "guide.json"), {"state": "generating", "paper": paper, "stage": "Waiting to start",
                                                    "started_at": time.time()})
    _guide_progress[split_id] = {"stage": "Waiting to start", "percent": 0}
    notify_worker()
    return guide_status(split_id)


def guide_path(split_id: int) -> tuple:
    """The stored PDF and its download name: "<model name> - assembly guide.pdf"."""
    record = get_split(split_id)
    path = _guide_file(split_id, "guide.pdf")
    if not path.is_file():
        raise jobs.JobError("The guide hasn't been made yet", 404)
    name = re.sub(r'[^ -~]|["\\/:*?<>|]', "_", Path(record["source_name"]).stem).strip() or "model"
    return path, f"{name} - assembly guide.pdf"


def _part_plates(split_id: int) -> dict:
    """Part number -> plate name for the parts of this split already laid out on plates (the guide's inventory shows a dash for the rest)."""
    import projects   # projects imports this module
    try:
        return projects.part_plates(split_id)
    except Exception:   # a plate lookup is never worth failing the guide for
        return {}


def _make_guide(split_id: int) -> None:
    """Build the guide PDF for a split whose guide.json says generating. Blocking."""
    import assembly_bom
    import assembly_pdf   # imports reportlab, so only when a guide is really made
    import assembly_render

    def progress(stage: str, percent: int) -> None:
        _guide_progress[split_id] = {"stage": stage, "percent": percent}

    work = work_dir(split_id)
    record = _read(work, "guide.json") or {}
    paper = record.get("paper") if record.get("paper") in PAPER_SIZES else default_paper()
    progress("Planning the build", 2)
    plan = assembly_plan(split_id)
    if not plan["ok"]:
        raise jobs.JobError(plan["headline"])
    progress("Preparing the parts", 4)
    result = _read(work, "result.json") or {}
    scene = assembly_render.Scene.from_split(result, planner.read_preview(work / "parts_preview.bin"))
    bom = assembly_bom.build_bom(result.get("pins") or [], plan)   # refuses when counts disagree
    data = assembly_pdf.GuideData(model_name=Path(get_split(split_id)["source_name"]).stem, split_id=split_id,
                                  plan=plan, paper=paper, bom=bom, plan_line=edits.plan_line(plan.get("edits")), scene=scene,
                                  labels={p["number"]: p.get("label") for p in result.get("parts") or []},
                                  plates=_part_plates(split_id))
    tmp = work / "guide.pdf.tmp"
    pages = assembly_pdf.render_guide(tmp, data, progress)
    os.replace(tmp, work / "guide.pdf")
    _write_json(work / "guide_plan.json", plan)
    _write_json(work / "guide_meta.json", {"plan_code": plan["code"], "paper": paper, "generated_at": time.time(),
                                           "pages": pages, "bytes": (work / "guide.pdf").stat().st_size})


def _next_guide_job() -> int | None:
    root = splits_dir()
    if not root.is_dir():
        return None
    for folder in sorted((p for p in root.iterdir() if p.name.isdigit()), key=lambda p: int(p.name)):
        if (_read(folder, "guide.json") or {}).get("state") == "generating":
            return int(folder.name)
    return None


async def process_next_guide() -> bool:
    """Make one waiting Assembly guide. Returns False when none is waiting. A guide interrupted by a restart runs again."""
    split_id = _next_guide_job()
    if split_id is None:
        return False
    _guide_progress[split_id] = {"stage": "Starting", "percent": 1}
    await broadcast()
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _make_guide, split_id)
    except Exception as e:
        message = str(e) or type(e).__name__
        if work_dir(split_id).is_dir():
            _write_json(_guide_file(split_id, "guide.json"), {"state": "failed", "error": message, "failed_at": time.time()})
        print(f"[Split] #{split_id} guide failed: {message}")
    else:
        _write_json(_guide_file(split_id, "guide.json"), {"state": "ready"})
    _guide_progress.pop(split_id, None)
    await broadcast()
    return True


# ── Sending parts to the queue ─────────────────────────────────────────────────

def queue_parts(split_id: int, body: dict) -> dict:
    """Create one slice per part: parts that need supports use the support profile."""
    record = _require(split_id, (SPLIT, QUEUED), "send parts to the queue")
    result = _read(work_dir(split_id), "result.json")
    if not result:
        raise jobs.JobError("Split the model first", 409)
    bundle = slicer.load_bundle(str(body.get("bundle_id") or ""))
    support_bundle = slicer.load_bundle(str(body.get("support_bundle_id") or bundle["id"]))
    bed = body.get("bed") or "textured"
    if bed not in slicer.BED_TYPES:
        raise jobs.JobError(f"bed must be one of: {', '.join(slicer.BED_TYPES)}")
    printer_id = body.get("printer_id") or None
    if printer_id:
        jobs._require_printer(printer_id)
    wanted = body.get("parts")
    parts = result["parts"] if not wanted else [p for p in result["parts"] if p["number"] in {int(n) for n in wanted}]
    if not parts:
        raise jobs.JobError("Choose at least one part")
    options = {"leveling": bool(body.get("leveling")), "timelapse": bool(body.get("timelapse"))}
    stem, total = Path(record["source_name"]).stem, len(result["parts"])
    created = []
    for part in parts:
        path = work_dir(split_id) / "parts" / part["file"]
        joins = ", ".join(str(n["part"]) for n in part["neighbours"]) or "none"
        note = (f"{record['source_name']} — part {part['number']} of {total}. Joins part{'s' if len(part['neighbours']) != 1 else ''} "
                f"{joins}; {part['holes']} rod hole{'s' if part['holes'] != 1 else ''}"
                + ("; printed with supports" if part["needs_supports"] and support_bundle["id"] != bundle["id"] else "") + ".")
        with path.open("rb") as fh:
            sliced = slicer.create_slice(fh, path.stat().st_size, f"{stem}_part{part['number']:02d}.stl",
                                         support_bundle["id"] if part["needs_supports"] else bundle["id"],
                                         bed=bed, printer_id=printer_id, print_opts=options, notes=note)
        with database.transaction() as conn:
            conn.execute("UPDATE slices SET split_id = ?, split_part = ? WHERE id = ?", (split_id, part["number"], sliced["id"]))
        created.append(sliced["id"])
    _set_state(split_id, QUEUED)
    return {**get_split(split_id), "slice_ids": created}


# ── Background worker ──────────────────────────────────────────────────────────

def _run_stage(split_id: int, stage: str) -> None:
    """Run one engine stage to completion, keeping its progress for the browser. Blocking."""
    work = work_dir(split_id)
    (work / "progress.json").unlink(missing_ok=True)
    with (work / "engine.log").open("a", encoding="utf-8") as log:
        log.write(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} {stage}\n")
        log.flush()
        proc = subprocess.Popen(engine_command(stage, work), stdout=log, stderr=subprocess.STDOUT,
                                cwd=str(APP_DIR), creationflags=NO_WINDOW)
        with _process_lock:
            _processes[split_id] = proc
        deadline = time.monotonic() + STAGE_TIMEOUT_S
        last_broadcast = 0.0
        try:
            while True:
                try:
                    code = proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                progress = _read(work, "progress.json")
                if progress and progress != _progress.get(split_id):
                    _progress[split_id] = progress
                    if time.monotonic() - last_broadcast > 1:
                        broadcast_threadsafe()
                        last_broadcast = time.monotonic()
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise jobs.JobError(f"The {stage} step took longer than {STAGE_TIMEOUT_S // 60} minutes")
        finally:
            with _process_lock:
                _processes.pop(split_id, None)
    if code != 0:
        error = _read(work, "error.json") or {}
        raise jobs.JobError(error.get("error") or f"The {stage} step stopped unexpectedly (exit code {code})")


def _still_there(split_id: int, created_at: float) -> bool:
    """Is this still the split a stage was started for? SQLite hands a deleted split's id to the next new split."""
    with database.transaction() as conn:
        return conn.execute("SELECT 1 FROM splits WHERE id = ? AND created_at = ?", (split_id, created_at)).fetchone() is not None


async def process_next() -> bool:
    with database.transaction() as conn:
        row = conn.execute(f"SELECT id, state, created_at FROM splits WHERE state IN ({', '.join('?' * len(STAGE_FOR))}) "
                           "ORDER BY updated_at, id LIMIT 1", tuple(STAGE_FOR)).fetchone()
    if row is None:
        return await process_next_guide()
    split_id, current, created_at = row["id"], row["state"], row["created_at"]
    stage = STAGE_FOR[current]
    _progress[split_id] = {"stage": stage, "percent": 0, "message": "Starting…"}
    await broadcast()
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _run_stage, split_id, stage)
    except Exception as e:
        if _still_there(split_id, created_at):
            _set_state(split_id, FAILED, stage=stage, error=str(e) or type(e).__name__)
            print(f"[Split] #{split_id} {stage} failed: {e}")
    else:
        if _still_there(split_id, created_at):
            fields = {"stage": None, "error": None}
            if stage == "split":
                result = _read(work_dir(split_id), "result.json") or {}
                fields["parts"] = len(result.get("parts") or [])
            _set_state(split_id, AFTER[current], **fields)
    _progress.pop(split_id, None)
    await broadcast()
    return True


async def worker_loop() -> None:
    """Background worker: run engine stages one at a time."""
    global _wakeup
    _wakeup = asyncio.Event()
    while True:
        _wakeup.clear()
        try:
            if await process_next():
                continue
        except Exception as e:  # the worker must survive anything a single stage throws
            print(f"[Split] Worker error: {e}")
        try:
            await asyncio.wait_for(_wakeup.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


def notify_worker() -> None:
    loop = state.loop
    if _wakeup is not None and loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_wakeup.set)


async def broadcast() -> None:
    if state.browser_clients:
        await state.broadcast_to_browsers({"type": "splits", "splits": list_splits()})


def broadcast_threadsafe() -> None:
    loop = state.loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast(), loop)
