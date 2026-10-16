"""Where objects stand on the plates and where they would go, changing nothing: meshes and placement, print-time features, the
nester's Arrange layout, orientation suggestions and Plan plates' plan (pure: no jobs, printer, Spoolman or network code).

`projects` imports these names back, so they stay reachable from where they always were; the helpers that save (Arrange, Orient
all, Accept plan) stay there. The read-only MCP server imports this module directly.
"""

import json
import math
import struct
from pathlib import Path

import problem_kinds
import time_estimate
from datapaths import model_dir, read_json
from errors import JobError
from profile_read import _machine_limits_for, bundle_settings
from project_read import FAILED, MAX_PLATES, READY, SLICED, _choice_key, _new_plate, _next_id, fingerprint, pack_for




# ── Geometry ───────────────────────────────────────────────────────────────────

def rotation_matrix(degrees):
    """The 3x3 matrix of an object's rotation (degrees about X, then Y, then Z)."""
    import orientation
    return orientation.rotation_matrix(degrees)


def read_mesh_bin(path: Path) -> list:
    import numpy as np
    data = path.read_bytes()
    if data[:4] != b"SPV1":
        raise JobError("Unexpected preview file")
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


# ── Rough print time before slicing ───────────────────────────────────────────

_estimate_cache: dict = {}   # (project id, plate fingerprint) → plate features; only complete answers are kept


def _plate_process(record: dict, plate: dict) -> dict:
    """The process settings a plate slices with, as far as they can be told without slicing."""
    data = record["data"]
    try:
        base = bundle_settings(data["profile"]["bundle_id"])["process"]
    except JobError:
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
            analysis = read_json(model_dir(project_id, obj["model"]), "analysis.json") or {}
            verts, _ = load_mesh(project_id, obj["model"], preview=True)
            placed = place(verts, {**obj, "position": [0.0, 0.0]})
            size = placed.max(axis=0) - placed.min(axis=0)
            factor = abs(float(obj["scale"][0]) * float(obj["scale"][1]) * float(obj["scale"][2]))
            volume += time_estimate.deposited_cm3(float(analysis["volume_cm3"]) * factor,
                                                  float(analysis.get("surface_cm2") or 0) * factor ** (2 / 3),
                                                  float(size[2]), settings)
            height = max(height, float(size[2]))
            footprint += float(size[0] * size[1]) / 100
    except (OSError, KeyError, TypeError, ValueError, JobError):
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


# ── Laying out plates ──────────────────────────────────────────────────────────

def _place_layout(data: dict, free: list, layout: list, overrides_for) -> list:
    """Move the objects in `data` as a layout (the nester's; keyed by object id) says, onto the plates in
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
                raise JobError(f"A project holds at most {MAX_PLATES} plates")
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


SUPPORT_OVERHANG_MM2 = 25.0            # underside area, off the bed, that makes an object need supports in its orientation


def _arrange_plate_ids(record: dict, plate_ids) -> list:
    """The plates an arrange covers, in plate order: the ones asked for (all by default) that exist."""
    plates = [p["id"] for p in record["data"]["plates"]]
    if plate_ids is None:
        return plates
    try:
        wanted = {int(i) for i in plate_ids}
    except (TypeError, ValueError):
        raise JobError("plates must be a list of plate numbers")
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
        settings = bundle_settings(record["data"]["profile"]["bundle_id"] or "")
        key = json.dumps([record["data"], [(m["id"], m["state"]) for m in record["models"]], settings["machine"], settings["process"]],
                         sort_keys=True, default=str)
        cached = _problem_cache.get(project_id)
        if cached and cached[0] == key:
            return cached[1]
        found = [{"key": p["key"], "plate": p["plate"], "problem": p["problem"], "other": p["other"]}
                 for p in placement_problems(project_id, record)]
    except (JobError, OSError, KeyError, ValueError):
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


def _nest_error() -> JobError:
    return JobError("Arranging needs shapely installed on the Spooler computer", 503)


# ── Orientation ────────────────────────────────────────────────────────────────

def _overhang_angle(record: dict, body: dict) -> float:
    """Degrees from horizontal at or below which the profile prints supports (support_threshold_angle, 30 when unset)."""
    if body.get("overhang_angle") not in (None, ""):
        try:
            angle = float(body["overhang_angle"])
        except (TypeError, ValueError):
            angle = 0.0
        if not 1 <= angle <= 89:
            raise JobError("overhang_angle is a number of degrees from 1 to 89")
        return angle
    profile = record["data"]["profile"]
    try:
        process = {**bundle_settings(profile["bundle_id"])["process"], **profile.get("process", {})} if profile["bundle_id"] else {}
    except JobError:
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
        raise JobError(f"mode must be one of {', '.join(orientation.MODES)}")
    options = {"mode": mode, "angle": _overhang_angle(record, body), "favoured": body.get("face")}
    try:
        options["limits"] = _bed_limits(record)
    except JobError:
        options["limits"] = None   # no profile chosen yet: orient without checking the bed
    return options


def _orientation_for(project_id: int, obj: dict, options: dict) -> dict:
    import orientation
    verts, tris = load_mesh(project_id, obj["model"], preview=True)
    try:
        return orientation.suggest(verts, tris, obj["scale"], obj["rotation"], **options)
    except orientation.OrientError as e:
        raise JobError(str(e))


# ── Plan plates ────────────────────────────────────────────────────────────────

KEPT_REASONS = {"queued": "In the print queue", "waiting": "In the print queue, waiting on a hold", "printing": "Printing now",
                "printed_ok": "Printed", "failed": "A print of it is on record, and it didn't finish"}


def _turned(before, after) -> bool:
    """Whether the object now stands on another side: its X or Y rotation changed. A turn about Z only spins it on the bed."""
    return any(abs((a - b + 180) % 360 - 180) > 0.01 for a, b in zip(before[:2], after[:2]))


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"
