"""
Putting a planner Plan into a Project's data, and the planner's greedy first-fit seed as a layout at once (auto-plating ticket 23).

The planner (`planner`) chooses orientations and plates; this module is the one place that turns what it chose into Project
data: the Objects' rotations, positions and plates, the settings a plate needs (supports when any Object on it needs them, a
brim for an Object that is tippy whatever the orientation), the plan's plate order and names. `plan_job` uses it to apply a
confirmed plan on Accept; `seed_data` uses it for the entry points that need a usable layout at once (Lay out on plates, Open as
project, adding another Split's Parts, the New project preview and Create, the Assistant's Plan plates).

It reads and writes nothing itself and opens no socket, so the Assistant's read-only MCP server may import it.
"""

import copy
import json
import re

import planner
import problem_kinds
import placement_read
from errors import JobError
from values import digest

SUPPORT_OVERRIDES = {"enable_support": "1", "support_type": "tree(auto)"}   # plates holding Objects that need supports
BRIM = {"brim_type": "outer_only", "brim_width": "5"}                         # given to Objects that are tippy in every orientation
SEED_PRINTER = "seed"


def plan_nozzle_of(record: dict) -> float:
    """The Project's plan nozzle: its profile bundle's nozzle."""
    import profile_read
    bundle = ((record.get("data") or {}).get("profile") or {}).get("bundle_id")
    if not bundle:
        raise JobError("Choose a profile for the project before planning", 409)
    try:
        value = profile_read.bundle_settings(bundle)["machine"].get("nozzle_diameter")
        return float(value[0] if isinstance(value, list) else value)
    except (KeyError, TypeError, ValueError, IndexError, JobError):
        raise JobError("The project's profile does not say what nozzle it is for", 409)


def kept_and_open(project_id: int, record: dict, ledger=None, ready_extra=()) -> tuple:
    """([{"id", "name", "reason"}] plates a plan leaves alone, [ids] of the plates it may replan)."""
    from project_read import FAILED, READY, read_ledger
    data = record["data"]
    ready = {m["id"] for m in record["models"] if m["state"] == READY} | set(ready_extra)
    failed = {m["id"] for m in record["models"] if m["state"] == FAILED} - set(ready_extra)
    importing = {o["plate"] for o in data["objects"] if o["model"] not in ready and o["model"] not in failed}
    if ledger is None:
        import database
        with database.transaction() as conn:
            ledger = read_ledger(conn, project_id)
    kept, replannable = [], []
    for plate in data["plates"]:
        reason = (placement_read.KEPT_REASONS.get(ledger.status(plate["id"]))
                  or ("A print of it is on record" if plate["id"] in ledger.recorded else None)
                  or ("Its models are still importing" if plate["id"] in importing else None))
        if reason:
            kept.append({"id": plate["id"], "name": plate["name"], "reason": reason})
        else:
            replannable.append(plate["id"])
    return kept, replannable


def order_and_name(data: dict, record: dict, plates: dict, ordered: list, kept_ids: set) -> None:
    """Put the plates in plan order (kept plates keep their place) and renumber the default names ("Plate 3") to follow it,
    leaving the numbers kept plates hold free of clashes; a name the user gave stays."""
    default = re.compile(r"^Plate (\d+)$")
    kept_at = {p["id"]: i for i, p in enumerate(record["data"]["plates"]) if p["id"] in kept_ids and p["id"] in plates}
    order, queue, later = [], list(ordered), sorted(kept_at, key=kept_at.get)
    for position in range(len(kept_at) + len(ordered)):
        holder = next((k for k in later if kept_at[k] == position), None)
        if holder is not None:
            order.append(holder)
            later.remove(holder)
        elif queue:
            order.append(queue.pop(0))
    order += later
    seen = set(order)
    order += [p["id"] for p in data["plates"] if p["id"] not in seen]    # plates the plan did not touch
    taken = {int(m.group(1)) for p in data["plates"] if p["id"] in kept_ids and (m := default.match(p["name"] or ""))}
    free = (n for n in range(1, 10000) if n not in taken)
    for plate_id in ordered:
        if not plates[plate_id]["name"] or default.match(plates[plate_id]["name"]):
            plates[plate_id]["name"] = f"Plate {next(free)}"
    data["plates"] = [plates[plate_id] for plate_id in order]


def place_plan(record: dict, problem: planner.Problem, replannable: list, kept_ids: set, ordered: list, printers=None,
               emptiable=None) -> dict:
    """The Project data `ordered` (PlanPlates, in the order they take plate numbers) would leave, `record` untouched.
    `printers` is the printer to assign each of `ordered` to (None, or an entry None, assigns none); `emptiable` the plates that go when the plan leaves them empty
    (every replannable one by default). Returns {"data", "objects" (by id, in `data`), "was" (each
    Object's rotation before), "tippy" (ids of Objects tippy whatever the orientation), "used" (plate id per ordered plate),
    "emptied" (plates the plan leaves empty and removes), "existing" (plate ids the Project had)}."""
    data = copy.deepcopy(record["data"])
    objects = {o["id"]: o for o in data["objects"]}
    was = {o["id"]: list(o["rotation"]) for o in data["objects"]}
    held = {i.key: i for i in problem.items + tuple(problem.oversize)}
    layout, tippy = [], []
    for plate in ordered:
        items = []
        for placed in plate.items:
            objects[placed.key]["rotation"] = list(placed.rotation)
            items.append({"key": placed.key, "center": placed.center, "angle": placed.angle})
            if held[placed.key].candidates[placed.candidate].tippy:
                tippy.append(placed.key)
        layout.append({"plate": plate.anchor, "group": plate.group, "items": items})
    used = placement_read._place_layout(data, replannable, layout, lambda group: json.loads(group)["overrides"]) if layout else []
    for key in tippy:   # tippy whatever the orientation: a brim holds it down
        objects[key]["overrides"] = {**BRIM, **(objects[key].get("overrides") or {})}
    plates = {p["id"]: p for p in data["plates"]}
    for index, (plate, plate_id) in enumerate(zip(ordered, used)):
        if plate.supports:
            plates[plate_id]["overrides"] = {**(plates[plate_id].get("overrides") or {}), **SUPPORT_OVERRIDES}
        if printers is not None and printers[index] is not None:
            plates[plate_id]["printer_id"] = printers[index]
    holding = {o["plate"] for o in data["objects"]}
    emptied = [p for p in (replannable if emptiable is None else emptiable) if p not in holding] if used else []
    if len(data["plates"]) - len(emptied) < 1:
        emptied = emptied[1:]
    data["plates"] = [p for p in data["plates"] if p["id"] not in emptied]
    plates = {p["id"]: p for p in data["plates"]}
    order_and_name(data, record, plates, [p for p in used if p in plates], kept_ids)
    return {"data": data, "objects": objects, "was": was, "tippy": tippy, "used": used, "emptied": emptied,
            "existing": {p["id"] for p in record["data"]["plates"]}}


def seed_data(project_id: int, record: dict, *, only=None, meshes=None, pack=None, plan_nozzle=None, models=None,
              ledger=None) -> dict:
    """The Project as the planner's greedy first-fit seed would leave it, changing nothing: instant and deterministic, with no
    search and no slicing. Planned: the Objects of `only` (every Object that may be planned when None; the rest stay where
    they are, as obstacles); `meshes` supplies Models not analysed yet (see `planner.problem_from_project`). `pack` ({"preset"
    …}) plans at that packing density without keeping it. The printers are assumed to fit: the seed leaves every plate's
    printer to the planner run.
    Returns {"data", "plates": [{"id", "new", "objects", "turned", "features"}] in plan order, "kept", "emptied", "too_big",
    "tippy", "unplanned", "fingerprint"}; "plates" is empty when there is nothing to plan."""
    from project_read import READY, _clean_pack
    if pack is not None:
        data = copy.deepcopy(record["data"])
        data["pack"] = _clean_pack({"tight_supports": bool((data.get("pack") or {}).get("tight_supports")), **pack})
        for plate in data["plates"]:
            plate.pop("pack", None)
        record = {**record, "data": data}
    nozzle = float(plan_nozzle) if plan_nozzle is not None else plan_nozzle_of(record)
    if models is None:
        import calibration
        models = calibration.fit_models()
    problem = planner.problem_from_project(project_id, record, plan_nozzle=nozzle, printers={SEED_PRINTER: nozzle}, models=models,
                                           ledger=ledger, only=only, meshes=meshes)
    kept, replannable = kept_and_open(project_id, record, ledger, set(meshes or {}))
    emptiable = None   # a plate the new Objects stood on is the one to free; any other empty plate is the user's
    if only is not None:
        emptiable = [p for p in replannable if any(o["plate"] == p and o["id"] in only for o in record["data"]["objects"])]
    plan = planner.seed_plan(problem) if (problem.items or problem.oversize) else None
    if plan is None or not plan.plates:
        return {"data": record["data"], "plates": [], "kept": kept, "emptied": [], "too_big": [], "tippy": [],
                "unplanned": [list(u) for u in problem.unplanned], "fingerprint": _fingerprint(record["data"], [])}
    ordered = sorted(plan.plates, key=lambda p: -p.seconds)
    placed = place_plan(record, problem, replannable, {k["id"] for k in kept}, ordered, emptiable=emptiable)
    data, used = placed["data"], placed["used"]
    emptied = placed["emptied"]
    names = problem_kinds.object_names(data)
    ready = {m["id"] for m in record["models"] if m["state"] == READY} | set(meshes or {})
    big = [i.key for i in problem.oversize]
    tippy_ids = [k for k in placed["tippy"] if k not in big]
    entries = []
    for plate_id in used:
        members = sorted((o for o in data["objects"] if o["plate"] == plate_id and (o["model"] in ready)), key=lambda o: o["id"])
        entries.append({"id": plate_id, "new": plate_id not in placed["existing"], "objects": members,
                        "turned": sum(1 for o in members if placement_read._turned(placed["was"][o["id"]], o["rotation"])),
                        "features": _features(project_id, record, data, plate_id, meshes)})
    return {"data": data, "plates": entries, "kept": kept, "emptied": emptied,
            "too_big": [{"id": k, "name": names.get(k, f"Object {k}")} for k in big],
            "tippy": [{"id": k, "name": names.get(k, f"Object {k}")} for k in tippy_ids],
            "unplanned": [list(u) for u in problem.unplanned], "fingerprint": _fingerprint(data, emptied)}


def _features(project_id: int, record: dict, data: dict, plate_id: int, meshes):
    """What the rough time estimate of a plate needs, None when a Model on it has no geometry yet."""
    if meshes:
        return None
    try:
        return placement_read.plate_features(project_id, {**record, "data": data}, plate_id)
    except (OSError, ValueError, JobError):
        return None


def _fingerprint(data: dict, emptied: list) -> str:
    return digest({"data": data, "emptied": sorted(emptied)}, length=16, compact=False)
