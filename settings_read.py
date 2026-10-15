"""The settings view of a Project for the Assistant, built without the app: what a setting's value is at a scope and which layer it
comes from, whether the dependency rules hide or grey it, whether it may be proposed, and the data a settings Proposal would make.

Socket-free and read-only (it imports only the read core, so the MCP server can use it; tests/test_spooler_mcp.py checks that):
nothing here saves. The MCP tools `get_settings`, `describe_setting` and `propose_settings` are thin callers of these functions.

  chain(record, scope, scope_id)                      the effective values and source layer of every setting at a scope
  get_settings(record, scope, scope_id, keys=None)    the rows get_settings returns (readable keys only)
  describe(record, key)                               what describe_setting returns (a readable key only)
  plan(record, scope, scope_id, changes)              check proposed {key: value}, drop no-ops, return the resulting data and the rows
                                                      (an Object of a Model still importing is refused)

Scopes are "project", "plate" and "object". Layers, lowest to highest: preset (the locked profile), project, plate, object; `bed` and
`bricklayers` are enforced layers nobody edits. Layers are the names `effective_settings.resolve` records as provenance."""
import copy
from collections import namedtuple

import effective_settings
import profile_read
import project_read
import proposable_settings as ps
import setting_rules
from errors import JobError
from values import first

SCOPES = ("project", "plate", "object")
OVERRIDE_LAYERS = ("project", "plate", "object")
LAYER_WORDS = {"preset": "the profile's value", "project": "set for the project", "plate": "set on the plate", "object": "set on the object",
               "bed": "enforced by the bed type", "bricklayers": "enforced by BrickLayers"}
UNSET = "(not set)"

Chain = namedtuple("Chain", "base values sources existing scope_id label nozzle has_filament_choice")


def _text(value):
    value = first(value)
    return None if value is None else str(value)


def _schema() -> dict:
    return profile_read.settings_schema()


def _meta(key: str) -> dict:
    schema = _schema()
    return schema.get("options", {}).get(key) or schema.get("definitions", {}).get(key) or {}


def _find(items, item_id, what):
    found = next((i for i in items or [] if i["id"] == item_id), None)
    if found is None:
        raise JobError(f"{what} {item_id} not found", 404)
    return found


def chain(record: dict, scope: str, scope_id=None) -> Chain:
    """Effective values and source layers at `scope`. Reads the locked profile; raises JobError when there is none."""
    if scope not in SCOPES:
        raise JobError("scope must be project, plate or object")
    data = record["data"]
    if not (data.get("profile") or {}).get("bundle_id"):
        raise JobError("This project has no locked profile yet, so there are no settings to read. The person must choose a profile first", 409)
    if scope != "project" and (isinstance(scope_id, bool) or not isinstance(scope_id, int)):
        raise JobError(f"Give the id of the {scope} to read")
    obj = None
    if scope == "object":
        obj = _find(data.get("objects"), scope_id, "Object")
        plate = _find(data.get("plates"), obj["plate"], "Plate")
        label = obj.get("name") or f"Object {scope_id}"
    elif scope == "plate":
        plate = _find(data.get("plates"), scope_id, "Plate")
        label = plate.get("name") or f"Plate {scope_id}"
    else:
        plate, label = {"id": 0, "overrides": {}}, "Project settings"
    base = profile_read.bundle_settings(data["profile"]["bundle_id"])
    resolved = effective_settings.resolve(record, plate, base, profile_read.BED_TYPES.get(data.get("bed"), data.get("bed")))
    values = {kind: dict(resolved[kind]) for kind in ("machine", "process", "filament")}
    sources = {kind: {key: layers[-1]["source"] for key, layers in resolved["provenance"][kind].items()}
               for kind in ("process", "filament")}
    if obj is not None:
        for key, value in (obj.get("overrides") or {}).items():
            values["process"][key] = value
            sources["process"][key] = "object"
    if scope == "project":
        existing = {"process": dict(data["profile"].get("process") or {}), "filament": dict(data["profile"].get("filament") or {})}
    elif scope == "plate":
        existing = {"process": dict(plate.get("overrides") or {}), "filament": {}}
    else:
        existing = {"process": dict(obj.get("overrides") or {}), "filament": {}}
    try:
        nozzle = float(first(values["machine"].get("nozzle_diameter")))
    except (TypeError, ValueError):
        nozzle = None
    return Chain(base, values, sources, existing, scope_id, label, nozzle, bool(data.get("filaments")))


def _kind_of(key: str, c: Chain = None) -> str:
    spec = ps.PROPOSABLE_SETTINGS.get(key)
    if spec:
        return spec.kind
    if c is not None and key in c.values["machine"] and key not in c.values["process"] and key not in c.values["filament"]:
        return "machine"
    scopes = _meta(key).get("scopes") or []
    return "filament" if scopes == ["filament"] else "process"


def _value(c: Chain, key: str, kind=None):
    return _text(c.values[kind or _kind_of(key)].get(key))


def _rule_values(c: Chain) -> dict:
    """What the dependency rules read: the effective process settings as strings (first entry of a vector)."""
    return {key: text for key, value in c.values["process"].items() if (text := _text(value)) is not None}


def _profile_value(c: Chain, key: str):
    return _text((c.base.get("process") or {}).get(key))


def _row(c: Chain, key: str, scope: str, rules: dict) -> dict:
    kind = _kind_of(key, c)
    meta = _meta(key)
    row = {"key": key, "label": meta.get("label") or key, "value": _value(c, key, kind),
           "source": c.sources.get(kind, {}).get(key) or ("preset" if kind == "machine" and key in c.values["machine"] else None)}
    if meta.get("sidetext"):
        row["units"] = meta["sidetext"]
    state = setting_rules.explain(rules, key) if kind == "process" else None
    row["hidden"] = bool(state and not state.visible)
    row["greyed"] = bool(state and state.greyed)
    if state and state.inert:
        row["inert_reason"] = state.reason
    reason = ps.why_not(key, scope)
    row["proposable"] = reason is None
    if reason is None:
        row["bounds"] = ps.bounds(key, scope, profile_value=_profile_value(c, key))
    else:
        row["why_not"] = reason
    return row


def get_settings(record: dict, scope: str, scope_id=None, keys=None) -> dict:
    """Rows of effective settings at a scope: the proposable keys, the read-only set and every readable key some layer overrides.
    With `keys`, exactly those that are readable (the rest are reported as not available, with no value). A key outside
    `proposable_settings.is_readable` is never returned."""
    c = chain(record, scope, scope_id)
    rules = _rule_values(c)
    not_available = []
    if keys is not None:
        wanted = []
        for key in dict.fromkeys(keys):
            (wanted if isinstance(key, str) and ps.is_readable(key) else not_available).append(key)
    else:
        overridden = [k for kind in ("process", "filament") for k, layer in c.sources[kind].items() if layer in OVERRIDE_LAYERS]
        wanted = [k for k in dict.fromkeys([k for k in ps.PROPOSABLE_SETTINGS if ps.is_proposable(k, scope)] + list(ps.READ_ONLY_SETTINGS) + overridden)
                  if ps.is_readable(k)]
    result = {"revision": record["revision"], "scope": scope, "settings": [_row(c, key, scope, rules) for key in wanted]}
    if scope != "project":
        result["id"] = scope_id
    if not_available:
        result["not_available"] = not_available
    if c.nozzle:
        result["nozzle_mm"] = c.nozzle
    return result


def describe(record, key: str) -> dict:
    """What a setting is, in OrcaSlicer's own words, and what the Assistant may do with it. `record` (may be None) supplies the
    profile's value that caps a speed."""
    meta = _meta(key) if isinstance(key, str) and ps.is_readable(key) else None
    if not meta:
        raise JobError(f"Setting {str(key)[:60]!r} is not available", 404)
    out = {"key": key, "label": meta.get("label") or key, "type": meta.get("type")}
    for field, name in (("full_label", "full_label"), ("tooltip", "tooltip"), ("sidetext", "units"), ("mode", "mode")):
        if meta.get(field):
            out[name] = meta[field]
    if meta.get("min") is not None:
        out["min"] = meta["min"]
    if meta.get("max") is not None:
        out["max"] = meta["max"]
    if meta.get("enum"):
        out["choices"] = [value for value, _ in meta["enum"]]
    out["object_scope"] = key in _schema().get("object_scope", [])
    profile_value = None
    bundle = ((record or {}).get("data") or {}).get("profile", {}).get("bundle_id")
    if bundle:
        try:
            profile_value = _text(profile_read.bundle_settings(bundle).get("process", {}).get(key))
        except JobError:
            pass
    allowed = [scope for scope in SCOPES if ps.is_proposable(key, scope)]
    out["proposable"] = bool(allowed)
    out["proposable_at"] = allowed
    if allowed:
        out["bounds"] = ps.bounds(key, allowed[0], profile_value=profile_value)
    else:
        out["why_not"] = ps.why_not(key, "project")
    depends = []
    for rule in setting_rules.RULES:
        if key in rule.keys:
            line = (f"no effect when {rule.because}" if rule.because else
                    f"{'hidden' if rule.what == 'line' else 'greyed out'} depending on " + ", ".join(setting_rules._drivers(rule.when)[:4]))
            if line not in depends:
                depends.append(line)
    if depends:
        out["depends_on"] = depends
    return out


# ── Proposing ─────────────────────────────────────────────────────────────────

Plan = namedtuple("Plan", "data rows refused unchanged notes scope_label")


def _canonical(key: str, value):
    """The text OrcaSlicer is given for a proposed value, before the schema normalises it (numbers without trailing zeros, a
    percentage with its % sign)."""
    spec = ps.PROPOSABLE_SETTINGS[key]
    if isinstance(value, bool):
        return "1" if value else "0"
    if spec.type == "percent":
        number = ps._number(value)
        return f"{number:g}%"
    if spec.type in ("int", "float", "choice", "speed"):
        number = ps._number(value)
        return f"{number:g}"
    return str(value)


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    a, b = str(a), str(b)
    if a == b:
        return True
    try:
        return a.endswith("%") == b.endswith("%") and abs(float(a.rstrip("%")) - float(b.rstrip("%"))) < 1e-9
    except ValueError:
        return False


def _write(data: dict, scope: str, scope_id, kind: str, key: str, value) -> None:
    if scope == "project":
        data["profile"].setdefault(kind, {})[key] = value
    elif scope == "plate":
        _find(data["plates"], scope_id, "Plate").setdefault("overrides", {})[key] = value
    else:
        _find(data["objects"], scope_id, "Object").setdefault("overrides", {})[key] = value


def plan(record: dict, scope: str, scope_id, changes: dict) -> Plan:
    """Check the proposed {key: value} at a scope and make the data an Accept would save. Never saves.

    Each key is refused with a reason (not on the allow-list, out of bounds, a speed above the profile, not a Quick print option at
    Object scope, rejected by OrcaSlicer's own validation, enforced by a bed type or BrickLayers) or dropped as `unchanged` when it
    already has that value. What is left goes through `normalize_overrides` and the dependency rules: a change that would do nothing
    stays but carries a note saying why. Returns Plan(data, rows, refused, unchanged, notes, scope_label); `data` is None when no
    change is left. Rows are {key, label, old, new, layer, notes}, `layer` being the layer the old value comes from."""
    c = chain(record, scope, scope_id)
    if scope == "object":
        obj = _find(record["data"].get("objects"), scope_id, "Object")
        model = next((m for m in record.get("models") or [] if m["id"] == obj["model"]), None)
        if model and model.get("state") == project_read.IMPORTING:
            raise JobError(f"Object {scope_id} cannot be targeted: its Model is still importing", 409)
    refused, unchanged, accepted = [], [], {}
    for key, value in changes.items():
        reason = ps.refusal(key, value, scope, profile_value=_profile_value(c, key), nozzle=c.nozzle)
        kind = _kind_of(key)
        layer = c.sources.get(kind, {}).get(key)
        if reason is None and layer in ("bed", "bricklayers"):
            reason = f"{key} is {LAYER_WORDS[layer]} and cannot be changed"
        if reason is None:
            try:
                normalized = profile_read.normalize_overrides({kind: {key: _canonical(key, value)}}, c.base, previous=c.existing,
                                                              check_coupled=False, scope=scope if scope == "object" else None)
            except JobError as e:
                reason = str(e)
            else:
                new = normalized[kind][key]
        if reason is not None:
            refused.append({"key": key, "reason": reason})
            continue
        old = _value(c, key, kind)
        if _same(old, _text(new)):
            unchanged.append({"key": key, "value": old, "layer": layer, "reason": f"already {old} ({LAYER_WORDS.get(layer, layer)})"})
            continue
        accepted[key] = {"kind": kind, "new": new, "old": old, "layer": layer or "preset"}
    if not accepted:
        return Plan(None, [], refused, unchanged, [], c.label)

    data = copy.deepcopy(record["data"])
    for key, item in accepted.items():
        _write(data, scope, scope_id, item["kind"], key, item["new"])
    # Whole-change checks: nothing may newly break an OrcaSlicer invariant (spiral vase with supports, a zero layer height ...)
    after = {kind: {**c.values[kind], **{k: i["new"] for k, i in accepted.items() if i["kind"] == kind}} for kind in ("process", "filament")}
    before_issues = profile_read.configuration_issues({**c.values["machine"], **c.values["filament"], **c.values["process"]})
    new_issues = [i for i in profile_read.configuration_issues({**c.values["machine"], **after["filament"], **after["process"]}) if i not in before_issues]
    if new_issues:
        raise JobError("These changes together are not valid: " + "; ".join(i["message"] for i in new_issues[:3]))

    effective = _rule_values(c)
    changed = {key: _text(item["new"]) for key, item in accepted.items() if item["kind"] == "process"}
    inert = setting_rules.inert_changes(effective, changed)
    choices = " Models with their own Filament choice use that filament's cooling, not the project's." if c.has_filament_choice else ""
    rows, notes = [], []
    for key, item in accepted.items():
        row_notes = []
        if key in inert:
            row_notes.append(inert[key][0].upper() + inert[key][1:])
        if item["kind"] == "filament" and choices:
            row_notes.append(choices.strip())
        rows.append({"key": key, "label": _meta(key).get("label") or key, "old": item["old"] if item["old"] is not None else UNSET,
                     "new": _text(item["new"]), "layer": item["layer"], "notes": row_notes})
        if key in inert:
            notes.append(f"{key}: {inert[key]}")
        if item["kind"] == "filament" and choices:
            notes.append(f"{key}: {choices.strip()}")
    return Plan(data, rows, refused, unchanged, notes, c.label)
