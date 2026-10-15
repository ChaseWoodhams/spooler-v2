"""Project data, read-only: the project record, its Outdated flags, the Queue's word on each plate, Purpose, filament choices and
packing density (pure: no jobs, printer, Spoolman or network code).

`projects` imports these names back, so they stay reachable from where they always were. The read-only MCP server imports
this module directly. It reads through a connection it supplies (`datapaths.open_readonly`): `database.transaction()` takes
a write lock, which a reader must not.
"""

import json
import time

import brick_post
import effective_settings as plate_settings
import hold_read
import packing
import profile_read
from errors import JobError
from values import digest

MAX_PLATES, MAX_OBJECTS = 50, 500
IMPORTING, READY, FAILED = "importing", "ready", "failed"
QUEUED, SLICING, SLICED = "queued", "slicing", "sliced"
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
    return digest(payload, length=16, compact=False)


_print_fingerprints: dict = {}   # raw-input hash → canonical print-input fingerprint (a pure function of locked, content-addressed inputs)


def print_fingerprint(record: dict, plate_id: int) -> str | None:
    """The canonical fingerprint of the effective print inputs of a Plate (see effective_settings.canonical_inputs), or None when
    they cannot be resolved right now (a missing/damaged profile or Filament snapshot, a mixed Plate). Never raises."""
    raw = fingerprint(record, plate_id)
    if raw in _print_fingerprints:
        return _print_fingerprints[raw]
    try:
        result = _resolve_plate(record, plate_id)[0]
    except (JobError, KeyError, OSError, ValueError):
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


def _new_plate(plate_id: int, overrides: dict | None = None) -> dict:
    return {"id": plate_id, "name": f"Plate {plate_id}", "overrides": dict(overrides or {})}


PURPOSES = ("general", "functional", "display", "draft")
MAX_PURPOSE_NOTE = 300
# What Orient all weighs for each Purpose when no mode is asked for. Display wants "looks", which needs a favoured side.
PURPOSE_ORIENT_MODE = {"general": "least-support", "draft": "least-support", "functional": "strength", "display": "looks"}


def effective_purpose(data: dict, model_id) -> dict:
    """The Purpose a model prints for: its own, else the project default, else General. "source" says which.
    Every Copy of a model has its model's Purpose."""
    own = (data.get("purposes") or {}).get(str(model_id))
    if own:
        return {"value": own["value"], "note": own.get("note", ""), "source": "model"}
    default = data.get("purpose")
    if default:
        return {"value": default["value"], "note": default.get("note", ""), "source": "project"}
    return {"value": "general", "note": "", "source": "default"}


def _next_id(items) -> int:
    return max((int(i["id"]) for i in items), default=0) + 1


PACK_PRESETS = {"tight": 3.0, "normal": packing.GAP_MM, "roomy": 12.0}   # how far apart Arrange and Plan plates keep objects
PACK_GAP_RANGE = (2.0, 40.0)                                              # a custom gap, mm


def _clean_pack(raw, project: bool = True) -> dict | None:
    """A packing density as stored: {"preset": tight | normal | roomy | custom, "gap": mm} and, on the project only,
    "tight_supports": true (objects with supports may then be as close as the gap says, not the supports' reach). None
    when nothing is chosen."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise JobError("Packing density must be an object")
    preset = raw.get("preset")
    if preset in PACK_PRESETS:
        gap = PACK_PRESETS[preset]
    elif preset == "custom":
        try:
            gap = float(raw.get("gap"))
        except (TypeError, ValueError):
            raise JobError("Give the gap between objects in millimetres")
        low, high = PACK_GAP_RANGE
        if not low <= gap <= high:
            raise JobError(f"The gap between objects must be between {low:g} and {high:g} mm")
        gap = round(gap, 1)
    else:
        raise JobError(f"Packing density is one of: {', '.join([*PACK_PRESETS, 'custom'])}")
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


def _ready_object(record: dict, object_id) -> dict:
    obj = next((o for o in record["data"]["objects"] if o["id"] == object_id), None)
    if obj is None:
        raise JobError("Object not found", 404)
    if next((m["state"] for m in record["models"] if m["id"] == obj["model"]), None) != READY:
        raise JobError("The model isn't ready yet", 409)
    return obj


def _resolve_plate(record: dict, plate_id: int) -> tuple:
    """The shared resolution of a Plate's print inputs from a captured record: (result, settings, alternate Filament values, choice).
    Reads the locked profile and Filament snapshot files; never the mutable preset catalogue."""
    data = record["data"]
    plate = next((p for p in data["plates"] if p["id"] == plate_id), None)
    if plate is None:
        raise JobError("Plate not found", 404)
    if not data["profile"]["bundle_id"]:
        raise JobError("Choose a profile first", 409)
    settings = profile_read.bundle_settings(data["profile"]["bundle_id"])
    profile_read.verify_bundle(settings["bundle"])
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
            profile_read.filament_compatibility(settings["filament"], settings["machine"])
        except JobError as exc:
            alternate = {}
            issues.append({"code": "unresolved-filament", "scope": "project", "plate_id": plate_id,
                           "severity": "error", "resolution_required": True, "message": str(exc)})
    if chosen and not mix:
        try:
            if not chosen.get("snapshot"):
                raise JobError(f"{chosen['preset']} has no verified saved material — select its Filament again. Historical profiles are retained but cannot establish this choice's material", 409)
            _, alternate = profile_read.filament_snapshot(chosen["snapshot"], settings["machine"])
        except JobError as exc:
            alternate = {}
            issues.append({"code": "unresolved-filament", "scope": "model", "plate_id": plate_id,
                           "affected_ids": [o["id"] for o in data["objects"] if o["plate"] == plate_id],
                           "severity": "error", "resolution_required": True, "message": f"{chosen['preset']}: {exc}"})
    result = plate_settings.resolve(record, plate, settings, profile_read.BED_TYPES[data["bed"]], alternate, issues)
    return result, settings, alternate, chosen


# ── Reading a project through any connection ───────────────────────────────────

def split_ids_of(conn, record: dict) -> list:
    """The ids of the splits a project is made from: its own split first, then the others it was given."""
    extra = [r["split_id"] for r in conn.execute("SELECT split_id FROM project_splits WHERE project_id = ? ORDER BY rowid", (record["id"],))]
    return list(dict.fromkeys(([record["split_id"]] if record.get("split_id") is not None else []) + extra))


def record_from_row(row, conn) -> dict:
    """A project record from its `projects` row: JSON columns parsed, split ids read through `conn`, and each sliced plate marked
    Outdated (or not) by comparing the fingerprint it was sliced with against its inputs now."""
    record = dict(row)
    record["data"] = json.loads(record["data"] or "{}")
    record["models"] = json.loads(record["models"] or "[]")
    record["slices"] = json.loads(record["slices"] or "{}")
    record["quick"] = bool(record.get("quick"))
    record["draft"] = bool(record.get("draft"))
    record["split_ids"] = split_ids_of(conn, record)
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


def read_project(conn, project_id: int) -> dict:
    """`projects.get_project` over a connection of the caller's choosing (a read-only one will do)."""
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise JobError("Project not found", 404)
    return record_from_row(row, conn)


# ── What the Queue and Print history say about a project's plates ─────────────

OPEN_JOB_STATES = ("queued", "starting", "printing")


class Ledger:
    """A project's open Jobs and Prints, read once, and what they make of each plate: printing, waiting, queued, printed OK,
    failed, sliced or unsliced (see `status`). Build one with `read_ledger`."""

    def __init__(self, job_rows, print_rows, waiting_jobs):
        self.job_rows = job_rows
        self.latest = {}            # plate id → outcome of its latest known Print (None while it is still printing)
        self.latest_recorded = {}   # includes unknown Outcomes without changing existing status precedence
        self.consumed = {}          # plate id → grams its finished Prints used, as the printer counted them
        for row in print_rows:
            self.latest_recorded[row["plate_id"]] = row["outcome"]
            if row["outcome"] in ("completed", "failed", "cancelled") and row["filament_g"]:
                self.consumed[row["plate_id"]] = self.consumed.get(row["plate_id"], 0.0) + row["filament_g"]
            if row["outcome"] == "unknown":
                continue
            self.latest[row["plate_id"]] = row["outcome"]
        self.printing = {r["plate_id"] for r in job_rows if r["state"] in ("starting", "printing")} | {n for n, o in self.latest.items() if o is None}
        self.queued = {r["plate_id"] for r in job_rows if r["state"] == "queued"}
        self.waiting = {r["plate_id"] for r in job_rows if r["state"] == "queued" and r["id"] in waiting_jobs}
        self.recorded = set(self.latest_recorded)   # a Print whose outcome is unknown still counts as on record
        self.open_job = {}          # plate id → its newest open Job
        for row in job_rows:
            if row["plate_id"] not in self.open_job or row["id"] > self.open_job[row["plate_id"]]["id"]:
                self.open_job[row["plate_id"]] = row

    def status(self, plate_id: int, sliced: bool = False) -> str:
        """printing → a Job is starting or printing, or a Print is open; waiting → a queued Job has a hold (on another Job or
        plate) that isn't satisfied yet; queued → a queued Job; otherwise the latest finished Print decides (completed →
        printed_ok, cancelled or failed → failed); otherwise sliced when the plate has a Slice, else unsliced."""
        if plate_id in self.printing:
            return "printing"
        if plate_id in self.queued:
            return "waiting" if plate_id in self.waiting else "queued"
        if self.latest.get(plate_id) == "completed":
            return "printed_ok"
        if self.latest.get(plate_id) in ("failed", "cancelled"):
            return "failed"
        return "sliced" if sliced else "unsliced"


def read_ledger(conn, project_id: int) -> Ledger:
    """The Jobs and Prints of a project's plates, through `conn`."""
    job_rows = conn.execute("SELECT id, plate_id, state, printer_id FROM jobs WHERE project_id = ? AND plate_id IS NOT NULL "
                            f"AND state IN ({', '.join('?' * len(OPEN_JOB_STATES))})", (project_id, *OPEN_JOB_STATES)).fetchall()
    print_rows = conn.execute("SELECT plate_id, outcome, filament_g FROM prints WHERE project_id = ? AND plate_id IS NOT NULL "
                              "ORDER BY started_at, id", (project_id,)).fetchall()
    waiting = {r["id"] for r in job_rows if r["state"] == "queued" and hold_read.waiting_on(conn, r["id"])}
    return Ledger(job_rows, print_rows, waiting)
