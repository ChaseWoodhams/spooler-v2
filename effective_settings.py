"""Deterministic Plate input resolution; I/O belongs to the Project boundary.

Contract version 1 keeps common CLI profile JSON separate from Object metadata.
Provenance lists are in precedence order, with the last entry the winner.
"""
import copy
import json

import brick_post
from values import digest, first as _first


def resolve(record, plate, settings, bed, alternate=None, issues=None):
    data, profile = record["data"], record["data"]["profile"]
    objects = [copy.deepcopy(o) for o in data["objects"] if o["plate"] == plate["id"]]
    provenance = {kind: {} for kind in ("machine", "process", "filament")}
    values = {kind: {} for kind in provenance}

    def layer(kind, incoming, source, reason=None):
        for key, value in sorted(incoming.items()):
            values[kind][key] = copy.deepcopy(value)
            provenance[kind].setdefault(key, []).append(
                {"source": source, "value": copy.deepcopy(value), **({"reason": reason} if reason else {})})

    layer("machine", settings["machine"], "preset")
    layer("process", settings["process"], "preset")
    layer("process", profile.get("process") or {}, "project")
    layer("process", plate.get("overrides") or {}, "plate")
    layer("process", {"curr_bed_type": bed}, "bed", "Project bed selection")
    bricks = brick_post.effective(profile.get("bricklayers"), plate.get("bricklayers"))
    if bricks:
        layer("process", brick_post.REQUIRED_PROCESS, "bricklayers", "Required by BrickLayers post-processing")
    layer("filament", alternate if alternate is not None else settings["filament"],
          "alternate-preset" if alternate is not None else "preset")
    if alternate is None:
        layer("filament", profile.get("filament") or {}, "project")
    inputs = canonical_inputs(record, values, objects, bricks)
    fingerprint = digest(inputs)
    return {"version": 1, "project_id": record["id"], "revision": record["revision"], "plate_id": plate["id"],
            "fingerprint": fingerprint, **values, "objects": objects, "bricklayers": bricks,
            "provenance": provenance, "issues": copy.deepcopy(issues or []), "evidence": "expected-inputs"}


def canonical_inputs(record, values, objects, bricks):
    """What actually reaches Orca for a Plate. Names, ids, colour labels and revisions are descriptive and left out;
    an Object override equal to what the Plate already resolves to changes nothing and is left out too."""
    models = {m["id"]: m for m in record["models"]}
    process = values["process"]
    placed = []
    for obj in objects:
        model = models.get(obj["model"], {})
        placed.append({"geometry": model.get("sha256"), "scale_up": model.get("import"),
                       "position": obj.get("position"), "rotation": obj.get("rotation"), "scale": obj.get("scale"),
                       "overrides": {k: v for k, v in sorted((obj.get("overrides") or {}).items()) if process.get(k) != v}})
    placed.sort(key=lambda o: json.dumps(o, sort_keys=True))
    return {"version": 2, "machine": values["machine"], "process": process, "filament": values["filament"],
            "objects": placed, "bricklayers": bricks, "postprocessor": brick_post.identity() if bricks else None}


def snapshot(result):
    """The immutable record of a resolve() result that a completed Slice keeps: effective values, provenance and the
    placed Objects as they were when work started."""
    return {"version": 2, "project_id": result["project_id"], "revision": result["revision"], "plate_id": result["plate_id"],
            "fingerprint": result["fingerprint"], "machine": result["machine"], "process": result["process"],
            "filament": result["filament"], "objects": result["objects"], "bricklayers": result["bricklayers"],
            "postprocessor": brick_post.identity() if result["bricklayers"] else None,
            "provenance": result["provenance"], "filament_choice": result.get("filament_choice"),
            "evidence": "expected-inputs"}


def differences(saved, current):
    """Setting-level differences between a saved snapshot and the current resolve() result."""
    out = []
    for kind in ("machine", "process", "filament"):
        for key in sorted(set(saved.get(kind) or {}) | set(current.get(kind) or {})):
            before, after = (saved.get(kind) or {}).get(key), (current.get(kind) or {}).get(key)
            if before != after:
                out.append({"kind": kind, "key": key, "saved": before, "current": after})
    placed = lambda r: canonical_inputs({"models": []}, r, r["objects"], r.get("bricklayers"))["objects"]
    if [{k: v for k, v in o.items() if k != "geometry"} for o in placed(saved)] != [{k: v for k, v in o.items() if k != "geometry"} for o in placed(current)]:
        out.append({"kind": "objects", "key": "objects", "saved": saved["objects"], "current": current["objects"]})
    if (saved.get("bricklayers") or None) != (current.get("bricklayers") or None):
        out.append({"kind": "post-processing", "key": "bricklayers", "saved": saved.get("bricklayers"), "current": current.get("bricklayers")})
    return out


# ── Review rows ────────────────────────────────────────────────────────────────
# A presentation of one resolve() result. Nothing here re-merges layers: every row reads the winner and chain that
# resolve() recorded, so the browser shows exactly what will be sent to Orca.

OVERRIDE_SOURCES = ("project", "plate", "object")
ENFORCED_SOURCES = ("bed", "bricklayers")
SECTIONS = ("material", "nozzle", "bed", "temperature", "cooling", "flow", "retraction", "other", "machine")
LABEL = "Settings that will be sent to Orca (expected inputs, not measured output)"
CHAMBER_NOTE = "Passed to Orca as written. Spooler does not claim control of a chamber heater."


def _section(kind, key):
    if kind == "machine":
        return "nozzle" if key in ("nozzle_diameter", "printer_model", "printer_variant") else "machine"
    if key in ("filament_type", "filament_vendor", "filament_density", "filament_diameter", "filament_colour"):
        return "material"
    if key == "curr_bed_type" or "plate_temp" in key or key.startswith("bed_temperature"):
        return "bed"
    if "retract" in key or key in ("wipe", "wipe_distance", "z_hop", "z_hop_types"):
        return "retraction"
    if "fan" in key or "cooling" in key or key.startswith("slow_down"):
        return "cooling"
    if "flow" in key or "pressure_advance" in key or "volumetric" in key:
        return "flow"
    if "temperature" in key or key.endswith("_temp"):
        return "temperature"
    return "other"


def _inactive(meta, values):
    """The reason a value is saved but unused, from the pinned dependency metadata; none when it is active or unknown."""
    for dependency in (meta or {}).get("dependencies") or []:
        value = _first((values.get(dependency["kind"]) or {}).get(dependency["key"]))
        if value in (None, "nil"):
            continue
        try:
            active = float(value) > dependency["value"] if dependency["test"] == "gt" else str(value) == dependency["value"]
        except (TypeError, ValueError):
            continue
        if not active:
            return dependency["reason"]
    return None


def link_issue(issue):
    """Add the owning control to a structured issue so a screen can take the person to it."""
    scope, key = issue.get("scope"), issue.get("key")
    if not key:
        return issue
    kind = issue.get("kind") or "process"
    if scope in ("project", "plate", "object"):
        control = {"scope": scope, "kind": kind, "key": key}
        if scope != "project" and "affected_id" in issue:
            control["id"] = issue["affected_id"]
        return {**issue, "control": control}
    if scope == "model":
        return {**issue, "control": {"scope": "model", "kind": kind, "key": key}}
    return {**issue, "control": None, "read_only": True}


def filament_basis(result, spool_id=None):
    choice = result.get("filament_choice")
    if choice:
        return {"kind": "alternate", "preset": choice["preset"], "colour": choice.get("colour", ""),
                "explanation": f"This Plate prints in the alternate Filament “{choice['preset']}” (a saved snapshot). It replaces the Project default "
                               "Filament entirely: Project Filament tuning and Spoolman values do not apply to it."
                               + (f" Colour label only: {choice['colour']}." if choice.get("colour") else "")}
    text = "Project default Filament preset plus the Project's Filament tuning."
    if spool_id:
        text += f" Values adopted from Spoolman spool #{spool_id} are part of that tuning and apply to the default Filament only."
    return {"kind": "default", "explanation": text}


def review(result, schema, *, object_id=None, search="", changed_only=False, spool_id=None, spool_keys=()):
    """Complete review rows for a Plate (or, with `object_id`, one Object's drill-down) from a resolve() result."""
    definitions = {**schema.get("definitions", {}), **schema.get("options", {})}
    editable_keys, object_keys = schema.get("options", {}), set(schema.get("object_scope") or [])
    obj = None
    if object_id is not None:
        obj = next((o for o in result["objects"] if o["id"] == object_id), None)
        if obj is None:
            raise KeyError(object_id)
    values = {kind: dict(result[kind]) for kind in ("machine", "process", "filament")}
    chains = {kind: {k: list(v) for k, v in result["provenance"][kind].items()} for kind in values}
    if obj:
        for key, value in (obj.get("overrides") or {}).items():
            values["process"][key] = copy.deepcopy(value)
            chains["process"].setdefault(key, []).append({"source": "object", "value": copy.deepcopy(value)})
    default_filament = not result.get("filament_choice")
    rows = []
    for kind in ("machine", "process", "filament"):
        for key, effective in values[kind].items():
            chain = chains[kind].get(key, [])
            preset = chain[0] if chain and chain[0]["source"] in ("preset", "alternate-preset") else None
            overrides = [(i, c) for i, c in enumerate(chain) if c["source"] in OVERRIDE_SOURCES]
            winner = chain[-1] if chain else {"source": "preset"}
            enforced = winner["source"] in ENFORCED_SOURCES
            meta = definitions.get(key)
            parent = chain[overrides[-1][0] - 1] if overrides and overrides[-1][0] > 0 else None
            if obj:
                changed = kind == "process" and effective != result["process"].get(key)
            else:
                changed = preset is None or preset["value"] != effective
            editable = (kind != "machine" and not enforced and key in editable_keys and not editable_keys[key].get("readonly")
                        and (not obj or key in object_keys))
            reason = None
            if not editable:
                if kind == "machine":
                    reason = "Machine value from the Printer preset; read-only here."
                elif enforced:
                    reason = winner.get("reason")
                elif key not in editable_keys:
                    reason = "Inherited from the preset; Spooler has no verified control for it, so it is passed through unchanged."
                elif obj and key not in object_keys:
                    reason = "Orca does not allow this at Object scope; set it on the Plate or Project."
            note = None
            if "chamber" in key:
                note = CHAMBER_NOTE
            elif kind == "filament" and default_filament and winner["source"] == "project" and key in spool_keys and spool_id:
                note = f"From the Project default Filament tuning (Spoolman spool #{spool_id} values apply to the default Filament only)."
            if enforced and overrides and overrides[-1][1]["value"] != effective:
                note = f"Requested {overrides[-1][1]['value']} but {winner.get('reason') or winner['source']} sets it."
            inactive = _inactive(meta, values)
            rows.append({
                "kind": kind, "key": key, "label": (meta or {}).get("full_label") or (meta or {}).get("label") or key,
                "section": _section(kind, key), "inherited": preset["value"] if preset else None, "has_inherited": preset is not None,
                "requested": overrides[-1][1]["value"] if overrides else None, "effective": effective,
                "source": winner["source"], "chain": chain, "changed": changed,
                "enforced": enforced, "enforced_reason": winner.get("reason") if enforced else None,
                "inactive": bool(inactive), "inactive_reason": inactive,
                "reset_to": ({"value": parent["value"], "source": parent["source"]} if parent
                             else {"value": None, "source": "orca-default"}) if overrides else None,
                "editable": editable, "read_only": not editable, "read_only_reason": reason,
                "control": {"scope": overrides[-1][1]["source"] if overrides else ("object" if obj else "plate"), "kind": kind, "key": key} if editable else None,
                "note": note})
    rows.sort(key=lambda r: (SECTIONS.index(r["section"]), 0 if r["key"] == "filament_type" else 1))
    needle = " ".join(str(search or "").lower().split())
    if needle:
        rows = [r for r in rows if needle in r["key"].lower() or needle in r["label"].lower()]
    if changed_only:
        rows = [r for r in rows if r["changed"]]
    return {"version": 1, "label": LABEL, "evidence": result["evidence"],
            "scope": {"kind": "object", "id": obj["id"], "name": obj.get("name")} if obj else {"kind": "plate", "id": result["plate_id"]},
            "changed_basis": "the Plate's effective value" if obj else "the preset's inherited value",
            "filament_basis": filament_basis(result, spool_id), "rows": rows}
