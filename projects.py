"""
Slicer workspace projects: models placed on plates, profile settings you can tweak, per-object setting
overrides, slicing a plate with OrcaSlicer, and the sliced result for the layer-by-layer preview.

Files (DATA_DIR/projects/<id>/)
  models/<model id>/  source/<file>, job.json, model.npz, preview.bin, analysis.json — made by `split_engine.py analyze`
  plates/<plate id>/  plate.3mf, profile/*.json, plate.gcode, analysis.json, toolpath.bin, slicer.log,
                      plate.orca.gcode (OrcaSlicer's own G-code, when BrickLayers post-processed plate.gcode)

Record
  split_id  the split model the project was started from, if any; its parts (or the whole model) are laid out on plates
  data    {"bed": "textured" | "smooth",
           "profile": {"bundle_id", "process": {…changes}, "filament": {…changes}, "bricklayers": {…} | null},
           "plates": [{"id", "name", "overrides": {…process changes for this plate}, "bricklayers": {…} | null}],
           "objects": [{"id", "model", "name", "plate", "position": [x, y], "rotation": [x, y, z], "scale": [x, y, z],
                        "overrides": {…process changes for this object},
                        "pinned": true — only when set: the planner leaves this object where it is}],
           "filaments": {"<model id>": {"preset", "colour"}} — only models with a filament choice; the rest use the project's,
           "arrange_engine": "orca" — only when chosen: Arrange uses OrcaSlicer's own arranger instead of the nester}
  models  [{"id", "name", "file", "state": importing | ready | failed, "error", "size", "triangles", "volume_cm3", "sha256",
            "split": {"id", "part" (0 = the whole model), "supports"} — models that came from a split}]
  slices  {"<plate id>": {"state": queued | slicing | sliced | failed, "error", "fingerprint", "estimate_s", "estimate_g", …}}

Settings stack: the locked profile, then the project's changes, the plate's, and the object's.
Placing an object: the model is centred on its bounding box, scaled, rotated about X, then Y, then Z (degrees),
lowered onto the bed, and moved so the centre of its footprint is at `position` (bed millimetres).
BrickLayers (brick_post.py) optionally post-processes a plate's G-code; the plate's setting overrides the project's.
A slice is outdated as soon as anything that affects the plate's G-code changes (its fingerprint).
Sending plates to the queue makes one batch of jobs that remember their project and plate.
"""

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import brick_post
import database
import gcode_analysis
import holds
import jobs  # used as a module: jobs.JobError, jobs.create_upload_job, …
import orca_3mf
import preflight
import packing
import remembered_defaults
import slicer
import splits
import spoolman
import state
import thumbnails
import time_estimate
from gcode_meta import safe_gcode_name
from persistence import DATA_DIR

MAX_PLATES, MAX_OBJECTS = 50, 500
MAX_FILAMENT_NAME, MAX_FILAMENT_COLOUR = 80, 40
# Plates sliced at the same time. Each OrcaSlicer process can use a few GB for a big plate, so the default is modest.
SLICE_WORKERS = max(1, int(os.getenv("SLICE_WORKERS", str(max(1, min(2, (os.cpu_count() or 2) // 4))))))
IMPORTING, READY, FAILED = "importing", "ready", "failed"
QUEUED, SLICING, SLICED = "queued", "slicing", "sliced"
DEFAULT_BED = (0.0, 0.0, 256.0, 256.0)
DEFAULT_HEIGHT = 256.0
DEFAULT_EXCLUDE = (246.0, 0.0, 256.0, 20.0)   # CC1 front-right nozzle purge area (x0, y0, x1, y1)
SUPPORT_OVERRIDES = {"enable_support": "1", "support_type": "tree(auto)"}  # plates for split parts that need supports
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')

_data_dir: Path = DATA_DIR
_wakeup: asyncio.Event | None = None


def engine_command(stage: str, work: Path) -> list:
    """The command that runs a split_engine stage (patched in tests)."""
    return [sys.executable, str(splits.ENGINE), stage, str(work)]


# ── Setup and paths ────────────────────────────────────────────────────────────

def configure(data_dir: Path | None = None) -> None:
    global _data_dir
    _data_dir = Path(data_dir or DATA_DIR)


def projects_dir() -> Path:
    return _data_dir / "projects"


def project_dir(project_id: int) -> Path:
    return projects_dir() / str(int(project_id))


def model_dir(project_id: int, model_id: int) -> Path:
    return project_dir(project_id) / "models" / str(int(model_id))


def plate_dir(project_id: int, plate_id: int) -> Path:
    return project_dir(project_id) / "plates" / str(int(plate_id))


def init() -> None:
    """Migrate, and queue again any plate that was slicing when Spooler stopped."""
    database.migrate()
    with database.transaction() as conn:
        for row in conn.execute("SELECT id, slices FROM projects").fetchall():
            entries = json.loads(row["slices"] or "{}")
            interrupted = [e for e in entries.values() if e.get("state") == SLICING]
            for entry in interrupted:
                entry["state"] = QUEUED
            if interrupted:
                conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(entries), row["id"]))
        for row in conn.execute("SELECT id, models FROM projects").fetchall():
            models = json.loads(row["models"] or "[]")
            claimed = [m for m in models if m.get("claimed_at")]  # an import a worker had taken when Spooler stopped
            for model in claimed:
                del model["claimed_at"]
            if claimed:
                conn.execute("UPDATE projects SET models = ? WHERE id = ?", (json.dumps(models), row["id"]))


# ── Records ────────────────────────────────────────────────────────────────────

def fingerprint(record: dict, plate_id: int) -> str:
    """Everything that affects a plate's G-code."""
    data = record["data"]
    models = {m["id"]: m for m in record["models"]}
    objects = sorted((o for o in data.get("objects", []) if o["plate"] == plate_id), key=lambda o: o["id"])
    payload = {
        "bed": data.get("bed"),
        "profile": {k: v for k, v in data["profile"].items() if k not in ("bricklayers", "spool_id")} if data.get("profile") else data.get("profile"),
        "objects": [{"model": models.get(o["model"], {}).get("sha256"), "scale_up": models.get(o["model"], {}).get("import"),
                     **{k: o[k] for k in ("position", "rotation", "scale", "overrides")}} for o in objects],
    }
    plate = next((p for p in data.get("plates", []) if p["id"] == plate_id), {})
    if plate.get("overrides"):  # only when set, so plates sliced before plates had settings stay up to date
        payload["plate"] = plate["overrides"]
    bricks = plate_bricklayers(data, plate)
    if bricks:  # likewise only when on, so plates sliced before BrickLayers existed stay up to date
        payload["bricklayers"] = bricks
    chosen = plate_filaments(data, plate_id)
    if any(chosen):  # only when an object's model has a choice, so plates on the project default stay up to date
        payload["filament"] = [{"preset": c["preset"], "colour": c.get("colour", "")} if c else None for c in chosen]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _choice_key(choice: dict | None) -> tuple | None:
    return (choice["preset"], choice.get("colour", "")) if choice else None


def choice_label(choice: dict | None) -> str:
    """How a filament choice reads on the page: "PETG grey" (the preset without its @printer suffix, then the colour)."""
    if not choice:
        return "the project default"
    return f"{choice['preset'].split(' @')[0]} {choice.get('colour', '')}".strip()


def plate_filaments(data: dict, plate_id: int) -> list:
    """The distinct filament choices of a plate's objects, in object order; None stands for the project default.
    A plate's filament is derived here every time and never stored on the plate."""
    choices = data.get("filaments") or {}
    found = {}
    for obj in sorted((o for o in data.get("objects", []) if o["plate"] == plate_id), key=lambda o: o["id"]):
        choice = choices.get(str(obj["model"]))
        found.setdefault(_choice_key(choice), choice)
    return list(found.values())


def plate_mix(data: dict, plate_id: int) -> str | None:
    """"Plate 3 mixes PETG grey and PLA white" when the plate holds more than one filament choice, else None."""
    found = plate_filaments(data, plate_id)
    if len(found) < 2:
        return None
    plate = next((p for p in data.get("plates", []) if p["id"] == plate_id), {})
    labels = [choice_label(c) for c in found]
    return f"{plate.get('name') or f'Plate {plate_id}'} mixes {', '.join(labels[:-1])} and {labels[-1]}"


def plate_bricklayers(data: dict, plate: dict) -> dict | None:
    """The BrickLayers setting a plate slices with, or None when it's off."""
    return brick_post.effective((data.get("profile") or {}).get("bricklayers"), plate.get("bricklayers"))


def _to_project(row) -> dict:
    record = dict(row)
    record["data"] = json.loads(record["data"] or "{}")
    record["models"] = json.loads(record["models"] or "[]")
    record["slices"] = json.loads(record["slices"] or "{}")
    record["quick"] = bool(record.get("quick"))
    record["server_time"] = time.time()  # lets a browser with a different clock show how long a slice has been running
    for plate in record["data"].get("plates", []):
        entry = record["slices"].get(str(plate["id"]))
        if entry and entry.get("state") == SLICED:
            entry["outdated"] = not entry.get("imported") and entry.get("fingerprint") != fingerprint(record, plate["id"])
    return record


def get_project(project_id: int) -> dict:
    with database.transaction() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise jobs.JobError("Project not found", 404)
    return _to_project(row)


def list_projects(include_quick: bool = False) -> list:
    """Projects, newest first. Quick projects (hidden behind Quick print) are left out unless `include_quick`."""
    with database.transaction() as conn:
        rows = conn.execute("SELECT * FROM projects" + ("" if include_quick else " WHERE quick = 0")
                            + " ORDER BY updated_at DESC, id DESC").fetchall()
    out = []
    for record in map(_to_project, rows):
        data = record["data"]
        out.append({"id": record["id"], "name": record["name"], "updated_at": record["updated_at"], "created_at": record["created_at"],
                    "split_id": record.get("split_id"), "quick": record["quick"],
                    "plates": len(data.get("plates", [])), "objects": len(data.get("objects", [])), "models": len(record["models"]),
                    "busy": any(m["state"] == IMPORTING for m in record["models"])
                    or any(s.get("state") in (QUEUED, SLICING) for s in record["slices"].values())})
    return out


def _clean_name(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:120]


def _default_bundle_id() -> str | None:
    bundles = slicer.list_bundles() if slicer.orca_executable() else []
    plain = [b for b in bundles if not b.get("overrides")] or bundles
    return plain[0]["id"] if plain else None


def _new_plate(plate_id: int, overrides: dict | None = None) -> dict:
    return {"id": plate_id, "name": f"Plate {plate_id}", "overrides": dict(overrides or {})}


def create_project(name: str = "", split_id: int | None = None, quick: bool = False) -> dict:
    """A project with one empty plate. With `split_id` it belongs to that split model and lays out its parts.
    A `quick` project is hidden from project listings; it is reached by id."""
    if split_id is not None:
        splits.get_split(split_id)
    remembered = remembered_defaults.usable()
    plate = _new_plate(1)
    if remembered["printer"]:
        plate["printer_id"] = remembered["printer"]
    data = {"bed": remembered["plate_side"] or "textured",
            "profile": {"bundle_id": remembered["profile"] or _default_bundle_id(), "process": {}, "filament": {},
                        "spool_id": remembered["spool"]},
            "plates": [plate], "objects": []}
    now = time.time()
    with database.transaction() as conn:
        project_id = conn.execute("INSERT INTO projects (name, data, split_id, quick, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                                  (_clean_name(name) or "Untitled project", json.dumps(data), split_id, int(bool(quick)), now, now)).lastrowid
    project_dir(project_id).mkdir(parents=True, exist_ok=True)
    return get_project(project_id)


def create_project_from_gcode(stream, length: int, filename: str, bed: str = "textured", quick: bool = False) -> dict:
    """A one-plate project with no objects whose plate holds the uploaded G-code as an imported (never outdated) Slice.
    `bed` is the plate side the file was sliced for; the file can't say."""
    if bed not in slicer.BED_TYPES:
        raise jobs.JobError("bed must be 'textured' or 'smooth'")
    try:
        name = safe_gcode_name(filename)
    except ValueError as e:
        raise jobs.JobError(str(e)) from e
    if length > jobs.MAX_UPLOAD_BYTES:
        raise jobs.JobError(f"The file is larger than {jobs.MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    path = _spool(stream, length)
    try:
        project = create_project(name.rsplit(".", 1)[0][:120], quick=quick)
        try:
            work = plate_dir(project["id"], 1)
            work.mkdir(parents=True)
            os.replace(path, work / "plate.gcode")
            analysis, toolpath = gcode_analysis.analyze(work / "plate.gcode")
            (work / "toolpath.bin").write_bytes(toolpath)
            (work / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
            estimates = analysis.get("estimates") or {}
            entry = {"state": SLICED, "error": None, "imported": True, "sliced_at": time.time(),
                     "estimate_s": estimates.get("estimate_s"), "estimate_g": estimates.get("estimate_g"),
                     "layers": len(analysis["layers"]), "slicer": analysis.get("slicer"),
                     "gcode_bytes": (work / "plate.gcode").stat().st_size,
                     "findings": [f for f in analysis.get("findings", []) if f["level"] == "fail"][:5]}
            _mutate(project["id"], "slices", lambda s: s.__setitem__("1", entry))
            _mutate(project["id"], "data", lambda d: d.__setitem__("bed", bed))
        except BaseException:
            delete_project(project["id"])
            raise
        return get_project(project["id"])
    finally:
        path.unlink(missing_ok=True)


def create_project_from_upload(stream, length: int, filename: str, scale=1.0, up="z", bed: str = "textured", quick: bool = False) -> dict:
    """Start the workflow from a model file: it's stored as a split model (analysed first) owned by a new project.
    A G-code file instead becomes a one-plate project holding it as an imported Slice."""
    if filename.lower().endswith(".gcode"):
        return create_project_from_gcode(stream, length, filename, bed, quick=quick)
    if filename.lower().endswith(".3mf"):
        path = _spool(stream, length)
        try:
            return _create_project_from_3mf(path, length, filename, scale, up, quick)
        finally:
            path.unlink(missing_ok=True)
    split = splits.create_split(stream, length, filename, {"scale": scale, "up": up})
    return create_project(Path(split["source_name"]).stem, split_id=split["id"], quick=quick)


def _create_project_from_3mf(path: Path, length: int, filename: str, scale, up, quick: bool) -> dict:
    """Objects and plates are counted before anything is created. One object on one plate is a model like any other
    (an Orca project's settings become the project's starting values); anything bigger becomes a normal, visible Project
    with its objects and plates, even when a quick print was asked for (the result then says `routed`)."""
    try:
        found = orca_3mf.summary(path)
    except orca_3mf.ThreeMFError as e:
        raise jobs.JobError(str(e)) from e
    stem = Path(filename.replace("\\", "/")).stem[:120]
    if found["objects"] > 1 or found["plates"] > 1:
        project = create_project(stem)
        try:
            imported = import_orca_project(project["id"], path, fresh=True)
        except BaseException:
            delete_project(project["id"])
            raise
        if quick:
            counts = [f"{found['objects']} objects" if found["objects"] > 1 else None,
                      f"{found['plates']} plates" if found["plates"] > 1 else None]
            imported["routed"] = {"to": "project", "reason": f"The file has {' on '.join(c for c in counts if c)}, so it opens as a project"}
        return imported
    return _create_project_from_single_object_3mf(path, stem, scale, up, quick)


def _create_project_from_single_object_3mf(path: Path, stem: str, scale, up, quick: bool) -> dict:
    """A one-object 3MF (plain or Orca/Bambu), read with our own parser, normalised to the shape of a plain model upload (a split-owned whole model, so the
    quick print intake works on it); the file's own settings and plate side become the project's starting values."""
    _require_geometry()
    try:
        parsed = orca_3mf.parse(path)
    except orca_3mf.ThreeMFError as e:
        raise jobs.JobError(str(e)) from e
    (obj,) = parsed["objects"]
    notes = list(parsed["warnings"])
    profile, bed = _orca_profile(parsed["project"], notes)
    base = slicer.bundle_settings(profile["bundle_id"]) if profile.get("bundle_id") else None
    profile["process"] = {**profile["process"], **_clean_object_settings(obj["settings"], base, obj["name"], notes)}
    instance = next((i for i in obj["instances"] if i["printable"]), obj["instances"][0])   # a disabled copy is not the one to print
    verts = obj["verts"] @ instance["matrix"]      # the object as the file poses it (its own scale and turn)
    staging = Path(tempfile.mkdtemp(prefix=".orca-", dir=path.parent))
    try:
        stl = staging / "model.stl"
        write_stl(stl, verts, obj["tris"])
        with stl.open("rb") as fh:
            split = splits.create_split(fh, stl.stat().st_size, f"{stem}.stl", {"scale": scale, "up": up})
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    project = create_project(stem, split_id=split["id"], quick=quick)
    try:
        _mutate(project["id"], "data", lambda d: d.update(bed=bed, profile={"spool_id": None, **profile}))
    except BaseException:
        delete_project(project["id"])
        raise
    return {**get_project(project["id"]), "import_notes": list(dict.fromkeys(notes))}


def delete_project(project_id: int) -> None:
    """Delete the project and its files, and its split model unless another project uses that split too."""
    record = get_project(project_id)
    split_id = record.get("split_id")
    with database.transaction() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        shared = split_id is not None and conn.execute("SELECT 1 FROM projects WHERE split_id = ?", (split_id,)).fetchone()
    shutil.rmtree(project_dir(project_id), ignore_errors=True)
    if split_id is not None and not shared:
        try:
            splits.delete_split(split_id)
        except jobs.JobError:
            pass  # already deleted


def _numbers(value, count: int, name: str, low: float, high: float) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        raise jobs.JobError(f"{name} needs {count} numbers")
    out = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            raise jobs.JobError(f"{name} needs {count} numbers")
        if not math.isfinite(number) or not low <= number <= high:
            raise jobs.JobError(f"{name} must be between {low:g} and {high:g}")
        out.append(round(number, 4))
    return out


def _spool_id(value) -> int | None:
    """The Spoolman spool chosen as this project's filament (a reference for display and the amount check)."""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise jobs.JobError("spool_id must be a spool number")
    if number < 1:
        raise jobs.JobError("spool_id must be a spool number")
    return number


def _pinned(value) -> bool:
    if not isinstance(value, bool):
        raise jobs.JobError("pinned must be true or false")
    return value


def _filament_choice(value, model_id) -> dict:
    """A model's filament choice: a material preset plus an optional colour label."""
    if not isinstance(value, dict):
        raise jobs.JobError(f"Model {model_id}'s filament choice needs a preset and an optional colour")
    preset, colour = value.get("preset"), value.get("colour") or ""
    if not isinstance(preset, str) or not preset.strip() or len(preset.strip()) > MAX_FILAMENT_NAME:
        raise jobs.JobError(f"Model {model_id}'s filament choice needs a preset name of up to {MAX_FILAMENT_NAME} characters")
    if not isinstance(colour, str) or len(colour.strip()) > MAX_FILAMENT_COLOUR:
        raise jobs.JobError(f"Model {model_id}'s filament colour is a label of up to {MAX_FILAMENT_COLOUR} characters")
    return {"preset": re.sub(r"\s+", " ", preset).strip(), "colour": re.sub(r"\s+", " ", colour).strip()}


def _filament_choices(value, model_ids: set) -> dict:
    """{"<model id>": choice}; a model without an entry (or with null) prints in the project's default filament."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise jobs.JobError("Filament choices are an object keyed by model id")
    choices = {}
    for key, choice in value.items():
        try:
            model_id = int(key)
        except (TypeError, ValueError):
            raise jobs.JobError("Filament choices are keyed by model id")
        if model_id not in model_ids:
            raise jobs.JobError(f"Filament choice for model {key}, which isn't in the project")
        if choice is not None:
            choices[str(model_id)] = _filament_choice(choice, model_id)
    return choices


def validate_data(raw, record: dict) -> dict:
    if not isinstance(raw, dict):
        raise jobs.JobError("Project data must be an object")
    bed = raw.get("bed", "textured")
    if bed not in slicer.BED_TYPES:
        raise jobs.JobError(f"bed must be one of: {', '.join(slicer.BED_TYPES)}")

    profile_raw = raw.get("profile") or {}
    bundle_id = profile_raw.get("bundle_id") or None
    base = slicer.bundle_settings(bundle_id) if bundle_id else None
    changes = {"process": profile_raw.get("process") or {}, "filament": profile_raw.get("filament") or {}}
    if base is None and (changes["process"] or changes["filament"]):
        raise jobs.JobError("Choose a profile before changing settings")
    normalized = slicer.normalize_overrides(changes, base) if base else {}
    profile = {"bundle_id": bundle_id, "process": normalized.get("process", {}), "filament": normalized.get("filament", {}),
               "bricklayers": brick_post.validate(profile_raw.get("bricklayers")), "spool_id": _spool_id(profile_raw.get("spool_id"))}

    plates_raw = raw.get("plates")
    if not isinstance(plates_raw, list) or not 1 <= len(plates_raw) <= MAX_PLATES:
        raise jobs.JobError(f"A project has between 1 and {MAX_PLATES} plates")
    plates, plate_ids = [], set()
    for plate in plates_raw:
        try:
            plate_id = int(plate["id"])
        except (TypeError, KeyError, ValueError):
            raise jobs.JobError("Every plate needs an id")
        if plate_id < 1 or plate_id in plate_ids:
            raise jobs.JobError("Plate ids must be unique positive numbers")
        plate_ids.add(plate_id)
        overrides = plate.get("overrides") or {}
        if overrides and base is None:
            raise jobs.JobError("Choose a profile before changing a plate's settings")
        plates.append({"id": plate_id, "name": _clean_name(plate.get("name")) or f"Plate {plate_id}",
                       "overrides": slicer.normalize_overrides({"process": overrides}, base).get("process", {}) if overrides else {},
                       "bricklayers": brick_post.validate(plate.get("bricklayers")),
                       "printer_id": plate.get("printer_id") if plate.get("printer_id") in state.printers else None})

    objects_raw = raw.get("objects") or []
    if not isinstance(objects_raw, list) or len(objects_raw) > MAX_OBJECTS:
        raise jobs.JobError(f"A project holds at most {MAX_OBJECTS} objects")
    model_ids = {m["id"] for m in record["models"]}
    objects, object_ids = [], set()
    for obj in objects_raw:
        try:
            object_id, model_id, plate_id = int(obj["id"]), int(obj["model"]), int(obj["plate"])
        except (TypeError, KeyError, ValueError):
            raise jobs.JobError("Every object needs an id, a model and a plate")
        if object_id < 1 or object_id in object_ids:
            raise jobs.JobError("Object ids must be unique positive numbers")
        if model_id not in model_ids:
            raise jobs.JobError(f"Object {object_id} refers to a model that isn't in the project")
        if plate_id not in plate_ids:
            raise jobs.JobError(f"Object {object_id} is on a plate that doesn't exist")
        object_ids.add(object_id)
        overrides = obj.get("overrides") or {}
        if overrides and base is None:
            raise jobs.JobError("Choose a profile before changing an object's settings")
        rotation = _numbers(obj.get("rotation", [0, 0, 0]), 3, "Rotation", -100000, 100000)
        pinned = _pinned(obj["pinned"]) if "pinned" in obj else False
        objects.append({
            "id": object_id, "model": model_id, "plate": plate_id,
            **({"pinned": True} if pinned else {}),   # only when set, so projects saved before pinning stay as they were
            "name": _clean_name(obj.get("name")) or f"Object {object_id}",
            "position": _numbers(obj.get("position", [128, 128]), 2, "Position", -1000, 3000),
            "rotation": [round(r % 360, 4) for r in rotation],
            "scale": _numbers(obj.get("scale", [1, 1, 1]), 3, "Scale", 0.01, 100),
            "overrides": slicer.normalize_overrides({"process": overrides}, base).get("process", {}) if overrides else {},
        })
    out = {"bed": bed, "profile": profile, "plates": plates, "objects": objects}
    filaments = _filament_choices(raw.get("filaments"), model_ids)
    if filaments:  # likewise only when a model has a choice
        out["filaments"] = filaments
    if raw.get("arrange_engine") == ORCA:  # likewise only when OrcaSlicer's arranger is chosen over the nester
        out["arrange_engine"] = ORCA
    # A quick print's chosen orientation is the server's alone: whatever a save sends, the stored value is kept (the
    # other writers, _mutate on "data", change a copy of the stored data and so keep it too).
    if (record.get("data") or {}).get("orientation"):
        out["orientation"] = record["data"]["orientation"]
    return out


def update_project(project_id: int, body: dict) -> dict:
    """Rename and/or replace the project data. Send `revision` to refuse overwriting a newer edit."""
    record = get_project(project_id)
    if "revision" in body and body["revision"] is not None and int(body["revision"]) != record["revision"]:
        raise jobs.JobError("The project was changed elsewhere — reload it", 409)
    name = _clean_name(body["name"]) or record["name"] if "name" in body else record["name"]
    data = validate_data(body["data"], record) if "data" in body else record["data"]
    with database.transaction() as conn:
        current = conn.execute("SELECT revision, models FROM projects WHERE id = ?", (project_id,)).fetchone()
        if current is None:
            raise jobs.JobError("Project not found", 404)
        if json.loads(current["models"]) != record["models"] and "data" in body:
            data = validate_data(body["data"], {**record, "models": json.loads(current["models"])})
        conn.execute("UPDATE projects SET name = ?, data = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                     (name, json.dumps(data), time.time(), project_id))
    return get_project(project_id)


def _mutate(project_id: int, column: str, change, created_at: float | None = None) -> None:
    """Apply change(value) to one JSON column inside a transaction. With `created_at`, only if the project is still
    the one that was read then (SQLite hands a deleted project's id to the next new project)."""
    with database.transaction() as conn:
        row = conn.execute(f"SELECT {column}, created_at FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None or (created_at is not None and row["created_at"] != created_at):
            return
        value = json.loads(row[column] or ("[]" if column == "models" else "{}"))
        change(value)
        conn.execute(f"UPDATE projects SET {column} = ?, updated_at = ? WHERE id = ?", (json.dumps(value), time.time(), project_id))


# ── Models ─────────────────────────────────────────────────────────────────────

def _save_data(project_id: int, record: dict, data: dict, emptied=()) -> None:
    """Write `data` as the project's next revision, only if the project is still at the revision `record` was read at.
    Plates in `emptied` lose their slices and their folders."""
    with database.transaction() as conn:
        row = conn.execute("SELECT revision, slices FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise jobs.JobError("Project not found", 404)
        if row["revision"] != record["revision"]:
            raise jobs.JobError("The project was changed elsewhere — reload it", 409)
        if emptied:
            slices = json.loads(row["slices"] or "{}")
            for plate_id in emptied:
                slices.pop(str(plate_id), None)
            conn.execute("UPDATE projects SET data = ?, slices = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                         (json.dumps(data), json.dumps(slices), time.time(), project_id))
        else:
            conn.execute("UPDATE projects SET data = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                         (json.dumps(data), time.time(), project_id))
    for plate_id in emptied:
        shutil.rmtree(plate_dir(project_id, plate_id), ignore_errors=True)


def _next_id(items) -> int:
    return max((int(i["id"]) for i in items), default=0) + 1


def _require_geometry() -> None:
    missing = splits.missing_modules()
    if missing:
        raise jobs.JobError(f"Importing models needs {', '.join(missing)} installed on the Spooler computer", 503)


def _store_source(project_id: int, model_id: int, name: str, write_source, scale: float, up: str) -> dict:
    """Write a model's source file and import settings. Returns its record, waiting to be imported."""
    folder = model_dir(project_id, model_id)
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "source").mkdir(parents=True)
    try:
        size, digest = write_source(folder / "source" / name)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    (folder / "job.json").write_text(json.dumps({"source": f"source/{name}", "scale": scale, "up": up}), encoding="utf-8")
    return {"id": model_id, "name": Path(name).stem, "file": name, "state": IMPORTING, "error": None, "size_bytes": size,
            "sha256": digest, "import": [scale, up]}


def _copy_from(source: Path):
    """A write_source for _store_source that copies an existing file."""
    def write(path: Path) -> tuple:
        shutil.copyfile(source, path)
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
        return path.stat().st_size, digest.hexdigest()
    return write


def _add_model(project_id: int, name: str, write_source, scale: float, up: str, plate_id: int | None) -> int:
    _require_geometry()
    record = get_project(project_id)
    name = splits.safe_model_name(name)
    import_settings = splits.normalize_settings({"scale": scale, "up": up})
    model_id = _next_id(record["models"])
    model = _store_source(project_id, model_id, name, write_source, import_settings["scale"], import_settings["up"])
    with database.transaction() as conn:
        row = conn.execute("SELECT data, models FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise jobs.JobError("Project not found", 404)
        data, models = json.loads(row["data"]), json.loads(row["models"])
        if plate_id not in {p["id"] for p in data["plates"]}:
            plate_id = data["plates"][0]["id"]
        if len(data["objects"]) >= MAX_OBJECTS:
            raise jobs.JobError(f"A project holds at most {MAX_OBJECTS} objects")
        models.append(model)
        data["objects"].append({"id": _next_id(data["objects"]), "model": model_id, "name": model["name"], "plate": plate_id,
                                "position": [128.0, 128.0], "rotation": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0], "overrides": {}})
        conn.execute("UPDATE projects SET data = ?, models = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                     (json.dumps(data), json.dumps(models), time.time(), project_id))
    notify_worker()
    return model_id


def import_model(project_id: int, stream, length: int, filename: str, scale=1.0, up="z", plate_id=None) -> dict:
    if length <= 0:
        raise jobs.JobError("The file is empty")
    if length > splits.MAX_UPLOAD_BYTES:
        raise jobs.JobError(f"The file is larger than {splits.MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    if filename.lower().endswith(".3mf"):
        path = _spool(stream, length)
        try:
            if orca_3mf.is_project(path):
                return import_orca_project(project_id, path)
            _add_model(project_id, filename, _copy_from(path), scale, up, int(plate_id) if plate_id else None)
            return get_project(project_id)
        finally:
            path.unlink(missing_ok=True)

    def write(path: Path) -> tuple:
        digest, remaining = hashlib.sha256(), length
        with path.open("wb") as fh:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise jobs.JobError("The upload was interrupted")
                fh.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        return length, digest.hexdigest()

    _add_model(project_id, filename, write, scale, up, int(plate_id) if plate_id else None)
    return get_project(project_id)


# ── OrcaSlicer projects (3MF) ───────────────────────────────────────────────────

BED_FROM_ORCA = {name: key for key, name in slicer.BED_TYPES.items()}


def _spool(stream, length: int) -> Path:
    """Stream an upload to a temporary file inside the data directory. The caller deletes it."""
    if length <= 0:
        raise jobs.JobError("The file is empty")
    if length > splits.MAX_UPLOAD_BYTES:
        raise jobs.JobError(f"The file is larger than {splits.MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    projects_dir().mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".upload-", suffix=".3mf", dir=projects_dir())
    remaining = length
    try:
        with os.fdopen(fd, "wb") as fh:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise jobs.JobError("The upload was interrupted")
                fh.write(chunk)
                remaining -= len(chunk)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def _orca_profile(project: dict | None, notes: list) -> tuple:
    """The profile (locked presets plus setting changes) and plate side an Orca project asks for.
    Presets that aren't among the stock Elegoo ones fall back to the defaults, and every setting of the project that
    differs from them is carried over as a change, so a user's own Orca presets still slice the same."""
    default = {"bundle_id": _default_bundle_id(), "process": {}, "filament": {}, "bricklayers": None}
    if not project or slicer.orca_executable() is None:
        return default, "textured"
    try:
        stock = slicer.stock_presets()
        machine = project.get("printer_settings_id")
        if machine not in stock["machines"]:
            machine = stock["defaults"]["machine"]
            notes.append("The project's printer isn't a Centauri Carbon preset — using the default printer")
        process = project.get("print_settings_id")
        filament = (project.get("filament_settings_id") or [None])[0]
        names = {"machine": machine,
                 "process": process if process in stock["processes"][machine] else stock["defaults"]["process"],
                 "filament": filament if filament in stock["filaments"][machine] else stock["defaults"]["filament"]}
        bundle = slicer.create_bundle(**names)
        base = slicer.bundle_settings(bundle["id"])
        allowed = {kind: slicer._tab_keys(kind) - slicer.BLOCKED_SETTINGS for kind in ("process", "filament")}
        wanted = orca_3mf.settings_changes(project, base, allowed)
        changes = {}
        for kind, values in wanted.items():
            for key, value in values.items():
                try:
                    changes.setdefault(kind, {}).update(slicer.normalize_overrides({kind: {key: value}}, base).get(kind, {}))
                except jobs.JobError:
                    notes.append(f"Setting “{key}” from the project can't be used here and was left out")
        profile = {"bundle_id": bundle["id"], "process": changes.get("process", {}), "filament": changes.get("filament", {}), "bricklayers": None}
        if names["process"] != process or names["filament"] != filament:
            notes.append(f"The project used “{process}” / “{filament}”; its differences are applied on top of "
                         f"“{names['process']}” / “{names['filament']}”")
        return profile, BED_FROM_ORCA.get(project.get("curr_bed_type"), "textured")
    except jobs.JobError as e:
        notes.append(f"The project's settings couldn't be applied: {e}")
        return default, "textured"


def _clean_object_settings(settings: dict, base: dict | None, label: str, notes: list) -> dict:
    """The per-object process settings of an Orca project that this profile accepts; the rest are noted and left out."""
    kept = {}
    for key, value in settings.items():
        try:
            if base is None:
                raise jobs.JobError("no profile")
            kept.update(slicer.normalize_overrides({"process": {key: value}}, base).get("process", {}))
        except jobs.JobError:
            notes.append(f"{label}: setting “{key}” can't be used here and was left out")
    return kept


def import_orca_project(project_id: int, path: Path, fresh: bool = False) -> dict:
    """Bring the plates, objects, per-object settings and (for a new project) the settings of an Orca/Bambu 3MF into a project.
    With `fresh` the project's empty first plate is replaced; otherwise the file's plates are added after the existing ones."""
    _require_geometry()
    try:
        parsed = orca_3mf.parse(path)
    except orca_3mf.ThreeMFError as e:
        raise jobs.JobError(str(e))
    notes = list(parsed["warnings"])
    record = get_project(project_id)
    data = record["data"]
    instances = sum(len(o["instances"]) for o in parsed["objects"])
    if len(data["objects"]) + instances > MAX_OBJECTS:
        raise jobs.JobError(f"A project holds at most {MAX_OBJECTS} objects")
    if not fresh and len(data["plates"]) + len(parsed["plates"]) > MAX_PLATES:
        raise jobs.JobError(f"A project has at most {MAX_PLATES} plates")
    profile, bed = _orca_profile(parsed["project"], notes) if fresh else (data["profile"], data["bed"])
    base = slicer.bundle_settings(profile["bundle_id"]) if profile.get("bundle_id") else None

    def clean(settings: dict, label: str) -> dict:
        return _clean_object_settings(settings, base, label, notes)

    model_ids, models = {}, []
    next_model = _next_id(record["models"])
    try:
        for index, obj in enumerate(parsed["objects"]):
            model_id = next_model + index
            name = splits.safe_model_name(f"{obj['name']}.stl") if obj["name"].strip(". ") else f"object{index + 1}.stl"

            def write(target: Path, verts=obj["verts"], tris=obj["tris"]) -> tuple:
                write_stl(target, verts, tris)
                return target.stat().st_size, hashlib.sha256(target.read_bytes()).hexdigest()
            models.append(_store_source(project_id, model_id, name, write, 1.0, "z"))
            model_ids[index] = model_id
    except BaseException:
        for model in models:
            shutil.rmtree(model_dir(project_id, model["id"]), ignore_errors=True)
        raise

    with database.transaction() as conn:
        row = conn.execute("SELECT data, models FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise jobs.JobError("Project not found", 404)
        data, existing = json.loads(row["data"]), json.loads(row["models"])
        if fresh:
            data["plates"], data["objects"] = [], []
            data["profile"], data["bed"] = profile, bed
        first = _next_id(data["plates"])
        plate_ids = {}
        for number, plate in enumerate(parsed["plates"]):
            plate_ids[plate["id"]] = first + number
            data["plates"].append({"id": first + number, "name": plate["name"] or f"Plate {first + number}", "overrides": {}})
        for index, obj in enumerate(parsed["objects"]):
            overrides = clean(obj["settings"], obj["name"])
            for copy, instance in enumerate(obj["instances"], start=1):
                placed = orca_3mf.pose(obj["verts"], instance["matrix"], instance["shift"])
                if placed["notes"]:
                    notes.append(f"{obj['name']} is {' and '.join(placed['notes'])} in the project; it was placed unmirrored/unskewed")
                data["objects"].append({
                    "id": _next_id(data["objects"]), "model": model_ids[index], "plate": plate_ids.get(instance["plate"], first),
                    "name": obj["name"] if copy == 1 else f"{obj['name']} ({copy})", "position": placed["position"],
                    "rotation": placed["rotation"], "scale": placed["scale"], "overrides": dict(overrides)})
        conn.execute("UPDATE projects SET data = ?, models = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                     (json.dumps(data), json.dumps(existing + models), time.time(), project_id))
    notify_worker()
    return {**get_project(project_id), "import_notes": list(dict.fromkeys(notes))}


def export_orca_project(project_id: int) -> bytes:
    """The whole project as an OrcaSlicer project 3MF: every plate, each object's placement and settings, the profile with
    the project's changes. OrcaSlicer's own command line supplies the complete settings file the desktop app expects."""
    import numpy as np
    record = get_project(project_id)
    data = record["data"]
    models = {m["id"]: m for m in record["models"]}
    if not data["objects"]:
        raise jobs.JobError("The project has no objects to export", 409)
    waiting = [o["name"] for o in data["objects"] if models.get(o["model"], {}).get("state") != READY]
    if waiting:
        raise jobs.JobError(f"Still importing: {', '.join(waiting)}", 409)
    objects = []
    for obj in data["objects"]:
        verts, tris = load_mesh(project_id, obj["model"])
        centre = (verts.min(axis=0) + verts.max(axis=0)) / 2
        turn = rotation_matrix(obj["rotation"]) @ np.diag(np.asarray(obj["scale"], float))
        matrix = turn.T                                  # 3MF's row-vector form
        shift = place(verts, obj)[0] - (verts[0] - centre) @ matrix
        objects.append({"name": obj["name"], "verts": verts - centre, "tris": tris, "matrix": matrix, "shift": shift,
                        "plate": obj["plate"], "settings": obj.get("overrides") or {}})
    config = {}
    bundle_id = data["profile"].get("bundle_id")
    if bundle_id:
        settings = slicer.bundle_settings(bundle_id)
        config = _orca_project_config(project_id, settings)
        for kind in ("process", "filament"):
            for key, value in (data["profile"].get(kind) or {}).items():
                config[key] = value
        config["curr_bed_type"] = slicer.BED_TYPES[data["bed"]]
    return orca_3mf.build(objects, data["plates"], config)


_IDENTITY_KEYS = {"name", "from", "instantiation", "type", "inherits", "setting_id", "compatible_printers", "filament_id"}


def _orca_project_config(project_id: int, settings: dict) -> dict:
    """A complete project_settings.config for a locked profile: what OrcaSlicer writes itself when it exports a 3MF with these
    presets (639 settings), falling back to the presets' own settings when OrcaSlicer isn't available."""
    merged = {}
    for kind in slicer.PRESET_KINDS:
        merged.update({k: v for k, v in settings[kind].items() if k not in _IDENTITY_KEYS})
    names = {kind: settings[kind]["name"] for kind in slicer.PRESET_KINDS}
    identity = {"from": "project", "name": "project_settings", "printer_settings_id": names["machine"],
                "print_settings_id": names["process"], "filament_settings_id": [names["filament"]]}
    import numpy as np
    orca = slicer.orca_executable()
    if orca is None:
        return {**merged, **identity}
    with tempfile.TemporaryDirectory(prefix=".export-", dir=project_dir(project_id)) as tmp:
        tmp = Path(tmp)
        for kind in slicer.PRESET_KINDS:
            (tmp / f"{kind}.json").write_text(json.dumps(settings[kind]), encoding="utf-8")
        write_stl(tmp / "template.stl", np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]], float),
                  np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]))
        out = tmp / "template.3mf"
        code, _ = slicer.runner([str(orca), "--datadir", str(_data_dir / "slicer-data"), "--arrange", "0",
                                 "--load-settings", f"{tmp / 'machine.json'};{tmp / 'process.json'}",
                                 "--load-filaments", str(tmp / "filament.json"), "--export-3mf", str(out), str(tmp / "template.stl")], 120)
        try:
            with zipfile.ZipFile(out) as archive:
                template = json.loads(archive.read("Metadata/project_settings.config").decode("utf-8", "replace"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return {**merged, **identity}
    return {**template, **{k: v for k, v in merged.items() if k in template}, **identity}


def add_split_parts(project_id: int, split_id: int, numbers=None) -> dict:
    """Bring the parts of any split model into the project, packed onto plates (see lay_out_split)."""
    return lay_out_split(project_id, numbers, split_id=split_id)


# ── Laying out plates ──────────────────────────────────────────────────────────

def _machine_limits_for(record: dict) -> dict:
    bundle_id = record["data"]["profile"]["bundle_id"]
    if not bundle_id:
        raise jobs.JobError("Choose a profile first", 409)
    return machine_limits(slicer.bundle_settings(bundle_id)["machine"])


def _gap_for(process: dict) -> float:
    """Room to leave between objects printed with these process settings: supports need far more than the objects alone."""
    return packing.SUPPORT_GAP_MM if str((process or {}).get("enable_support")) == "1" else packing.GAP_MM


def _drop_split_models(data: dict, models: list, split_id: int) -> tuple:
    """Remove a split's models, their objects and the plates that held only them (in place).
    Returns (removed model ids, removed plate ids)."""
    doomed = {m["id"] for m in models if (m.get("split") or {}).get("id") == split_id}
    held = {o["plate"] for o in data["objects"] if o["model"] in doomed}
    data["objects"] = [o for o in data["objects"] if o["model"] not in doomed]
    remaining = {o["plate"] for o in data["objects"]}
    removed_plates = [p["id"] for p in data["plates"] if p["id"] in held and p["id"] not in remaining]
    data["plates"] = [p for p in data["plates"] if p["id"] not in removed_plates]
    models[:] = [m for m in models if m["id"] not in doomed]
    return sorted(doomed), removed_plates


def _replace_split_models(project_id: int, split_id: int, new_models: list, layout: list) -> dict:
    """Swap the split's models in the project for `new_models`, placed as `layout` says (packing.pack's plates, keyed
    by part number). A project whose only plate is empty uses that plate first."""
    by_part = {m["split"]["part"]: m for m in new_models}
    try:
        with database.transaction() as conn:
            row = conn.execute("SELECT data, models, slices FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise jobs.JobError("Project not found", 404)
            data, models, slices = json.loads(row["data"]), json.loads(row["models"]), json.loads(row["slices"] or "{}")
            dropped, removed_plates = _drop_split_models(data, models, split_id)
            if len(data["objects"]) + len(new_models) > MAX_OBJECTS:
                raise jobs.JobError(f"A project holds at most {MAX_OBJECTS} objects")
            next_object = _next_id(data["objects"])
            for index, plate in enumerate(layout):
                overrides = SUPPORT_OVERRIDES if plate["group"] else {}
                if index == 0 and len(data["plates"]) == 1 and not data["objects"]:
                    target = data["plates"][0]
                    target["overrides"] = dict(overrides)
                else:
                    if len(data["plates"]) >= MAX_PLATES:
                        raise jobs.JobError(f"A project holds at most {MAX_PLATES} plates")
                    target = _new_plate(_next_id(data["plates"]), overrides)
                    data["plates"].append(target)
                for item in plate["items"]:
                    model = by_part[item["key"]]
                    data["objects"].append({"id": next_object, "model": model["id"], "name": model["name"], "plate": target["id"],
                                            "position": [round(item["center"][0], 3), round(item["center"][1], 3)],
                                            "rotation": [0.0, 0.0, 90.0 if item["turned"] else 0.0], "scale": [1.0, 1.0, 1.0],
                                            "overrides": {}})
                    next_object += 1
            models.extend(new_models)
            for plate_id in removed_plates:
                slices.pop(str(plate_id), None)
            conn.execute("UPDATE projects SET data = ?, models = ?, slices = ?, split_id = COALESCE(split_id, ?), "
                         "revision = revision + 1, updated_at = ? WHERE id = ?",
                         (json.dumps(data), json.dumps(models), json.dumps(slices), split_id, time.time(), project_id))
    except BaseException:
        for model in new_models:
            shutil.rmtree(model_dir(project_id, model["id"]), ignore_errors=True)
        raise
    for model_id in dropped:
        shutil.rmtree(model_dir(project_id, model_id), ignore_errors=True)
    for plate_id in removed_plates:
        shutil.rmtree(plate_dir(project_id, plate_id), ignore_errors=True)
    notify_worker()
    return get_project(project_id)


def _file_footprint(path: Path) -> tuple:
    """Width and depth of a part file as it lies on the bed. (A split result's print_size is the oriented footprint,
    smallest side first, which needn't line up with the file's X and Y.)"""
    import numpy as np
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if len(data) == 84 + 50 * count:
        records = np.frombuffer(data, dtype=[("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")], count=count, offset=84)
        verts = records["vertices"].reshape(-1, 3)
    else:
        import trimesh
        verts = np.asarray(trimesh.load(path, force="mesh").vertices)
    size = verts.max(axis=0) - verts.min(axis=0)
    return float(size[0]), float(size[1])


def lay_out_split(project_id: int, numbers=None, split_id: int | None = None) -> dict:
    """Put a split model's parts on plates, packed onto as few as fit the printer profile's bed. Parts that need
    supports get plates of their own with tree supports on. Laying out again replaces the parts placed before."""
    _require_geometry()
    record = get_project(project_id)
    split_id = split_id if split_id is not None else record.get("split_id")
    if split_id is None:
        raise jobs.JobError("This project has no split model", 409)
    split = splits.get_split(split_id, detail=True)
    wanted = {int(n) for n in numbers} if numbers else None
    parts = [p for p in (split.get("result") or {}).get("parts", []) if wanted is None or p["number"] in wanted]
    if not parts:
        raise jobs.JobError("Split the model before laying out its parts", 409)
    limits = _machine_limits_for(record)
    items = [{"key": p["number"], "size": _file_footprint(splits.work_dir(split_id) / "parts" / p["file"]),
              "group": bool(p["needs_supports"])} for p in parts]
    profile = record["data"]["profile"]["process"]
    layout = [plate for supported in (False, True)   # plain plates first, then the ones with supports
              for plate in packing.pack([i for i in items if i["group"] == supported], limits["bed"], limits["exclude"],
                                        gap=_gap_for({**profile, **(SUPPORT_OVERRIDES if supported else {})}))]
    stem, first_id, new_models = Path(split["source_name"]).stem, _next_id(record["models"]), []
    try:
        for offset, part in enumerate(parts):
            model = _store_source(project_id, first_id + offset, splits.safe_model_name(f"{stem}_part{part['number']:02d}.stl"),
                                  _copy_from(splits.work_dir(split_id) / "parts" / part["file"]), 1.0, "z")
            model["split"] = {"id": split_id, "part": part["number"], "supports": bool(part["needs_supports"])}
            new_models.append(model)
    except BaseException:
        for model in new_models:
            shutil.rmtree(model_dir(project_id, model["id"]), ignore_errors=True)
        raise
    return _replace_split_models(project_id, split_id, new_models, layout)


def _whole_position(size, limits: dict):
    """Where a model of this size (width, depth, height in mm) can sit on the bed clear of the nozzle purge area: the
    middle if it is clear there, else against a corner. None when it can't be placed whole."""
    width, depth, height = size
    x0, y0, x1, y1 = limits["bed"]
    if width > x1 - x0 + 0.01 or depth > y1 - y0 + 0.01 or height > limits["height"] + 0.01:
        return None
    corners = [(x0 + width / 2, y0 + depth / 2), (x0 + width / 2, y1 - depth / 2),
               (x1 - width / 2, y0 + depth / 2), (x1 - width / 2, y1 - depth / 2)]
    for cx, cy in [((x0 + x1) / 2, (y0 + y1) / 2)] + corners:
        if limits["exclude"]:
            ex0, ey0, ex1, ey1 = limits["exclude"]
            if cx + width / 2 > ex0 and cx - width / 2 < ex1 and cy + depth / 2 > ey0 and cy - depth / 2 < ey1:
                continue
        return (cx, cy)
    return None


def place_whole_model(project_id: int) -> dict:
    """Put the project's uploaded model on a plate in one piece, for a model that fits the printer without splitting."""
    _require_geometry()
    record = get_project(project_id)
    split_id = record.get("split_id")
    if split_id is None:
        raise jobs.JobError("This project has no uploaded model", 409)
    split = splits.get_split(split_id, detail=True)
    if not split.get("analysis"):
        raise jobs.JobError("Wait for the model to be analysed", 409)
    limits = _machine_limits_for(record)
    x0, y0, x1, y1 = limits["bed"]
    rotation = _orientation(record)
    size = _oriented_size(split, rotation) if any(rotation) else split["analysis"]["size"]
    center = _whole_position(size, limits) or ((x0 + x1) / 2, (y0 + y1) / 2)
    model = _store_source(project_id, _next_id(record["models"]), split["source_name"],
                          _copy_from(splits.work_dir(split_id) / split["source_file"]), split["settings"]["scale"], split["settings"]["up"])
    model["split"] = {"id": split_id, "part": 0, "supports": False}
    layout = [{"group": False, "items": [{"key": 0, "center": center, "turned": False, "oversize": False}]}]
    placed = _replace_split_models(project_id, split_id, [model], layout)
    if any(rotation):
        _mutate(project_id, "data", lambda d: [o.__setitem__("rotation", list(rotation)) for o in d["objects"] if o["model"] == model["id"]])
        placed = get_project(project_id)
    return placed


# ── Import and Quick print ────────────────────────────────────────────────

PLAUSIBLE_MM = (5.0, 2000.0)   # the longest side of a model outside this is more likely a unit mistake than a real part
SCALE_GUESSES = ((1000.0, "metres"), (25.4, "inches"), (10.0, "centimetres"), (100.0, "decimetres"),
                 (0.001, "micrometres"), (0.0254, "thousandths of an inch"))
ROUGH_G_PER_CM3 = 0.5      # PLA at 1.24 g/cm3 with walls and sparse infill: about 40% of the solid volume
ROUGH_G_PER_HOUR = 15.0    # typical 0.2 mm layer print on this printer class


def _scale_suggestion(longest: float, scale: float):
    """A unit mix-up that would make `longest` (mm) a believable size, as a new scale for the model."""
    for low, high in ((10.0, 300.0), (10.0, 1000.0)):
        for factor, unit in SCALE_GUESSES:
            if low <= longest * factor <= high:
                return {"factor": factor, "scale": round(scale * factor, 6), "label": f"It's in {unit}: scale x{factor:g}"}
    return None


def intake(project_id: int) -> dict:
    """What an imported model needs before it can print: analysed yet, repaired, whether it fits the bed whole (so Split
    can be skipped), whether its size is plausible for its scale, and a rough estimate."""
    record = get_project(project_id)
    split_id = record.get("split_id")
    entry = (record["slices"] or {}).get("1") or {}
    if split_id is None and entry.get("imported") and not record["data"]["objects"]:   # uploaded G-code: already sliced
        grams = entry.get("estimate_g")
        return {"state": "ready", "kind": "gcode", "file": f"{record['name']}.gcode", "placed": True, "fits": True, "scale_ok": True,
                "can_print": True, "reason": None,
                "estimate": {"grams": round(grams) if grams is not None else None, "seconds": entry.get("estimate_s"), "rough": False}}
    if split_id is None:
        return {"state": "none", "can_print": False, "reason": "This project has no uploaded model"}
    split = splits.get_split(split_id, detail=True)
    placed = any((m.get("split") or {}).get("id") == split_id for m in record["models"])
    out = {"state": "analysing", "kind": "model", "file": split["source_name"], "placed": placed, "can_print": False, "reason": None}
    analysis = split.get("analysis")
    if split["state"] == splits.FAILED and not analysis:
        return {**out, "state": "failed", "reason": split.get("error") or "The model couldn't be read"}
    if not analysis:
        return {**out, "reason": "The model is still being checked"}
    bundle_id = record["data"]["profile"].get("bundle_id")
    limits = machine_limits(slicer.bundle_settings(bundle_id)["machine"] if bundle_id else {})
    size = [float(x) for x in analysis["size"]]
    scale = float(split["settings"]["scale"])
    longest = max(analysis.get("raw_size") or size) * scale    # the rounded size reads 0.0 for a speck
    doubtful = not PLAUSIBLE_MM[0] <= longest <= PLAUSIBLE_MM[1]
    fix = _scale_suggestion(longest, scale) if doubtful else None
    if doubtful:   # the analysis rounds to 0.1 mm, which reads 0 for a speck
        size = [round(x * scale, 4) for x in analysis.get("raw_size") or size]
    rotation = _orientation(record)
    if any(rotation) and not doubtful:
        size = _oriented_size(split, rotation)
    fits = _whole_position(size, limits) is not None
    reason = None
    if doubtful:
        reason = f"The model is {' x '.join(f'{x:g}' for x in size)} mm (longest side {longest:.3g} mm), which looks like the wrong scale"
    elif not fits:
        reason = "The model doesn't fit the bed whole, so it needs the Model and Split steps"
    grams = analysis["volume_cm3"] * ROUGH_G_PER_CM3
    return {**out, "state": "ready", "size": size, "volume_cm3": analysis["volume_cm3"],
            "repaired": bool(analysis.get("repairs")), "repairs": analysis.get("repairs") or [],
            "scale": scale, "rotation": list(rotation), "scale_ok": not doubtful, "scale_fix": fix,
            "fits": fits, "can_print": reason is None, "reason": reason,
            "estimate": {"grams": round(grams), "seconds": round(grams / ROUGH_G_PER_HOUR * 3600), "rough": True}}


def _orientation(record: dict) -> list:
    """The rotation (degrees about X, Y, Z) a quick model will be placed with, before it is on a plate."""
    return [float(r) for r in record["data"].get("orientation") or (0.0, 0.0, 0.0)]


def _split_mesh(split: dict):
    import numpy as np
    with np.load(splits.work_dir(split["id"]) / "model.npz") as data:
        return data["verts"].astype(float), np.asarray(data["tris"])


def _oriented_size(split: dict, rotation) -> list:
    """Width, depth and height (mm, one decimal) of the analysed model turned by `rotation`."""
    verts, _ = _split_mesh(split)
    turned = verts @ rotation_matrix(rotation).T
    return [round(float(x), 1) for x in turned.max(axis=0) - turned.min(axis=0)]


def _euler_zyx(matrix) -> list:
    """Degrees about X, Y, Z that rotation_matrix turns back into `matrix`."""
    sy = -float(matrix[2][0])
    ry = math.asin(max(-1.0, min(1.0, sy)))
    if abs(sy) < 0.999999:
        rx, rz = math.atan2(matrix[2][1], matrix[2][2]), math.atan2(matrix[1][0], matrix[0][0])
    else:
        rx, rz = math.atan2(-matrix[1][2], matrix[1][1]), 0.0
    return [round(math.degrees(a) % 360, 4) % 360 for a in (rx, ry, rz)]


def _largest_face_normal(verts, tris):
    """Normal of the largest flat face: triangles facing the same way (within about 1 degree) are one face."""
    import numpy as np
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    cross = np.cross(b - a, c - a)
    area = np.linalg.norm(cross, axis=1) / 2
    keep = area > 1e-9
    normals = cross[keep] / (2 * area[keep, None])
    area = area[keep]
    best, best_area = None, -1.0
    for index in np.argsort(-area)[:2000]:                  # a face is as good as its biggest triangles suggest
        close = normals @ normals[index] > math.cos(math.radians(1.0))
        total = float(area[close].sum())
        candidate = normals[close].T @ area[close]
        # equal faces (a cylinder's two caps) tie: take the one already facing down, so a model resting flat stays put
        if total > best_area * (1 + 1e-6) or (total > best_area * (1 - 1e-6) and candidate[2] / np.linalg.norm(candidate) < best[2] / np.linalg.norm(best)):
            best, best_area = candidate, total
    return best / np.linalg.norm(best)


def _quick_orientation(project_id: int, change) -> dict:
    """Change a quick model's orientation before it is placed: `change(split, current rotation)` gives the new one."""
    record = get_project(project_id)
    split_id = record.get("split_id")
    if split_id is None:
        raise jobs.JobError("This project has no uploaded model", 409)
    if any((m.get("split") or {}).get("id") == split_id for m in record["models"]):
        raise jobs.JobError("The model is already on a plate; turn it there", 409)
    split = splits.get_split(split_id, detail=True)
    if not split.get("analysis"):
        raise jobs.JobError("Wait for the model to be analysed", 409)
    rotation = [r % 360 for r in change(split, _orientation(record))]
    _mutate(project_id, "data", lambda d: d.__setitem__("orientation", rotation))
    return intake(project_id)


def lay_flat(project_id: int) -> dict:
    """Put the model on its largest flat face, turned to take the least room on the bed."""
    import numpy as np

    def change(split, current):
        verts, tris = _split_mesh(split)
        normal = _largest_face_normal(verts, tris)
        down = np.array([0.0, 0.0, -1.0])
        axis = np.cross(normal, down)
        sine, cosine = float(np.linalg.norm(axis)), float(normal @ down)
        if sine < 1e-9:
            flip = np.eye(3) if cosine > 0 else np.diag([1.0, -1.0, -1.0])
        else:
            k = axis / sine
            cross = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
            flip = np.eye(3) + sine * cross + (1 - cosine) * (cross @ cross)
        # then spin it about the vertical axis to the tightest footprint, so a long model lies along the bed's diagonal
        flat = verts[:: max(1, len(verts) // 4000)] @ flip.T
        best = min(range(90), key=lambda deg: np.prod(np.ptp(flat[:, :2] @ rotation_matrix([0, 0, deg])[:2, :2].T, axis=0)))
        return _euler_zyx(rotation_matrix([0, 0, best]) @ flip)
    return _quick_orientation(project_id, change)


def rotate_quarter(project_id: int) -> dict:
    """Turn the model 90 degrees about the vertical axis."""
    return _quick_orientation(project_id, lambda split, current: [current[0], current[1], (current[2] + 90) % 360])


def auto_place(project_id: int) -> dict:
    """Skip Split for a model that fits the bed: it lands on one auto plate. Nothing happens for a model that doesn't fit,
    has a doubtful scale, or is already placed."""
    info = intake(project_id)
    placed = False
    if info["can_print"] and not info["placed"]:
        place_whole_model(project_id)
        placed = True
        info = intake(project_id)
    return {"placed": placed, "intake": info, "project": get_project(project_id)}


def open_as_project(project_id: int) -> dict:
    """Promote a quick project to a normal, visible Project (also the hand-off for a model too big for the bed).
    Only the flag changes, so the model, plate and slice stay as they are; updated_at moves it to the top of the list."""
    get_project(project_id)
    with database.transaction() as conn:
        conn.execute("UPDATE projects SET quick = 0, updated_at = ? WHERE id = ?", (time.time(), project_id))
    return get_project(project_id)


def fix_scale(project_id: int) -> dict:
    """Apply the suggested scale correction; the model is analysed again."""
    info = intake(project_id)
    if info["state"] != "ready" or not info.get("scale_fix"):
        raise jobs.JobError("There is no scale correction to apply", 409)
    if info["placed"]:
        raise jobs.JobError("The model is already on a plate; change its scale there", 409)
    splits.update_settings(get_project(project_id)["split_id"], {"scale": info["scale_fix"]["scale"]})
    return intake(project_id)


QUICK_LAYER_HEIGHTS = (0.08, 0.12, 0.16, 0.2, 0.24, 0.28, 0.32)   # the fixed list the Quick print dialog offers (0.4 mm nozzle)
QUICK_SUPPORTS = ("off", "normal", "tree")
QUICK_BRIMS = ("off", "auto", "custom")
QUICK_OPTION_KEYS = ("layer_height", "infill", "walls", "supports", "brim", "brim_width")


def _quick_overrides(options) -> dict:
    """The per-project process overrides for the dialog's options (any subset). Refuses anything outside the choices."""
    if not isinstance(options, dict):
        raise jobs.JobError("options must be an object")
    unknown = sorted(set(options) - set(QUICK_OPTION_KEYS))
    if unknown:
        raise jobs.JobError(f"Unknown option: {', '.join(unknown)}")
    out = {}

    def number(key, label):
        try:
            value = float(options[key])
        except (TypeError, ValueError):
            raise jobs.JobError(f"{label} must be a number")
        if not math.isfinite(value):
            raise jobs.JobError(f"{label} must be a number")
        return value
    if "layer_height" in options:
        value = number("layer_height", "Layer height")
        if not any(abs(value - choice) < 1e-6 for choice in QUICK_LAYER_HEIGHTS):
            raise jobs.JobError("Layer height must be one of: " + ", ".join(f"{c:g}" for c in QUICK_LAYER_HEIGHTS) + " mm")
        out["layer_height"] = f"{value:g}"
    if "infill" in options:
        value = number("infill", "Infill")
        if not 0 <= value <= 100 or value != int(value):
            raise jobs.JobError("Infill must be a whole percentage from 0 to 100")
        out["sparse_infill_density"] = f"{int(value)}%"
    if "walls" in options:
        value = number("walls", "Walls")
        if not 1 <= value <= 20 or value != int(value):
            raise jobs.JobError("Walls must be a whole number from 1 to 20")
        out["wall_loops"] = str(int(value))
    if "supports" in options:
        choice = options["supports"]
        if choice not in QUICK_SUPPORTS:
            raise jobs.JobError(f"Supports must be one of: {', '.join(QUICK_SUPPORTS)}")
        out["enable_support"] = "0" if choice == "off" else "1"
        if choice != "off":
            out["support_type"] = "tree(auto)" if choice == "tree" else "normal(auto)"
    if "brim" in options:
        choice = options["brim"]
        if choice not in QUICK_BRIMS:
            raise jobs.JobError(f"Brim must be one of: {', '.join(QUICK_BRIMS)}")
        out["brim_type"] = {"off": "no_brim", "auto": "auto_brim", "custom": "outer_only"}[choice]   # off is written, not implied
        if choice == "custom":
            if "brim_width" not in options:
                raise jobs.JobError("A custom brim needs a brim_width")
            width = number("brim_width", "Brim width")
            if not 0 < width <= 20:
                raise jobs.JobError("Brim width must be between 0 and 20 mm")
            out["brim_width"] = f"{width:g}"
    elif "brim_width" in options:
        raise jobs.JobError("brim_width only goes with brim: custom")
    return out


def quick_options(project_id: int, preset: str | None = None) -> dict:
    """What the Quick print dialog starts from: the option values (the preset's own, with this project's choices over
    them), the fixed layer-height list and the remembered printer, preset, spool and plate side."""
    record = get_project(project_id)
    profile = record["data"]["profile"]
    bundle_id = preset or profile.get("bundle_id")
    process = {}
    if bundle_id:
        process = {**slicer.bundle_settings(bundle_id)["process"], **profile.get("process", {})}

    def num(key, default, cast):
        try:
            return cast(float(str(process.get(key, default)).rstrip("%")))
        except (TypeError, ValueError):
            return cast(default)
    # What the dialog can't express is reported as "preset" (the preset's own value, left alone unless the user picks another)
    supports = "off"
    if str(process.get("enable_support", "0")) == "1":
        kind = str(process.get("support_type", ""))
        supports = "tree" if kind == "tree(auto)" else "normal" if kind == "normal(auto)" else "preset"
    brim_type = process.get("brim_type", "auto_brim")   # the slicer's default when a preset says nothing
    brim = {"no_brim": "off", "auto_brim": "auto", "outer_only": "custom"}.get(brim_type, "preset")
    infill = num("sparse_infill_density", 15, float)
    options = {"layer_height": num("layer_height", 0.2, float), "infill": int(infill) if infill == int(infill) else infill,
               "walls": num("wall_loops", 2, int), "supports": supports, "brim": brim}
    if brim == "custom":
        options["brim_width"] = num("brim_width", 5, float)
    plate = record["data"]["plates"][0]
    remembered = remembered_defaults.usable()
    return {"options": options, "layer_heights": list(QUICK_LAYER_HEIGHTS), "preset": bundle_id,
            "printer_id": plate.get("printer_id"), "bed": record["data"]["bed"], "spool_id": profile.get("spool_id"),
            "leveling": bool(remembered["options"].get("leveling")), "timelapse": bool(remembered["options"].get("timelapse"))}


def _apply_quick_choices(project_id: int, body: dict) -> None:
    """Write the dialog's printer, preset, plate side, spool and options to the project (nothing if any is refused)."""
    options = _quick_overrides(body["options"]) if body.get("options") is not None else {}
    if body.get("printer_id"):
        jobs._require_printer(body["printer_id"])
    if body.get("bed") is not None and body["bed"] not in slicer.BED_TYPES:
        raise jobs.JobError(f"bed must be one of: {', '.join(slicer.BED_TYPES)}")
    spool = _spool_id(body.get("spool_id")) if "spool_id" in body else None
    bundle_id = body.get("preset") or get_project(project_id)["data"]["profile"].get("bundle_id")
    base = slicer.bundle_settings(bundle_id) if bundle_id else None   # a preset that doesn't exist is refused here

    def change(data):
        profile = data["profile"]
        if body.get("printer_id"):
            data["plates"][0]["printer_id"] = body["printer_id"]
        if body.get("bed") is not None:
            data["bed"] = body["bed"]
        if "spool_id" in body:
            profile["spool_id"] = spool
        if body.get("preset"):
            profile["bundle_id"] = body["preset"]          # the options stay: they are overrides, not part of the preset
        merged = {**profile.get("process", {}), **options}
        if base is not None and merged:
            merged = slicer.normalize_overrides({"process": merged}, base).get("process", {})
        profile["process"] = merged
    _mutate(project_id, "data", change)


def _plate_preflight(project_id: int, plate: dict, bed: str) -> dict:
    """The preflight checks a sliced plate would get as a Job on its printer, before it is queued."""
    printer = state.printers.get(plate.get("printer_id") or "")
    analysis = json.loads(plate_file(project_id, plate["id"], "analysis").read_text(encoding="utf-8"))
    spool_info = preflight.loaded_spool(printer) if printer is not None else {"spool": None}
    hardware = preflight.printer_hardware(printer.id) if printer is not None else {"nozzle_diameter": None}
    job = {"id": 0, "source": "upload", "print_opts": {"smooth_plate": bed == "smooth"}, "estimate_g": None}
    checks = preflight.evaluate(job, printer, analysis, spool_info, hardware)
    worst = max((c["level"] for c in checks), key=preflight.LEVEL_ORDER.get, default="pass")
    return {"state": "ready", "can_start": worst != "fail", "needs_acknowledgement": worst == "warn",
            "warnings": [{"level": c["level"], "title": c["title"], "detail": c["detail"]} for c in checks if c["level"] in ("warn", "fail")],
            "checks": sorted(checks, key=lambda c: -preflight.LEVEL_ORDER[c["level"]])}


def slice_and_print(project_id: int, body: dict) -> dict:
    """One action: apply the dialog's choices, place the model whole if it isn't yet, slice the plate and queue it when it's
    done. With `review` it stops after slicing and returns the preflight warnings under "review" instead of queueing."""
    info = intake(project_id)
    if not info["can_print"]:
        raise jobs.JobError(info["reason"] or "The model can't be printed yet", 409)
    if info.get("kind") == "gcode":      # already sliced: its preset and options are fixed in the file, so none are written
        body = {k: v for k, v in body.items() if k not in ("options", "preset")}
    _apply_quick_choices(project_id, body)
    if info.get("kind") == "gcode":      # queue it as it is
        record = get_project(project_id)
        _require_printers(record, {1})
        plate = record["data"]["plates"][0]
        if body.get("review"):      # the preflight report first, as for a sliced model
            return {**record, "review": _plate_preflight(project_id, plate, record["data"]["bed"])}
        acknowledge = bool(body.get("acknowledge_warnings"))
        job = queue_plate(project_id, 1, {"leveling": bool(body.get("leveling")), "timelapse": bool(body.get("timelapse")), "acknowledge": acknowledge})
        if acknowledge:
            jobs.acknowledge_warnings(job["id"], plate.get("printer_id"))
        return get_project(project_id)
    if not info["placed"]:
        place_whole_model(project_id)
    record = get_project(project_id)
    plate = record["data"]["plates"][0]
    plate_id = plate["id"]
    entry = record["slices"].get(str(plate_id)) or {}
    review = bool(body.get("review"))
    acknowledge = bool(body.get("acknowledge_warnings"))
    opts = {"leveling": bool(body.get("leveling")), "timelapse": bool(body.get("timelapse")), "acknowledge": acknowledge}
    if entry.get("state") == SLICED and not entry.get("outdated"):
        _require_printers(record, {plate_id})
        report = _plate_preflight(project_id, plate, record["data"]["bed"])
        if review:
            return {**record, "review": report}
        if not report["can_start"]:
            raise jobs.JobError("Preflight failed: " + "; ".join(c["title"] for c in report["checks"] if c["level"] == "fail"), 409)
        job = queue_plate(project_id, plate_id, opts)
        if acknowledge:
            jobs.acknowledge_warnings(job["id"], plate.get("printer_id"))
        return get_project(project_id)
    if entry.get("state") in (QUEUED, SLICING) and review:
        return {**record, "review": {"state": "slicing"}}
    if review:
        _require_printers(record, {plate_id})
        return {**request_slice(project_id, plate_id), "review": {"state": "slicing"}}
    return request_slice(project_id, plate_id, send=True, send_opts=opts)


def _footprint(project_id: int, obj: dict) -> tuple:
    """Width and depth of an object on the bed at its current rotation and scale."""
    verts, _ = load_mesh(project_id, obj["model"], preview=True)
    placed = place(verts, {**obj, "position": [0.0, 0.0]})
    size = placed.max(axis=0) - placed.min(axis=0)
    return float(size[0]), float(size[1])


# ── Rough print time before slicing ───────────────────────────────────────────

_estimate_cache: dict = {}   # (project id, plate fingerprint) → plate features; only complete answers are kept


def _plate_process(record: dict, plate: dict) -> dict:
    """The process settings a plate slices with, as far as they can be told without slicing."""
    data = record["data"]
    try:
        base = slicer.bundle_settings(data["profile"]["bundle_id"])["process"]
    except jobs.JobError:
        base = {}
    return {**base, **data["profile"].get("process", {}), **(plate.get("overrides") or {})}


def plate_features(project_id: int, record: dict, plate_id: int) -> dict | None:
    """What a plate's print time is estimated from: layers, deposited volume (cm3) and first-layer footprint (cm2), the
    first-layer footprint being the objects' bounding boxes on the bed. None while a model's geometry isn't available."""
    plate = next((p for p in record["data"]["plates"] if p["id"] == plate_id), None)
    objects = [o for o in record["data"]["objects"] if o["plate"] == plate_id]
    if plate is None or not objects:
        return None
    key = (project_id, fingerprint(record, plate_id))
    if key in _estimate_cache:
        return _estimate_cache[key]
    settings = time_estimate.settings_from(_plate_process(record, plate))
    height = volume = footprint = 0.0
    try:
        for obj in objects:
            analysis = splits._read(model_dir(project_id, obj["model"]), "analysis.json") or {}
            verts, _ = load_mesh(project_id, obj["model"], preview=True)
            placed = place(verts, {**obj, "position": [0.0, 0.0]})
            size = placed.max(axis=0) - placed.min(axis=0)
            factor = abs(float(obj["scale"][0]) * float(obj["scale"][1]) * float(obj["scale"][2]))
            volume += time_estimate.deposited_cm3(float(analysis["volume_cm3"]) * factor,
                                                  float(analysis.get("surface_cm2") or 0) * factor ** (2 / 3),
                                                  float(size[2]), settings)
            height = max(height, float(size[2]))
            footprint += float(size[0] * size[1]) / 100
    except (OSError, KeyError, TypeError, ValueError, jobs.JobError):
        return None   # the model isn't imported (yet): no estimate, which is better than a wrong one
    features = {"layers": time_estimate.layer_count(height, settings["layer_height"]),
                "volume_cm3": round(volume, 2), "first_layer_cm2": round(footprint, 2)}
    _estimate_cache[key] = features
    return features


def plate_time(project_id: int, record: dict, plate_id: int, entry: dict) -> dict | None:
    """A plate's time: the slicer's figure while its slice is current, otherwise the rough estimate marked approximate."""
    features = plate_features(project_id, record, plate_id)
    out = {"layers": features["layers"], "volume_cm3": features["volume_cm3"]} if features else {}
    if entry.get("state") == SLICED and not entry.get("outdated") and entry.get("estimate_s"):
        return {**out, "seconds": entry["estimate_s"], "approximate": False, "source": "slicer"}
    if not features:
        return None
    seconds = time_estimate.predict_seconds(features["layers"], features["volume_cm3"], features["first_layer_cm2"])
    return {**out, "seconds": seconds, "approximate": True, "source": "model"}


def _place_layout(data: dict, free: list, layout: list, overrides_for) -> list:
    """Move the objects in `data` as a layout (packing.pack's, or the nester's; keyed by object id) says, onto the plates in
    `free` in order and new plates after them. A layout plate with a "plate" goes onto that plate and keeps its settings; an
    item turns by its "angle" degrees about Z, else a quarter turn when "turned". Returns the ids of the plates used."""
    objects = {o["id"]: o for o in data["objects"]}
    plates = {p["id"]: p for p in data["plates"]}
    spare = [plate_id for plate_id in free if plate_id not in {p.get("plate") for p in layout}]
    used = []
    for plate in layout:
        if plate.get("plate") is not None:
            target = plates[plate["plate"]]
        elif spare:
            target = plates[spare.pop(0)]
            target["overrides"] = dict(overrides_for(plate["group"]))
        else:
            if len(data["plates"]) >= MAX_PLATES:
                raise jobs.JobError(f"A project holds at most {MAX_PLATES} plates")
            target = _new_plate(_next_id(data["plates"]), overrides_for(plate["group"]))
            data["plates"].append(target)
            plates[target["id"]] = target
        used.append(target["id"])
        for item in plate["items"]:
            obj = objects[item["key"]]
            obj["plate"] = target["id"]
            obj["position"] = [round(item["center"][0], 3), round(item["center"][1], 3)]
            turn = item["angle"] if "angle" in item else 90 if item["turned"] else 0
            if turn:
                obj["rotation"][2] = round((obj["rotation"][2] + turn) % 360, 4)
    return used


def _apply_layout(project_id: int, record: dict, plate_ids: list, layout: list, overrides_for) -> dict:
    """Apply a layout (see `_place_layout`) onto `plate_ids` and new plates. Plates among `plate_ids` left empty are removed;
    plates still holding importing models are kept."""
    data = json.loads(json.dumps(record["data"]))
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    failed = {m["id"] for m in record["models"] if m["state"] == FAILED}
    busy = {o["plate"] for o in data["objects"] if o["plate"] in plate_ids and o["model"] not in ready and o["model"] not in failed}
    holding = {o["plate"] for o in data["objects"] if o["model"] in failed}   # a failed model's objects stay, so their plate does
    free = [plate_id for plate_id in plate_ids if plate_id not in busy]
    used = _place_layout(data, free, layout, overrides_for)
    emptied = [plate_id for plate_id in free if plate_id not in used and plate_id not in holding]
    data["plates"] = [p for p in data["plates"] if p["id"] not in emptied]
    _save_data(project_id, record, data, emptied)
    return get_project(project_id)


NESTER, ORCA = ARRANGE_ENGINES = ("nester", "orca")   # the outline nester (default), or OrcaSlicer's own --arrange as a fallback
SUPPORT_OVERHANG_MM2 = 25.0            # underside area, off the bed, that makes an object need supports in its orientation


def _arrange_plate_ids(record: dict, plate_ids) -> list:
    """The plates an arrange covers, in plate order: the ones asked for (all by default) that exist."""
    plates = [p["id"] for p in record["data"]["plates"]]
    if plate_ids is None:
        return plates
    try:
        wanted = {int(i) for i in plate_ids}
    except (TypeError, ValueError):
        raise jobs.JobError("plates must be a list of plate numbers")
    return [p for p in plates if p in wanted]


def _threshold_angle(process: dict) -> float:
    """Degrees from horizontal at or below which the profile prints supports (support_threshold_angle). 0 is "automatic":
    the usual angle."""
    import orientation
    try:
        angle = float((process or {}).get("support_threshold_angle") or 0)
    except (TypeError, ValueError):
        angle = 0.0
    return angle if 1 <= angle <= 89 else orientation.DEFAULT_OVERHANG_ANGLE


def _needs_supports(process: dict, verts, tris, obj: dict) -> bool:
    """Whether the object, placed as it is, gets tree supports: supports are on in its settings and an underside off the
    bed slopes no more than the profile's overhang angle (from horizontal) over enough area."""
    import numpy as np
    if str(process.get("enable_support")) != "1":
        return False
    angle = _threshold_angle(process)
    placed = place(verts, {**obj, "position": [0.0, 0.0]})[np.asarray(tris)]
    cross = np.cross(placed[:, 1] - placed[:, 0], placed[:, 2] - placed[:, 0])
    area = np.linalg.norm(cross, axis=1) / 2
    with np.errstate(divide="ignore", invalid="ignore"):
        facing_down = -cross[:, 2] / (2 * area) >= math.cos(math.radians(angle)) - 1e-9
    off_bed = placed[:, :, 2].min(axis=1) > 0.3
    return bool(area[facing_down & off_bed & (area > 1e-9)].sum() > SUPPORT_OVERHANG_MM2)


class _Shapes:
    """What the nester needs to know of each object of a project: its outline from above and its clearance
    (SUPPORT_CLEARANCE_MM when it needs tree supports in its orientation, else CLEARANCE_MM), cached."""

    def __init__(self, project_id: int, record: dict, data: dict):
        import nesting
        self.nesting, self.project_id, self.record = nesting, project_id, record
        self.plates = {p["id"]: p for p in data["plates"]}
        self.processes, self.meshes, self.shapes = {}, {}, {}

    def mesh(self, model_id: int):
        if model_id not in self.meshes:
            self.meshes[model_id] = load_mesh(self.project_id, model_id)
        return self.meshes[model_id]

    def outline_and_clearance(self, obj: dict) -> tuple:
        key = (obj["model"], obj["plate"], tuple(obj["rotation"]), tuple(obj["scale"]), json.dumps(obj.get("overrides") or {}, sort_keys=True))
        if key not in self.shapes:
            if obj["plate"] not in self.processes:
                self.processes[obj["plate"]] = _plate_process(self.record, self.plates[obj["plate"]])
            verts, tris = self.mesh(obj["model"])
            hull = self.nesting.hull_outline(place(verts, {**obj, "position": [0.0, 0.0]})[:, :2])
            needs = _needs_supports({**self.processes[obj["plate"]], **(obj.get("overrides") or {})}, verts, tris, obj)
            self.shapes[key] = hull, self.nesting.SUPPORT_CLEARANCE_MM if needs else self.nesting.CLEARANCE_MM
        return self.shapes[key]

    def polygon(self, obj: dict) -> list:
        """The object's outline where it stands on the bed."""
        return self.nesting.hull_outline(place(self.mesh(obj["model"])[0], obj)[:, :2])


def placement_problems(project_id: int, record: dict, plate_ids=None) -> list:
    """Whether the ready objects on these plates (all by default) are placed validly, by the one definition every layout
    is held to (`nesting.placement_problems`): on the bed, clear of the purge area, and as far apart as Arrange keeps them
    (the larger of their two clearances). Judged on each object's real outline where it stands, not on its bounding box.
    Returns [{"key": object id, "plate", "problem": "off the bed" | "over the purge area" | "overlaps", "other": object id
    or None}], empty when all is well. Objects of models that failed or are still importing have no outline and are left
    out."""
    try:
        import nesting
    except ImportError:
        raise _nest_error()
    data = record["data"]
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    covered = None if plate_ids is None else set(plate_ids)
    shapes = _Shapes(project_id, record, data)
    placed = [{"key": o["id"], "plate": o["plate"], "polygon": shapes.polygon(o), "clearance": shapes.outline_and_clearance(o)[1]}
              for o in data["objects"] if o["model"] in ready and (covered is None or o["plate"] in covered)]
    limits = _machine_limits_for(record)
    return nesting.placement_problems(placed, limits["bed"], limits["exclude"])


def arrange_layout(project_id: int, record: dict, plate_ids=None) -> list:
    """Where the nester would put the unpinned objects on these plates (all by default), changing nothing. Each object
    is nested by the hull of the shape it has now, turned about Z, with its own clearance (SUPPORT_CLEARANCE_MM when it
    needs tree supports in its orientation, else CLEARANCE_MM); pinned objects are fixed shapes the others keep clear of
    and stay on their plates; objects on plates with different settings, or whose models have different filament
    choices, never share a plate. Plates holding a model that is still importing are left out. Returns, in plate order
    for each group, [{"group", "plate": the existing plate it is (a plate holding pinned objects) or None for one to
    fill, "overrides", "fixed": [pinned object ids], "items": [{"key": object id, "center": (x, y), "turned", "angle":
    degrees about Z, "oversize"}]}]. `_apply_layout` applies it."""
    try:
        import nesting
    except ImportError:
        raise _nest_error()
    data = record["data"]
    limits = _machine_limits_for(record)
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    failed = {m["id"] for m in record["models"] if m["state"] == FAILED}
    busy = {o["plate"] for o in data["objects"] if o["model"] not in ready and o["model"] not in failed}
    order = [p for p in _arrange_plate_ids(record, plate_ids) if p not in busy]
    plates = {p["id"]: p for p in data["plates"]}
    choices = data.get("filaments") or {}
    objects = [o for o in data["objects"] if o["plate"] in order and o["model"] in ready]   # a failed model's objects are skipped
    shapes = _Shapes(project_id, record, data)

    def group_of(obj):
        plate = plates[obj["plate"]]
        return json.dumps({"overrides": plate.get("overrides") or {}, "filament": _choice_key(choices.get(str(obj["model"])))},
                          sort_keys=True)

    pinned_plates = {}   # plate id → the group it holds: its first pinned object's
    for obj in sorted((o for o in objects if o.get("pinned")), key=lambda o: o["id"]):
        pinned_plates.setdefault(obj["plate"], group_of(obj))
    slots = {}           # group → [plate ids holding pinned objects], a plate's place in the list being its slot
    for plate_id in order:
        if plate_id in pinned_plates:
            slots.setdefault(pinned_plates[plate_id], []).append(plate_id)
    fixed = []
    for obj in objects:
        if obj.get("pinned"):
            group = pinned_plates[obj["plate"]]
            _, clearance = shapes.outline_and_clearance(obj)
            fixed.append({"key": obj["id"], "group": group, "slot": slots[group].index(obj["plate"]), "clearance": clearance,
                          "polygon": shapes.polygon(obj)})
    items = []
    for obj in (o for o in objects if not o.get("pinned")):
        hull, clearance = shapes.outline_and_clearance(obj)
        items.append({"key": obj["id"], "outline": hull, "clearance": clearance, "group": group_of(obj)})
    if not items:
        return []
    overrides = {group_of(o): dict(plates[o["plate"]].get("overrides") or {}) for o in objects}
    seen = {}
    layout = []
    for plate in nesting.nest(items, limits["bed"], limits["exclude"], fixed):
        index = seen[plate["group"]] = seen.get(plate["group"], -1) + 1
        held = slots.get(plate["group"], [])
        layout.append({"group": plate["group"], "plate": held[index] if index < len(held) else None,
                       "overrides": overrides[plate["group"]], "fixed": plate["fixed"], "items": plate["items"]})
    occupied = [p for p in order if any(o["plate"] == p for o in objects)]
    if len(layout) > len(occupied):   # the nester is greedy: don't make an arrangement that already holds worse by adding a plate
        standing = _standing_layout(project_id, record, objects, occupied, group_of, overrides)
        if standing:
            return standing
    return layout


def _standing_layout(project_id: int, record: dict, objects: list, occupied: list, group_of, overrides: dict) -> list:
    """The objects as they stand, as a layout (see `arrange_layout`), when that is a valid one: every object on the bed and
    clear of the others, no plate mixing groups. Else []."""
    if any(len({group_of(o) for o in objects if o["plate"] == plate_id}) > 1 for plate_id in occupied):
        return []
    if placement_problems(project_id, record, occupied):
        return []
    return [{"group": (group := group_of(next(o for o in objects if o["plate"] == plate_id))), "plate": plate_id,
             "overrides": overrides[group], "fixed": [o["id"] for o in objects if o["plate"] == plate_id and o.get("pinned")],
             "items": [{"key": o["id"], "center": tuple(o["position"]), "turned": False, "angle": 0.0, "oversize": False}
                       for o in objects if o["plate"] == plate_id and not o.get("pinned")]}
            for plate_id in occupied]


def _set_arrange_engine(project_id: int, record: dict, engine: str) -> dict:
    """Remember in the project which engine arranges it: only OrcaSlicer's is stored, the nester being the default."""
    data = json.loads(json.dumps(record["data"]))
    if engine == ORCA:
        data["arrange_engine"] = ORCA
    else:
        data.pop("arrange_engine", None)
    _save_data(project_id, record, data)
    return get_project(project_id)


def _skipped_failed(record: dict, plate_ids=None) -> str:
    """"; N objects skipped (their model failed to import)" for the failed models' objects on these plates, else ""."""
    failed = {m["id"] for m in record["models"] if m["state"] == FAILED}
    covered = None if plate_ids is None else set(plate_ids)
    n = sum(1 for o in record["data"]["objects"] if o["model"] in failed and (covered is None or o["plate"] in covered))
    return f"; {_count(n, 'object')} skipped (their model failed to import)" if n else ""


def _too_big_for_the_bed(after: dict, plate_ids: list) -> str:
    """"; N objects are bigger than the bed (left centred on a plate of its own)" for the unpinned ready objects still off
    the bed after an Arrange of these plates, which only a part too big for any plate can be; else "". A pinned object
    stays where its owner put it, so it is not counted."""
    pinned = {o["id"] for o in after["data"]["objects"] if o.get("pinned")}
    big = {p["key"] for p in placement_problems(after["id"], after, plate_ids) if p["problem"] == "off the bed"} - pinned
    return f"; {_count(len(big), 'object')} {'is' if len(big) == 1 else 'are'} bigger than the bed (left centred on a plate of its own)" if big else ""


def _arranged_outcome(before: dict, after: dict, skipped: str, too_big: str = "") -> str:
    """The one-line outcome every Arrange of ready objects carries, then what could not be done: `too_big` (objects that fit
    no plate) and `skipped` (objects of failed models)."""
    def stand(record):
        return [(o["id"], o["plate"], o["position"], o["rotation"]) for o in record["data"]["objects"]]
    added = len(after["data"]["plates"]) - len(before["data"]["plates"])
    if stand(before) == stand(after):
        text = "Nothing moved: already arranged"
    elif added > 0:
        text = f"Arranged; objects that didn't fit or differ in filament went on {_count(added, 'new plate')}"
    elif added < 0:
        text = f"Arranged onto {_count(len(after['data']['plates']), 'plate')} (was {len(before['data']['plates'])})"
    else:
        text = "Arranged"
    return text + too_big + skipped


def arrange(project_id: int, plate_ids=None, engine: str | None = None) -> dict:
    """Arrange (see `_arrange`) and return the project plus "outcome": one line saying what happened, which the UI always
    shows, naming the objects skipped because their model failed to import."""
    before = get_project(project_id)
    order = _arrange_plate_ids(before, plate_ids)
    after = _arrange(project_id, plate_ids, engine)
    ready = {m["id"] for m in after["models"] if m["state"] == READY}
    skipped = _skipped_failed(after, order)
    if not any(o["plate"] in order and o["model"] in ready for o in after["data"]["objects"]):
        return {**after, "outcome": "Nothing ready to arrange" + skipped}
    return {**after, "outcome": _arranged_outcome(before, after, skipped, _too_big_for_the_bed(after, order))}


def _arrange(project_id: int, plate_ids=None, engine: str | None = None) -> dict:
    """The one Arrange: lay out the unpinned objects on these plates (all by default) with `arrange_layout`, onto as few
    plates as fit; plates left empty are removed, never one holding a pinned object. `engine` "orca" has OrcaSlicer arrange
    each plate in place instead (its arranger knows nothing of pins, so a plate holding pinned objects is refused, nor of
    filaments or clearance); a choice of engine is kept in the project and used from then on when none is named."""
    record = get_project(project_id)
    stored = record["data"].get("arrange_engine", NESTER)
    if engine is not None and engine not in ARRANGE_ENGINES:
        raise jobs.JobError(f"engine must be one of: {', '.join(ARRANGE_ENGINES)}")
    order = _arrange_plate_ids(record, plate_ids)
    if (engine or stored) == ORCA and any(o.get("pinned") and o["plate"] in order for o in record["data"]["objects"]):
        raise jobs.JobError("OrcaSlicer's arranger can't keep clear of pinned objects: unpin them or use the nester")
    if engine is not None and engine != stored:
        record, stored = _set_arrange_engine(project_id, record, engine), engine
    if stored == ORCA:
        ready = {m["id"] for m in record["models"] if m["state"] == READY}
        for plate_id in order:
            if any(o["plate"] == plate_id and o["model"] in ready and not o.get("pinned") for o in record["data"]["objects"]):
                record = _arrange_with_orca(project_id, plate_id)
        return record
    layout = arrange_layout(project_id, record, order)
    if not layout:
        return record
    return _apply_layout(project_id, record, order, layout, lambda group: json.loads(group)["overrides"])


def set_copies(project_id: int, object_id, copies, revision=None) -> dict:
    """Make the number of Objects made from this object's Model (across all plates) `copies`. More copies are clones of
    the object, placed where they fit (its plate first, then plates with the same settings, then a new plate); fewer
    remove unpinned objects, last plate first, never the object itself. The result is the project plus
    `copies`: {model, count, asked, pinned} so a count above `asked` can say that pinned objects held it up."""
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= MAX_OBJECTS:
        raise jobs.JobError(f"Copies must be a whole number from 1 to {MAX_OBJECTS}")
    record = get_project(project_id)
    if revision is not None and revision != record["revision"]:
        raise jobs.JobError("The project was changed elsewhere — reload it", 409)
    data = json.loads(json.dumps(record["data"]))
    source = next((o for o in data["objects"] if o["id"] == object_id), None)
    if source is None:
        raise jobs.JobError("Object not found", 404)
    made = [o for o in data["objects"] if o["model"] == source["model"]]
    result = {"model": source["model"], "count": len(made), "asked": copies,
              "pinned": sum(1 for o in made if o.get("pinned"))}
    if copies > len(made):
        if source["model"] not in {m["id"] for m in record["models"] if m["state"] == READY}:
            raise jobs.JobError("Wait for the model to finish importing", 409)
        if len(data["objects"]) + copies - len(made) > MAX_OBJECTS:
            raise jobs.JobError(f"A project holds at most {MAX_OBJECTS} objects")
        _add_copies(project_id, record, data, source, copies - len(made))
    elif copies < len(made):
        order = {p["id"]: i for i, p in enumerate(data["plates"])}
        removable = [o for o in data["objects"] if o["model"] == source["model"] and o["id"] != source["id"] and not o.get("pinned")]
        removable.sort(key=lambda o: (order[o["plate"]], data["objects"].index(o)), reverse=True)
        gone = {o["id"] for o in removable[:len(made) - copies]}
        data["objects"] = [o for o in data["objects"] if o["id"] not in gone]
    if data["objects"] == record["data"]["objects"] and data["plates"] == record["data"]["plates"]:
        return {**record, "copies": result}
    _save_data(project_id, record, data)
    result["count"] = sum(1 for o in data["objects"] if o["model"] == source["model"])
    return {**get_project(project_id), "copies": result}


def _nest_error() -> jobs.JobError:
    return jobs.JobError("Arranging needs shapely installed on the Spooler computer", 503)


def _add_copies(project_id: int, record: dict, data: dict, source: dict, count: int) -> None:
    """Append `count` clones of `source` to `data`, each where the nester finds room for it: its plate first, then plates
    with the same settings, then a new plate."""
    try:
        import nesting
    except ImportError:
        raise _nest_error()
    limits = _machine_limits_for(record)
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    plates = {p["id"]: p for p in data["plates"]}
    shapes = _Shapes(project_id, record, data)
    settings = lambda plate_id: json.dumps(plates[plate_id].get("overrides") or {}, sort_keys=True)
    hull, clearance = shapes.outline_and_clearance(source)
    same = [source["plate"]] + [p["id"] for p in data["plates"] if p["id"] != source["plate"] and settings(p["id"]) == settings(source["plate"])]
    for _ in range(count):
        spot = None
        for plate_id in same:
            fixed = [{"key": o["id"], "group": 0, "slot": 0, "polygon": shapes.polygon(o), "clearance": shapes.outline_and_clearance(o)[1]}
                     for o in data["objects"] if o["plate"] == plate_id and o["model"] in ready]
            spot = _nest_one(nesting, hull, clearance, limits, fixed)
            if spot:
                spot = plate_id, spot
                break
        if spot is None:
            if len(data["plates"]) >= MAX_PLATES:
                raise jobs.JobError(f"A project holds at most {MAX_PLATES} plates")
            plate = _new_plate(_next_id(data["plates"]), plates[source["plate"]].get("overrides"))
            data["plates"].append(plate)
            plates[plate["id"]] = shapes.plates[plate["id"]] = plate
            same.append(plate["id"])
            spot = plate["id"], _nest_one(nesting, hull, clearance, limits, []) or ((128.0, 128.0), 0.0)   # too big for any plate: it sits centred
        plate_id, ((x, y), angle) = spot
        added = json.loads(json.dumps(source))
        added.pop("pinned", None)
        added.update(id=_next_id(data["objects"]), plate=plate_id, position=[round(x, 3), round(y, 3)])
        if angle:
            added["rotation"][2] = round((added["rotation"][2] + angle) % 360, 4)
        data["objects"].append(added)


def _nest_one(nesting, hull, clearance: float, limits: dict, fixed: list):
    """((centre x, centre y), turn about Z in degrees) for one more object on a plate holding `fixed` shapes, or None when
    it doesn't fit there."""
    plates = nesting.nest([{"key": 0, "outline": hull, "clearance": clearance, "group": 0}], limits["bed"], limits["exclude"], fixed)
    placed = [i for i in plates[0]["items"] if i["key"] == 0 and not i["oversize"]]
    return (placed[0]["center"], placed[0]["angle"]) if placed else None


def merge_plates(project_id: int, source_id: int, into_id: int) -> dict:
    """Move everything on one plate onto another, nested to fit. The combined plate keeps the settings of `into_id`; plates
    of different filament choices don't merge."""
    try:
        import nesting
    except ImportError:
        raise _nest_error()
    record = get_project(project_id)
    plates = {p["id"]: p for p in record["data"]["plates"]}
    if source_id not in plates or into_id not in plates:
        raise jobs.JobError("Plate not found", 404)
    if source_id == into_id:
        raise jobs.JobError("Choose another plate to merge into")
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    objects = [o for o in record["data"]["objects"] if o["plate"] in (into_id, source_id)]
    if any(o["model"] not in ready for o in objects):
        raise jobs.JobError("Wait for the models on these plates to finish importing", 409)
    found = plate_filaments({"objects": [{**o, "plate": into_id} for o in objects], "filaments": record["data"].get("filaments")}, into_id)
    if len(found) > 1:
        labels = [choice_label(c) for c in found]
        raise jobs.JobError(f"{plates[into_id]['name']} and {plates[source_id]['name']} together mix {', '.join(labels[:-1])} and {labels[-1]}", 409)
    limits = _machine_limits_for(record)
    shapes = _Shapes(project_id, record, record["data"])
    items = []
    for o in objects:
        hull, clearance = shapes.outline_and_clearance({**o, "plate": into_id})
        items.append({"key": o["id"], "outline": hull, "clearance": clearance, "group": "merged"})
    layout = nesting.nest(items, limits["bed"], limits["exclude"]) or [{"group": "merged", "items": []}]   # both plates empty
    if len(layout) > 1 or any(item["oversize"] for plate in layout for item in plate["items"]):
        raise jobs.JobError(f"Those objects don't fit on one plate — {plates[source_id]['name']} stays as it is", 409)
    return _apply_layout(project_id, record, [into_id, source_id], layout, lambda _: plates[into_id].get("overrides") or {})


# ── Orientation ────────────────────────────────────────────────────────────────

def _overhang_angle(record: dict, body: dict) -> float:
    """Degrees from horizontal at or below which the profile prints supports (support_threshold_angle, 30 when unset)."""
    if body.get("overhang_angle") not in (None, ""):
        try:
            angle = float(body["overhang_angle"])
        except (TypeError, ValueError):
            angle = 0.0
        if not 1 <= angle <= 89:
            raise jobs.JobError("overhang_angle is a number of degrees from 1 to 89")
        return angle
    profile = record["data"]["profile"]
    try:
        process = {**slicer.bundle_settings(profile["bundle_id"])["process"], **profile.get("process", {})} if profile["bundle_id"] else {}
    except jobs.JobError:
        process = {}
    return _threshold_angle(process)


def _bed_limits(record: dict) -> dict:
    limits = _machine_limits_for(record)
    x0, y0, x1, y1 = limits["bed"]
    return {"height": limits["height"], "bed_x": x1 - x0, "bed_y": y1 - y0}


def _orient_options(record: dict, body: dict) -> dict:
    import orientation
    mode = body.get("mode") or orientation.LEAST_SUPPORT
    if mode not in orientation.MODES:
        raise jobs.JobError(f"mode must be one of {', '.join(orientation.MODES)}")
    options = {"mode": mode, "angle": _overhang_angle(record, body), "favoured": body.get("face")}
    try:
        options["limits"] = _bed_limits(record)
    except jobs.JobError:
        options["limits"] = None   # no profile chosen yet: orient without checking the bed
    return options


def _orientation_for(project_id: int, obj: dict, options: dict) -> dict:
    import orientation
    verts, tris = load_mesh(project_id, obj["model"], preview=True)
    try:
        return orientation.suggest(verts, tris, obj["scale"], obj["rotation"], **options)
    except orientation.OrientError as e:
        raise jobs.JobError(str(e))


def _ready_object(record: dict, object_id) -> dict:
    obj = next((o for o in record["data"]["objects"] if o["id"] == object_id), None)
    if obj is None:
        raise jobs.JobError("Object not found", 404)
    if next((m["state"] for m in record["models"] if m["id"] == obj["model"]), None) != READY:
        raise jobs.JobError("The model isn't ready yet", 409)
    return obj


def suggest_orientation(project_id: int, object_id: int, body: dict) -> dict:
    """The orientation suggested for one object; changes nothing. `mode`: least-support (default), strength or looks
    (which needs `face`, the side that should look best, as a direction in the model's own space)."""
    record = get_project(project_id)
    obj = _ready_object(record, object_id)
    options = _orient_options(record, body)
    suggestion = _orientation_for(project_id, obj, options)
    return {"object": object_id, "mode": options["mode"], "overhang_angle": options["angle"],
            "rotation_from": obj["rotation"], **suggestion}


def _apply_rotations(project_id: int, record: dict, rotations: dict) -> dict:
    data = json.loads(json.dumps(record["data"]))
    for obj in data["objects"]:
        if obj["id"] in rotations:
            obj["rotation"] = rotations[obj["id"]]
    _save_data(project_id, record, data)
    return get_project(project_id)


def orient_object(project_id: int, object_id: int, body: dict) -> dict:
    """Accept the suggestion for one object. Works on a pinned object too: pinning only keeps the planner away."""
    record = get_project(project_id)
    obj = _ready_object(record, object_id)
    suggestion = _orientation_for(project_id, obj, _orient_options(record, body))
    return _apply_rotations(project_id, record, {object_id: suggestion["rotation"]}) if suggestion["changed"] else record


def orient_all(project_id: int, body: dict) -> dict:
    """Orient every unpinned, ready object on one plate (`plate`) or in the whole project. Pinned objects are left alone.
    Returns the project plus "orient": {"oriented": [...], "unchanged": [...], "pinned": [...]} (object ids)."""
    record = get_project(project_id)
    plate_id = body.get("plate")
    if plate_id is not None and plate_id not in {p["id"] for p in record["data"]["plates"]}:
        raise jobs.JobError("Plate not found", 404)
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    if body.get("mode") == "looks":
        raise jobs.JobError("Looks needs a favoured side, so it is chosen for one object at a time")
    options = _orient_options(record, body)
    summary = {"oriented": [], "unchanged": [], "pinned": []}
    rotations = {}
    for obj in record["data"]["objects"]:
        if (plate_id is not None and obj["plate"] != plate_id) or obj["model"] not in ready:
            continue
        if obj.get("pinned"):
            summary["pinned"].append(obj["id"])
            continue
        suggestion = _orientation_for(project_id, obj, options)
        if suggestion["changed"]:
            rotations[obj["id"]] = suggestion["rotation"]
            summary["oriented"].append(obj["id"])
        else:
            summary["unchanged"].append(obj["id"])
    result = _apply_rotations(project_id, record, rotations) if rotations else record
    return {**result, "orient": summary, "outcome": _orient_outcome(summary)}


def _orient_outcome(summary: dict) -> str:
    """One line for the toast: how many objects were turned (zero included), how many were already on a good face,
    and how many pinned ones were left alone."""
    turned, fine, pinned = len(summary["oriented"]), len(summary["unchanged"]), len(summary["pinned"])
    if not (turned or fine or pinned):
        return "Nothing to orient"
    parts = [f"Turned {_count(turned, 'object')}"]
    if fine or not turned:
        parts.append(f"{fine} already on a good face")
    if pinned:
        parts.append(f"{pinned} pinned left alone")
    return ", ".join(parts)


# ── Plan plates ────────────────────────────────────────────────────────────────

KEPT_REASONS = {"queued": "In the print queue", "waiting": "In the print queue, waiting on a hold", "printing": "Printing now",
                "printed_ok": "Printed", "failed": "A print of it is on record, and it didn't finish"}
_DEFAULT_PLATE_NAME = re.compile(r"^Plate (\d+)$")


def _turned(before, after) -> bool:
    """Whether the object now stands on another side: its X or Y rotation changed. A turn about Z only spins it on the bed."""
    return any(abs((a - b + 180) % 360 - 180) > 0.01 for a, b in zip(before[:2], after[:2]))


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _flat_on_face(rotation) -> bool:
    """Whether an orientation stands the part on a side of its own box: X and Y are quarter turns (a degree of slack).
    Any other tilt leans it on an angled face, which Plan plates only does when the user opts in."""
    return all(abs((r + 45) % 90 - 45) <= 1.0 for r in rotation[:2])


def _plan_problems(project_id: int, record: dict, data: dict, plate_ids) -> list:
    """What is wrong with the plan's layout on these plates, by the one validity check (`placement_problems`). A pinned object
    stays where its owner put it, so a problem that only involves pinned objects is not the plan's doing and is left out."""
    pinned = {o["id"] for o in data["objects"] if o.get("pinned")}
    return [p for p in placement_problems(project_id, {**record, "data": data}, plate_ids)
            if not (p["key"] in pinned and (p["other"] is None or p["other"] in pinned))]


def _refusal(data: dict, problems: list) -> str:
    """Why a plan is not offered, naming the objects: "2 objects would be off the bed (cover, knob); 1 object would overlap …"."""
    names = {o["id"]: o.get("name") or f"Object {o['id']}" for o in data["objects"]}
    parts = []
    for kind, wording in (("off the bed", "off the bed"), ("over the purge area", "over the purge area"), ("overlaps", "overlapping another")):
        keys = sorted({p["key"] for p in problems if p["problem"] == kind})
        if keys:
            shown = ", ".join(names.get(k, f"Object {k}") for k in keys[:5]) + (", …" if len(keys) > 5 else "")
            parts.append(f"{_count(len(keys), 'object')} would be {wording} ({shown})")
    return "; ".join(parts)


def _plan_data(project_id: int, record: dict, allow_tilt: bool = False) -> dict:
    """The project data as Plan plates would leave it, changing nothing. Plates that are queued, printing, have a Print on
    record, or hold a model still importing are kept as they are. On the others, every unpinned object is oriented
    (in memory, as Orient all would) and packed with `arrange_layout` (a pinned object keeps its plate and place; a plate
    never mixes filament choices), then the plates are ordered longest first by `time_estimate.rank_key`; plates keep their
    ids, which are what slices and jobs follow, while the order and the default names ("Plate 3") follow the plan, filling
    the numbers the kept plates leave free. Returns {"data", "plates": [{"id", "new", "objects", "turned", "features"}],
    "kept": [{"id", "name", "reason"}], "emptied": [plate ids removed]}; "plates" is empty when there is nothing to plan."""
    data = json.loads(json.dumps(record["data"]))
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    failed = {m["id"] for m in record["models"] if m["state"] == FAILED}
    importing = {o["plate"] for o in data["objects"] if o["model"] not in ready and o["model"] not in failed}
    status = {p["id"]: p["status"] for p in plate_status(project_id)["plates"]}
    with database.transaction() as conn:   # a Print whose outcome is unknown still counts as on record
        recorded = {r["plate_id"] for r in conn.execute("SELECT DISTINCT plate_id FROM prints WHERE project_id = ? AND plate_id IS NOT NULL", (project_id,))}
    kept, replannable = [], []
    for plate in data["plates"]:
        reason = (KEPT_REASONS.get(status.get(plate["id"])) or ("A print of it is on record" if plate["id"] in recorded else None)
                  or ("Its models are still importing" if plate["id"] in importing else None))
        if reason:
            kept.append({"id": plate["id"], "name": plate["name"], "reason": reason})
        else:
            replannable.append(plate["id"])
    before = {o["id"]: list(o["rotation"]) for o in data["objects"]}
    options = _orient_options(record, {})
    suggested, withheld = {}, 0
    for obj in data["objects"]:
        if obj["plate"] not in replannable or obj.get("pinned") or obj["model"] not in ready:
            continue
        key = (obj["model"], tuple(obj["scale"]), tuple(obj["rotation"]))
        if key not in suggested:
            try:
                suggested[key] = _orientation_for(project_id, obj, options)
            except jobs.JobError:
                suggested[key] = None   # nothing to suggest for it: it stays as it is
        if suggested[key] and suggested[key]["changed"]:
            if allow_tilt or _flat_on_face(suggested[key]["rotation"]):
                obj["rotation"] = list(suggested[key]["rotation"])
            else:
                withheld += 1   # it would lean on an angled face: left as it is unless the user opts in
    layout = arrange_layout(project_id, {**record, "data": data}, replannable)
    if not layout:
        return {"data": record["data"], "plates": [], "kept": kept, "emptied": [], "fingerprint": _plan_fingerprint(record["data"], [])}
    used = _place_layout(data, replannable, layout, lambda group: json.loads(group)["overrides"])
    problems = _plan_problems(project_id, record, data, used)
    if problems:   # a plan that is not valid is not offered: nothing is proposed and the reason is said
        return {"data": record["data"], "plates": [], "kept": kept, "emptied": [], "problems": problems,
                "refused": _refusal(data, problems), "fingerprint": _plan_fingerprint(record["data"], [])}
    holding = {o["plate"] for o in data["objects"] if o["model"] in failed}
    for plate in data["plates"]:   # a plate left holding only a failed model's objects stays as it is
        if plate["id"] in replannable and plate["id"] not in used and plate["id"] in holding:
            kept.append({"id": plate["id"], "name": plate["name"], "reason": "Holds an object whose model failed to import"})
    kept_now = {k["id"] for k in kept}
    emptied = [plate_id for plate_id in replannable if plate_id not in used and plate_id not in kept_now]
    data["plates"] = [p for p in data["plates"] if p["id"] not in emptied]
    plates = {p["id"]: p for p in data["plates"]}
    for plate_id in used:   # a plate that now holds other objects isn't the plate the printer was chosen for
        if {o["id"] for o in data["objects"] if o["plate"] == plate_id} != {o["id"] for o in record["data"]["objects"] if o["plate"] == plate_id}:
            plates[plate_id].pop("printer_id", None)
    existing = {p["id"] for p in record["data"]["plates"]}
    features = {plate_id: plate_features(project_id, {**record, "data": data}, plate_id) for plate_id in used}
    ordered = (sorted((p for p in used if features[p]), key=lambda p: time_estimate.rank_key(features[p]), reverse=True)
               + [p for p in used if not features[p]])
    kept_ids = {k["id"] for k in kept}
    kept_at = {p["id"]: i for i, p in enumerate(record["data"]["plates"]) if p["id"] in kept_ids}   # kept plates keep their place
    order, queue, later = [], list(ordered), sorted(kept_at, key=kept_at.get)
    for index in range(len(kept) + len(ordered)):
        holder = next((k for k in later if kept_at[k] == index), None)
        if holder is not None:
            order.append(holder)
            later.remove(holder)
        elif queue:
            order.append(queue.pop(0))
    order += later
    taken = {int(m.group(1)) for p in data["plates"] if p["id"] in kept_ids and (m := _DEFAULT_PLATE_NAME.match(p["name"] or ""))}
    free = (n for n in range(1, 10000) if n not in taken)
    for plate_id in ordered:   # default names follow the plan; a name the user gave stays
        if not plates[plate_id]["name"] or _DEFAULT_PLATE_NAME.match(plates[plate_id]["name"]):
            plates[plate_id]["name"] = f"Plate {next(free)}"
    data["plates"] = [plates[plate_id] for plate_id in order]
    proposed = [{"id": plate_id, "new": plate_id not in existing, "features": features[plate_id],
                 "objects": sorted((o for o in data["objects"] if o["plate"] == plate_id and o["model"] in ready),
                                   key=lambda o: o["id"])} for plate_id in ordered]
    for entry in proposed:
        entry["turned"] = sum(1 for o in entry["objects"] if _turned(before[o["id"]], o["rotation"]))
    return {"data": data, "plates": proposed, "kept": kept, "emptied": emptied, "tilts_withheld": withheld,
            "fingerprint": _plan_fingerprint(data, emptied)}


def _plan_fingerprint(data: dict, emptied: list) -> str:
    """Identifies what a plan would do: the data it would leave and the plates it would remove."""
    return hashlib.sha256(json.dumps({"data": data, "emptied": sorted(emptied)}, sort_keys=True).encode()).hexdigest()[:16]


def _plan_view(record: dict, planned: dict) -> dict:
    """What a plan shows: the proposed plates in plan order, the plates kept as they are, and a one-line summary."""
    data = planned["data"]
    names = {p["id"]: p["name"] for p in data["plates"]}
    plates = []
    for entry in planned["plates"]:
        found = plate_filaments(data, entry["id"])
        features = entry["features"]
        estimate = ({"layers": features["layers"], "volume_cm3": features["volume_cm3"], "approximate": True, "source": "model",
                     "seconds": time_estimate.predict_seconds(features["layers"], features["volume_cm3"], features["first_layer_cm2"])}
                    if features else None)
        plates.append({"id": entry["id"], "name": names[entry["id"]], "new": entry["new"],
                       "filament": choice_label(found[0]) if found else None, "parts": len(entry["objects"]),
                       "turned": entry["turned"], "time_estimate": estimate,
                       "objects": [{"id": o["id"], "name": o.get("name"), "model": o["model"], "position": o["position"],
                                    "rotation": o["rotation"], "pinned": bool(o.get("pinned"))} for o in entry["objects"]]})
    kept = planned["kept"]
    if planned.get("refused"):
        summary = f"No valid plan: {planned['refused']}"
    elif plates:
        summary = (f"{_count(len(plates), 'plate')} (was {len(record['data']['plates']) - len(kept)}), "
                   f"{_count(sum(p['parts'] for p in plates), 'object')}, {sum(p['turned'] for p in plates)} turned")
        if kept:
            summary += f"; {_count(len(kept), 'plate')} kept as {'it is' if len(kept) == 1 else 'they are'}"
        if planned.get("tilts_withheld"):
            summary += f"; {_count(planned['tilts_withheld'], 'tilt')} left out (allow tilting to include)"
    else:
        summary = "Nothing to plan"
    summary += _skipped_failed(record)
    return {"revision": record["revision"], "fingerprint": planned["fingerprint"], "plates": plates, "kept": kept,
            "summary": summary, "outcome": summary, "tilts_withheld": planned.get("tilts_withheld", 0),
            "problems": planned.get("problems", []), "refused": planned.get("refused")}


def plan_plates(project_id: int, allow_tilt: bool = False) -> dict:
    """Plan the project's plates and change nothing: {"revision", "plates": [{"id", "name", "new", "filament", "parts" (how many
    objects it holds; the key keeps its name), "turned", "time_estimate" (approximate seconds, layers, volume_cm3), "objects"}] in plan order (longest first by
    layers, then volume), "kept": [{"id", "name", "reason"}] (plates with a Job or a Print on record, which are never
    touched), "summary", "tilts_withheld", "problems", "refused"}. Only orientations that stand a part on a side of its own box
    are planned; a tilt onto an angled face needs `allow_tilt`. A plan whose layout would leave an object off the bed, over
    the purge area or overlapping is not offered: "plates" is empty, "refused" says why and "problems" lists them.
    `accept_plan` applies it."""
    record = get_project(project_id)
    return _plan_view(record, _plan_data(project_id, record, allow_tilt))


def accept_plan(project_id: int, plan, fingerprint=None, allow_tilt: bool = False) -> dict:
    """Apply the plan as one change: `plan` is the revision it was made at, or the plan itself, and `fingerprint` the plan's
    own (taken from `plan` when that is the plan). A project that has changed since is refused ("changed elsewhere"), and so
    is a plan the server would now make differently ("The plan changed"): what is applied is what was reviewed. Slices of
    replanned plates become Outdated by their fingerprints; plates left empty go. Returns the project (one revision later,
    so the client records one undo step) plus "plan", the view of what was applied."""
    if isinstance(plan, dict):
        revision, fingerprint = plan.get("revision"), plan.get("fingerprint") or fingerprint
    else:
        revision = plan
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        raise jobs.JobError("Send the revision the plan was made at, or the plan itself")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise jobs.JobError("Send the fingerprint of the plan that was reviewed")
    record = get_project(project_id)
    if revision != record["revision"]:
        raise jobs.JobError("The project was changed elsewhere — reload it", 409)
    planned = _plan_data(project_id, record, allow_tilt)
    if planned["fingerprint"] != fingerprint:
        raise jobs.JobError("The plan changed, review it again", 409)
    view = _plan_view(record, planned)
    if planned.get("refused"):
        raise jobs.JobError(view["summary"], 409)
    if not planned["plates"]:
        return {**record, "plan": view}
    _save_data(project_id, record, planned["data"], planned["emptied"])
    return {**get_project(project_id), "plan": view}


def delete_model(project_id: int, model_id: int) -> dict:
    record = get_project(project_id)
    if not any(m["id"] == model_id for m in record["models"]):
        raise jobs.JobError("Model not found", 404)
    with database.transaction() as conn:
        row = conn.execute("SELECT data, models FROM projects WHERE id = ?", (project_id,)).fetchone()
        data, models = json.loads(row["data"]), json.loads(row["models"])
        data["objects"] = [o for o in data["objects"] if o["model"] != model_id]
        if data.get("filaments"):
            data["filaments"].pop(str(model_id), None)
            if not data["filaments"]:
                del data["filaments"]
        models = [m for m in models if m["id"] != model_id]
        conn.execute("UPDATE projects SET data = ?, models = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                     (json.dumps(data), json.dumps(models), time.time(), project_id))
    shutil.rmtree(model_dir(project_id, model_id), ignore_errors=True)
    return get_project(project_id)


def retry_model(project_id: int, model_id: int) -> dict:
    """Re-queue a failed import. The model goes back to importing with no claim, so the workers take it under the
    same atomic rule as a fresh upload."""
    record = get_project(project_id)
    model = next((m for m in record["models"] if m["id"] == model_id), None)
    if model is None:
        raise jobs.JobError("Model not found", 404)
    if model["state"] != FAILED:
        raise jobs.JobError("Only a model that failed to import can be retried", 409)

    def requeue(models):
        for m in models:
            if m["id"] == model_id and m["state"] == FAILED:
                m.update(state=IMPORTING, error=None)
                m.pop("claimed_at", None)
    _mutate(project_id, "models", requeue, record["created_at"])
    return get_project(project_id)


def model_preview(project_id: int, model_id: int) -> Path:
    record = get_project(project_id)
    model = next((m for m in record["models"] if m["id"] == model_id), None)
    path = model_dir(project_id, model_id) / "preview.bin"
    if model is None or model["state"] != READY or not path.is_file():
        raise jobs.JobError("Preview not available", 404)
    return path


def _run_import(project_id: int, model_id: int) -> dict:
    folder = model_dir(project_id, model_id)
    (folder / "error.json").unlink(missing_ok=True)
    with (folder / "engine.log").open("a", encoding="utf-8") as log:
        proc = subprocess.run(engine_command("analyze", folder), stdout=log, stderr=subprocess.STDOUT, cwd=str(splits.APP_DIR),
                              timeout=splits.STAGE_TIMEOUT_S, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        error = splits._read(folder, "error.json") or {}
        raise jobs.JobError(error.get("error") or f"The model couldn't be imported (exit code {proc.returncode})")
    analysis = splits._read(folder, "analysis.json") or {}
    return {"state": READY, "error": None, "size": analysis.get("size"), "triangles": analysis.get("triangles"),
            "volume_cm3": analysis.get("volume_cm3"), "repairs": analysis.get("repairs", [])}


# ── Geometry ───────────────────────────────────────────────────────────────────

def rotation_matrix(degrees):
    """The 3x3 matrix of an object's rotation (degrees about X, then Y, then Z)."""
    import orientation
    return orientation.rotation_matrix(degrees)


def read_mesh_bin(path: Path) -> list:
    import numpy as np
    data = path.read_bytes()
    if data[:4] != b"SPV1":
        raise jobs.JobError("Unexpected preview file")
    meshes, offset = [], 8
    for _ in range(struct.unpack_from("<I", data, 4)[0]):
        vertices, triangles = struct.unpack_from("<II", data, offset)
        offset += 8
        verts = np.frombuffer(data, "<f4", vertices * 3, offset).reshape(-1, 3).astype(float)
        offset += vertices * 12
        tris = np.frombuffer(data, "<u4", triangles * 3, offset).reshape(-1, 3)
        offset += triangles * 12
        meshes.append((verts, tris))
    return meshes


def load_mesh(project_id: int, model_id: int, preview: bool = False) -> tuple:
    import numpy as np
    folder = model_dir(project_id, model_id)
    if preview:
        return read_mesh_bin(folder / "preview.bin")[0]
    with np.load(folder / "model.npz") as data:
        return data["verts"].astype(float), np.asarray(data["tris"])


def place(verts, obj: dict):
    """Model vertices → bed coordinates for an object (see the module docstring)."""
    import numpy as np
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    placed = ((verts - (lo + hi) / 2) * np.asarray(obj["scale"], float)) @ rotation_matrix(obj["rotation"]).T
    lo, hi = placed.min(axis=0), placed.max(axis=0)
    return placed + [obj["position"][0] - (lo[0] + hi[0]) / 2, obj["position"][1] - (lo[1] + hi[1]) / 2, -lo[2]]


def machine_limits(machine: dict) -> dict:
    def points(value):
        out = []
        for item in value or []:
            try:
                x, y = str(item).lower().split("x")
                out.append((float(x), float(y)))
            except ValueError:
                continue
        return out

    area = points(machine.get("printable_area"))
    exclude = points(machine.get("bed_exclude_area"))
    try:
        height = float(machine.get("printable_height") or DEFAULT_HEIGHT)
    except ValueError:
        height = DEFAULT_HEIGHT
    bed = (min(p[0] for p in area), min(p[1] for p in area), max(p[0] for p in area), max(p[1] for p in area)) if area else DEFAULT_BED
    excluded = (min(p[0] for p in exclude), min(p[1] for p in exclude), max(p[0] for p in exclude), max(p[1] for p in exclude)) \
        if len(exclude) >= 3 else (DEFAULT_EXCLUDE if bed == DEFAULT_BED else None)
    return {"bed": bed, "height": height, "exclude": excluded}


def check_fit(name: str, verts, limits: dict) -> None:
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    x0, y0, x1, y1 = limits["bed"]
    if lo[0] < x0 - 0.01 or lo[1] < y0 - 0.01 or hi[0] > x1 + 0.01 or hi[1] > y1 + 0.01:
        raise jobs.JobError(f"{name} reaches past the edge of the bed")
    if hi[2] > limits["height"] + 0.01:
        raise jobs.JobError(f"{name} is taller than the printer can print ({limits['height']:g} mm)")
    if limits["exclude"]:
        ex0, ey0, ex1, ey1 = limits["exclude"]
        inside = (verts[:, 0] > ex0) & (verts[:, 0] < ex1) & (verts[:, 1] > ey0) & (verts[:, 1] < ey1)
        if inside.any():
            raise jobs.JobError(f"{name} is over the nozzle purge area in the front-right corner")


def write_stl(path: Path, verts, tris) -> None:
    import numpy as np
    records = np.zeros(len(tris), dtype=[("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    records["vertices"] = verts[tris]
    with open(path, "wb") as fh:
        fh.write(b"Spooler".ljust(80, b"\0") + struct.pack("<I", len(tris)))
        fh.write(records.tobytes())


def write_plate_3mf(path: Path, project_id: int, objects: list, limits: dict) -> None:
    """One plate as a 3MF: vertices already in bed coordinates, per-object settings in model_settings.config.
    (Don't add OrcaSlicer's Application metadata: the 2.4.2 CLI crashes on such files.)"""
    import numpy as np
    config = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        with archive.open("3D/3dmodel.model", "w") as fh:
            fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
                     b'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n<resources>\n')
            for index, obj in enumerate(objects, start=1):
                verts, tris = load_mesh(project_id, obj["model"])
                placed = place(verts, obj)
                check_fit(obj["name"], placed, limits)
                fh.write(f'<object id="{index}" name={escape_attr(obj["name"])} type="model"><mesh><vertices>\n'.encode())
                for chunk in np.array_split(placed, max(1, len(placed) // 50000)):
                    fh.write("".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in chunk).encode())
                fh.write(b"\n</vertices><triangles>\n")
                for chunk in np.array_split(tris, max(1, len(tris) // 50000)):
                    fh.write("".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in chunk).encode())
                fh.write(b"\n</triangles></mesh></object>\n")
                config.append(f'  <object id="{index}">\n    <metadata key="name" value={escape_attr(obj["name"])}/>\n'
                              '    <metadata key="extruder" value="1"/>\n')
                for key, value in sorted((obj.get("overrides") or {}).items()):
                    config.append(f'    <metadata key={escape_attr(key)} value={escape_attr(value)}/>\n')
                config.append(f'    <part id="1" subtype="normal_part"><metadata key="name" value={escape_attr(obj["name"])}/></part>\n'
                              '  </object>\n')
            fh.write(b"</resources>\n<build>\n")
            for index in range(1, len(objects) + 1):
                fh.write(f'<item objectid="{index}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'.encode())
            fh.write(b"</build>\n</model>\n")
        config.append("</config>\n")
        archive.writestr("Metadata/model_settings.config", "".join(config))


def escape_attr(value) -> str:
    return '"' + escape(str(value), {'"': "&quot;"}) + '"'


# ── Arrange ────────────────────────────────────────────────────────────────────

def arrange_plate(project_id: int, plate_id: int, engine: str | None = None) -> dict:
    """Arrange one plate (see `arrange`); a plate with nothing ready on it is refused."""
    record = get_project(project_id)
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    if not any(o["plate"] == plate_id and o["model"] in ready for o in record["data"]["objects"]):
        raise jobs.JobError("There's nothing ready to arrange on this plate" + _skipped_failed(record, [plate_id]), 409)
    return arrange(project_id, [plate_id], engine)


def _arrange_with_orca(project_id: int, plate_id: int) -> dict:
    """Let OrcaSlicer arrange a plate's unpinned objects (it knows the bed and its exclusion area). Blocking."""
    record = get_project(project_id)
    data = record["data"]
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    objects = [o for o in data["objects"] if o["plate"] == plate_id and o["model"] in ready and not o.get("pinned")]
    if not objects:
        raise jobs.JobError("There's nothing ready to arrange on this plate", 409)
    orca = slicer.require_orca()
    bundle_id = data["profile"]["bundle_id"]
    if not bundle_id:
        raise jobs.JobError("Choose a profile first", 409)
    settings = slicer.bundle_settings(bundle_id)
    with tempfile.TemporaryDirectory(prefix=".arrange-", dir=project_dir(project_id)) as tmp:
        tmp = Path(tmp)
        for kind in slicer.PRESET_KINDS:
            (tmp / f"{kind}.json").write_text(json.dumps(settings[kind]), encoding="utf-8")
        files = []
        for obj in objects:
            verts, tris = load_mesh(project_id, obj["model"], preview=True)
            path = tmp / f"object{obj['id']}.stl"
            write_stl(path, place(verts, {**obj, "position": [0.0, 0.0]}), tris)
            files.append(str(path))
        out = tmp / "arranged.3mf"
        code, _ = slicer.runner([str(orca), "--datadir", str(_data_dir / "slicer-data"), "--arrange", "1",
                                 "--load-settings", f"{tmp / 'machine.json'};{tmp / 'process.json'}",
                                 "--load-filaments", str(tmp / "filament.json"), "--export-3mf", str(out), *files], 300)
        code = code - (1 << 32) if code >= (1 << 31) else code
        if code != 0 or not out.is_file():
            raise jobs.JobError(slicer.explain_exit(code) if code else "OrcaSlicer didn't arrange the plate")
        placements = read_arrangement(out)
    with database.transaction() as conn:
        row = conn.execute("SELECT data FROM projects WHERE id = ?", (project_id,)).fetchone()
        current = json.loads(row["data"])
        for obj in current["objects"]:
            if obj["id"] in placements:
                x, y, turn = placements[obj["id"]]
                obj["position"] = [round(x, 3), round(y, 3)]
                obj["rotation"][2] = round((obj["rotation"][2] + turn) % 360, 3)
        conn.execute("UPDATE projects SET data = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                     (json.dumps(current), time.time(), project_id))
    return get_project(project_id)


def read_arrangement(path: Path) -> dict:
    """object id → (x, y, extra rotation about Z in degrees) from a 3MF exported by `orca-slicer --export-3mf`."""
    with zipfile.ZipFile(path) as archive:
        main = archive.read("3D/3dmodel.model").decode("utf-8", "replace")
    paths = {}
    for obj in re.finditer(r'<object\b[^>]*\bid="(\d+)"[^>]*>(.*?)</object>', main, re.S):
        source = re.search(r'object(\d+)\.stl', obj.group(0))
        if source:
            paths[obj.group(1)] = int(source.group(1))
    placements = {}
    for item in re.finditer(r'<item\b[^>]*objectid="(\d+)"[^>]*transform="([^"]+)"', main):
        object_id = paths.get(item.group(1))
        values = [float(v) for v in item.group(2).split()]
        if object_id is None or len(values) != 12:
            continue
        placements[object_id] = (values[9], values[10], math.degrees(math.atan2(values[1], values[0])))
    return placements


# ── Slicing a plate ────────────────────────────────────────────────────────────

def _queued_entry(previous: dict | None, queued_at: float, send: bool = False, send_opts: dict | None = None) -> dict:
    """A plate waiting to be sliced. `last_duration_ms` (how long it took before) lets the page show an expected time."""
    previous = previous or {}
    last = previous.get("duration_ms", previous.get("last_duration_ms"))
    return {"state": QUEUED, "queued_at": queued_at, **({"last_duration_ms": last} if last is not None else {}),
            **({"auto_send": True} if send else {}), **({"send_opts": send_opts} if send and send_opts else {})}


def _require_printers(record: dict, plate_ids) -> None:
    """Slice & send needs a printer on every plate it will send."""
    missing = [p["name"] for p in record["data"]["plates"] if p["id"] in plate_ids and not p.get("printer_id")]
    if missing:
        raise jobs.JobError(f"Choose a printer for {_names(missing)} first", 409)


def _stage(project_id: int, plate_id: int, stage: str) -> None:
    """Tell the page what a running slice is doing (preparing → slicing → post-processing → analysing)."""
    def change(slices):
        entry = slices.get(str(plate_id))
        if entry and entry.get("state") == SLICING:
            entry.update(stage=stage, stage_at=time.time())
    _mutate(project_id, "slices", change)


def _require_one_filament(record: dict, plate_ids) -> None:
    """A plate that mixes filament choices can't be sliced."""
    for plate in record["data"]["plates"]:
        if plate["id"] in plate_ids:
            mix = plate_mix(record["data"], plate["id"])
            if mix:
                raise jobs.JobError(mix, 409)


def request_slice(project_id: int, plate_id: int, send: bool = False, send_opts: dict | None = None) -> dict:
    """Slice one plate. With `send`, the finished plate goes to the queue for its own printer."""
    record = get_project(project_id)
    if plate_id not in {p["id"] for p in record["data"]["plates"]}:
        raise jobs.JobError("Plate not found", 404)
    if not any(o["plate"] == plate_id for o in record["data"]["objects"]):
        raise jobs.JobError("The plate is empty", 409)
    if record["slices"].get(str(plate_id), {}).get("state") in (QUEUED, SLICING):
        raise jobs.JobError("The plate is already being sliced", 409)
    _require_one_filament(record, {plate_id})
    if send:
        _require_printers(record, {plate_id})
    _mutate(project_id, "slices", lambda s: s.__setitem__(str(plate_id), _queued_entry(s.get(str(plate_id)), time.time(), send, send_opts)))
    notify_worker()
    return get_project(project_id)


def _plate_list(plate_ids) -> set | None:
    if plate_ids is None:
        return None
    try:
        return {int(i) for i in plate_ids}
    except (TypeError, ValueError):
        raise jobs.JobError("plates must be a list of plate numbers")


def request_slices(project_id: int, plate_ids=None, send: bool = False) -> dict:
    """Slice every plate (or the listed plates) that has objects and isn't sliced up to date or slicing already."""
    record = get_project(project_id)
    wanted = _plate_list(plate_ids)
    todo = []
    for plate in record["data"]["plates"]:
        entry = record["slices"].get(str(plate["id"])) or {}
        if (wanted is None or plate["id"] in wanted) and any(o["plate"] == plate["id"] for o in record["data"]["objects"]) \
                and entry.get("state") not in (QUEUED, SLICING) and not (entry.get("state") == SLICED and not entry.get("outdated")):
            todo.append(plate["id"])
    if not todo:
        raise jobs.JobError("Nothing to slice — every plate is empty, already sliced or slicing", 409)
    _require_one_filament(record, set(todo))
    if send:
        _require_printers(record, set(todo))
    now = time.time()

    def mark(slices):
        for index, plate_id in enumerate(todo):
            slices[str(plate_id)] = _queued_entry(slices.get(str(plate_id)), now + index / 1000, send)  # sliced in plate order
    _mutate(project_id, "slices", mark)
    notify_worker()
    return get_project(project_id)


def _slice(project_id: int, plate_id: int, slot: int = 0) -> dict:
    """Build the plate's 3MF and profile, slice it and analyse the G-code. Blocking.
    Concurrent slices each get their own OrcaSlicer data directory (`slot`), so their caches and logs don't collide."""
    record = get_project(project_id)
    data = record["data"]
    plate = next((p for p in data["plates"] if p["id"] == plate_id), None)
    if plate is None:
        raise jobs.JobError("The plate was removed")
    models = {m["id"]: m for m in record["models"]}
    objects = [o for o in data["objects"] if o["plate"] == plate_id]
    if not objects:
        raise jobs.JobError("The plate is empty")
    waiting = [o["name"] for o in objects if models.get(o["model"], {}).get("state") != READY]
    if waiting:
        raise jobs.JobError(f"Still importing: {', '.join(waiting)}")
    if not data["profile"]["bundle_id"]:
        raise jobs.JobError("Choose a profile first")
    _require_one_filament(record, {plate_id})
    _stage(project_id, plate_id, "preparing")
    stamp = fingerprint(record, plate_id)
    settings = slicer.bundle_settings(data["profile"]["bundle_id"])
    slicer.verify_bundle(settings["bundle"])
    orca = slicer.require_orca()
    process = {**settings["process"], **data["profile"]["process"], **(plate.get("overrides") or {}),
               "curr_bed_type": slicer.BED_TYPES[data["bed"]]}
    bricks = plate_bricklayers(data, plate)
    if bricks:
        process.update(brick_post.REQUIRED_PROCESS)
    filament = {**settings["filament"], **data["profile"]["filament"]}
    chosen = next((c for c in plate_filaments(data, plate_id) if c), None)
    if chosen:  # the profile's filament changes tweak the default preset, so they don't carry over to another one
        filament, _ = slicer.flatten_preset(slicer._presets("filament", orca), chosen["preset"])
        machine = settings["machine"]["name"]
        if filament.get("type") != "filament" or not slicer._compatible(filament, machine):
            raise jobs.JobError(f"“{chosen['preset']}” isn't a filament preset for {machine}")

    work = plate_dir(project_id, plate_id)
    shutil.rmtree(work, ignore_errors=True)
    (work / "profile").mkdir(parents=True)
    (work / "output").mkdir()
    for kind, values in (("machine", settings["machine"]), ("process", process), ("filament", filament)):
        (work / "profile" / f"{kind}.json").write_text(json.dumps(values, indent=1, sort_keys=True), encoding="utf-8")
    write_plate_3mf(work / "plate.3mf", project_id, objects, machine_limits(settings["machine"]))

    args = [str(orca), "--datadir", str(_data_dir / ("slicer-data" if slot == 0 else f"slicer-data-{slot}")), "--slice", "0", "--arrange", "0",
            "--load-settings", f"{work / 'profile' / 'machine.json'};{work / 'profile' / 'process.json'}",
            "--load-filaments", str(work / "profile" / "filament.json"), "--outputdir", str(work / "output"), str(work / "plate.3mf")]
    _stage(project_id, plate_id, "slicing")
    started = time.monotonic()
    code, text = slicer.runner(args, slicer.SLICE_TIMEOUT_S)
    duration_ms = int((time.monotonic() - started) * 1000)
    code = code - (1 << 32) if code >= (1 << 31) else code
    (work / "slicer.log").write_text(f"$ {subprocess.list2cmdline(args)}\nexit code {code} after {duration_ms} ms\n{text}", encoding="utf-8")
    if code != 0:
        raise slicer.SliceError(slicer.explain_exit(code), code)
    produced = sorted((work / "output").glob("plate_*.gcode"))
    if not produced:
        raise slicer.SliceError("OrcaSlicer finished without producing G-code", code)
    gcode = work / "plate.gcode"
    if bricks:
        _stage(project_id, plate_id, "post-processing")
        orca_gcode = work / "plate.orca.gcode"
        os.replace(produced[0], orca_gcode)
        started = time.monotonic()
        try:
            brick_args, brick_text = brick_post.run(orca_gcode, gcode, bricks)
        finally:
            duration_ms += int((time.monotonic() - started) * 1000)
        with (work / "slicer.log").open("a", encoding="utf-8") as log:
            log.write(f"\n$ {subprocess.list2cmdline(brick_args)}\n{brick_text}")
    else:
        os.replace(produced[0], gcode)
    _stage(project_id, plate_id, "analysing")
    add_thumbnails(project_id, objects, settings["machine"], gcode, work / "slicer.log")
    analysis, toolpath = gcode_analysis.analyze(gcode)
    (work / "toolpath.bin").write_bytes(toolpath)
    (work / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    estimates = analysis.get("estimates") or {}
    try:
        time_estimate.record_slice(project_id, plate_id, plate_features(project_id, record, plate_id), analysis)
    except sqlite3.Error as e:   # the log must never fail a slice
        print(f"[Projects] Could not log the slice of plate {plate_id} of project {project_id}: {e}")
    return {"state": SLICED, "error": None, "fingerprint": stamp, "sliced_at": time.time(), "duration_ms": duration_ms,
            "estimate_s": estimates.get("estimate_s"), "estimate_g": estimates.get("estimate_g"),
            "layers": len(analysis["layers"]), "slicer": analysis.get("slicer"), "gcode_bytes": gcode.stat().st_size,
            "bricklayers": bricks,
            "findings": [f for f in analysis.get("findings", []) if f["level"] == "fail"][:5]}


def add_thumbnails(project_id: int, objects: list, machine: dict, gcode: Path, log: Path) -> None:
    """Embed the picture the printer shows for this G-code. A failure is logged, never fatal to the slice."""
    try:
        meshes = []
        for obj in objects:
            verts, tris = load_mesh(project_id, obj["model"], preview=True)
            meshes.append((place(verts, obj), tris))
        count = thumbnails.add_to_gcode(gcode, machine, meshes)
        note = f"embedded {count} thumbnail(s)" if count else "no thumbnail embedded"
    except Exception as e:  # the picture is a nicety
        note = f"thumbnail failed: {e}"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[Spooler] {note}\n")


def plate_file(project_id: int, plate_id: int, kind: str) -> Path:
    record = get_project(project_id)
    if record["slices"].get(str(plate_id), {}).get("state") != SLICED:
        raise jobs.JobError("The plate hasn't been sliced", 404)
    path = plate_dir(project_id, plate_id) / {"toolpath": "toolpath.bin", "gcode": "plate.gcode", "analysis": "analysis.json"}[kind]
    if not path.is_file():
        raise jobs.JobError("The sliced files are missing — slice the plate again", 404)
    return path


_previews: dict = {}  # (project, plate) → (fingerprint, PNG), so the page's polling doesn't render again
PREVIEW_SIZE = (320, 240)


def plate_preview(project_id: int, plate_id: int) -> bytes:
    """A picture of the plate as laid out: its parts placed on the bed, drawn the way the printer's own thumbnail is."""
    record = get_project(project_id)
    if not any(p["id"] == plate_id for p in record["data"]["plates"]):
        raise jobs.JobError("The plate was removed", 404)
    stamp = fingerprint(record, plate_id)
    cached = _previews.get((project_id, plate_id))
    if cached and cached[0] == stamp:
        return cached[1]
    ready = {m["id"] for m in record["models"] if m.get("state") == READY}
    meshes, complete = [], True
    for obj in record["data"].get("objects", []):
        if obj["plate"] == plate_id and obj["model"] in ready:
            try:
                verts, tris = load_mesh(project_id, obj["model"], preview=True)
            except (OSError, jobs.JobError):
                complete = False
                continue
            meshes.append((place(verts, obj), tris))
    png = thumbnails.render(meshes, *PREVIEW_SIZE)
    if complete:
        _previews[(project_id, plate_id)] = (stamp, png)
    return png


def plate_analysis(project_id: int, plate_id: int) -> dict:
    analysis = json.loads(plate_file(project_id, plate_id, "analysis").read_text(encoding="utf-8"))
    analysis.pop("occupancy", None)
    return analysis


def _names(names: list) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _open_job_plates(project_id: int) -> set:
    """Plates of this project with a job still waiting or printing in the queue."""
    with database.transaction() as conn:
        rows = conn.execute("SELECT plate_id FROM jobs WHERE project_id = ? AND state IN ('queued', 'starting', 'printing')",
                            (project_id,)).fetchall()
    return {r["plate_id"] for r in rows if r["plate_id"] is not None}


def _send_plate(record: dict, plate: dict, body: dict, batch_id: str) -> dict:
    entry = record["slices"][str(plate["id"])]
    gcode = plate_file(record["id"], plate["id"], "gcode")
    with gcode.open("rb") as fh:
        job = jobs.create_upload_job(fh, gcode.stat().st_size, safe_gcode_name(f"{record['name']}_{plate['name']}.gcode"),
                                     body.get("printer_id") or plate.get("printer_id") or None)
    models = {m["id"]: m for m in record["models"]}
    parts = sorted({((models.get(o["model"]) or {}).get("split") or {}).get("part") for o in record["data"]["objects"]
                    if o["plate"] == plate["id"]} - {None, 0})
    notes = f"{record['name']} — {plate['name']}" + (f": part{'s' if len(parts) != 1 else ''} {', '.join(map(str, parts))}" if parts else "")
    options = {"smooth_plate": record["data"]["bed"] == "smooth", "leveling": bool(body.get("leveling")),
               "timelapse": bool(body.get("timelapse"))}
    jobs.update_job(job["id"], {"notes": notes, "print_opts": options})
    with database.transaction() as conn:
        conn.execute("UPDATE jobs SET project_id = ?, plate_id = ?, batch_id = ? WHERE id = ?", (record["id"], plate["id"], batch_id, job["id"]))
    jobs.log_event(job["id"], "sliced", f"Slicer project “{record['name']}”, {plate['name']}, {entry.get('slicer') or 'OrcaSlicer'}")
    return jobs.get_job(job["id"])


def queue_plates(project_id: int, body: dict) -> dict:
    """Send sliced plates — the listed ones, or every plate with objects — to the print queue as one batch.
    Plates already waiting or printing in the queue are refused unless `again` is set (to print another copy)."""
    record = get_project(project_id)
    data = record["data"]
    wanted = _plate_list(body.get("plates"))
    if wanted is None:
        plates = [p for p in data["plates"] if any(o["plate"] == p["id"] for o in data["objects"])
                  or (record["slices"].get(str(p["id"])) or {}).get("imported")]
    else:
        plates = [p for p in data["plates"] if p["id"] in wanted]
        if len(plates) != len(wanted):
            raise jobs.JobError("Plate not found", 404)
    if not plates:
        raise jobs.JobError("There are no plates with objects to send", 409)
    if body.get("printer_id"):
        jobs._require_printer(body["printer_id"])
    unsliced = [p["name"] for p in plates if (record["slices"].get(str(p["id"])) or {}).get("state") != SLICED]
    if unsliced:
        raise jobs.JobError(f"Slice {_names(unsliced)} first", 409)
    outdated = [p["name"] for p in plates if record["slices"][str(p["id"])].get("outdated")]
    if outdated:
        one = len(outdated) == 1
        raise jobs.JobError(f"{_names(outdated)} changed after {'it was' if one else 'they were'} sliced — slice {'it' if one else 'them'} again", 409)
    if not body.get("again"):
        repeats = [p["name"] for p in plates if p["id"] in _open_job_plates(project_id)]
        if repeats and record["quick"]:      # a quick print's user never sees plates
            raise jobs.JobError("This print is already waiting in the queue — send again only to print another copy", 409)
        if repeats:
            raise jobs.JobError(f"{_names(repeats)} {'is' if len(repeats) == 1 else 'are'} already in the queue — "
                                "send again only to print another copy", 409)
    batch_id = f"{project_id}-{int(time.time() * 1000)}"
    return {"batch_id": batch_id, "jobs": [_send_plate(record, plate, body, batch_id) for plate in plates]}


def queue_plate(project_id: int, plate_id: int, body: dict) -> dict:
    """Put one sliced plate's G-code in the print queue."""
    return queue_plates(project_id, {**body, "plates": [plate_id]})["jobs"][0]


def project_jobs(project_id: int) -> list:
    """Every queue job sent from this project, oldest first."""
    get_project(project_id)
    with database.transaction() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()
    return [jobs._to_job(row) for row in rows]


# ── Plate status and project progress ─────────────────────────────────────────

PLATE_STATUSES = ("unsliced", "sliced", "waiting", "queued", "printing", "printed_ok", "failed")


def plate_status(project_id: int) -> dict:
    """Each plate's status and the project's progress, worked out from the slices, the queue and print history.

    printing → a Job is starting or printing, or a Print is open; waiting → a queued Job has a hold (on another Job or
    plate) that isn't satisfied yet; queued → a queued Job; otherwise the latest finished Print decides (completed → printed_ok,
    cancelled or failed → failed); otherwise sliced when the plate has a Slice, else unsliced."""
    record = get_project(project_id)
    with database.transaction() as conn:
        job_rows = conn.execute("SELECT id, plate_id, state, printer_id FROM jobs WHERE project_id = ? AND plate_id IS NOT NULL "
                                "AND state IN ('queued', 'starting', 'printing')", (project_id,)).fetchall()
        print_rows = conn.execute("SELECT plate_id, outcome, filament_g FROM prints WHERE project_id = ? AND plate_id IS NOT NULL "
                                  "ORDER BY started_at, id", (project_id,)).fetchall()
    latest = {}  # plate id → outcome of its latest Print (None while it is still printing)
    consumed = {}  # plate id → grams its finished Prints used, as the printer counted them
    for row in print_rows:
        if row["outcome"] in ("completed", "failed", "cancelled") and row["filament_g"]:
            consumed[row["plate_id"]] = consumed.get(row["plate_id"], 0.0) + row["filament_g"]
        if row["outcome"] == "unknown":
            continue
        latest[row["plate_id"]] = row["outcome"]
    printing = {r["plate_id"] for r in job_rows if r["state"] in ("starting", "printing")} | {n for n, o in latest.items() if o is None}
    queued = {r["plate_id"] for r in job_rows if r["state"] == "queued"}
    waiting = {r["plate_id"] for r in job_rows if r["state"] == "queued" and holds.waiting_on(r["id"])}

    open_job = {}  # plate id → its newest open Job
    for row in job_rows:
        if row["plate_id"] not in open_job or row["id"] > open_job[row["plate_id"]]["id"]:
            open_job[row["plate_id"]] = row
    parts = {}
    for obj in record["data"].get("objects", []):
        parts[obj.get("plate")] = parts.get(obj.get("plate"), 0) + 1

    plates, counts = [], {status: 0 for status in PLATE_STATUSES}
    filament = {"used": 0.0, "total": 0.0, "unknown_plates": 0}
    by_choice = {}   # filament choice → its plates' totals; None is the project default
    for plate in record["data"]["plates"]:
        n = plate["id"]
        entry = record["slices"].get(str(n)) or {}
        if n in printing:
            status = "printing"
        elif n in queued:
            status = "waiting" if n in waiting else "queued"
        elif latest.get(n) == "completed":
            status = "printed_ok"
        elif latest.get(n) in ("failed", "cancelled"):
            status = "failed"
        elif entry.get("state") == SLICED:
            status = "sliced"
        else:
            status = "unsliced"
        counts[status] += 1
        sliced = entry.get("state") == SLICED
        grams = entry.get("estimate_g") if sliced else None
        used = consumed[n] if n in consumed else (grams or 0.0) if status == "printed_ok" else 0.0   # what the printer counted wins;
        # a plate printed OK with no count falls back to its estimate
        if grams:
            filament["total"] += grams
        else:
            filament["unknown_plates"] += 1  # not sliced yet, so its filament isn't in the total
        filament["used"] += used
        found = plate_filaments(record["data"], n)
        mixed = plate_mix(record["data"], n)
        if found and not mixed:   # a mixed plate belongs to no single choice
            group = by_choice.setdefault(_choice_key(found[0]), {"choice": found[0], "total": 0.0, "used": 0.0, "plates": 0, "unknown_plates": 0})
            group["plates"] += 1
            group["total"] += grams or 0.0
            group["used"] += used
            group["unknown_plates"] += 0 if grams else 1
        job = open_job.get(n)
        plates.append({"id": n, "name": plate.get("name"), "status": status,
                       "outdated": bool(entry.get("outdated")) if sliced else False,
                       "mixed": mixed, "filament": choice_label(found[0]) if found and not mixed else None,
                       "parts": parts.get(n, 0), "layout": fingerprint(record, n), "job_id": job["id"] if job else None,
                       "printer_id": (job["printer_id"] if job else None) or plate.get("printer_id"),
                       "estimate_s": entry.get("estimate_s") if sliced else None, "estimate_g": grams,
                       "time_estimate": plate_time(project_id, record, n, entry)})
    total = len(plates)
    return {"project_id": project_id, "plates": plates,
            "print_order": [p["id"] for p in time_estimate.longest_first(plates, lambda p: p["time_estimate"])],
            "progress":{"total": total, "counts": counts, "printed_ok": counts["printed_ok"],
                         "percent": round(100 * counts["printed_ok"] / total) if total else 0,
                         "filament_g": {"used": round(filament["used"], 1), "total": round(filament["total"], 1),
                                        "unknown_plates": filament["unknown_plates"], "spool": _spool_level(record),
                                        "by_filament": [{"label": choice_label(g["choice"]), "default": g["choice"] is None,
                                                         "preset": g["choice"]["preset"] if g["choice"] else None,
                                                         "colour": g["choice"].get("colour", "") if g["choice"] else None,
                                                         "plates": g["plates"], "total": round(g["total"], 1), "used": round(g["used"], 1),
                                                         "unknown_plates": g["unknown_plates"]}
                                                        for g in sorted(by_choice.values(), key=lambda g: (g["choice"] is not None, choice_label(g["choice"])))]}}}


def _spool_level(record: dict) -> dict | None:
    """The Spoolman spool chosen for the project and what is left on it (None values when Spoolman can't say)."""
    spool_id = (record["data"].get("profile") or {}).get("spool_id")
    if not spool_id:
        return None
    level = spoolman.spool_level(spool_id) or {}
    return {"id": spool_id, "name": level.get("name"), "remaining_g": level.get("remaining_g")}


def reprint_plate(project_id: int, plate_id: int) -> dict:
    """Queue a failed plate again by re-queuing its newest failed Job (the whole plate, even if it only partly failed)."""
    status = {p["id"]: p["status"] for p in plate_status(project_id)["plates"]}
    if plate_id not in status:
        raise jobs.JobError("Plate not found", 404)
    if status[plate_id] != "failed":
        raise jobs.JobError("Only a failed plate can be reprinted", 409)
    with database.transaction() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE project_id = ? AND plate_id = ? AND state IN ('failed', 'cancelled', 'completed') "
                           "ORDER BY COALESCE(finished_at, updated_at) DESC, id DESC LIMIT 1", (project_id, plate_id)).fetchone()
    if row is None:
        raise jobs.JobError("The G-code for this plate is no longer stored — slice it again", 410)
    return jobs.requeue(row["id"])


# ── Background worker ──────────────────────────────────────────────────────────

def _claim() -> tuple | None:
    """The next import or slice to run, marked as running."""
    with database.transaction() as conn:
        rows = conn.execute("SELECT id, models, slices, created_at FROM projects ORDER BY updated_at, id").fetchall()
        for row in rows:
            models = json.loads(row["models"] or "[]")
            for model in models:
                if model["state"] == IMPORTING and not model.get("claimed_at"):
                    model["claimed_at"] = time.time()  # taken: no other worker analyses it too
                    conn.execute("UPDATE projects SET models = ? WHERE id = ?", (json.dumps(models), row["id"]))
                    return "import", row["id"], model["id"], row["created_at"]
        for row in rows:
            entries = json.loads(row["slices"] or "{}")
            for plate_id, entry in sorted(entries.items(), key=lambda kv: kv[1].get("queued_at", 0)):
                if entry.get("state") == QUEUED:
                    entry.update(state=SLICING, started_at=time.time())
                    conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(entries), row["id"]))
                    return "slice", row["id"], int(plate_id), row["created_at"]
    return None


async def process_next(slot: int = 0) -> bool:
    task = _claim()
    if task is None:
        return False
    kind, project_id, item, created_at = task
    loop = asyncio.get_running_loop()
    if kind == "import":
        try:
            result = await loop.run_in_executor(None, _run_import, project_id, item)
        except Exception as e:
            result = {"state": FAILED, "error": str(e) or type(e).__name__}
            print(f"[Projects] Import of model {item} in project {project_id} failed: {e}")

        def update(models):
            for model in models:
                if model["id"] == item:
                    model.pop("claimed_at", None)
                    model.update(result)
        _mutate(project_id, "models", update, created_at)
    else:
        try:
            queued = get_project(project_id)["slices"].get(str(item), {})
            auto, send_opts = bool(queued.get("auto_send")), queued.get("send_opts") or {}
        except jobs.JobError:
            auto, send_opts = False, {}
        try:
            result = await loop.run_in_executor(None, _slice, project_id, item, slot)
        except Exception as e:
            result = {"state": FAILED, "error": str(e) or type(e).__name__, "finished_at": time.time()}
            print(f"[Projects] Slicing plate {item} of project {project_id} failed: {e}")
        if auto and result.get("state") == SLICED:
            result["auto_send"] = True   # the page keeps checking until the plate is in the queue (or says why not)
        _mutate(project_id, "slices", lambda s: s.__setitem__(str(item), result), created_at)
        if auto and result.get("state") == SLICED:
            await loop.run_in_executor(None, _auto_send, project_id, item, created_at, send_opts)
            await jobs.broadcast()
    return True


def _auto_send(project_id: int, plate_id: int, created_at: float, send_opts: dict | None = None) -> None:
    """Slice & send: put the finished plate in the queue for its printer, and note the job (or why it couldn't be sent)."""
    try:
        send_opts = send_opts or {}
        job = queue_plate(project_id, plate_id, {"leveling": send_opts.get("leveling"), "timelapse": send_opts.get("timelapse")})
        if send_opts.get("acknowledge"):
            jobs.acknowledge_warnings(job["id"], job.get("printer_id"))
        outcome = {"sent_job": job["id"], "sent_to": job.get("printer_id")}
    except jobs.JobError as e:
        outcome = {"send_error": str(e)}
        print(f"[Projects] Plate {plate_id} of project {project_id} sliced but not sent: {e}")
    _mutate(project_id, "slices", lambda s: s.get(str(plate_id)) is not None and s[str(plate_id)].update(outcome), created_at)


async def _slot_loop(slot: int) -> None:
    while True:
        _wakeup.clear()
        try:
            if await process_next(slot):
                continue
        except Exception as e:  # the worker must survive anything a single task throws
            print(f"[Projects] Worker {slot} error: {e}")
        try:
            await asyncio.wait_for(_wakeup.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


async def worker_loop() -> None:
    """Background workers: model imports and plate slices, SLICE_WORKERS at a time."""
    global _wakeup
    _wakeup = asyncio.Event()
    if SLICE_WORKERS > 1:
        print(f"[Projects] Slicing up to {SLICE_WORKERS} plates at once")
    await asyncio.gather(*(_slot_loop(slot) for slot in range(SLICE_WORKERS)))


def notify_worker() -> None:
    loop = state.loop
    if _wakeup is not None and loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_wakeup.set)
