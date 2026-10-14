"""Reviewed adoption of newer preset content or Spoolman properties into a Project.

A saved Project never follows the preset catalogue or Spoolman by itself: its default Filament is a locked profile and
each alternate Model Filament is an immutable snapshot. Adopting newer content is an explicit Project edit in two steps.
`preview` reads the catalogue (or the spool) and shows exactly what would change against the Project's own saved,
immutable validation base; it writes nothing. `apply` repeats that calculation, refuses if the Project revision or the
proposed content differ from what was reviewed, stores the new immutable references and saves one revision-checked
Project edit. Nothing here rebases Plate or Object overrides: incompatibility rejects the whole adoption.
"""
import copy
import hashlib
import json

import farm
import jobs
import preflight
import projects
import slicer

VERSION = 1


def preview(project_id: int, body: dict) -> dict:
    return _plan(projects.get_project(project_id), body)["preview"]


def apply(project_id: int, body: dict) -> dict:
    """Adopt a reviewed change as one revision-checked Project edit, or change nothing."""
    record = projects.get_project(project_id)
    if body.get("revision") != record["revision"]:
        raise jobs.JobError("The Project changed since this review — preview the change again", 409)
    plan = _plan(record, body)
    preview = plan["preview"]
    if body.get("token") != preview["token"]:
        raise jobs.JobError("The preset or spool changed since this review — preview the change again", 409)
    if preview["unchanged"]:
        raise jobs.JobError("Nothing to adopt: the Project already matches", 409)
    blockers = [i for i in preview["issues"] if i.get("resolution_required")]
    unacknowledged = [i for i in preview["issues"] if i.get("acknowledgement_required") and not body.get("accept_" + i["code"].replace("-", "_"))]
    if blockers or unacknowledged:
        error = jobs.JobError((blockers or unacknowledged)[0]["message"], 409)
        error.issues = blockers + unacknowledged
        raise error
    projects._save_data(project_id, record, projects.validate_data(plan["commit"](), record))
    return projects.get_project(project_id)


def _plan(record: dict, body: dict) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("target") or {}, dict):
        raise jobs.JobError("Choose a preset refresh or a spool to review", 400)
    source = body.get("source")
    target = body.get("target") or {"kind": "default"}
    if source == "preset" and target.get("kind") == "default":
        return _default_preset(record)
    if source == "preset" and target.get("kind") == "model":
        return _model_preset(record, target.get("model_id"))
    if source == "spool":
        return _spool(record, body.get("spool_id"))
    raise jobs.JobError("Choose a preset refresh or a spool to review", 400)


def _token(parts: dict) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def _difference(before: dict, after: dict) -> list:
    return [{"key": key, "before": before.get(key), "after": after.get(key)}
            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]


def _plates(data: dict, uses_default=None, model_id=None) -> list:
    """The Plates an adoption reaches: those printing in the default Filament, or those holding a Model's Copies."""
    plates = []
    for plate in data["plates"]:
        if model_id is not None:
            hit = any(o["plate"] == plate["id"] and o["model"] == model_id for o in data["objects"])
        else:
            hit = None in projects.plate_filaments(data, plate["id"])
        if hit:
            plates.append({"id": plate["id"], "name": plate["name"]})
    return plates


def _alternates(data: dict) -> list:
    return [{"model_id": int(mid), "preset": c["preset"], "colour": c.get("colour", ""),
             "sha256": (c.get("snapshot") or {}).get("sha256"), "material": (c.get("snapshot") or {}).get("material")}
            for mid, c in sorted((data.get("filaments") or {}).items(), key=lambda item: int(item[0])) if c]


def _outdated(record: dict, plates: list) -> list:
    return [p["id"] for p in plates if (record["slices"].get(str(p["id"])) or {}).get("state") == projects.SLICED]


def _new_coupled(record: dict, new_data: dict, new_base: dict, staged=None) -> list:
    """Setting conflicts the adoption would introduce (ones already in the saved Project are not its doing)."""
    old_base = slicer.bundle_settings(record["data"]["profile"]["bundle_id"])
    old = projects._coupled_setting_issues(record["data"], old_base)
    return [{**issue, "severity": "error", "resolution_required": True}
            for issue in projects._coupled_setting_issues(new_data, new_base, staged) if issue not in old]


def _default_preset(record: dict) -> dict:
    data = record["data"]
    profile = data["profile"]
    if not profile.get("bundle_id"):
        raise jobs.JobError("Choose a profile first", 409)
    proposal = slicer.propose_bundle_refresh(profile["bundle_id"])
    current = proposal["current"]
    saved = current["bundle"]["presets"]["filament"]
    overrides = profile.get("filament") or {}
    nozzle = (current["machine"].get("nozzle_diameter") or [None])[0]
    issues, changes = [], []
    proposed = None
    if proposal["reference"]:
        proposed = {"name": proposal["reference"]["name"], "material": proposal["reference"]["material"],
                    "nozzle": nozzle, "sha256": proposal["reference"]["sha256"],
                    "source_chain": proposal["reference"]["source_chain"]}
    if proposal["issue"]:
        issues.append({**proposal["issue"], "severity": "error", "resolution_required": True})
    material_changed = bool(proposed) and proposed["material"] != current["filament"].get("filament_type")
    if material_changed and not proposal["issue"]:
        issues.append({"code": "material-change", "severity": "warning", "acknowledgement_required": True,
                       "message": f"Material changes from {', '.join(current['filament'].get('filament_type') or ['unknown'])} "
                                  f"to {', '.join(proposed['material'] or ['unknown'])}"})
    if proposal["manifest"]:
        changes = _difference({**current["filament"], **overrides}, {**proposal["settings"]["filament"], **overrides})
        new_data = copy.deepcopy(data)
        new_data["profile"]["bundle_id"] = proposal["bundle_id"]
        issues.extend(_new_coupled(record, new_data, proposal["settings"]))
    plates = _plates(data)
    unchanged = bool(proposal["manifest"]) and proposal["bundle_id"] == profile["bundle_id"]
    preview = {
        "version": VERSION, "source": "preset", "project_id": record["id"], "revision": record["revision"],
        "target": {"kind": "default"}, "applies_to": "project-default", "unchanged": unchanged,
        "base": {"bundle_id": profile["bundle_id"], "machine": current["machine"].get("name"), "nozzle": nozzle,
                 "validated_against": "saved Project profile"},
        "current": {"name": saved["name"], "material": current["filament"].get("filament_type"), "sha256": saved["sha256"]},
        "proposed": proposed, "material_changed": material_changed, "changes": changes,
        "preserved": {"project_overrides": sorted(overrides), "alternates": _alternates(data)},
        "affected": {"plates": plates, "outdated_slices": _outdated(record, plates)},
        "issues": issues,
    }
    preview["token"] = _token({"source": "preset", "revision": record["revision"], "target": preview["target"],
                               "current": saved["sha256"], "proposed": (proposed or {}).get("sha256"),
                               "bundle": proposal["bundle_id"], "changes": changes})

    def commit():
        new_data = copy.deepcopy(data)
        new_data["profile"]["bundle_id"] = slicer.commit_bundle_refresh(proposal)["id"]
        return new_data
    return {"preview": preview, "commit": commit}


def _model_preset(record: dict, model_id) -> dict:
    data = record["data"]
    choice = (data.get("filaments") or {}).get(str(model_id))
    if not choice:
        raise jobs.JobError(f"Model {model_id} has no alternate Filament to refresh", 409)
    if not data["profile"].get("bundle_id"):
        raise jobs.JobError("Choose a profile first", 409)
    base = slicer.bundle_settings(data["profile"]["bundle_id"])
    machine = base["machine"]
    nozzle = (machine.get("nozzle_diameter") or [None])[0]
    issues = []
    try:
        saved, before = slicer.filament_snapshot(choice.get("snapshot"), machine)
        current = {"name": saved["name"], "material": saved["material"], "sha256": saved["sha256"]}
    except jobs.JobError as exc:
        saved, before, current = None, {}, None
        issues.append({"code": "no-verified-snapshot", "severity": "info", "message": f"{choice['preset']} has no verified saved material to compare: {exc}"})
    reference = values = content = proposed = None
    try:
        reference, values, content = slicer.propose_filament(choice["preset"], machine, verify=False)
        proposed = {"name": reference["name"], "material": reference["material"], "nozzle": nozzle,
                    "sha256": reference["sha256"], "source_chain": reference["source_chain"]}
        slicer.filament_compatibility(values, machine)
    except jobs.JobError as exc:
        code = "incompatible-filament" if reference else "preset-unavailable"
        issues.append({"code": code, "severity": "error", "resolution_required": True,
                       "message": f"“{choice['preset']}” can't be adopted: {exc}"})
        values = None
    material_changed = bool(current and proposed and current["material"] != proposed["material"])
    if material_changed:
        issues.append({"code": "material-change", "severity": "warning", "acknowledgement_required": True,
                       "message": f"Material changes from {', '.join(current['material'])} to {', '.join(proposed['material'] or ['unknown'])}"})
    changes = _difference(before, values) if values is not None and current else []
    new_data = copy.deepcopy(data)
    if values is not None:
        new_data["filaments"][str(model_id)] = {**choice, "snapshot": reference}
        issues.extend(_new_coupled(record, new_data, base, {reference["sha256"]: values}))
    plates = _plates(data, model_id=model_id)
    preview = {
        "version": VERSION, "source": "preset", "project_id": record["id"], "revision": record["revision"],
        "target": {"kind": "model", "model_id": model_id, "preset": choice["preset"], "colour": choice.get("colour", "")},
        "applies_to": "model-filament",
        "unchanged": bool(current and proposed and current["sha256"] == proposed["sha256"]),
        "base": {"bundle_id": data["profile"]["bundle_id"], "machine": machine.get("name"), "nozzle": nozzle,
                 "validated_against": "saved Project profile"},
        "current": current, "proposed": proposed, "material_changed": material_changed, "changes": changes,
        "preserved": {"project_default": {"bundle_id": data["profile"]["bundle_id"]},
                      "alternates": [a for a in _alternates(data) if a["model_id"] != model_id]},
        "affected": {"plates": plates, "outdated_slices": _outdated(record, plates)},
        "issues": issues,
    }
    preview["token"] = _token({"source": "preset", "revision": record["revision"], "target": preview["target"],
                               "current": (current or {}).get("sha256"), "proposed": (proposed or {}).get("sha256")})

    def commit():
        stored = slicer.store_filament(reference, content, machine)
        committed = copy.deepcopy(data)
        committed["filaments"][str(model_id)] = {**choice, "snapshot": stored}
        return committed
    return {"preview": preview, "commit": commit}


def _spool(record: dict, spool_id) -> dict:
    """Spool properties become Project-default Filament overrides; alternate Model Filaments never take them."""
    data = record["data"]
    profile = data["profile"]
    spool_id = projects._spool_id(spool_id)
    if spool_id is None:
        raise jobs.JobError("Choose a spool to review", 400)
    if not profile.get("bundle_id"):
        raise jobs.JobError("Choose a profile first", 409)
    spool = farm.summary(farm._get(f"/api/v1/spool/{spool_id}"))
    base = slicer.bundle_settings(profile["bundle_id"])           # the chosen default's immutable content validates
    overrides = profile.get("filament") or {}
    changes, skipped = farm.filament_proposal(spool, data["bed"], base)
    kept = {k: v for k, v in overrides.items() if k not in farm.SPOOL_KEYS}
    proposed_overrides = {**kept, **changes}
    cleared = sorted(k for k in overrides if k in farm.SPOOL_KEYS and k not in changes)
    saved = base["bundle"]["presets"]["filament"]
    material = base["filament"].get("filament_type")
    nozzle = (base["machine"].get("nozzle_diameter") or [None])[0]
    issues = []
    wanted, have = preflight.material_family(spool.get("material")), preflight.material_family((material or [None])[0])
    if wanted and have and wanted != have:
        try:
            suggestion = farm.suggest_preset(spool, base["machine"]["name"])
        except jobs.JobError:
            suggestion = None
        issues.append({"code": "spool-material-mismatch", "severity": "error", "resolution_required": True, "suggested_preset": suggestion,
                       "message": f"{spool['name']} is {spool['material']}, but the Project's Filament “{saved['name']}” is {', '.join(material or ['unknown'])}"
                                  + (f". Choose “{suggestion}” first." if suggestion else ". Choose a matching Filament preset first.")})
    elif not wanted:
        issues.append({"code": "spool-material-unknown", "severity": "info", "message": f"{spool['name']} has no material in Spoolman, so it cannot be checked against the Filament"})
    if skipped:
        issues.append({"code": "spool-properties-skipped", "severity": "info",
                       "message": "Not applied: " + ", ".join(sorted(s["key"] for s in skipped))})
    new_data = copy.deepcopy(data)
    new_data["profile"]["filament"] = proposed_overrides
    issues.extend(_new_coupled(record, new_data, base))
    plates = _plates(data)
    changed = _difference({**base["filament"], **overrides}, {**base["filament"], **proposed_overrides})
    unchanged = proposed_overrides == overrides and profile.get("spool_id") == spool_id
    preview = {
        "version": VERSION, "source": "spool", "project_id": record["id"], "revision": record["revision"],
        "target": {"kind": "default"}, "applies_to": "project-default", "unchanged": unchanged,
        "spool": {key: spool[key] for key in ("id", "name", "vendor", "material", "color")},
        "stock": {"remaining_weight": spool["remaining_weight"], "initial_weight": spool["initial_weight"]},
        "base": {"bundle_id": profile["bundle_id"], "machine": base["machine"].get("name"), "nozzle": nozzle,
                 "validated_against": "saved Project default Filament"},
        "current": {"name": saved["name"], "material": material, "sha256": saved["sha256"]},
        "proposed": {"name": saved["name"], "material": material, "nozzle": nozzle, "sha256": saved["sha256"]},
        "changes": changed, "skipped": skipped, "cleared": cleared,
        "preserved": {"alternates": _alternates(data)},
        "affected": {"plates": plates, "alternate_plates": [p for p in _plates_all(data) if p not in plates],
                     "outdated_slices": _outdated(record, plates)},
        "issues": issues,
    }
    # Remaining stock is display information and deliberately not part of what the token binds.
    preview["token"] = _token({"source": "spool", "revision": record["revision"], "spool_id": spool_id,
                               "bundle": profile["bundle_id"], "changes": changed, "cleared": cleared})

    def commit():
        committed = copy.deepcopy(data)
        committed["profile"]["filament"] = proposed_overrides
        committed["profile"]["spool_id"] = spool_id
        return committed
    return {"preview": preview, "commit": commit}


def _plates_all(data: dict) -> list:
    """Plates that hold at least one Object printing in an alternate Filament."""
    return [{"id": p["id"], "name": p["name"]} for p in data["plates"] if any(projects.plate_filaments(data, p["id"]))]
