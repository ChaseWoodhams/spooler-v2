"""
Slicer workspace projects: models placed on plates, profile settings you can tweak, per-object setting
overrides, slicing a plate with OrcaSlicer, and the sliced result for the layer-by-layer preview.

Files (DATA_DIR/projects/<id>/)
  models/<model id>/  source/<file>, job.json, model.npz, preview.bin, analysis.json — made by `split_engine.py analyze`
  plates/<plate id>/  plate.3mf, profile/*.json, plate.gcode, analysis.json, toolpath.bin, slicer.log,
                      plate.orca.gcode (OrcaSlicer's own G-code, when BrickLayers post-processed plate.gcode)

Record
  split_id  the split model the project was started from, if any; its parts (or the whole model) are laid out on plates
  split_ids (read-only) every split the project owns: split_id first, then the rest from the project_splits table — a drop of
            several oversized models is one project with one split each
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

import copy
import asyncio
import hashlib
import io
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
import uuid
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import brick_post
import database
import effective_settings as plate_settings
import export_lossy as lossy_report
import export_plan as export_planner
import gcode_analysis
import holds
import observed_output
import jobs  # used as a module: jobs.JobError, jobs.create_upload_job, …
import orca_3mf
import preflight
import packing
import problem_kinds
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
    sweep_import_staging(startup=True)    # a review that was being prepared when Spooler stopped
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

INPUTS_VERSION = 2   # Slices carrying inputs.json and a canonical print-input fingerprint; older Slices keep the raw hash below


def fingerprint(record: dict, plate_id: int) -> str:
    """A cheap hash of the raw stored inputs of a plate. It judges Slices made before saved inputs existed and keys caches;
    current Slices are judged by print_fingerprint."""
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
        payload["filament"] = [{"preset": c["preset"], "colour": c.get("colour", ""), **({"sha256": c["snapshot"]["sha256"]} if c.get("snapshot") else {})} if c else None for c in chosen]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


_print_fingerprints: dict = {}   # raw-input hash → canonical print-input fingerprint (a pure function of locked, content-addressed inputs)


def print_fingerprint(record: dict, plate_id: int) -> str | None:
    """The canonical fingerprint of the effective print inputs of a Plate (see effective_settings.canonical_inputs), or None when
    they cannot be resolved right now (a missing/damaged profile or Filament snapshot, a mixed Plate). Never raises."""
    raw = fingerprint(record, plate_id)
    if raw in _print_fingerprints:
        return _print_fingerprints[raw]
    try:
        result = _resolve_plate(record, plate_id)[0]
    except (jobs.JobError, KeyError, OSError, ValueError):
        return None
    if any(i.get("code") in ("unresolved-filament", "conflicting-filament-choices") for i in result["issues"]):
        return None
    if len(_print_fingerprints) > 2000:
        _print_fingerprints.clear()
    _print_fingerprints[raw] = result["fingerprint"]
    return result["fingerprint"]


def _choice_key(choice: dict | None) -> tuple | None:
    return (choice["preset"], choice.get("colour", ""), (choice.get("snapshot") or {}).get("sha256")) if choice else None


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
    record["draft"] = bool(record.get("draft"))
    with database.transaction() as conn:
        extra = [r["split_id"] for r in conn.execute("SELECT split_id FROM project_splits WHERE project_id = ? ORDER BY rowid", (record["id"],))]
    record["split_ids"] = list(dict.fromkeys(([record["split_id"]] if record.get("split_id") is not None else []) + extra))
    record["server_time"] = time.time()  # lets a browser with a different clock show how long a slice has been running
    for plate in record["data"].get("plates", []):
        entry = record["slices"].get(str(plate["id"]))
        if entry and entry.get("state") == SLICED:
            if entry.get("imported"):
                entry["outdated"] = False
            elif entry.get("inputs_version") == INPUTS_VERSION:
                current = print_fingerprint(record, plate["id"])
                entry["outdated"] = current != entry.get("fingerprint")
                if current is None:
                    entry["outdated_reason"] = "inputs-unresolved"
            else:   # a Slice from before inputs were saved: judged by the raw hash it was made with
                entry["outdated"] = entry.get("fingerprint") != fingerprint(record, plate["id"])
    return record


def get_project(project_id: int) -> dict:
    with database.transaction() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise jobs.JobError("Project not found", 404)
    return _to_project(row)


def list_projects(include_quick: bool = False) -> list:
    """Projects, newest first. Quick projects (hidden behind Quick print) are left out unless `include_quick`;
    drafts (models uploaded before Create) are always left out."""
    with database.transaction() as conn:
        rows = conn.execute("SELECT * FROM projects WHERE draft = 0" + ("" if include_quick else " AND quick = 0")
                            + " ORDER BY updated_at DESC, id DESC").fetchall()
        open_jobs = {}   # project id → {job state → how many}: what the Projects page shows beside each project
        for row in conn.execute("SELECT project_id, state, COUNT(*) AS n FROM jobs WHERE project_id IS NOT NULL "
                                "AND state IN ('queued', 'starting', 'printing') GROUP BY project_id, state"):
            open_jobs.setdefault(row["project_id"], {})[row["state"]] = row["n"]
    out = []
    for record in map(_to_project, rows):
        data = record["data"]
        slices = [s.get("state") for s in record["slices"].values()]
        jobs_here = open_jobs.get(record["id"], {})
        out.append({"id": record["id"], "name": record["name"], "updated_at": record["updated_at"], "created_at": record["created_at"],
                    "split_id": record.get("split_id"), "quick": record["quick"],
                    "plates": len(data.get("plates", [])), "objects": len(data.get("objects", [])), "models": len(record["models"]),
                    "sliced": slices.count(SLICED), "slicing": sum(s in (QUEUED, SLICING) for s in slices),
                    "queued": jobs_here.get("queued", 0), "printing": jobs_here.get("starting", 0) + jobs_here.get("printing", 0),
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


def create_project(name: str = "", split_id: int | None = None, quick: bool = False, draft: bool = False) -> dict:
    """A project with one empty plate. With `split_id` it belongs to that split model and lays out its parts.
    A `quick` project is hidden from project listings; it is reached by id. A `draft` is hidden too, until
    create_draft promotes it, and is swept when abandoned."""
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
    if remembered.get("pack"):   # the density last chosen for a project
        data["pack"] = remembered["pack"]
    now = time.time()
    with database.transaction() as conn:
        project_id = conn.execute("INSERT INTO projects (name, data, split_id, quick, draft, created_at, updated_at) "
                                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (_clean_name(name) or "Untitled project", json.dumps(data), split_id, int(bool(quick)), int(bool(draft)),
                                   now, now)).lastrowid
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
        result = _import_directly(None, stream, length, filename, scale=scale, up=up, quick=quick)
        record = result["import_record"]
        if quick and record["kind"] == "project":
            counts = [f"{record['objects']} objects" if record["objects"] > 1 else None,
                      f"{record['plates']} plates" if record["plates"] > 1 else None]
            result["routed"] = {"to": "project", "reason": f"The file has {' on '.join(c for c in counts if c)}, so it opens as a project"}
        return result
    split = splits.create_split(stream, length, filename, {"scale": scale, "up": up})
    return create_project(Path(split["source_name"]).stem, split_id=split["id"], quick=quick)


def delete_project(project_id: int, only_draft_idle_before: float | None = None, only_draft: bool = False) -> None:
    """Delete the project and its files, and each of its split models unless another project uses that split too.
    With `only_draft` the row goes only if it is still a draft (and, with `only_draft_idle_before`, still idle since then),
    decided in the same transaction as the delete so a Create that lands meanwhile keeps its project."""
    record = get_project(project_id)
    only_draft = only_draft or only_draft_idle_before is not None
    with database.transaction() as conn:
        sql, args = "DELETE FROM projects WHERE id = ?", [project_id]
        if only_draft:
            sql += " AND draft = 1"
        if only_draft_idle_before is not None:
            sql += " AND updated_at < ?"
            args.append(only_draft_idle_before)
        if conn.execute(sql, args).rowcount == 0 and only_draft:
            raise jobs.JobError(DRAFT_GONE, 409)
        doomed = [i for i in record["split_ids"]
                  if not conn.execute("SELECT 1 FROM projects WHERE split_id = ? UNION SELECT 1 FROM project_splits WHERE split_id = ?",
                                      (i, i)).fetchone()]
    shutil.rmtree(project_dir(project_id), ignore_errors=True)
    for split_id in doomed:
        try:
            splits.delete_split(split_id)
        except jobs.JobError:
            pass  # already deleted


# ── Drafts ─────────────────────────────────────────────────────────────────────

def draft_idle_hours() -> float:
    """How long a draft may sit untouched before the sweep deletes it (DRAFT_IDLE_HOURS, default 24)."""
    try:
        return max(0.0, float(os.getenv("DRAFT_IDLE_HOURS", "24")))
    except ValueError:
        return 24.0


DRAFT_GONE = "This new project was already created or cleared away. Start again."


def require_draft(project_id: int, token) -> dict:
    """The new-project table's guard: the project must still be the very draft the page made. SQLite reuses ids, so a tab
    holding a swept draft's id is told so (409) rather than editing whatever project took the id; `token` is the draft's
    created_at as the page received it."""
    record = get_project(project_id)
    try:
        same = float(token) == record["created_at"]
    except (TypeError, ValueError):
        same = False
    if not record["draft"] or not same:
        raise jobs.JobError(DRAFT_GONE, 409)
    return record


def _create_blocker(rows: list, summary: dict) -> str:
    """Why a draft can't be created yet, in plain words ("" when it can)."""
    if summary["analysing"]:
        return "Wait for the models to be checked."
    if summary["failed"]:
        return "Retry or remove the model that couldn't be read."
    if summary["check_units"]:
        return f"Check the size of {summary['check_units']} model{'s' if summary['check_units'] != 1 else ''} marked Check units."
    return "" if rows else "Add a model first."


def create_draft(project_id: int, name: str, arrange: bool = True) -> dict:
    """Create: give the draft its final name and make it a normal, visible project. Returns the project and the first
    step to open: `model` while any of its split models is not yet placed, else `plates`. Every row too big for the bed
    becomes a split of its own first (make_split); the fits-whole models stay where they are. The conversion comes first
    and the promotion last, so a failure leaves a draft that can Create again."""
    record = get_project(project_id)
    if not record["draft"]:
        raise jobs.JobError(DRAFT_GONE, 409)
    table = intake_files(project_id)
    blocker = _create_blocker(table["rows"], table["summary"])
    if blocker and not (blocker == "Add a model first." and record["split_ids"]):
        raise jobs.JobError(blocker, 409)
    final = _clean_name(name) or record["name"]
    for row in table["rows"]:
        if row["status"] == "split":
            make_split(project_id, row["model"])
    with database.transaction() as conn:
        promoted = conn.execute("UPDATE projects SET name = ?, draft = 0, updated_at = ? WHERE id = ? AND draft = 1",
                                (final, time.time(), project_id)).rowcount
    if not promoted:
        raise jobs.JobError(DRAFT_GONE, 409)
    arranged = None
    if arrange and len(get_project(project_id)["data"]["objects"]) > 1:   # every model starts on plate 1 at one spot
        arranged = _arrange_new_project(project_id)
    project = get_project(project_id)
    return {"project": project, "step": "model" if unplaced_splits(project) else "plates", "arranged": arranged}


def _arrange_new_project(project_id: int):
    """Apply the plan the New project dialog showed (Plan plates at the chosen density), once. A plan that cannot be made
    leaves the models as they are and is said in plain words: Create has already succeeded. Returns the plan's summary, or
    {"error": text}."""
    try:
        record = get_project(project_id)
        planned = _plan_data(project_id, record)
        view = _plan_view(record, planned)
        if planned.get("refused") or not planned["plates"]:
            return {"error": view["summary"]}
        _save_data(project_id, record, planned["data"], planned["emptied"])
        return {"summary": view["summary"], "plates": len(planned["plates"]), "too_big": planned.get("too_big", [])}
    except jobs.JobError as e:
        return {"error": str(e)}


def unplaced_splits(project: dict) -> list:
    """The project's splits that have no parts or whole model on a plate yet."""
    placed = {(m.get("split") or {}).get("id") for m in project["models"]}
    return [i for i in project["split_ids"] if i not in placed]


def make_split(project_id: int, model_id: int) -> dict:
    """Turn an imported model (one too big for the bed) into a split of this project: the model is stored as a new split
    model with the model's scale and up axis and analysed again, and the model and its object are removed."""
    record = get_project(project_id)
    model = _find_model(record, model_id)
    if model.get("split") or model["state"] != READY:
        raise jobs.JobError("Only a model that has been analysed and is not split parts can become a split", 409)
    source = model_dir(project_id, model_id) / "source" / model["file"]
    with source.open("rb") as fh:
        split = splits.create_split(fh, source.stat().st_size, model["file"], {"scale": model["import"][0], "up": model["import"][1]})
    copies = sum(1 for o in record["data"]["objects"] if o["model"] == model_id)
    if copies > 1:   # the Copies the row asked for go onto whatever the split becomes (see _split_copies)
        _mutate(project_id, "data", lambda d: d.setdefault("split_copies", {}).__setitem__(str(split["id"]), copies))
    with database.transaction() as conn:
        if record.get("split_id") is None:
            conn.execute("UPDATE projects SET split_id = ? WHERE id = ?", (split["id"], project_id))
        else:
            conn.execute("INSERT OR IGNORE INTO project_splits (project_id, split_id) VALUES (?, ?)", (project_id, split["id"]))
    return delete_model(project_id, model_id)


def discard_draft(project_id: int, idle_before: float | None = None) -> None:
    """Delete a draft and its files (the page was closed or cancelled). Anything that is not a draft is refused, and the
    row is deleted only while it is still a draft, so a Create that lands meanwhile keeps its project."""
    if not get_project(project_id)["draft"]:
        raise jobs.JobError(DRAFT_GONE, 409)
    delete_project(project_id, only_draft=True, only_draft_idle_before=idle_before)


def sweep_drafts(idle_hours: float | None = None) -> list:
    """Delete drafts untouched for `idle_hours` (default draft_idle_hours()) and return their ids. Only rows flagged
    draft are ever considered, so real, quick and promoted projects are safe."""
    limit = draft_idle_hours() if idle_hours is None else idle_hours
    cutoff = time.time() - limit * 3600
    with database.transaction() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM projects WHERE draft = 1 AND updated_at < ?", (cutoff,))]
    swept = []
    for project_id in ids:
        try:
            discard_draft(project_id, idle_before=cutoff)
            swept.append(project_id)
        except jobs.JobError:
            pass   # promoted or deleted since the list was read
    return swept


async def draft_sweep_loop(interval_s: float = 3600) -> None:
    """Sweep abandoned drafts at startup and then every `interval_s`."""
    while True:
        try:
            swept = await asyncio.to_thread(sweep_drafts)
            if swept:
                print(f"[Projects] Removed {len(swept)} abandoned draft project(s)")
        except Exception as e:
            print(f"[Projects] Draft sweep failed: {e}")
        await asyncio.sleep(interval_s)


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


def _filament_choices(value, model_ids: set, base=None, previous=None, preserve_unresolved=True) -> dict:
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
            clean = _filament_choice(choice, model_id)
            old = (previous or {}).get(str(model_id)) or {}
            if base is None:
                raise jobs.JobError("Choose a profile before selecting a Model Filament")
            same = old.get("preset") == clean["preset"]
            reference = choice.get("snapshot") or (old.get("snapshot") if same else None)
            try:
                if reference:
                    reference, _ = slicer.filament_snapshot(reference, base["machine"])
                    if reference["name"] != clean["preset"]:
                        raise jobs.JobError("The Filament snapshot does not match the selected preset", 409)
                else:
                    reference = slicer.lock_filament(clean["preset"], base["machine"])
                choices[str(model_id)] = {**clean, "snapshot": reference}
                if same and not old.get("snapshot"):
                    choices[str(model_id)]["migration"] = {"version": 1, "evidence": "verified-catalogue-at-save", "legacy_name": old["preset"]}
                elif old.get("migration") and same:
                    choices[str(model_id)]["migration"] = old["migration"]
            except jobs.JobError:
                if not (same and preserve_unresolved and reference == old.get("snapshot")):
                    raise
                choices[str(model_id)] = {**old, **clean}
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
    previous = (record.get("data") or {}).get("profile") or {}
    if base and bundle_id != previous.get("bundle_id"):
        slicer.verify_bundle(base["bundle"])
        slicer.filament_compatibility(base["filament"], base["machine"])
    normalized = slicer.normalize_overrides(changes, base, previous={k: previous.get(k, {}) for k in ("process", "filament")}, check_coupled=False, scope="project") if base else {}
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
                       "overrides": slicer.normalize_overrides({"process": overrides}, base, previous={"process": next((p.get("overrides", {}) for p in (record.get("data") or {}).get("plates", []) if p["id"] == plate_id), {})}, check_coupled=False, scope="plate", affected_id=plate_id).get("process", {}) if overrides else {},
                       "bricklayers": brick_post.validate(plate.get("bricklayers")),
                       "printer_id": plate.get("printer_id") if plate.get("printer_id") in state.printers else None,
                       **({"pack": chosen} if (chosen := _clean_pack(plate.get("pack"), project=False)) else {})})

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
            "overrides": slicer.normalize_overrides({"process": overrides}, base, previous={"process": next((o.get("overrides", {}) for o in (record.get("data") or {}).get("objects", []) if o["id"] == object_id), {})}, check_coupled=False, scope="object", affected_id=object_id).get("process", {}) if overrides else {},
        })
    out = {"bed": bed, "profile": profile, "plates": plates, "objects": objects}
    filaments = _filament_choices(raw.get("filaments"), model_ids, base, (record.get("data") or {}).get("filaments"),
                                  preserve_unresolved=bundle_id == previous.get("bundle_id"))
    if filaments:  # likewise only when a model has a choice
        out["filaments"] = filaments
    if raw.get("arrange_engine") == ORCA:  # likewise only when OrcaSlicer's arranger is chosen over the nester
        out["arrange_engine"] = ORCA
    if pack := _clean_pack(raw.get("pack")):   # likewise only when a density is chosen
        out["pack"] = pack
    # A quick print's chosen orientation is the server's alone: whatever a save sends, the stored value is kept (the
    # other writers, _mutate on "data", change a copy of the stored data and so keep it too).
    for key in ("orientation", "orientations", "split_copies"):    # one orientation per split once a project has turned several; Copies a split was made with
        if (record.get("data") or {}).get(key):
            out[key] = record["data"][key]
    if base:
        old_data = record.get("data") or {}
        old_bundle_id = (old_data.get("profile") or {}).get("bundle_id")
        old_base = slicer.bundle_settings(old_bundle_id) if old_bundle_id else base
        old_issues = _coupled_setting_issues(old_data, old_base)
        issues = [i for i in _coupled_setting_issues(out, base) if i not in old_issues]
        if issues:
            error = jobs.JobError(issues[0]["message"])
            error.issues = issues
            raise error
    return out


def _coupled_setting_issues(data, base, staged=None):
    profile = data.get("profile") or {}
    common = {**base.get("machine", {}), **base.get("process", {}), **profile.get("process", {})}
    issues = []
    for plate in data.get("plates", []):
        choices = plate_filaments(data, plate["id"])
        alternate = next((c for c in choices if c), None)
        filament = {**base.get("filament", {}), **profile.get("filament", {})}
        if alternate:
            try:
                # `staged` holds proposed snapshots a review has not stored yet, keyed by hash
                staged_values = (staged or {}).get((alternate.get("snapshot") or {}).get("sha256"))
                filament = staged_values or slicer.filament_snapshot(alternate.get("snapshot"), base["machine"])[1]
            except jobs.JobError:
                filament = {}  # the material issue itself blocks this Plate
        effective = {**common, **plate.get("overrides", {}), **filament}
        for issue in slicer.configuration_issues(effective):
            kind = "filament" if issue["key"] in filament else "process"
            scope = "model" if alternate and kind == "filament" else "plate" if issue["key"] in plate.get("overrides", {}) else "project"
            issues.append({**issue, "kind": kind,
                           "scope": scope, "plate_id": plate["id"], **({"affected_id": plate["id"]} if scope == "plate" else {})})
        for obj in data.get("objects", []):
            if obj["plate"] == plate["id"] and obj.get("overrides"):
                for issue in slicer.configuration_issues({**effective, **obj["overrides"]}):
                    if issue not in slicer.configuration_issues(effective):
                        issues.append({**issue, "kind": "process", "scope": "object", "plate_id": plate["id"], "affected_id": obj["id"]})
    return issues


def update_project(project_id: int, body: dict) -> dict:
    """Rename and/or replace the project data. Send `revision` to refuse overwriting a newer edit."""
    record = get_project(project_id)
    if "revision" in body and body["revision"] is not None and int(body["revision"]) != record["revision"]:
        raise jobs.JobError("The project was changed elsewhere — reload it", 409)
    name = _clean_name(body["name"]) or record["name"] if "name" in body else record["name"]
    data = validate_data(body["data"], record) if "data" in body else record["data"]
    with database.transaction() as conn:
        current = conn.execute("SELECT revision, models, created_at FROM projects WHERE id = ?", (project_id,)).fetchone()
        if current is None:
            raise jobs.JobError("Project not found", 404)
        if current["revision"] != record["revision"] or current["created_at"] != record["created_at"]:
            raise jobs.JobError("The project was changed elsewhere — reload it", 409)
        if json.loads(current["models"]) != record["models"] and "data" in body:
            data = validate_data(body["data"], {**record, "models": json.loads(current["models"])})
        conn.execute("UPDATE projects SET name = ?, data = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                     (name, json.dumps(data), time.time(), project_id))
    return get_project(project_id)


def resolve_setting(project_id: int, body: dict) -> dict:
    """Explicitly remove an old override or move it to its owning scope."""
    record = get_project(project_id)
    if body.get("revision") != record["revision"]:
        raise jobs.JobError("The Project changed — review its settings again", 409)
    data = copy.deepcopy(record["data"])
    scope, kind, key = body.get("scope"), body.get("kind", "process"), body.get("key")
    if kind not in ("process", "filament") or not isinstance(key, str):
        raise jobs.JobError("Choose a setting to resolve")
    owner = None
    if scope == "project":
        source = data["profile"].get(kind, {})
    elif scope in ("object", "plate") and kind == "process":
        owner = next((item for item in data[scope + "s"] if item["id"] == body.get("affected_id")), None)
        if owner is None:
            raise jobs.JobError("Setting owner not found", 404)
        source = owner.get("overrides", {})
    else:
        raise jobs.JobError("Only saved Project, Plate and Object overrides can be resolved here")
    if key not in source:
        raise jobs.JobError("The saved setting was changed — review it again", 409)
    if body.get("action") == "move":
        destination = body.get("destination")
        if destination == "project":
            target = data["profile"].setdefault(kind, {})
        elif destination == "plate" and scope == "object":
            target = next(p for p in data["plates"] if p["id"] == owner["plate"]).setdefault("overrides", {})
        else:
            raise jobs.JobError("Move an Object setting to its Plate or Project, or a Plate setting to its Project")
        if target is source:
            raise jobs.JobError("Choose a different destination")
        if key in target and target[key] != source[key] and body.get("replace") is not True:
            error = jobs.JobError("The destination already has a different value. Review it before replacing it", 409)
            error.issues = [{"code": "setting-move-collision", "key": key, "scope": scope,
                             "affected_id": body.get("affected_id"), "value": source[key],
                             "destination": destination, "destination_value": target[key], "message": str(error)}]
            raise error
        # Validate the moved value even if another legacy destination held the same value.
        base = slicer.bundle_settings(data["profile"]["bundle_id"])
        target[key] = slicer.normalize_overrides({kind: {key: source[key]}}, base, check_coupled=False,
                                                scope=destination).get(kind, {})[key]
    elif body.get("action") != "remove":
        raise jobs.JobError("Choose remove or move")
    del source[key]
    _save_data(project_id, record, validate_data(data, record))
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
        row = conn.execute("SELECT revision, slices, created_at FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise jobs.JobError("Project not found", 404)
        if row["revision"] != record["revision"] or row["created_at"] != record["created_at"]:
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
    return {"id": model_id, **_write_source_folder(folder, name, write_source, scale, up)}


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
        return _import_directly(project_id, stream, length, filename, scale=scale, up=up, plate=plate_id)

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


_first = lambda value: (value[0] if isinstance(value, list) and value else value) or None
MATERIAL_KEYS = ("nozzle_temperature", "nozzle_temperature_initial_layer", "hot_plate_temp", "textured_plate_temp", "cool_plate_temp",
                 "eng_plate_temp", "filament_flow_ratio", "fan_min_speed", "fan_max_speed", "filament_retraction_length")


def _new_context(resolution=None) -> dict:
    """What a staging run reports besides the conversion: the caller's `resolution`, the `differences` kept in the report, what was `applied`."""
    return {"resolution": resolution or {}, "differences": [], "applied": {}}


def _flat(kind: str, name: str) -> dict:
    return slicer.flatten_preset(slicer._presets(kind, slicer.require_orca()), name)[0]


def _material_offers(project: dict, stock: dict, machine: str, machine_values: dict) -> dict:
    """The ways an unmatched material can be resolved: the file's own configuration (when it can be verified) or a stock preset,
    each with the settings that would differ from what the file asks for."""
    nozzle = _first(machine_values.get("nozzle_diameter"))
    wanted = _first(project.get("filament_type"))
    try:
        values, left_out = slicer.imported_filament(project, machine_values, _first(project.get("filament_settings_id")))
        imported = {"available": True, "name": values["name"], "material": _first(values["filament_type"]), "nozzle": nozzle,
                    "left_out": [item["key"] for item in left_out]}
    except jobs.JobError as error:
        imported = {"available": False, "reason": str(error), "material": wanted, "nozzle": _first(project.get("nozzle_diameter"))}
    presets = []
    for name in stock["filaments"][machine]:
        values = _flat("filament", name)
        material = _first(values.get("filament_type"))
        differences = [{"key": key, "before": _first(project[key]), "after": _first(values[key])} for key in MATERIAL_KEYS
                       if key in project and key in values and _first(project[key]) != _first(values[key])]
        presets.append({"preset": name, "material": material, "nozzle": nozzle, "same_material": bool(wanted) and material == wanted,
                        "differences": differences})
    presets.sort(key=lambda p: (not p["same_material"], p["preset"]))
    return {"imported": imported, "presets": presets}


def _material_choice(project: dict, stock: dict, machine: str, machine_values: dict, ctx: dict):
    """Apply the caller's explicit material resolution: ("imported", values) or ("preset", name); None when nothing was chosen.
    A preset of another material than the file's needs `accept_material_change`."""
    choice = ctx["resolution"].get("material")
    if not choice:
        return None
    if choice.get("choice") == "imported":
        try:
            values, left_out = slicer.imported_filament(project, machine_values, _first(project.get("filament_settings_id")))
        except jobs.JobError as error:
            raise _review_error(f"The file's own material can't be used: {error}", 409, "imported-material-unavailable") from None
        ctx["differences"].extend({"scope": "project", **item} for item in left_out)
        ctx["applied"]["material"] = {"choice": "imported", "name": values["name"], "material": _first(values["filament_type"])}
        return "imported", values
    preset = choice.get("preset")
    if preset not in stock["filaments"][machine]:
        raise _review_error(f"“{preset}” isn't a material preset for {machine}", 409, "unknown-preset")
    wanted, offered = _first(project.get("filament_type")), _first(_flat("filament", preset).get("filament_type"))
    change = not wanted or wanted != offered
    if change and choice.get("accept_material_change") is not True:
        raise _review_error(f"“{preset}” is {offered or 'an unknown material'}, not {wanted or 'the file’s material'}. Confirm the material change to use it",
                            409, "material-change-unconfirmed", requested=wanted, proposed=offered)
    ctx["applied"]["material"] = {"choice": "preset", "preset": preset, "material": offered, "material_change": change}
    return "preset", preset


def _unmatched_material_blocker(project: dict, stock: dict, machine: str, machine_values: dict, proposed: str, scope: str = "project") -> dict:
    filament, wanted = _first(project.get("filament_settings_id")), _first(project.get("filament_type"))
    offered = _first(_flat("filament", proposed).get("filament_type"))
    return {"code": "unmatched-material", "scope": scope, "name": filament or "No material",
            "requested": {"filament": filament, "material": wanted, "nozzle": _first(project.get("nozzle_diameter"))},
            "proposed": {"filament": proposed, "material": offered, "nozzle": _first(machine_values.get("nozzle_diameter"))},
            "resolutions": _material_offers(project, stock, machine, machine_values),
            "message": f"The project's material “{filament}” ({wanted or 'unknown type'}) has no matching preset. "
                       f"Spooler would use “{proposed}” ({offered or 'unknown type'}) instead. Choose a compatible material before importing."}


def _note_uncompared(project: dict, base: dict, ctx: dict, scope: str) -> None:
    """Settings of the file that the chosen presets don't state can't be compared with them, so aren't carried: the report lists them."""
    keys = sorted(key for kind in ("process", "filament") for key in slicer._tab_keys(kind) - slicer.BLOCKED_SETTINGS
                  if key in project and key not in (base.get(kind) or {}) and not key.endswith("_gcode"))
    if keys:
        ctx["differences"].append({"scope": scope, "code": "uncompared-settings", "keys": keys,
                                   "reason": "These settings are in the file but not in the chosen presets, so they could not be compared and were not carried"})


def _note_blocked(project: dict, ctx: dict) -> None:
    """Scripts and post-processing never enter a Project; the report says which of the file's were left out."""
    for key in sorted(project):
        if key == "post_process" or key.endswith("_gcode"):
            ctx["differences"].append({"scope": "project", "code": "blocked-setting", "key": key,
                                       "reason": "Scripts and G-code are never carried into a Project"})


def _orca_profile(project: dict | None, notes: list, blockers: list | None = None, ctx: dict | None = None) -> tuple:
    """The profile (locked presets plus setting changes) and plate side an Orca project asks for.
    A printer, or a material that would become a different one, is never replaced quietly: it is appended to `blockers`
    (requested and proposed values and the `resolutions` on offer) until the caller's explicit resolution in `ctx["resolution"]`
    names the file's own material (an immutable snapshot) or a stock preset. Every setting of the file that differs from the chosen
    presets is carried over as a change; those that can't be are kept in `ctx["differences"]`."""
    default = {"bundle_id": _default_bundle_id(), "process": {}, "filament": {}, "bricklayers": None}
    if not project or slicer.orca_executable() is None:
        return default, "textured"
    blockers = [] if blockers is None else blockers
    ctx = ctx or _new_context()
    try:
        stock = slicer.stock_presets()
        machine = requested_machine = project.get("printer_settings_id")
        printer_open = False
        if machine not in stock["machines"]:
            mapped = ctx["resolution"].get("printer")
            if mapped is not None:
                if mapped not in stock["machines"]:
                    raise _review_error(f"“{mapped}” isn't a supported printer", 409, "unknown-printer")
                machine = mapped
                ctx["applied"]["printer"] = mapped
                ctx["differences"].append({"scope": "project", "code": "printer-mapped", "key": "printer_settings_id",
                                           "reason": f"The file's printer “{requested_machine}” was mapped to “{mapped}”"})
            else:
                machine, printer_open = stock["defaults"]["machine"], True
                notes.append("The project's printer isn't a Centauri Carbon preset — using the default printer")
        process = project.get("print_settings_id")
        filament = (project.get("filament_settings_id") or [None])[0]
        names = {"machine": machine,
                 "process": process if process in stock["processes"][machine] else stock["defaults"]["process"],
                 "filament": filament if filament in stock["filaments"][machine] else stock["defaults"]["filament"]}
        machine_values = _flat("machine", machine)
        if printer_open:
            printers = sorted(({"printer": name, "nozzle": _first(_flat("machine", name).get("nozzle_diameter"))} for name in stock["machines"]),
                              key=lambda p: (p["printer"] != machine, p["printer"]))
            blockers.append({"code": "unmatched-printer", "scope": "project", "name": requested_machine or "No printer",
                             "requested": {"printer": requested_machine, "nozzle": _first(project.get("nozzle_diameter"))},
                             "proposed": {"printer": machine, "nozzle": _first(machine_values.get("nozzle_diameter"))},
                             "resolutions": {"printers": printers},
                             "message": f"The project's printer “{requested_machine}” has no matching Centauri Carbon preset. "
                                        f"Choose a compatible printer before importing."})
        imported = None
        if names["filament"] != filament and not printer_open:
            wanted, offered = _first(project.get("filament_type")), _first(_flat("filament", names["filament"]).get("filament_type"))
            if not wanted or wanted != offered:
                chosen = _material_choice(project, stock, machine, machine_values, ctx)
                if chosen is None:
                    blockers.append(_unmatched_material_blocker(project, stock, machine, machine_values, names["filament"]))
                elif chosen[0] == "imported":
                    imported = chosen[1]
                else:
                    names["filament"] = chosen[1]
        bundle = slicer.create_bundle(**names, filament_values=imported)
        base = slicer.bundle_settings(bundle["id"])
        allowed = {kind: slicer._tab_keys(kind) - slicer.BLOCKED_SETTINGS for kind in ("process", "filament")}
        wanted = orca_3mf.settings_changes(project, base, allowed)
        changes = {}
        for kind, values in wanted.items():
            for key, value in values.items():
                try:
                    changes.setdefault(kind, {}).update(slicer.normalize_overrides({kind: {key: value}}, base).get(kind, {}))
                except jobs.JobError as error:
                    notes.append(f"Setting “{key}” from the project can't be used here and was left out")
                    ctx["differences"].append({"scope": "project", "code": "invalid-setting", "key": key, "reason": str(error)})
        _note_blocked(project, ctx)
        _note_uncompared(project, base, ctx, "project")
        profile = {"bundle_id": bundle["id"], "process": changes.get("process", {}), "filament": changes.get("filament", {}), "bricklayers": None}
        if names["process"] != process or (names["filament"] != filament and imported is None):
            notes.append(f"The project used “{process}” / “{filament}”; its differences are applied on top of "
                         f"“{names['process']}” / “{names['filament']}”")
        return profile, BED_FROM_ORCA.get(project.get("curr_bed_type"), "textured")
    except jobs.JobError as e:
        if getattr(e, "issues", None):
            raise                       # a refused resolution is reported to the caller, never swallowed
        notes.append(f"The project's settings couldn't be applied: {e}")
        return default, "textured"


def _append_settings(project: dict, record: dict, base: dict, ctx: dict, notes: list, blockers: list) -> dict:
    """What appending a file to an existing Project keeps of the file's own settings: its process differences become Plate overrides
    for the Plates it brings, its material a snapshot-backed Model choice. A different printer, nozzle or bed cannot coexist with the
    Project's: it blocks until the file is imported as a separate Project or its conversion is explicitly accepted."""
    carried = {"plate_overrides": {}, "filament": None}
    if slicer.orca_executable() is None:
        return carried
    data, profile, machine = record["data"], record["data"]["profile"], base["machine"]
    convert = ctx["resolution"].get("conflicts") == "convert"
    printer, nozzle, wanted_bed = project.get("printer_settings_id"), _first(project.get("nozzle_diameter")), project.get("curr_bed_type")
    try:
        nozzle_differs = nozzle is not None and float(nozzle) != float(_first(machine.get("nozzle_diameter")))
    except (TypeError, ValueError):
        nozzle_differs = True
    conflicts = []
    if (printer and printer != machine["name"]) or nozzle_differs:
        conflicts.append(("incompatible-machine", printer or machine["name"], "printer_settings_id",
                          {"printer": printer, "nozzle": nozzle}, {"printer": machine["name"], "nozzle": _first(machine.get("nozzle_diameter"))},
                          f"The file was made for “{printer or machine['name']}” ({nozzle or '?'} mm nozzle); this Project prints on "
                          f"“{machine['name']}” ({_first(machine.get('nozzle_diameter'))} mm nozzle)."))
    file_bed = BED_FROM_ORCA.get(wanted_bed)
    if file_bed and file_bed != data["bed"]:
        conflicts.append(("incompatible-bed", wanted_bed, "curr_bed_type", {"bed": file_bed}, {"bed": data["bed"]},
                          f"The file prints on the {file_bed} bed; this Project uses the {data['bed']} bed for every Plate."))
    for code, name, key, requested, proposed, message in conflicts:
        if convert:
            ctx["applied"]["conflicts"] = "convert"
            ctx["differences"].append({"scope": "project", "code": code, "key": key, "reason": message + " The Project's own was kept."})
        else:
            blockers.append({"code": code, "scope": "project", "name": name, "requested": requested, "proposed": proposed,
                             "options": ["separate-project", "convert"],
                             "message": message + " Import it as a separate Project, or convert it to this Project's printer and bed after reviewing the difference."})
    effective = {**base, "process": {**base["process"], **(profile.get("process") or {})}}
    allowed = {"process": slicer._tab_keys("process") - slicer.BLOCKED_SETTINGS}
    for key, value in orca_3mf.settings_changes(project, effective, allowed)["process"].items():
        try:
            carried["plate_overrides"].update(slicer.normalize_overrides({"process": {key: value}}, effective, scope="plate").get("process", {}))
        except jobs.JobError as error:
            notes.append(f"Setting “{key}” from the file can't be used on a Plate and was left out")
            ctx["differences"].append({"scope": "plate", "code": "invalid-setting", "key": key, "reason": str(error)})
    _note_blocked(project, ctx)
    _note_uncompared(project, effective, ctx, "plate")
    carried["plate_overrides"] = dict(sorted(carried["plate_overrides"].items()))
    carried["filament"] = _append_material(project, record, base, ctx, notes, blockers)
    return carried


def _append_material(project: dict, record: dict, base: dict, ctx: dict, notes: list, blockers: list):
    """The Model Filament choice (preset name, snapshot) an appended file's material needs, or None when the Project's own already is it."""
    machine = base["machine"]
    stock = slicer.stock_presets()
    name, kind = _first(project.get("filament_settings_id")), _first(project.get("filament_type"))
    if machine["name"] not in stock["filaments"] or not (name or kind):
        return None
    profile = record["data"]["profile"]
    target = base["bundle"]["presets"]["filament"]["name"]
    available = stock["filaments"][machine["name"]]
    values = unmatched = None
    if name in available:
        preset = name
    elif kind and kind == _first(base["filament"].get("filament_type")):
        # Same material, no preset of that name: the file's own filament values are compared with the Project's preset and
        # carried as a snapshot when they differ, never dropped.
        preset, unmatched = target, name
    else:
        chosen = _material_choice(project, stock, machine["name"], machine, ctx)
        if chosen is None:
            blockers.append(_unmatched_material_blocker(project, stock, machine["name"], machine, target))
            return None
        if chosen[0] == "imported":
            values = chosen[1]
        preset = chosen[1] if chosen[0] == "preset" else None
    if values is None:
        flat = _flat("filament", preset)
        allowed = {"filament": slicer._tab_keys("filament") - slicer.BLOCKED_SETTINGS}
        deltas = {}
        for key, value in orca_3mf.settings_changes(project, {"filament": flat}, allowed)["filament"].items():
            try:
                deltas.update(slicer.normalize_overrides({"filament": {key: value}}, {"machine": machine, "filament": flat}).get("filament", {}))
            except jobs.JobError as error:
                ctx["differences"].append({"scope": "project", "code": "invalid-setting", "key": key, "reason": str(error)})
        if preset == target and deltas == (profile.get("filament") or {}):
            if unmatched:
                notes.append(f"The file's material “{unmatched}” has no preset; it is the same material as the Project's “{target}”, which is kept")
                ctx["differences"].append({"scope": "project", "code": "material-name-unmatched", "key": "filament_settings_id",
                                           "reason": f"“{unmatched}” has no preset; its settings match the Project's {target}, which was kept"})
            return None
        if not deltas:
            reference = slicer.lock_filament(preset, machine)
            return {"preset": reference["name"], "colour": "", "snapshot": reference}
        values = {**flat, **deltas, "name": f"{unmatched or preset} (imported)"}
        if unmatched:
            notes.append(f"The file's material “{unmatched}” has no preset; its own settings are kept as a saved Filament choice")
    reference, content = slicer.snapshot_proposal(values, machine)
    reference = slicer.store_filament(reference, content, machine)
    return {"preset": reference["name"], "colour": "", "snapshot": reference}


def _clean_object_settings(settings: dict, base: dict | None, label: str, notes: list, ctx: dict | None = None, scope: str | None = None) -> dict:
    """The per-object process settings of an Orca project that this profile accepts; the rest are noted and left out."""
    kept = {}
    for key, value in settings.items():
        try:
            if base is None:
                raise jobs.JobError("no profile")
            kept.update(slicer.normalize_overrides({"process": {key: value}}, base, scope=scope).get("process", {}))
        except jobs.JobError as error:
            notes.append(f"{label}: setting “{key}” can't be used here and was left out")
            if ctx is not None:
                ctx["differences"].append({"scope": "object", "name": label, "code": "invalid-setting", "key": key, "reason": str(error)})
    return kept


# ── Import review ──────────────────────────────────────────────────────────────
#
# An Orca/Bambu or plain 3MF is first inspected and converted into private staging under projects/.import-staging/<token>/:
# the original archive, the converted Model sources and a review.json that records the loss report and what the conversion would
# do. Nothing visible changes. Confirming binds the content hash, the proposed conversion (profile choice, Models, Plates, import
# options), the target Project's identity and revision, and the report version; the prepared delta is then published in one
# transaction. Cancelling, expiry or any failure removes only that token's staging folder.

IMPORT_REVIEW_TTL_S = 30 * 60
STAGING_NAME = ".import-staging"
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


def _staging_root() -> Path:
    return projects_dir() / STAGING_NAME


def _staging_path(token) -> Path:
    if not isinstance(token, str) or not _TOKEN.match(token):
        raise jobs.JobError("That import review isn't valid", 400)
    return _staging_root() / token


def _review_error(message: str, status: int, code: str, **extra) -> jobs.JobError:
    error = jobs.JobError(message, status)
    error.issues = [{"code": code, "message": message, **extra}]
    return error


def _load_review(token: str) -> dict:
    try:
        return json.loads((_staging_path(token) / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _review_error("This import review was cancelled or has expired. Choose the file again", 404, "import-review-gone") from None


def cancel_import(token: str) -> dict:
    """Throw away a review's staging. Only the named token's own folder is touched; cancelling twice is harmless."""
    shutil.rmtree(_staging_path(token), ignore_errors=True)
    return {"ok": True}


def sweep_import_staging(now: float | None = None, startup: bool = False) -> int:
    """Remove staging that has expired, or was interrupted before it recorded a review (all of those at startup, when no
    preparation can be running). Folders that aren't a staging token are never touched."""
    root = _staging_root()
    if not root.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for entry in list(root.iterdir()):
        if not entry.is_dir() or not _TOKEN.match(entry.name):
            continue
        try:
            expired = json.loads((entry / "review.json").read_text(encoding="utf-8"))["expires_at"] <= now
        except (OSError, ValueError, KeyError, TypeError):
            try:
                expired = startup or entry.stat().st_mtime + IMPORT_REVIEW_TTL_S <= now
            except OSError:
                expired = False
        if expired:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _binding(review: dict) -> dict:
    return {"token": review["token"], "content_hash": review["content_hash"], "report_version": review["report"]["version"],
            "conversion": review["conversion_digest"], "target": review["target"]}


def _public_review(review: dict) -> dict:
    report = review["report"]
    return {"token": review["token"], "kind": review["kind"], "filename": review["filename"], "expires_at": review["expires_at"],
            "required": _needs_acknowledgement(report), "blocked": bool(report["blockers"]), "report": report, "notes": review["notes"],
            "binding": _binding(review)}


def get_import_review(token: str) -> dict:
    review = _load_review(token)
    if review["expires_at"] <= time.time():
        cancel_import(token)
        raise _review_error("This import review has expired. Choose the file again", 409, "import-review-expired")
    return _public_review(review)


def _write_source_folder(folder: Path, name: str, write_source, scale: float, up: str) -> dict:
    """A Model's source folder (source file and import settings) in any directory. Returns its record without an id."""
    (folder / "source").mkdir(parents=True)
    try:
        size, digest = write_source(folder / "source" / name)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    (folder / "job.json").write_text(json.dumps({"source": f"source/{name}", "scale": scale, "up": up}), encoding="utf-8")
    return {"name": Path(name).stem, "file": name, "state": IMPORTING, "error": None, "size_bytes": size,
            "sha256": digest, "import": [scale, up]}


def _stage_conversion(kind: str, path: Path, folder: Path, record: dict | None, filename: str, options: dict, notes: list, blockers: list,
                      ctx: dict | None = None):
    """Convert the staged archive into Model sources and a Project delta, all inside `folder`. A file whose geometry or settings
    can't be converted adds a blocker and returns None."""
    ctx = ctx or _new_context()
    if kind == "plain":
        name = splits.safe_model_name(filename)
        template = _write_source_folder(folder / "models" / "0", name, _copy_from(path), options["scale"], options["up"])
        return {"kind": kind, "models": [{"folder": "0", "record": template, "object": template["name"]}],
                "objects": [{"model": 0, "plate_target": options["plate"], "name": template["name"], "position": [128.0, 128.0],
                             "rotation": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0], "overrides": {}}], "plates": []}
    _require_geometry()
    try:
        parsed = orca_3mf.parse(path)
    except orca_3mf.ThreeMFError as e:
        blockers.append({"code": "unreadable-geometry", "scope": "project", "name": filename, "message": str(e)})
        return None
    notes.extend(w for w in parsed["warnings"] if "were left out" not in w)
    wanted = []        # (object, its enabled copies): a disabled copy is a reported loss and is not imported
    for obj in parsed["objects"]:
        copies = [i for i in obj["instances"] if i["printable"]]
        if copies:
            wanted.append((obj, copies))
    if not wanted or (kind == "single" and len(wanted) != 1):
        blockers.append({"code": "nothing-printable", "scope": "project", "name": filename,
                         "message": "The file has no enabled copy to import" if not wanted else "The file holds more than one Object"})
        return None
    fresh = kind in ("single", "project")
    if fresh:
        profile, bed = _orca_profile(parsed["project"], notes, blockers, ctx)
    else:
        profile, bed = record["data"]["profile"], record["data"]["bed"]
    base = slicer.bundle_settings(profile["bundle_id"]) if profile.get("bundle_id") else None
    carried = {"plate_overrides": {}, "filament": None}
    if kind == "append" and parsed["project"] and base is not None:
        carried = _append_settings(parsed["project"], record, base, ctx, notes, blockers)
    if kind == "single":
        obj, copies = wanted[0]
        profile = {"spool_id": None, **profile}
        profile["process"] = {**profile["process"], **_clean_object_settings(obj["settings"], base, obj["name"], notes, ctx)}
        write_stl(folder / "model.stl", obj["verts"] @ copies[0]["matrix"], obj["tris"])
        stl = folder / "model.stl"
        return {"kind": kind, "stl": "model.stl", "size": stl.stat().st_size, "sha256": _file_digest(stl), "profile": profile, "bed": bed,
                "name": Path(filename.replace("\\", "/")).stem[:120], "models": [], "objects": [], "plates": []}
    if kind == "append":
        data = record["data"]
        if len(data["objects"]) + sum(len(c) for _, c in wanted) > MAX_OBJECTS:
            raise jobs.JobError(f"A project holds at most {MAX_OBJECTS} objects")
        if len(data["plates"]) + len(parsed["plates"]) > MAX_PLATES:
            raise jobs.JobError(f"A project has at most {MAX_PLATES} plates")
    elif sum(len(c) for _, c in wanted) > MAX_OBJECTS:
        raise jobs.JobError(f"A project holds at most {MAX_OBJECTS} objects")
    models, objects = [], []
    for index, (obj, copies) in enumerate(wanted):
        name = splits.safe_model_name(f"{obj['name']}.stl") if obj["name"].strip(". ") else f"object{index + 1}.stl"

        def write(target: Path, verts=obj["verts"], tris=obj["tris"]) -> tuple:
            write_stl(target, verts, tris)
            return target.stat().st_size, _file_digest(target)
        models.append({"folder": str(index), "record": _write_source_folder(folder / "models" / str(index), name, write, 1.0, "z"),
                       "object": obj["name"]})
        overrides = _clean_object_settings(obj["settings"], base, obj["name"], notes, ctx, scope="object")
        for copy, instance in enumerate(copies, start=1):
            placed = orca_3mf.pose(obj["verts"], instance["matrix"], instance["shift"])
            objects.append({"model": index, "plate_source": instance["plate"], "name": obj["name"] if copy == 1 else f"{obj['name']} ({copy})",
                            "position": placed["position"], "rotation": placed["rotation"], "scale": placed["scale"], "overrides": dict(overrides)})
    return {"kind": kind, "models": models, "objects": objects, "plates": [{"id": p["id"], "name": p["name"]} for p in parsed["plates"]],
            "plate_overrides": carried["plate_overrides"], "filament": carried["filament"],
            "profile": profile if fresh else None, "bed": bed if fresh else None,
            "name": Path(filename.replace("\\", "/")).stem[:120]}


def _run_staging(kind: str, path: Path, folder: Path, record: dict | None, filename: str, options: dict, inspected: list) -> tuple:
    """Convert the staged archive into `folder`. Returns (conversion, notes, blockers, differences, applied): the conversion is None
    while blocked; `applied` echoes the explicit resolution that shaped it."""
    notes, blockers, ctx = [], list(inspected), _new_context(options.get("resolution"))
    conversion = None if blockers else _stage_conversion(kind, path, folder, record, filename, options, notes, blockers, ctx)
    seen, differences = set(), []
    for item in ctx["differences"]:
        if (item.get("scope"), item.get("name"), item.get("key"), item["code"]) not in seen:
            seen.add((item.get("scope"), item.get("name"), item.get("key"), item["code"]))
            differences.append(item)
    return conversion, notes, blockers, differences, ctx["applied"]


# Differences that are settings of the file which would not reach the Project; the person acknowledges them like losses.
# The rest record choices already made (a mapped printer, a convert, a kept material) and need no acknowledgement.
UNCARRIED_DIFFERENCES = ("invalid-setting", "uncompared-settings", "blocked-setting")


def _uncarried(report: dict) -> list:
    return [d for d in report.get("differences") or [] if d.get("code") in UNCARRIED_DIFFERENCES]


def _needs_acknowledgement(report: dict) -> bool:
    return bool(report["losses"]) or bool(_uncarried(report)) or bool((report.get("resolution") or {}).get("material", {}).get("material_change"))


def prepare_import(stream, length: int, filename: str, target: int | None = None, scale=1.0, up="z", plate=None, quick: bool = False) -> dict:
    """Stage and inspect a 3MF without changing any Project. `target` is the Project it would be added to (none: it would start a
    new Project). Returns the review: its `token`, the `report` of losses and blockers, whether acknowledgement is `required`,
    and the `binding` that confirm_import must echo."""
    if not str(filename).lower().endswith(".3mf"):
        raise jobs.JobError("Only 3MF files have an import review")
    sweep_import_staging()
    record = get_project(target) if target is not None else None
    options = {**splits.normalize_settings({"scale": scale, "up": up}), "plate": int(plate) if plate else None, "quick": bool(quick)}
    source = _spool(stream, length)
    token = uuid.uuid4().hex
    folder = _staging_path(token)
    try:
        folder.mkdir(parents=True)
        path = folder / "source.3mf"
        os.replace(source, path)
        digest = _file_digest(path)
        try:
            found = orca_3mf.summary(path)
            kind = ("project" if found["objects"] > 1 or found["plates"] > 1 else "single") if record is None \
                else "append" if orca_3mf.is_project(path) else "plain"
            report = orca_3mf.inspect(path, "single" if kind == "single" else "project")
        except orca_3mf.ThreeMFError as e:
            raise jobs.JobError(str(e)) from e
        except (zipfile.BadZipFile, ElementTree.ParseError, zlib.error, EOFError):
            raise jobs.JobError("That isn't a valid 3MF file") from None
        report.pop("project", None)
        inspected = list(report["blockers"])
        conversion, notes, blockers, differences, applied = _run_staging(kind, path, folder, record, filename, options, inspected)
        report.update(blockers=blockers, differences=differences, resolution=applied)
        summary = json.dumps({"conversion": conversion, "options": options}, sort_keys=True, default=str)
        now = time.time()
        review = {"token": token, "kind": kind, "filename": Path(filename.replace("\\", "/")).name, "created_at": now,
                  "expires_at": now + IMPORT_REVIEW_TTL_S, "content_hash": digest, "size": path.stat().st_size, "report": report,
                  "notes": list(dict.fromkeys(notes)), "conversion": conversion, "options": options, "inspected_blockers": inspected,
                  "conversion_digest": hashlib.sha256(summary.encode()).hexdigest(),
                  "target": {"project_id": record["id"], "created_at": record["created_at"], "revision": record["revision"]} if record else None}
        (folder / "review.json").write_text(json.dumps(review, default=str), encoding="utf-8")
        return _public_review(review)
    except BaseException:
        source.unlink(missing_ok=True)
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _clean_resolution(raw) -> dict:
    """The caller's explicit choices: a material (the file's own `imported` configuration or a named stock `preset`), a stock
    `printer` for a new Project, and `conflicts: convert` to accept converting an appended file to the Project's printer and bed."""
    if not isinstance(raw, dict):
        raise jobs.JobError("Choose how to resolve the import")
    clean = {}
    material = raw.get("material")
    if material is not None:
        if not isinstance(material, dict) or material.get("choice") not in ("imported", "preset"):
            raise jobs.JobError("Choose the file's own material or a preset")
        clean["material"] = {"choice": material["choice"]}
        if material["choice"] == "preset":
            if not isinstance(material.get("preset"), str) or not material["preset"].strip():
                raise jobs.JobError("Choose which preset to use")
            clean["material"].update(preset=material["preset"], accept_material_change=material.get("accept_material_change") is True)
    if raw.get("printer") is not None:
        if not isinstance(raw["printer"], str):
            raise jobs.JobError("Choose a printer preset")
        clean["printer"] = raw["printer"]
    if raw.get("conflicts") is not None:
        if raw["conflicts"] != "convert":
            raise jobs.JobError("The only conversion on offer is to the Project's own printer and bed")
        clean["conflicts"] = "convert"
    if not clean:
        raise jobs.JobError("Choose how to resolve the import")
    return clean


def resolve_import(token: str, resolution: dict) -> dict:
    """Apply an explicit resolution (see _clean_resolution) to a pending review and return the re-staged review. A choice that can't
    be applied is refused and leaves the review as it was; an accepted one changes the conversion, so the binding changes and
    an earlier confirmation is stale."""
    review = _load_review(token)
    folder = _staging_path(token)
    if review["expires_at"] <= time.time():
        cancel_import(token)
        raise _review_error("This import review has expired. Choose the file again", 409, "import-review-expired")
    if review.get("inspected_blockers"):
        raise _review_error("This file can't be imported, so there is nothing to resolve", 409, "import-blocked",
                            blockers=review["report"]["blockers"])
    resolution = _clean_resolution(resolution)
    options = {**review["options"], "resolution": {**(review["options"].get("resolution") or {}), **resolution}}
    record = None
    if review["target"] is not None:
        target = review["target"]
        try:
            record = get_project(target["project_id"])
        except jobs.JobError:
            record = None
        if record is None or record["revision"] != target["revision"] or record["created_at"] != target["created_at"]:
            cancel_import(token)
            raise _review_error("The Project changed since the import was reviewed. Review it again", 409, "import-review-stale")
    path = folder / "source.3mf"
    if _file_digest(path) != review["content_hash"]:
        cancel_import(token)
        raise _review_error("The staged file changed. Choose the file again", 409, "import-review-stale")
    fresh = folder / "next"
    shutil.rmtree(fresh, ignore_errors=True)
    fresh.mkdir()
    try:
        conversion, notes, blockers, differences, applied = _run_staging(review["kind"], path, fresh, record, review["filename"], options, [])
    except BaseException:
        shutil.rmtree(fresh, ignore_errors=True)
        raise
    shutil.rmtree(folder / "models", ignore_errors=True)
    (folder / "model.stl").unlink(missing_ok=True)
    for entry in fresh.iterdir():
        os.replace(entry, folder / entry.name)
    shutil.rmtree(fresh, ignore_errors=True)
    review["report"].update(blockers=blockers, differences=differences, resolution=applied)
    summary = json.dumps({"conversion": conversion, "options": options}, sort_keys=True, default=str)
    review.update(conversion=conversion, options=options, notes=list(dict.fromkeys(notes)),
                  conversion_digest=hashlib.sha256(summary.encode()).hexdigest())
    (folder / "review.json").write_text(json.dumps(review, default=str), encoding="utf-8")
    return _public_review(review)


def _import_directly(target, stream, length: int, filename: str, **options) -> dict:
    """The one-step import of callers that have no review screen: a 3MF with nothing to review is converted as usual; any loss
    or blocker refuses it (409, nothing changed) rather than skipping the review."""
    review = prepare_import(stream, length, filename, target=target, **options)
    if review["required"] or review["blocked"]:
        cancel_import(review["token"])
        raise _review_error("This 3MF has features Spooler can't keep or settings it can't match. Add it from a Project's New project or Add models "
                            "page to review it first", 409,
                            "import-review-required", losses=review["report"]["losses"], blockers=review["report"]["blockers"],
                            differences=_uncarried(review["report"]))
    return confirm_import(review["token"], review["binding"])


def confirm_import(token: str, confirmation: dict) -> dict:
    """Commit a reviewed import. `confirmation` is the review's binding, plus `acknowledged: true` when it lists losses.
    A confirmation that no longer matches (the file, the proposed conversion, the Project's identity or revision) conflicts and its
    staging is removed; so are staging that expired or fails to commit."""
    review = _load_review(token)
    folder = _staging_path(token)
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    if review["expires_at"] <= time.time():
        cancel_import(token)
        raise _review_error("This import review has expired. Choose the file again", 409, "import-review-expired")
    if any(confirmation.get(key) != value for key, value in _binding(review).items()):
        cancel_import(token)
        raise _review_error("The import review is out of date. Choose the file again", 409, "import-review-stale")
    report = review["report"]
    if report["blockers"]:
        raise _review_error("This import can't continue until its blocking problems are resolved", 409, "import-blocked",
                            blockers=report["blockers"])
    if _needs_acknowledgement(report) and confirmation.get("acknowledged") is not True:
        raise _review_error("Review the losses and differences and choose Continue", 409, "import-review-required", losses=report["losses"],
                            differences=_uncarried(report))
    try:
        if _file_digest(folder / "source.3mf") != review["content_hash"]:
            raise _review_error("The staged file changed. Choose the file again", 409, "import-review-stale")
        result = _commit_import(review, folder)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    shutil.rmtree(folder, ignore_errors=True)
    return result


def _commit_import(review: dict, folder: Path) -> dict:
    conversion, target = review["conversion"], review["target"]
    notes = review["notes"]
    if target is not None:
        summary = _apply_conversion(target["project_id"], review, folder, target, fresh=False)
        project_id = target["project_id"]
    elif conversion["kind"] == "single":
        project_id = _create_single_model_project(review, folder)
        summary = {"model_ids": [], "plate_ids": []}
    else:
        project = create_project(conversion["name"])
        project_id = project["id"]
        try:
            summary = _apply_conversion(project_id, review, folder, {"revision": project["revision"], "created_at": project["created_at"]},
                                        fresh=True)
        except BaseException:
            delete_project(project_id)
            raise
    record = import_record(project_id, review["token"])
    return {**get_project(project_id), "import_notes": notes, "import_record": record}


def _retain_import(project_id: int, review: dict, folder: Path, summary: dict) -> Path:
    """Keep the original archive and the accepted loss report with the Project (downloadable later). Returns the new folder,
    which only this import owns."""
    keep = project_dir(project_id) / "imports" / review["token"]
    keep.mkdir(parents=True)
    os.replace(folder / "source.3mf", keep / "source.3mf")
    report = review["report"]
    (keep / "record.json").write_text(json.dumps({
        "id": review["token"], "kind": review["kind"], "filename": review["filename"], "content_hash": review["content_hash"],
        "size": review["size"], "accepted_at": time.time(), "acknowledged": _needs_acknowledgement(report), "report_version": report["version"],
        "losses": report["losses"], "differences": report.get("differences", []),
        "resolution": report.get("resolution", {}), "objects": report["objects"], "plates": report["plates"], "notes": review["notes"], **summary}), encoding="utf-8")
    return keep


def _apply_conversion(project_id: int, review: dict, folder: Path, expected: dict, fresh: bool) -> dict:
    """Publish the staged Models and Project delta in one transaction, only if the Project is still the one that was reviewed.
    Folders already moved into place are removed again if anything fails; nothing else is ever deleted."""
    conversion = review["conversion"]
    published = []
    try:
        with database.transaction() as conn:
            row = conn.execute("SELECT data, models, revision, created_at FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise _review_error("The Project this import was reviewed for is gone", 409, "import-review-stale")
            if row["revision"] != expected["revision"] or row["created_at"] != expected["created_at"]:
                raise _review_error("The Project changed since the import was reviewed. Review it again", 409, "import-review-stale")
            data, existing = json.loads(row["data"]), json.loads(row["models"])
            plates = [] if fresh else data["plates"]
            if len(plates) + len(conversion["plates"]) > MAX_PLATES:
                raise jobs.JobError(f"A project has at most {MAX_PLATES} plates")
            if (0 if fresh else len(data["objects"])) + len(conversion["objects"]) > MAX_OBJECTS:
                raise jobs.JobError(f"A project holds at most {MAX_OBJECTS} objects")
            next_model = _next_id(existing)
            model_ids, new_models = {}, []
            for spec in conversion["models"]:
                while model_dir(project_id, next_model).exists():     # never replace assets we don't own
                    next_model += 1
                destination = model_dir(project_id, next_model)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(folder / "models" / spec["folder"], destination)
                published.append(destination)
                model_ids[int(spec["folder"])] = next_model
                new_models.append({**spec["record"], "id": next_model, "origin": {"import": review["token"]}})
                if conversion.get("filament"):
                    data.setdefault("filaments", {})[str(next_model)] = dict(conversion["filament"])
                next_model += 1
            if fresh:
                data["plates"], data["objects"] = [], []
                data["profile"], data["bed"] = conversion["profile"], conversion["bed"]
            first = _next_id(data["plates"])
            plate_ids = {}
            for number, plate in enumerate(conversion["plates"]):
                plate_ids[plate["id"]] = first + number
                data["plates"].append({"id": first + number, "name": plate["name"] or f"Plate {first + number}",
                                       "overrides": dict(conversion.get("plate_overrides") or {})})
            valid = {p["id"] for p in data["plates"]}
            for obj in conversion["objects"]:
                if "plate_source" in obj:
                    plate_id = plate_ids.get(obj["plate_source"], first)
                else:
                    plate_id = obj["plate_target"] if obj["plate_target"] in valid else data["plates"][0]["id"]
                data["objects"].append({"id": _next_id(data["objects"]), "model": model_ids[obj["model"]], "plate": plate_id,
                                        "name": obj["name"], "position": obj["position"], "rotation": obj["rotation"],
                                        "scale": obj["scale"], "overrides": dict(obj["overrides"])})
            summary = {"model_ids": sorted(model_ids.values()), "plate_ids": sorted(plate_ids.values())}
            published.append(_retain_import(project_id, review, folder, summary))
            conn.execute("UPDATE projects SET data = ?, models = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                         (json.dumps(data), json.dumps(existing + new_models), time.time(), project_id))
    except BaseException:
        for owned in reversed(published):
            shutil.rmtree(owned, ignore_errors=True)
        raise
    notify_worker()
    return summary


def _create_single_model_project(review: dict, folder: Path) -> int:
    """A one-Object 3MF becomes a Split-owned whole Model in a new Project, like a plain model upload."""
    conversion, options = review["conversion"], review["options"]
    stl = folder / conversion["stl"]
    if _file_digest(stl) != conversion["sha256"]:
        raise _review_error("The staged conversion changed. Choose the file again", 409, "import-review-stale")
    with stl.open("rb") as fh:
        split = splits.create_split(fh, stl.stat().st_size, f"{conversion['name']}.stl", {"scale": options["scale"], "up": options["up"]})
    project = None
    try:
        project = create_project(conversion["name"], split_id=split["id"], quick=options["quick"])
        _mutate(project["id"], "data", lambda d: d.update(bed=conversion["bed"], profile=conversion["profile"]))
        _retain_import(project["id"], review, folder, {"model_ids": [], "plate_ids": [], "split_id": split["id"]})
        return project["id"]
    except BaseException:
        if project is not None:
            delete_project(project["id"])
        _drop_split(split["id"])
        raise


def _drop_split(split_id: int) -> None:
    try:
        splits.delete_split(split_id)
    except jobs.JobError:
        pass  # already deleted with its Project


_RECORD_ID = _TOKEN


def import_records(project_id: int) -> list:
    """The accepted imports of a Project, newest first: each keeps its loss report, notes and the original archive."""
    get_project(project_id)
    root = project_dir(project_id) / "imports"
    out = []
    for entry in (root.iterdir() if root.is_dir() else []):
        try:
            out.append(json.loads((entry / "record.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r["accepted_at"], reverse=True)


def import_record(project_id: int, record_id: str) -> dict:
    get_project(project_id)
    if not isinstance(record_id, str) or not _RECORD_ID.match(record_id):
        raise jobs.JobError("Import record not found", 404)
    try:
        return json.loads((project_dir(project_id) / "imports" / record_id / "record.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise jobs.JobError("Import record not found", 404) from None


def import_source(project_id: int, record_id: str) -> tuple:
    """(path, download name) of the original archive an import kept."""
    record = import_record(project_id, record_id)
    return project_dir(project_id) / "imports" / record_id / "source.3mf", record["filename"]


def _export_error(code: str, message: str, status: int = 409, **extra) -> jobs.JobError:
    error = jobs.JobError(message, status)
    error.issues = [{"code": code, "message": message, **extra}]
    return error


def export_plan(project_id: int, *, record: dict | None = None) -> dict:
    """What a faithful Orca export of the Project can represent, bound to the revision it was computed from. Read-only; it
    resolves every nonempty Plate with `effective_settings`, the calculation a Slice uses (see export_plan.py)."""
    record = record if record is not None else get_project(project_id)
    data = record["data"]
    models = {m["id"]: m for m in record["models"]}
    entries, unavailable = [], []
    for plate in data["plates"]:
        if not any(o["plate"] == plate["id"] for o in data["objects"]):
            continue
        try:
            entries.append((plate, effective_settings(record["id"], plate["id"], record=record)))
        except jobs.JobError as exc:
            unavailable.append({"code": "settings-unavailable", "message": str(exc), "plate_id": plate["id"]})
    waiting = [o["name"] for o in data["objects"] if models.get(o["model"], {}).get("state") != READY]
    plan = export_planner.plan(record, entries, unavailable, waiting, slicer.orca_executable() is not None)
    plan["lossy"] = lossy_report.alternative(record, plan, entries, import_records(record["id"]))
    return plan


def _export_template(project_id: int, review: dict) -> dict:
    """Orca's complete project configuration for the effective machine/process/filament: what OrcaSlicer itself writes when it
    exports a 3MF with these inputs. Isolated data directory; an unreadable result is an error, never a thinner file."""
    orca = slicer.orca_executable()
    problem = "OrcaSlicer could not write the project configuration"
    if orca is None:
        raise _export_error("export-template-unavailable", "OrcaSlicer is needed to write the project configuration, and it was not found")
    with tempfile.TemporaryDirectory(prefix=".export-", dir=project_dir(project_id)) as tmp:
        tmp = Path(tmp)
        for kind in export_planner.KINDS:
            (tmp / f"{kind}.json").write_text(json.dumps(review[kind], sort_keys=True), encoding="utf-8")
        corners = [(x, y, z) for x in (0, 10) for y in (0, 10) for z in (0, 10)]
        faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
        # ASCII: Orca's STL reader refuses the binary files write_stl makes, and a bare tetrahedron
        (tmp / "template.stl").write_text("solid template\n" + "".join(
            "facet normal 0 0 0\n outer loop\n" + "".join(f"  vertex {x} {y} {z}\n" for x, y, z in (corners[i] for i in face)) + " endloop\nendfacet\n"
            for face in faces) + "endsolid template\n", encoding="ascii")
        out = tmp / "template.3mf"
        code, text = slicer.runner([str(orca), "--datadir", str(_data_dir / "slicer-data-export"), "--arrange", "0",
                                    "--load-settings", f"{tmp / 'machine.json'};{tmp / 'process.json'}",
                                    "--load-filaments", str(tmp / "filament.json"), "--export-3mf", str(out), str(tmp / "template.stl")], 120)
        try:
            with zipfile.ZipFile(out) as archive:
                template = json.loads(archive.read("Metadata/project_settings.config").decode("utf-8"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            raise _export_error("export-template-unavailable", f"{problem} (exit code {code - (1 << 32) if code >= (1 << 31) else code})", log=(text or "")[-1500:]) from None
    if not isinstance(template, dict):
        raise _export_error("export-template-unavailable", problem)
    return template


def _authored_keys(record: dict, entries: list) -> set:
    profile = record["data"]["profile"]
    keys = set(profile.get("process") or {}) | set(profile.get("filament") or {})
    for obj in record["data"]["objects"]:
        keys |= set(obj.get("overrides") or {})
    for plate in record["data"]["plates"]:
        keys |= set(plate.get("overrides") or {})
    return keys


def _plate_entries(record: dict) -> list:
    data = record["data"]
    return [(p, effective_settings(record["id"], p["id"], record=record)) for p in data["plates"]
            if any(o["plate"] == p["id"] for o in data["objects"])]


def _object_overrides(result: dict, base: dict | None = None) -> dict:
    """Object overrides that differ from `base`: the Plate's own process unless the file is written with another Plate's."""
    base = result["process"] if base is None else base
    return {obj["id"]: {k: v for k, v in sorted((obj.get("overrides") or {}).items()) if base.get(k) != v}
            for obj in result["objects"]}


def _export_object(project_id: int, obj: dict, overrides: dict, plate_number: int, offset) -> dict:
    """An Object as the 3MF writer takes it: centred mesh plus the transform that reproduces the placement a Slice sends."""
    import numpy as np
    verts, tris = load_mesh(project_id, obj["model"])
    centre = (verts.min(axis=0) + verts.max(axis=0)) / 2
    turn = rotation_matrix(obj["rotation"]) @ np.diag(np.asarray(obj["scale"], float))
    matrix = turn.T                                  # 3MF's row-vector form
    shift = place(verts, obj)[0] - (verts[0] - centre) @ matrix + [*offset, 0.0]
    return {"name": obj["name"], "verts": verts - centre, "tris": tris, "matrix": matrix, "shift": shift,
            "plate": plate_number, "settings": overrides.get(obj["id"], {})}


def _checked_config(project_id: int, record: dict, entries: list, review: dict, template: dict) -> dict:
    config, dropped = export_planner.project_config(review, template, _authored_keys(record, entries))
    if dropped:
        raise _export_error("export-template-incomplete", "Orca has no place for these saved settings, so a faithful file is not possible: " + ", ".join(dropped), keys=dropped)
    return config


def _single_file(project_id: int, record: dict, plan: dict) -> bytes:
    data = record["data"]
    entries = _plate_entries(record)
    review = entries[0][1]
    config = _checked_config(project_id, record, entries, review, _export_template(project_id, review))
    overrides = {}
    for _, result in entries:
        overrides.update(_object_overrides(result, review["process"]))
    # Orca numbers Plates 1..n and keeps each at its own place in a grid, so a Plate's Objects stand at that Plate's origin.
    numbers = {p["id"]: n for n, p in enumerate(data["plates"], start=1)}
    limits = machine_limits(review["machine"])
    width, depth = limits["bed"][2] - limits["bed"][0], limits["bed"][3] - limits["bed"][1]
    objects = [_export_object(project_id, obj, overrides, numbers[obj["plate"]],
                              orca_3mf.plate_origin(numbers[obj["plate"]], len(numbers), width, depth)) for obj in data["objects"]]
    return orca_3mf.build(objects, [{**p, "id": numbers[p["id"]]} for p in data["plates"]], config)


def _plate_package(project_id: int, record: dict, plan: dict) -> bytes:
    """A ZIP of one self-contained Orca 3MF per nonempty Plate (each from that Plate's own effective settings, with the
    Plate alone at Orca's first Plate position) plus MANIFEST.txt. Plates sharing a configuration share one template run."""
    data = record["data"]
    entries = _plate_entries(record)
    templates, files = {}, []
    for (plate, review), item in zip(entries, plan["package"]["files"]):
        key = json.dumps({k: review[k] for k in export_planner.KINDS}, sort_keys=True)
        if key not in templates:
            templates[key] = _export_template(project_id, review)
        config = _checked_config(project_id, record, entries, review, templates[key])
        overrides = _object_overrides(review)
        objects = [_export_object(project_id, obj, overrides, 1, (0.0, 0.0)) for obj in data["objects"] if obj["plate"] == plate["id"]]
        files.append((item["filename"], orca_3mf.build(objects, [{"id": 1, "name": plate["name"]}], config)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(export_planner.MANIFEST, export_planner.manifest(record["name"], plan, entries))
        for name, body in files:
            archive.writestr(name, body)
    return out.getvalue()


def _review_required(plan: dict) -> jobs.JobError:
    if plan["blockers"]:
        error = jobs.JobError(plan["blockers"][0]["message"], 409)
        error.issues = plan["blockers"]
        return error
    names = sorted({d["key"] for d in plan["differences"]})
    message = ("A single Orca file cannot keep this Project's settings: Plates differ in " + ", ".join(names[:6])
               + (" and more" if len(names) > 6 else "") + ". Review the export plan and choose another way to export")
    return _export_error("export-review-required", message, differences=plan["differences"], revision=plan["revision"],
                         fingerprint=plan["fingerprint"])


def export_project(project_id: int, revision, fingerprint, representation: str = export_planner.SINGLE_FILE) -> dict:
    """The faithful export, in the named representation, for a plan the caller reviewed: {"body", "filename", "content_type",
    "plan"}. Refused when the Project changed since that plan or when the plan offers no faithful artifact of that kind."""
    if representation not in (export_planner.SINGLE_FILE, export_planner.PACKAGE):
        raise jobs.JobError(f"Unknown export representation: {representation}", 400)
    record = get_project(project_id)
    plan = export_plan(project_id, record=record)
    try:
        current = int(revision) == record["revision"]
    except (TypeError, ValueError):
        current = False
    if not current or fingerprint != plan["fingerprint"]:
        raise _export_error("export-plan-stale", "The Project changed since this export was planned — plan the export again",
                            revision=record["revision"], fingerprint=plan["fingerprint"])
    if representation == export_planner.PACKAGE:
        if not plan["package"]["available"]:
            raise _review_required(plan)
        package = plan["package"]
        return {"body": _plate_package(project_id, record, plan), "filename": package["filename"],
                "content_type": package["content_type"], "plan": plan}
    if not plan["faithful"]:
        raise _review_required(plan)
    return {"body": _single_file(project_id, record, plan), "filename": plan["filename"], "content_type": plan["content_type"], "plan": plan}


def export_lossy(project_id: int, acknowledgement) -> dict:
    """The explicit lossy whole-Project 3MF: {"body", "filename", "content_type", "plan"}. Generated only for an acknowledgement of
    the report as it is now: it must repeat the plan's revision and fingerprint, the report's version and fingerprint and the
    representation, and say `acknowledged: true`. The direct and faithful exports never come here. The file is the same single
    Orca project file, written with the first Plate's configuration (see export_lossy.py)."""
    ack = acknowledgement if isinstance(acknowledgement, dict) else {}
    record = get_project(project_id)
    plan = export_plan(project_id, record=record)
    lossy = plan["lossy"]

    def stale(message):
        return _export_error("export-acknowledgement-stale", message, revision=record["revision"], fingerprint=plan["fingerprint"],
                             report_fingerprint=lossy["report_fingerprint"])
    try:
        current = int(ack.get("revision")) == record["revision"]
    except (TypeError, ValueError):
        current = False
    if not current or ack.get("fingerprint") != plan["fingerprint"]:
        raise stale("The Project changed since you reviewed these losses — review the export again")
    if ack.get("acknowledged") is not True:
        raise _export_error("export-acknowledgement-required", "Acknowledge the losses before exporting the lossy 3MF")
    if ack.get("representation") != lossy["representation"]:
        raise _export_error("export-representation-mismatch", f"This acknowledgement is for {ack.get('representation')!r}, not {lossy['representation']!r}",
                            representation=lossy["representation"])
    if plan["blockers"]:
        raise _review_required(plan)
    if not lossy["available"]:
        raise _export_error("export-lossy-unavailable", "Nothing is lost exporting this Project, so there is no lossy file to acknowledge — use the faithful export"
                            if lossy["reason"] == "faithful-available" else "The lossy export is not available for this Project", reason=lossy["reason"])
    if ack.get("report_version") != lossy["report_version"] or ack.get("report_fingerprint") != lossy["report_fingerprint"]:
        raise stale("The list of losses changed since you reviewed it — review the export again")
    return {"body": _single_file(project_id, record, plan), "filename": lossy["filename"], "content_type": lossy["content_type"], "plan": plan}


def export_orca_project(project_id: int) -> bytes:
    """The legacy direct export: the single faithful file for the Project as it is now, or a review-required conflict. It never
    falls back to a thinner file or another file type."""
    record = get_project(project_id)
    plan = export_plan(project_id, record=record)
    return export_project(project_id, plan["revision"], plan["fingerprint"])["body"]


def add_split_parts(project_id: int, split_id: int, numbers=None) -> dict:
    """Bring the parts of any split model into the project, packed onto plates (see lay_out_split)."""
    return lay_out_split(project_id, numbers, split_id=split_id, adopt=True)


# ── Laying out plates ──────────────────────────────────────────────────────────

def _find_model(record: dict, model_id: int) -> dict:
    """The project's model with this id, or a 404."""
    model = next((m for m in record["models"] if m["id"] == model_id), None)
    if model is None:
        raise jobs.JobError("Model not found", 404)
    return model


def _split_or_first(record: dict, split_id: int | None) -> int | None:
    """The split a call is about: the one asked for, else the project's first (a Quick print has just one)."""
    return split_id if split_id is not None else record.get("split_id")


def _machine_limits_for(record: dict) -> dict:
    bundle_id = record["data"]["profile"]["bundle_id"]
    if not bundle_id:
        raise jobs.JobError("Choose a profile first", 409)
    return machine_limits(slicer.bundle_settings(bundle_id)["machine"])


PACK_PRESETS = {"tight": 3.0, "normal": packing.GAP_MM, "roomy": 12.0}   # how far apart Arrange and Plan plates keep objects
PACK_GAP_RANGE = (2.0, 40.0)                                              # a custom gap, mm


def _clean_pack(raw, project: bool = True) -> dict | None:
    """A packing density as stored: {"preset": tight | normal | roomy | custom, "gap": mm} and, on the project only,
    "tight_supports": true (objects with supports may then be as close as the gap says, not the supports' reach). None
    when nothing is chosen."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise jobs.JobError("Packing density must be an object")
    preset = raw.get("preset")
    if preset in PACK_PRESETS:
        gap = PACK_PRESETS[preset]
    elif preset == "custom":
        try:
            gap = float(raw.get("gap"))
        except (TypeError, ValueError):
            raise jobs.JobError("Give the gap between objects in millimetres")
        low, high = PACK_GAP_RANGE
        if not low <= gap <= high:
            raise jobs.JobError(f"The gap between objects must be between {low:g} and {high:g} mm")
        gap = round(gap, 1)
    else:
        raise jobs.JobError(f"Packing density is one of: {', '.join([*PACK_PRESETS, 'custom'])}")
    out = {"preset": preset, "gap": gap}
    if project and raw.get("tight_supports"):
        out["tight_supports"] = True
    return out


def pack_for(data: dict, plate_id=None) -> dict:
    """The packing density in force for a plate: its own choice, else the project's, else Normal. {"preset", "gap" (mm
    between plain objects), "support_gap" (between objects that need tree supports: never less than their reach unless the
    project allows it)}."""
    project = data.get("pack") or {}
    plate = next((p for p in data.get("plates", []) if p["id"] == plate_id), {}).get("pack") or {}
    chosen = plate or project
    gap = float(chosen.get("gap", packing.GAP_MM))
    support = gap if project.get("tight_supports") else max(gap, packing.SUPPORT_GAP_MM)
    return {"preset": chosen.get("preset", "normal"), "gap": gap, "support_gap": support, "tight_supports": bool(project.get("tight_supports"))}


def _gap_for(process: dict, data: dict | None = None) -> float:
    """Room to leave between objects printed with these process settings: supports need far more than the objects alone."""
    pack = pack_for(data or {})
    return pack["support_gap"] if str((process or {}).get("enable_support")) == "1" else pack["gap"]


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
                    model = by_part[item["key"][0]]
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


def _split_copies(record: dict, split_id: int) -> int:
    """How many of the split (every part of it, or the whole model) the project asked for."""
    return int((record["data"].get("split_copies") or {}).get(str(split_id), 1))


def lay_out_split(project_id: int, numbers=None, split_id: int | None = None, adopt: bool = False) -> dict:
    """Put a split model's parts on plates, packed onto as few as fit the printer profile's bed. Parts that need
    supports get plates of their own with tree supports on. Laying out again replaces the parts placed before. The split
    must belong to the project unless `adopt` (bringing in the parts of another split model)."""
    _require_geometry()
    record = get_project(project_id)
    split_id = _split_or_first(record, split_id)
    if split_id is None:
        raise jobs.JobError("This project has no split model", 409)
    if not adopt and split_id not in record["split_ids"]:
        raise jobs.JobError("That split model doesn't belong to this project", 409)
    split = splits.get_split(split_id, detail=True)
    wanted = {int(n) for n in numbers} if numbers else None
    parts = [p for p in (split.get("result") or {}).get("parts", []) if wanted is None or p["number"] in wanted]
    if not parts:
        raise jobs.JobError("Split the model before laying out its parts", 409)
    limits = _machine_limits_for(record)
    copies = _split_copies(record, split_id)
    items = [{"key": (p["number"], copy), "size": _file_footprint(splits.work_dir(split_id) / "parts" / p["file"]),
              "group": bool(p["needs_supports"])} for p in parts for copy in range(copies)]
    profile = record["data"]["profile"]["process"]
    layout = [plate for supported in (False, True)   # plain plates first, then the ones with supports
              for plate in packing.pack([i for i in items if i["group"] == supported], limits["bed"], limits["exclude"],
                                        gap=_gap_for({**profile, **(SUPPORT_OVERRIDES if supported else {})}, record["data"]))]
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


def place_whole_model(project_id: int, split_id: int | None = None) -> dict:
    """Put one of the project's uploaded models (the first split unless `split_id`) on a plate whole, for a model
    that fits the printer without splitting."""
    _require_geometry()
    record = get_project(project_id)
    split_id = _split_or_first(record, split_id)
    if split_id is None or split_id not in record["split_ids"]:
        raise jobs.JobError("This project has no uploaded model", 409)
    split = splits.get_split(split_id, detail=True)
    if not split.get("analysis"):
        raise jobs.JobError("Wait for the model to be analysed", 409)
    limits = _machine_limits_for(record)
    x0, y0, x1, y1 = limits["bed"]
    rotation = _orientation(record, split_id)
    size = _oriented_size(split, rotation) if any(rotation) else split["analysis"]["size"]
    center = _whole_position(size, limits) or ((x0 + x1) / 2, (y0 + y1) / 2)
    model = _store_source(project_id, _next_id(record["models"]), split["source_name"],
                          _copy_from(splits.work_dir(split_id) / split["source_file"]), split["settings"]["scale"], split["settings"]["up"])
    model["split"] = {"id": split_id, "part": 0, "supports": False}
    copies = _split_copies(record, split_id)
    if copies > 1:
        layout = packing.pack([{"key": (0, i), "size": tuple(size[:2]), "group": False} for i in range(copies)], limits["bed"], limits["exclude"])
    else:
        layout = [{"group": False, "items": [{"key": (0, 0), "center": center, "turned": False, "oversize": False}]}]
    placed = _replace_split_models(project_id, split_id, [model], layout)
    if any(rotation):   # a copy the packer turned a quarter keeps that turn on top of the model's own orientation
        def orient(d):
            for o in d["objects"]:
                if o["model"] == model["id"]:
                    o["rotation"] = [rotation[0], rotation[1], (rotation[2] + o["rotation"][2]) % 360]
        _mutate(project_id, "data", orient)
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


def _scale_fixes(longest: float, scale: float, size: list) -> list:
    """Every unit mix-up that would make `longest` (mm) a believable size (10 to 1000 mm), most common units first, each with
    the scale it sets and the model size it gives. `size` is the model's size at `scale`."""
    return [{"factor": factor, "unit": unit, "scale": round(scale * factor, 6), "size": [round(x * factor, 1) for x in size]}
            for factor, unit in SCALE_GUESSES if 10.0 <= longest * factor <= 1000.0]


def _loose_limits(record: dict) -> dict:
    """The project's machine limits, or the defaults while it has no profile yet."""
    bundle_id = record["data"]["profile"].get("bundle_id")
    return machine_limits(slicer.bundle_settings(bundle_id)["machine"] if bundle_id else {})


def _verdict(analysis: dict, scale: float, limits: dict, rotation, turned=None) -> dict:
    """What one analysed model needs: its size at `scale` (the unscaled size when that scale looks wrong), whether the size is
    plausible, the scale fix if not, whether it fits the bed whole, and the reason it can't print yet. `turned(rotation)`
    gives the size after the model is rotated, for a model whose mesh is at hand."""
    size = [float(x) for x in analysis["size"]]
    longest = max(analysis.get("raw_size") or size) * scale    # the rounded size reads 0.0 for a speck
    doubtful = not PLAUSIBLE_MM[0] <= longest <= PLAUSIBLE_MM[1]
    fix = _scale_suggestion(longest, scale) if doubtful else None
    if doubtful:   # the analysis rounds to 0.1 mm, which reads 0 for a speck
        size = [round(x * scale, 4) for x in analysis.get("raw_size") or size]
    if any(rotation) and not doubtful and turned:
        size = turned(rotation)
    fits = _whole_position(size, limits) is not None
    reason = None
    if doubtful:
        reason = f"The model is {' x '.join(f'{x:g}' for x in size)} mm (longest side {longest:.3g} mm), which looks like the wrong scale"
    elif not fits:
        reason = "The model doesn't fit the bed whole, so it needs the Model and Split steps"
    return {"size": size, "scale": scale, "fix": fix, "scale_fixes": _scale_fixes(longest, scale, size) if doubtful else [],
            "fits": fits, "doubtful": doubtful, "reason": reason}


def intake(project_id: int, split_id: int | None = None) -> dict:
    """What an imported model needs before it can print: analysed yet, repaired, whether it fits the bed whole (so Split
    can be skipped), whether its size is plausible for its scale, and a rough estimate."""
    record = get_project(project_id)
    split_id = _split_or_first(record, split_id)
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
    rotation = _orientation(record, split_id)
    verdict = _verdict(analysis, float(split["settings"]["scale"]), _loose_limits(record), rotation, lambda turned: _oriented_size(split, turned))
    size, scale, fix, fits, doubtful, reason = (verdict[k] for k in ("size", "scale", "fix", "fits", "doubtful", "reason"))
    grams = analysis["volume_cm3"] * ROUGH_G_PER_CM3
    return {**out, "state": "ready", "size": size, "volume_cm3": analysis["volume_cm3"],
            "repaired": bool(analysis.get("repairs")), "repairs": analysis.get("repairs") or [],
            "scale": scale, "rotation": list(rotation), "scale_ok": not doubtful, "scale_fix": fix,
            "fits": fits, "can_print": reason is None, "reason": reason,
            "estimate": {"grams": round(grams), "seconds": round(grams / ROUGH_G_PER_HOUR * 3600), "rough": True}}


def _orientation(record: dict, split_id: int | None = None) -> list:
    """The rotation (degrees about X, Y, Z) a split's model will be placed with, before it is on a plate. A split without
    one of its own takes the project's single orientation when it is the project's first split (how projects saved before
    this were stored), else none."""
    split_id = _split_or_first(record, split_id)
    own = (record["data"].get("orientations") or {}).get(str(split_id))
    if own is None and split_id == record.get("split_id"):
        own = record["data"].get("orientation")
    return [float(r) for r in own or (0.0, 0.0, 0.0)]


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


def _quick_orientation(project_id: int, change, split_id: int | None = None) -> dict:
    """Change a quick model's orientation before it is placed: `change(split, current rotation)` gives the new one."""
    record = get_project(project_id)
    split_id = _split_or_first(record, split_id)
    if split_id is None:
        raise jobs.JobError("This project has no uploaded model", 409)
    if any((m.get("split") or {}).get("id") == split_id for m in record["models"]):
        raise jobs.JobError("The model is already on a plate; turn it there", 409)
    split = splits.get_split(split_id, detail=True)
    if not split.get("analysis"):
        raise jobs.JobError("Wait for the model to be analysed", 409)
    rotation = [r % 360 for r in change(split, _orientation(record, split_id))]

    def store(data):
        data.setdefault("orientations", {})[str(split_id)] = rotation
        if split_id == record.get("split_id"):   # the first split's stays where older projects (and readers) keep it
            data["orientation"] = rotation
    _mutate(project_id, "data", store)
    return intake(project_id, split_id)


def lay_flat(project_id: int, split_id: int | None = None) -> dict:
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
    return _quick_orientation(project_id, change, split_id)


def rotate_quarter(project_id: int, split_id: int | None = None) -> dict:
    """Turn the model 90 degrees about the vertical axis."""
    return _quick_orientation(project_id, lambda split, current: [current[0], current[1], (current[2] + 90) % 360], split_id)


def auto_place(project_id: int, split_id: int | None = None) -> dict:
    """Skip Split for a model that fits the bed: it lands on one auto plate. Nothing happens for a model that doesn't fit,
    has a doubtful scale, or is already placed."""
    info = intake(project_id, split_id)
    placed = False
    if info["can_print"] and not info["placed"]:
        place_whole_model(project_id, split_id)
        placed = True
        info = intake(project_id, split_id)
    return {"placed": placed, "intake": info, "project": get_project(project_id)}


def open_as_project(project_id: int) -> dict:
    """Promote a quick project to a normal, visible Project (also the hand-off for a model too big for the bed).
    Only the flag changes, so the model, plate and slice stay as they are; updated_at moves it to the top of the list."""
    get_project(project_id)
    with database.transaction() as conn:
        conn.execute("UPDATE projects SET quick = 0, updated_at = ? WHERE id = ?", (time.time(), project_id))
    return get_project(project_id)


def fix_scale(project_id: int, split_id: int | None = None) -> dict:
    """Apply the suggested scale correction; the model is analysed again."""
    info = intake(project_id, split_id)
    if info["state"] != "ready" or not info.get("scale_fix"):
        raise jobs.JobError("There is no scale correction to apply", 409)
    if info["placed"]:
        raise jobs.JobError("The model is already on a plate; change its scale there", 409)
    splits.update_settings(_split_or_first(get_project(project_id), split_id), {"scale": info["scale_fix"]["scale"]})
    return intake(project_id, split_id)


def intake_files(project_id: int) -> dict:
    """The intake table: one row per imported model with what it needs before the project is created (the per-model slice of
    intake), and a summary (`models`, `objects` made by the Copies, how many are too big, fit, need a units check, ...). `status` is the row's chip: fits, split (too big for the bed), check_units, or None while it has
    no verdict. Fit is worked out here against the project's own machine, never the engine's default printer."""
    record = get_project(project_id)
    limits = _loose_limits(record)
    rows = []
    for model in record["models"]:
        objects = [o for o in record["data"]["objects"] if o["model"] == model["id"]]
        row = {"model": model["id"], "object": objects[0]["id"] if objects else None, "file": model["file"], "kind": "model",
               "state": model["state"], "error": model.get("error"), "size_bytes": model.get("size_bytes"),
               "size": None, "scale": model["import"][0], "up": model["import"][1], "status": None, "fits": None,
               "scale_ok": None, "scale_fix": None, "scale_fixes": [], "reason": None, "repairs": model.get("repairs") or [],
               "triangles": model.get("triangles"), "volume_cm3": model.get("volume_cm3"), "copies": len(objects), "estimate": None}
        if model["state"] == READY and model.get("size"):
            verdict = _verdict(model, float(row["scale"]), limits, (0.0, 0.0, 0.0))
            grams = (model.get("volume_cm3") or 0) * ROUGH_G_PER_CM3
            row.update(size=verdict["size"], fits=verdict["fits"], scale_ok=not verdict["doubtful"], scale_fix=verdict["fix"], scale_fixes=verdict["scale_fixes"],
                       reason=verdict["reason"], status="check_units" if verdict["doubtful"] else "fits" if verdict["fits"] else "split",
                       estimate={"grams": round(grams), "seconds": round(grams / ROUGH_G_PER_HOUR * 3600), "rough": True})
        rows.append(row)
    count = lambda status: sum(1 for r in rows if r["status"] == status)
    analysing = sum(1 for r in rows if r["state"] not in (READY, FAILED))
    failed = sum(1 for r in rows if r["state"] == FAILED)
    summary = {"models": len(rows), "objects": sum(r["copies"] for r in rows), "split": count("split"), "fits": count("fits"),
               "check_units": count("check_units"), "analysing": analysing, "failed": failed}
    summary["can_create"] = bool(rows) and not (summary["check_units"] or analysing or failed)
    summary["steps"] = (["split"] if summary["split"] else []) + ["plates", "slice", "print"]
    suggested = None
    if bundle_id := record["data"].get("profile", {}).get("bundle_id"):
        try:
            settings = slicer.bundle_settings(bundle_id)
            slicer.verify_bundle(settings["bundle"])
            slicer.filament_compatibility(settings["filament"], settings["machine"])
            suggested = {"name": settings["filament"]["name"], "material": settings["filament"]["filament_type"]}
        except jobs.JobError as exc:
            suggested = {"error": str(exc)}
    return {"rows": rows, "summary": summary, "suggested_filament": suggested}


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
    """What the nester needs to know of each object of a project: its outline from above and its clearance (the packing
    density's support gap when it needs tree supports in its orientation, else its gap; `pack_for`), cached."""

    def __init__(self, project_id: int, record: dict, data: dict):
        import nesting
        self.nesting, self.project_id, self.record = nesting, project_id, record
        self.plates = {p["id"]: p for p in data["plates"]}
        self.data = data
        self.processes, self.meshes, self.shapes = {}, {}, {}

    def mesh(self, model_id: int):
        if model_id not in self.meshes:
            self.meshes[model_id] = load_mesh(self.project_id, model_id)
        return self.meshes[model_id]

    def outline_and_clearance(self, obj: dict) -> tuple:
        pack = pack_for(self.data, obj["plate"])
        key = (obj["model"], obj["plate"], tuple(obj["rotation"]), tuple(obj["scale"]), json.dumps(obj.get("overrides") or {}, sort_keys=True),
               pack["gap"], pack["support_gap"])
        if key not in self.shapes:
            if obj["plate"] not in self.processes:
                self.processes[obj["plate"]] = _plate_process(self.record, self.plates[obj["plate"]])
            verts, tris = self.mesh(obj["model"])
            hull = self.nesting.hull_outline(place(verts, {**obj, "position": [0.0, 0.0]})[:, :2])
            needs = _needs_supports({**self.processes[obj["plate"]], **(obj.get("overrides") or {})}, verts, tris, obj)
            self.shapes[key] = hull, pack["support_gap"] if needs else pack["gap"]
        return self.shapes[key]

    def polygon(self, obj: dict) -> list:
        """The object's outline where it stands on the bed."""
        return self.nesting.hull_outline(place(self.mesh(obj["model"])[0], obj)[:, :2])


def placement_problems(project_id: int, record: dict, plate_ids=None) -> list:
    """Whether the ready objects on these plates (all by default) are placed validly, by the one definition every layout
    is held to (`nesting.placement_problems`): on the bed, clear of the purge area, and as far apart as Arrange keeps them
    (the larger of their two clearances). Judged on each object's real outline where it stands, not on its bounding box.
    Returns [{"key": object id, "plate", "problem": a kind of `problem_kinds`, "other": object id
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


_problem_cache: dict = {}   # project id → (key, problems), so the page's polling doesn't measure every mesh again


def _judged_placement(project_id: int, record: dict) -> list:
    """`placement_problems` of every ready object, remembered while nothing it depends on changes: the project's data, its
    models' states and the profile's machine and process settings (bed, purge area and supports all move the limits).
    [] when nothing can be judged (no geometry libraries, no profile, a model's mesh unreadable); that is never remembered,
    so the next ask measures again."""
    try:
        settings = slicer.bundle_settings(record["data"]["profile"]["bundle_id"] or "")
        key = json.dumps([record["data"], [(m["id"], m["state"]) for m in record["models"]], settings["machine"], settings["process"]],
                         sort_keys=True, default=str)
        cached = _problem_cache.get(project_id)
        if cached and cached[0] == key:
            return cached[1]
        found = [{"key": p["key"], "plate": p["plate"], "problem": p["problem"], "other": p["other"]}
                 for p in placement_problems(project_id, record)]
    except (jobs.JobError, OSError, KeyError, ValueError):
        return []
    _problem_cache[project_id] = (key, found)
    return found


def plate_problems(project_id: int, record: dict, plate_ids=None) -> list:
    """The one definition of a problem Object, used by the Plates step, the step bar, the Slice findings and the queue send:
    an Object that is off the bed, over the purge area, overlapping or too close to another (`placement_problems`), or
    whose model failed to import. [{"key", "plate", "name", "problem", "other", "text"}], in object order; "text" says it in
    words (`problem_kinds` has the kinds and the wording)."""
    data = record["data"]
    covered = None if plate_ids is None else set(plate_ids)
    failed = {m["id"] for m in record["models"] if m["state"] == FAILED}
    found = _judged_placement(project_id, record) + [
        {"key": o["id"], "plate": o["plate"], "problem": problem_kinds.FAILED_MODEL, "other": None}
        for o in data["objects"] if o["model"] in failed]
    order = {o["id"]: i for i, o in enumerate(data["objects"])}
    found.sort(key=lambda p: (order.get(p["key"], 0), p["problem"]))
    return [p for p in problem_kinds.describe(found, problem_kinds.object_names(data)) if covered is None or p["plate"] in covered]


def hard_plate_problems(project_id: int, record: dict, plate_ids=None) -> list:
    """`plate_problems` without the warnings (`problem_kinds.SOFT`): the ones that stop a Slice or a send."""
    return [p for p in plate_problems(project_id, record, plate_ids) if not p["soft"]]


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


def _too_big_text(count: int) -> str:
    """"; N objects are bigger than the bed (left centred on a plate of its own)", else "": how Arrange and Plan plates both
    say that an object fits no plate."""
    return f"; {_count(count, 'object')} {'is' if count == 1 else 'are'} bigger than the bed (left centred on a plate of its own)" if count else ""


def _too_big_for_the_bed(after: dict, plate_ids: list) -> str:
    """`_too_big_text` for the unpinned ready objects still off the bed after an Arrange of these plates, which only an
    object too big for any plate can be. A pinned object stays where its owner put it, and a plate still importing was not
    arranged, so neither is counted."""
    ready = {m["id"] for m in after["models"] if m["state"] == READY}
    failed = {m["id"] for m in after["models"] if m["state"] == FAILED}
    busy = {o["plate"] for o in after["data"]["objects"] if o["model"] not in ready and o["model"] not in failed}
    pinned = {o["id"] for o in after["data"]["objects"] if o.get("pinned")}
    arranged = [p for p in plate_ids if p not in busy]
    big = {p["key"] for p in placement_problems(after["id"], after, arranged) if p["problem"] == problem_kinds.OFF_THE_BED} - pinned
    return _too_big_text(len(big))


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
        arranging = [plate_id for plate_id in order
                     if any(o["plate"] == plate_id and o["model"] in ready and not o.get("pinned") for o in record["data"]["objects"])]
        return _arrange_with_orca(project_id, arranging) if arranging else record
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
    """Whether an orientation stands the object on a side of its own box: X and Y are quarter turns (a degree of slack).
    Any other tilt leans it on an angled face, which Plan plates only does when the user opts in."""
    return all(abs((r + 45) % 90 - 45) <= 1.0 for r in rotation[:2])


def _plan_problems(project_id: int, record: dict, data: dict, plate_ids, oversize=()) -> list:
    """What is wrong with the plan's layout on these plates, by the one validity check (`placement_problems`). A pinned object
    stays where its owner put it, so a problem that only involves pinned objects is not the plan's doing and is left out;
    nor is an `oversize` object (one too big for any plate), which the plan puts on a plate of its own and reports."""
    pinned = {o["id"] for o in data["objects"] if o.get("pinned")}
    return [p for p in placement_problems(project_id, {**record, "data": data}, plate_ids)
            if p["key"] not in oversize and not (p["key"] in pinned and (p["other"] is None or p["other"] in pinned))]


def _plan_data(project_id: int, record: dict, allow_tilt: bool = False, pack=None) -> dict:
    """The project data as Plan plates would leave it, changing nothing. Plates that are queued, printing, have a Print on
    record, or hold a model still importing are kept as they are. On the others, every unpinned object is oriented
    (in memory, as Orient all would) and packed with `arrange_layout` (a pinned object keeps its plate and place; a plate
    never mixes filament choices), then the plates are ordered longest first by `time_estimate.rank_key`; plates keep their
    ids, which are what slices and jobs follow, while the order and the default names ("Plate 3") follow the plan, filling
    the numbers the kept plates leave free. Returns {"data", "plates": [{"id", "new", "objects", "turned", "features"}],
    "kept": [{"id", "name", "reason"}], "emptied": [plate ids removed]}; "plates" is empty when there is nothing to plan."""
    data = json.loads(json.dumps(record["data"]))
    if pack is not None:   # "what if": plan with this density without keeping it
        chosen = _clean_pack({"tight_supports": bool((data.get("pack") or {}).get("tight_supports")), **pack})
        data["pack"] = chosen
        for plate in data["plates"]:
            plate.pop("pack", None)
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
    # The orientation chosen for printing can make a part too big for the bed that it fit in as it was (a long, thin part laid
    # on a broad face, say): such a part goes back to the orientation it came in, and the layout is made again, but only when
    # it stands steady that way. A sheet stood on its edge to squeeze it in would not print; it stays too big and is reported.
    unfit = {item["key"] for plate in layout for item in plate["items"] if item["oversize"]}
    back = [o for o in data["objects"] if o["id"] in unfit and list(o["rotation"]) != before[o["id"]]
            and _steady(project_id, {**o, "rotation": before[o["id"]]})]
    if back:
        for obj in back:
            obj["rotation"] = list(before[obj["id"]])
        layout = arrange_layout(project_id, {**record, "data": data}, replannable)
    if not layout:
        return {"data": record["data"], "plates": [], "kept": kept, "emptied": [], "fingerprint": _plan_fingerprint(record["data"], [])}
    used = _place_layout(data, replannable, layout, lambda group: json.loads(group)["overrides"])
    names = problem_kinds.object_names(data)
    too_big = [{"id": item["key"], "name": names[item["key"]]} for plate in layout for item in plate["items"] if item["oversize"]]
    big_ids = {big["id"] for big in too_big}
    tippy = [{"id": o["id"], "name": names[o["id"]]} for o in data["objects"]   # fits the bed only standing on a narrow edge
             if o["plate"] in used and o["model"] in ready and not o.get("pinned") and o["id"] not in big_ids
             and not _steady(project_id, o)]
    problems = _plan_problems(project_id, record, data, used, {big["id"] for big in too_big})
    if problems:   # a plan that is not valid is not offered: nothing is proposed and the reason is said
        return {"data": record["data"], "plates": [], "kept": kept, "emptied": [], "problems": problems,
                "refused": problem_kinds.planned_reason(problems, names, _count), "fingerprint": _plan_fingerprint(record["data"], [])}
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
    return {"data": data, "plates": proposed, "kept": kept, "emptied": emptied, "tilts_withheld": withheld, "too_big": too_big,
            "tippy": tippy, "fingerprint": _plan_fingerprint(data, emptied)}


STEADY_RATIO = 3.0   # a part taller than this many times its narrowest width on the bed is too tippy to print as it stands


def _steady(project_id: int, obj: dict) -> bool:
    """Whether the object, posed as `obj` says, stands steady: its height is at most STEADY_RATIO times the narrowest width
    of its footprint (measured across every turn about Z, so a part lying diagonally isn't mistaken for a wide one)."""
    import numpy as np
    try:
        verts = place(np.asarray(load_mesh(project_id, obj["model"], preview=True)[0], float), obj)
    except (OSError, ValueError):
        return False
    height = np.ptp(verts[:, 2])
    xy = verts[:, :2]
    widths = []
    for degrees in range(0, 180, 5):
        a = np.radians(degrees)
        widths.append(np.ptp(xy @ np.array([np.cos(a), np.sin(a)])))
    return height <= STEADY_RATIO * min(widths)


def _plan_fingerprint(data: dict, emptied: list) -> str:
    """Identifies what a plan would do: the data it would leave and the plates it would remove."""
    return hashlib.sha256(json.dumps({"data": data, "emptied": sorted(emptied)}, sort_keys=True).encode()).hexdigest()[:16]


def _plan_view(record: dict, planned: dict) -> dict:
    """What a plan shows: the proposed plates in plan order, the plates kept as they are, and a one-line summary."""
    data = planned["data"]
    names = {p["id"]: p["name"] for p in data["plates"]}
    shapes = _Shapes(record["id"], record, data) if planned["plates"] else None

    def outline(obj: dict) -> list | None:   # the outline from above the sheet draws: a bounding box would overlap its neighbours
        try:
            return [[round(x, 1), round(y, 1)] for x, y in shapes.polygon(obj)]
        except (OSError, ValueError):
            return None

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
                                    "rotation": o["rotation"], "pinned": bool(o.get("pinned")), "outline": outline(o)}
                                   for o in entry["objects"]]})
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
        summary += _too_big_text(len(planned.get("too_big", [])))
        tippy = planned.get("tippy", [])
        if tippy:
            summary += (f"; {', '.join(t['name'] for t in tippy[:3])}{' …' if len(tippy) > 3 else ''} only "
                        f"{'fits' if len(tippy) == 1 else 'fit'} standing on a narrow edge, too tall to print steadily "
                        f"(too big to lie flat: {'it needs' if len(tippy) == 1 else 'they need'} splitting)")
    else:
        summary = "Nothing to plan"
    summary += _skipped_failed(record)
    return {"revision": record["revision"], "fingerprint": planned["fingerprint"], "plates": plates, "kept": kept,
            "bed": list(_machine_limits_for(record)["bed"]), "pack": pack_for(data),
            "summary": summary, "outcome": summary,   # "outcome" is the key every Arrange-like answer carries: the page shows it as is, "summary" is the plan's own name for it
            "tilts_withheld": planned.get("tilts_withheld", 0), "too_big": planned.get("too_big", []), "tippy": planned.get("tippy", []),
            "problems": planned.get("problems", []), "refused": planned.get("refused")}


def plan_plates(project_id: int, allow_tilt: bool = False, pack=None) -> dict:
    """Plan the project's plates and change nothing: {"revision", "plates": [{"id", "name", "new", "filament", "parts" (how many
    objects it holds; the key keeps its name), "turned", "time_estimate" (approximate seconds, layers, volume_cm3), "objects"}] in plan order (longest first by
    layers, then volume), "kept": [{"id", "name", "reason"}] (plates with a Job or a Print on record, which are never
    touched), "summary", "tilts_withheld", "too_big" ([{"id", "name"}]: objects too big for any plate, each left centred on a plate of its own),
    "problems", "refused"}. Only orientations that stand an object on a side of its own box
    are planned; a tilt onto an angled face needs `allow_tilt`. A plan whose layout would leave an object (other than one too big
    for any plate) off the bed, over the purge area, overlapping or too close to another is not offered: "plates" is empty, "refused" says why and "problems" lists them.
    `accept_plan` applies it."""
    record = get_project(project_id)
    return _plan_view(record, _plan_data(project_id, record, allow_tilt, pack))


def plan_densities(project_id: int, allow_tilt: bool = False) -> dict:
    """How many plates the plan needs at each preset density, changing nothing: {"tight": {"plates", "refused"}, "normal":
    …, "roomy": …}. "plates" counts the plates the plan fills (plates kept as they are not included)."""
    record = get_project(project_id)
    found = {}
    for preset in PACK_PRESETS:
        planned = _plan_data(project_id, record, allow_tilt, {"preset": preset})
        found[preset] = {"plates": len(planned["plates"]), "refused": planned.get("refused")}
    return found


def set_pack(project_id: int, body: dict) -> dict:
    """Choose the packing density: `pack` for the project ({"preset": tight | normal | roomy | custom, "gap" for custom,
    "tight_supports"}; null goes back to Normal), and/or `plate_id` with `plate_pack` for one plate (null follows the
    project). Objects already on plates are not moved: Arrange and Plan plates use it from now on, and "too close" is
    judged by it."""
    record = get_project(project_id)
    data = json.loads(json.dumps(record["data"]))
    if "plate_id" in body and body["plate_id"] is not None:
        plate = next((p for p in data["plates"] if p["id"] == body["plate_id"]), None)
        if plate is None:
            raise jobs.JobError("Plate not found", 404)
        chosen = _clean_pack(body.get("plate_pack"), project=False)
        plate.pop("pack", None)
        if chosen:
            plate["pack"] = chosen
    if "pack" in body:
        chosen = _clean_pack(body["pack"])
        data.pop("pack", None)
        if chosen:
            data["pack"] = chosen
    if data != record["data"]:
        _save_data(project_id, record, data)
        _problem_cache.pop(project_id, None)
    return get_project(project_id)


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
    _find_model(record, model_id)
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
    model = _find_model(record, model_id)
    if model["state"] != FAILED:
        raise jobs.JobError("Only a model that failed to import can be retried", 409)

    def requeue(models):
        for m in models:
            if m["id"] == model_id and m["state"] == FAILED:
                m.update(state=IMPORTING, error=None)
                m.pop("claimed_at", None)
    _mutate(project_id, "models", requeue, record["created_at"])
    return get_project(project_id)


def rescale_model(project_id: int, model_id: int, scale=None, up=None) -> dict:
    """Change an imported model's scale and/or up axis (the unit fix) and analyse it again. A model that is already
    parts laid out from a split can't be changed this way."""
    record = get_project(project_id)
    model = _find_model(record, model_id)
    if model.get("split"):
        raise jobs.JobError("This model is already laid out as parts; change its scale on the plate", 409)
    if model["state"] == IMPORTING and model.get("claimed_at"):
        raise jobs.JobError("The model is being analysed; try again in a moment", 409)
    settings = splits.normalize_settings({k: v for k, v in (("scale", scale), ("up", up)) if v is not None},
                                         {"scale": model["import"][0], "up": model["import"][1]})
    folder = model_dir(project_id, model_id)
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    (folder / "job.json").write_text(json.dumps({**job, "scale": settings["scale"], "up": settings["up"]}), encoding="utf-8")
    for name in ("analysis.json", "model.npz", "work.npz", "preview.bin", "error.json"):
        (folder / name).unlink(missing_ok=True)

    def requeue(models):
        for m in models:
            if m["id"] == model_id:
                for key in ("claimed_at", "size", "raw_size", "triangles", "volume_cm3", "repairs"):
                    m.pop(key, None)
                m.update(state=IMPORTING, error=None, **{"import": [settings["scale"], settings["up"]]})
    _mutate(project_id, "models", requeue, record["created_at"])
    notify_worker()
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
    return {"state": READY, "error": None, "size": analysis.get("size"), "raw_size": analysis.get("raw_size"),
            "triangles": analysis.get("triangles"), "volume_cm3": analysis.get("volume_cm3"), "repairs": analysis.get("repairs", [])}


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
                    config.append(f'    <metadata key={escape_attr(key)} value={escape_attr(orca_3mf.metadata_value(value))}/>\n')
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


def _arrange_with_orca(project_id: int, plate_ids: list) -> dict:
    """Let OrcaSlicer arrange the unpinned objects of these plates, one plate at a time (it knows the bed and its exclusion
    area). Its answers are held to the nester's definition of a valid layout as they would be written, and are all used or
    none is: an invalid answer for any plate is refused and no plate changes. Blocking."""
    record = get_project(project_id)
    placements = {}
    for plate_id in plate_ids:
        placements.update(_orca_arrangement(project_id, record, plate_id))
    data = json.loads(json.dumps(record["data"]))
    _move_objects(data, placements)
    _require_valid_arrangement(project_id, record, data, plate_ids)
    _save_data(project_id, record, data)   # refused if the project changed while OrcaSlicer was working: what was checked is what is written
    return get_project(project_id)


def _orca_arrangement(project_id: int, record: dict, plate_id: int) -> dict:
    """OrcaSlicer's answer for one plate: object id → (x, y, extra turn about Z), nothing changed. Blocking."""
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
        return read_arrangement(out)


def _move_objects(data: dict, placements: dict) -> None:
    """Put the objects of `data` where an arrangement says (object id → (x, y, extra turn about Z)), in place."""
    for obj in data["objects"]:
        if obj["id"] in placements:
            x, y, turn = placements[obj["id"]]
            obj["position"] = [round(x, 3), round(y, 3)]
            obj["rotation"][2] = round((obj["rotation"][2] + turn) % 360, 3)


def _require_valid_arrangement(project_id: int, record: dict, data: dict, plate_ids: list) -> None:
    """Hold OrcaSlicer's answer, as it is in `data` (what would be written), to the nester's definition of a valid layout
    (`placement_problems`) on these plates: on the bed, off the purge area, objects as far apart as Arrange keeps them. An
    answer that fails is refused with the reason and nothing is changed, so choosing OrcaSlicer is never worse than the nester."""
    found = placement_problems(project_id, {**record, "data": data}, plate_ids)
    if not found:
        return
    reason = problem_kinds.reason(problem_kinds.describe(found, problem_kinds.object_names(data)))
    raise jobs.JobError(f"OrcaSlicer's arrangement was not used, {'the plate is' if len(plate_ids) == 1 else 'the plates are'} unchanged: "
                        f"{reason}. Arrange again, use the nester, or move the objects by hand", 409)


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

def _resolve_plate(record: dict, plate_id: int) -> tuple:
    """The shared resolution of a Plate's print inputs from a captured record: (result, settings, alternate Filament values, choice).
    Reads the locked profile and Filament snapshot files; never the mutable preset catalogue."""
    data = record["data"]
    plate = next((p for p in data["plates"] if p["id"] == plate_id), None)
    if plate is None:
        raise jobs.JobError("Plate not found", 404)
    if not data["profile"]["bundle_id"]:
        raise jobs.JobError("Choose a profile first", 409)
    settings = slicer.bundle_settings(data["profile"]["bundle_id"])
    slicer.verify_bundle(settings["bundle"])
    issues = []
    mix = plate_mix(data, plate_id)
    if mix:
        issues.append({"code": "conflicting-filament-choices", "scope": "plate", "plate_id": plate_id,
                       "affected_ids": [o["id"] for o in data["objects"] if o["plate"] == plate_id],
                       "affected_names": [o["name"] for o in data["objects"] if o["plate"] == plate_id],
                       "severity": "error", "resolution_required": True, "message": mix})
    alternate = {} if mix else None
    chosen = next((c for c in plate_filaments(data, plate_id) if c), None)
    if not chosen and not mix:
        try:
            slicer.filament_compatibility(settings["filament"], settings["machine"])
        except jobs.JobError as exc:
            alternate = {}
            issues.append({"code": "unresolved-filament", "scope": "project", "plate_id": plate_id,
                           "severity": "error", "resolution_required": True, "message": str(exc)})
    if chosen and not mix:
        try:
            if not chosen.get("snapshot"):
                raise jobs.JobError(f"{chosen['preset']} has no verified saved material — select its Filament again. Historical profiles are retained but cannot establish this choice's material", 409)
            _, alternate = slicer.filament_snapshot(chosen["snapshot"], settings["machine"])
        except jobs.JobError as exc:
            alternate = {}
            issues.append({"code": "unresolved-filament", "scope": "model", "plate_id": plate_id,
                           "affected_ids": [o["id"] for o in data["objects"] if o["plate"] == plate_id],
                           "severity": "error", "resolution_required": True, "message": f"{chosen['preset']}: {exc}"})
    result = plate_settings.resolve(record, plate, settings, slicer.BED_TYPES[data["bed"]], alternate, issues)
    return result, settings, alternate, chosen


def effective_settings(project_id: int, plate_id: int, revision=None, *, record=None, object_id=None, search="", changed_only=False) -> dict:
    """Read the exact common inputs for a Plate without mutating the Project.

    Version 1 preserves existing alternate-preset resolution; immutable alternate
    snapshots and stricter Object scope are subsequent tickets. Slice preparation
    passes its captured record so review and preparation share the same calculation.
    """
    import farm   # imported late: farm reads Projects
    record = record if record is not None else get_project(project_id)
    if revision is not None and int(revision) != record["revision"]:
        raise jobs.JobError("The Project changed — review its settings again", 409)
    data = record["data"]
    result, settings, alternate, chosen = _resolve_plate(record, plate_id)
    plate = next(p for p in data["plates"] if p["id"] == plate_id)
    for kind in ("process", "filament"):
        if kind == "filament" and alternate is not None:
            continue
        result["issues"].extend(slicer.setting_issues(data["profile"].get(kind) or {}, settings, kind, "project"))
    result["issues"].extend(slicer.setting_issues(plate.get("overrides") or {}, settings, "process", "plate", plate_id))
    for obj in result["objects"]:
        obj["effective_process"] = {**result["process"], **obj.get("overrides", {})}
        result["issues"].extend(slicer.setting_issues(obj.get("overrides") or {}, settings, "process", "object", obj["id"]))
    for kind in ("process", "filament"):
        inherited = alternate if kind == "filament" and alternate is not None else settings[kind]
        result["issues"].extend(slicer.setting_issues(inherited, settings, kind, "preset", inherited=True))
    result["issues"].extend(i for i in _coupled_setting_issues(data, settings)
                            if i["plate_id"] == plate_id)
    result["filament_choice"] = chosen
    result["issues"] = [plate_settings.link_issue(i) for i in result["issues"]]
    try:
        result["review"] = plate_settings.review(result, slicer.settings_schema(), object_id=object_id, search=search, changed_only=changed_only,
                                                 spool_id=data["profile"].get("spool_id"), spool_keys=farm.SPOOL_KEYS)
    except KeyError:
        raise jobs.JobError("Object not found on this Plate", 404) from None
    return result

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


def _stage(project_id: int, plate_id: int, stage: str, created_at: float | None = None) -> None:
    """Tell the page what a running slice is doing (preparing → slicing → post-processing → analysing)."""
    def change(slices):
        entry = slices.get(str(plate_id))
        if entry and entry.get("state") == SLICING:
            entry.update(stage=stage, stage_at=time.time())
    _mutate(project_id, "slices", change, created_at)


def _require_one_filament(record: dict, plate_ids) -> None:
    """A plate that mixes filament choices can't be sliced."""
    for plate in record["data"]["plates"]:
        if plate["id"] in plate_ids:
            review = effective_settings(record["id"], plate["id"], record=record)
            for issue in review["issues"]:
                if issue.get("code") in ("unresolved-filament", "conflicting-filament-choices"):
                    error = jobs.JobError(issue["message"], 409)
                    error.issues = [issue]
                    raise error


def _require_resolved_settings(record: dict, plate_ids) -> None:
    for plate_id in plate_ids:
        review = effective_settings(record["id"], plate_id, record=record)
        blockers = [i for i in review["issues"] if i.get("resolution_required") and i.get("scope") != "preset"]
        if blockers:
            error = jobs.JobError("Resolve the saved settings before creating new output: " + blockers[0]["message"], 409)
            error.issues = blockers
            raise error


def request_slice(project_id: int, plate_id: int, send: bool = False, send_opts: dict | None = None) -> dict:
    """Slice one plate. With `send`, the finished plate goes to the queue for its own printer (or the next free one when it has none)."""
    record = get_project(project_id)
    if plate_id not in {p["id"] for p in record["data"]["plates"]}:
        raise jobs.JobError("Plate not found", 404)
    if not any(o["plate"] == plate_id for o in record["data"]["objects"]):
        raise jobs.JobError("The plate is empty", 409)
    if record["slices"].get(str(plate_id), {}).get("state") in (QUEUED, SLICING):
        raise jobs.JobError("The plate is already being sliced", 409)
    _require_one_filament(record, {plate_id})
    _require_resolved_settings(record, {plate_id})
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
    _require_resolved_settings(record, set(todo))
    now = time.time()

    def mark(slices):
        for index, plate_id in enumerate(todo):
            slices[str(plate_id)] = _queued_entry(slices.get(str(plate_id)), now + index / 1000, send)  # sliced in plate order
    _mutate(project_id, "slices", mark)
    notify_worker()
    return get_project(project_id)


def _staging_dir(project_id: int, plate_id: int) -> Path:
    """Where one run of a plate's slice works; the plate's folder is replaced by it only when the result is published."""
    return plate_dir(project_id, plate_id).with_name(f".work-{int(plate_id)}-{uuid.uuid4().hex[:8]}")


def _slice(project_id: int, plate_id: int, slot: int = 0, created_at: float | None = None, work: Path | None = None) -> dict:
    """Build the plate's 3MF and profile, slice it and analyse the G-code. Blocking.
    Concurrent slices each get their own OrcaSlicer data directory (`slot`), so their caches and logs don't collide.
    Work happens in `work` (a staging folder) on the Project captured at the start; `_publish_slice` makes it the plate's."""
    record = get_project(project_id)
    if created_at is not None and record["created_at"] != created_at:
        raise jobs.JobError("The Project was replaced")
    created_at = record["created_at"]
    work = work or _staging_dir(project_id, plate_id)
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
    _require_resolved_settings(record, {plate_id})
    _stage(project_id, plate_id, "preparing", created_at)
    settings = effective_settings(project_id, plate_id, record=record)    # the captured inputs: everything below uses this
    stamp = settings["fingerprint"]
    orca = slicer.require_orca()
    process, filament, bricks = settings["process"], settings["filament"], settings["bricklayers"]

    shutil.rmtree(work, ignore_errors=True)
    (work / "profile").mkdir(parents=True)
    inputs = plate_settings.snapshot(settings)
    (work / "inputs.json").write_text(json.dumps(inputs, sort_keys=True), encoding="utf-8")
    (work / "output").mkdir()
    for kind, values in (("machine", settings["machine"]), ("process", process), ("filament", filament)):
        (work / "profile" / f"{kind}.json").write_text(json.dumps(values, indent=1, sort_keys=True), encoding="utf-8")
    write_plate_3mf(work / "plate.3mf", project_id, objects, machine_limits(settings["machine"]))

    args = [str(orca), "--datadir", str(_data_dir / ("slicer-data" if slot == 0 else f"slicer-data-{slot}")), "--slice", "0", "--arrange", "0",
            "--load-settings", f"{work / 'profile' / 'machine.json'};{work / 'profile' / 'process.json'}",
            "--load-filaments", str(work / "profile" / "filament.json"), "--outputdir", str(work / "output"), str(work / "plate.3mf")]
    _stage(project_id, plate_id, "slicing", created_at)
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
        _stage(project_id, plate_id, "post-processing", created_at)
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
    _stage(project_id, plate_id, "analysing", created_at)
    evidence = work / ("plate.orca.gcode" if bricks else "plate.gcode")   # Orca's own output, before any post-processing
    observed = observed_output.check(inputs, evidence, slicer.settings_schema(), fingerprint=stamp, post_processed=bool(bricks))
    (work / "observed.json").write_text(json.dumps(observed), encoding="utf-8")
    add_thumbnails(project_id, objects, settings["machine"], gcode, work / "slicer.log")
    analysis, toolpath = gcode_analysis.analyze(gcode)
    (work / "toolpath.bin").write_bytes(toolpath)
    (work / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    estimates = analysis.get("estimates") or {}
    try:
        time_estimate.record_slice(project_id, plate_id, plate_features(project_id, record, plate_id), analysis)
    except sqlite3.Error as e:   # the log must never fail a slice
        print(f"[Projects] Could not log the slice of plate {plate_id} of project {project_id}: {e}")
    return {"state": SLICED, "error": None, "fingerprint": stamp, "inputs_version": INPUTS_VERSION, "inputs_revision": record["revision"],
            "sliced_at": time.time(), "duration_ms": duration_ms,
            "estimate_s": estimates.get("estimate_s"), "estimate_g": estimates.get("estimate_g"),
            "layers": len(analysis["layers"]), "slicer": analysis.get("slicer"), "gcode_bytes": gcode.stat().st_size,
            "bricklayers": bricks, "observed": observed_output.summary(observed),
            "findings": _placement_findings(project_id, record, plate_id)
                        + [f for f in analysis.get("findings", []) if f["level"] == "fail"][:5]}


def _placement_findings(project_id: int, record: dict, plate_id: int) -> list:
    """A slice of a plate with a problem Object records each problem as a failing finding."""
    return [{"line": None, "command": "placement", "reason": p["text"], "level": "fail"}
            for p in hard_plate_problems(project_id, record, {plate_id})]


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
    """A picture of the plate as laid out: its objects placed on the bed, drawn the way the printer's own thumbnail is."""
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
    problems = hard_plate_problems(project_id, record, {p["id"] for p in plates})
    if problems:
        raise jobs.JobError(f"Fix the objects marked in red first: {problem_kinds.reason(problems)}", 409)
    unsliced = [p["name"] for p in plates if (record["slices"].get(str(p["id"])) or {}).get("state") != SLICED]
    if unsliced:
        raise jobs.JobError(f"Slice {_names(unsliced)} first", 409)
    outdated = [p["name"] for p in plates if record["slices"][str(p["id"])].get("outdated")]
    if outdated:
        one = len(outdated) == 1
        raise jobs.JobError(f"{_names(outdated)} changed after {'it was' if one else 'they were'} sliced — slice {'it' if one else 'them'} again", 409)
    unverified = [p for p in plates if (record["slices"][str(p["id"])].get("observed") or {}).get("state") == "mismatch"]
    if unverified and not body.get("accept_unverified"):
        what = "; ".join(f"{p['name']}: " + ", ".join(sorted({d["key"] for d in record["slices"][str(p["id"])]["observed"]["differences"]})[:5])
                         for p in unverified)
        raise jobs.JobError(f"Orca's output for {_names([p['name'] for p in unverified])} does not match the settings it was prepared with "
                            f"({what}) — review the differences, or send it anyway only if you accept them", 409)
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
    latest = {}  # plate id → outcome of its latest known Print (None while it is still printing)
    latest_recorded = {}  # includes unknown Outcomes without changing existing status precedence
    consumed = {}  # plate id → grams its finished Prints used, as the printer counted them
    for row in print_rows:
        latest_recorded[row['plate_id']] = row['outcome']
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

    by_plate = {}   # plate id → its problem Objects
    for problem in plate_problems(project_id, record):
        by_plate.setdefault(problem["plate"], []).append(problem)
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
                       "outcome_unknown": latest_recorded.get(n) == 'unknown',
                       "outdated": bool(entry.get("outdated")) if sliced else False,
                       "mixed": mixed, "filament": choice_label(found[0]) if found and not mixed else None,
                       "parts": parts.get(n, 0), "layout": fingerprint(record, n), "job_id": job["id"] if job else None,
                       "printer_id": (job["printer_id"] if job else None) or plate.get("printer_id"),
                       "problems": by_plate.get(n, []),
                       "problem_reason": problem_kinds.reason([p for p in by_plate.get(n, []) if not p["soft"]]),
                       "warning_reason": problem_kinds.reason([p for p in by_plate.get(n, []) if p["soft"]]),
                       "estimate_s": entry.get("estimate_s") if sliced else None, "estimate_g": grams,
                       "time_estimate": plate_time(project_id, record, n, entry)})
    total = len(plates)
    return {"project_id": project_id, "plates": plates,
            "print_order": [p["id"] for p in time_estimate.longest_first(plates, lambda p: p["time_estimate"])],
            "progress":{"total": total, "counts": counts, "printed_ok": counts["printed_ok"],
                         "outcome_unknown": sum(p['outcome_unknown'] for p in plates),
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
        work = _staging_dir(project_id, item)
        try:
            result = await loop.run_in_executor(None, _slice, project_id, item, slot, created_at, work)
        except Exception as e:
            result = {"state": FAILED, "error": str(e) or type(e).__name__, "finished_at": time.time()}
            print(f"[Projects] Slicing plate {item} of project {project_id} failed: {e}")
            try:   # a plate with a problem Object says so even when the slice stops on it
                result["findings"] = await loop.run_in_executor(None, lambda: _placement_findings(project_id, get_project(project_id), item))
            except Exception as e:
                print(f"[Projects] Could not record the placement problems of plate {item} of project {project_id}: {e}")
        if auto and result.get("state") == SLICED:
            result["auto_send"] = True   # the page keeps checking until the plate is in the queue (or says why not)
        published = await loop.run_in_executor(None, _publish_slice, project_id, item, created_at, result, work)
        if published and auto and result.get("state") == SLICED:
            await loop.run_in_executor(None, _auto_send, project_id, item, created_at, send_opts)
            await jobs.broadcast()
    return True


def _publish_slice(project_id: int, plate_id: int, created_at: float, result: dict, work: Path) -> bool:
    """Make a finished run the plate's Slice, only if the Project is still the one the run started on and the plate is
    still being sliced. The run's own fingerprint is kept, so an edit made meanwhile shows it as Outdated rather than
    giving old output the newer settings. Otherwise the run's files are discarded. The previous folder is replaced as one step."""
    old = None
    with database.transaction() as conn:
        row = conn.execute("SELECT data, slices, created_at FROM projects WHERE id = ?", (project_id,)).fetchone()
        entries = json.loads(row["slices"] or "{}") if row else {}
        entry = entries.get(str(plate_id))
        same = row is not None and row["created_at"] == created_at and entry is not None and entry.get("state") == SLICING
        keep = same and any(p["id"] == plate_id for p in json.loads(row["data"] or "{}").get("plates", []))
        if same and not keep:   # the plate was removed meanwhile: its running entry goes too
            del entries[str(plate_id)]
            conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(entries), project_id))
        if keep:
            final = plate_dir(project_id, plate_id)
            if work.is_dir():
                final.parent.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    old = work.with_name(f".old-{int(plate_id)}-{uuid.uuid4().hex[:8]}")
                    os.replace(final, old)
                os.replace(work, final)
            entries[str(plate_id)] = result
            conn.execute("UPDATE projects SET slices = ?, updated_at = ? WHERE id = ?", (json.dumps(entries), time.time(), project_id))
    shutil.rmtree(old, ignore_errors=True) if old else None
    shutil.rmtree(work, ignore_errors=True)
    return keep


def slice_inputs(project_id: int, plate_id: int) -> dict:
    """Review a completed Slice: the inputs saved with it, apart from the Project as it is now. Expected inputs, not measured output.
    A Slice made before inputs were saved (or whose saved file is gone) says so; nothing is reconstructed or guessed."""
    record = get_project(project_id)
    entry = record["slices"].get(str(plate_id))
    if entry is None or entry.get("state") != SLICED:
        raise jobs.JobError("The plate hasn't been sliced", 404)
    saved, reason = None, None
    if entry.get("imported"):
        reason = "This Slice is uploaded G-code; Spooler never had its print inputs."
    elif entry.get("inputs_version") != INPUTS_VERSION:
        reason = "This Slice was made before its inputs were saved, so what it was prepared with cannot be reconstructed."
    else:
        try:
            saved = json.loads((plate_dir(project_id, plate_id) / "inputs.json").read_text(encoding="utf-8"))
            if saved.get("fingerprint") != entry.get("fingerprint") or saved.get("version") != INPUTS_VERSION:
                saved, reason = None, "The saved inputs of this Slice do not match its record (damaged)."
        except FileNotFoundError:
            reason = "The saved inputs of this Slice are missing."
        except (OSError, ValueError):
            reason = "The saved inputs of this Slice are damaged."
    current, differences = {"state": "unresolved", "fingerprint": None, "revision": record["revision"]}, []
    observed = _saved_observation(project_id, plate_id, entry)
    try:
        result = effective_settings(project_id, plate_id, record=record)
        if not any(i.get("code") in ("unresolved-filament", "conflicting-filament-choices") for i in result["issues"]):
            current.update(state="available", fingerprint=result["fingerprint"])
            if saved is not None:
                differences = plate_settings.differences(saved, result)
    except jobs.JobError:
        pass
    return {"version": 1, "project_id": project_id, "plate_id": plate_id, "revision": record["revision"],
            "label": "Saved inputs this Slice was prepared with (expected inputs, not measured output); the current Project is shown separately",
            "slice": {"state": entry["state"], "sliced_at": entry.get("sliced_at"), "fingerprint": entry.get("fingerprint"),
                      "outdated": bool(entry.get("outdated")), "outdated_reason": entry.get("outdated_reason")},
            "saved": {"state": "saved" if saved else "unavailable", "inputs": saved, "reason": reason},
            "current": current, "differences": differences, "observed": observed}


def _saved_observation(project_id: int, plate_id: int, entry: dict) -> dict:
    """What Orca's output showed when the Slice was made, or why that evidence is unavailable. Never guessed or recomputed."""
    if entry.get("imported"):
        return observed_output.unavailable("This Slice is uploaded G-code; there is no Orca output to examine.")
    if not entry.get("observed"):
        return observed_output.unavailable("This Slice was made before Orca's output was checked, so no evidence was kept.")
    try:
        saved = json.loads((plate_dir(project_id, plate_id) / "observed.json").read_text(encoding="utf-8"))
        if saved.get("fingerprint") != entry.get("fingerprint") or saved.get("state") != entry["observed"].get("state"):
            return observed_output.unavailable("The saved output check of this Slice does not match its record (damaged).")
        return saved
    except FileNotFoundError:
        return observed_output.unavailable("The saved output check of this Slice is missing.")
    except (OSError, ValueError):
        return observed_output.unavailable("The saved output check of this Slice is damaged.")


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
