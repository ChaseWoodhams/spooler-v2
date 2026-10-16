"""Locked profiles, the settings schema and override checking: the read side of the slicer module (pure; no jobs, printer or
network code).

`slicer` and `projects` import these names back, so they stay reachable from where they always were. The read-only MCP server
imports this module directly: it must not import `slicer`, which loads `jobs` and with it every printer module.
"""

import hashlib
import json
import math
import re
from pathlib import Path

import datapaths
from errors import JobError

APP_DIR = Path(__file__).parent

PRESET_KINDS    = ("machine", "process", "filament")
PRINTER_FAMILY  = "Elegoo Centauri Carbon 0."   # CC1 nozzle variants, not the Centauri Carbon 2
BED_TYPES       = {"textured": "Textured PEI Plate", "smooth": "High Temp Plate"}  # CC plate sides A / B
_BUNDLE_ID      = re.compile(r"^[0-9a-f]{12}$")

DEFAULT_BED = (0.0, 0.0, 256.0, 256.0)
DEFAULT_HEIGHT = 256.0
DEFAULT_EXCLUDE = (246.0, 0.0, 256.0, 20.0)   # CC1 front-right nozzle purge area (x0, y0, x1, y1)

# Nozzle sizes Orca ships no Centauri Carbon profiles for, derived from a stock size: {size: stock size}
DERIVED_NOZZLES = {"0.7": "0.8"}


def profiles_dir() -> Path:
    return datapaths.profiles_dir()


def load_bundle(bundle_id: str) -> dict:
    if not isinstance(bundle_id, str) or not _BUNDLE_ID.match(bundle_id):
        raise JobError("Profile not found", 404)
    try:
        return json.loads((profiles_dir() / bundle_id / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise JobError("Profile not found", 404)


def verify_bundle(manifest: dict) -> None:
    folder = profiles_dir() / manifest["id"]
    for kind, info in manifest["presets"].items():
        try:
            data = (folder / info["file"]).read_bytes()
        except OSError:
            data = b""
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise JobError(f"The locked {kind} preset in “{manifest['label']}” has been changed or damaged "
                           "— lock the profile again", 409)


def settings_hash(bundle_id: str) -> str | None:
    """A hash of the bundle's settings content as the files hold it now. A bundle id names the content it was locked with, so an
    in-place edit keeps the id and changes this hash."""
    digest = hashlib.sha256()
    folder = profiles_dir() / bundle_id
    for kind in PRESET_KINDS:
        try:
            digest.update((folder / f"{kind}.json").read_bytes())
        except OSError:
            return None
        digest.update(b"\0")
    return digest.hexdigest()


def bundle_settings(bundle_id: str) -> dict:
    """A locked profile's manifest and its flattened machine, process and filament settings."""
    manifest = load_bundle(bundle_id)
    folder = profiles_dir() / manifest["id"]
    try:
        return {"bundle": manifest, **{kind: json.loads((folder / f"{kind}.json").read_text(encoding="utf-8"))
                                       for kind in PRESET_KINDS}}
    except (OSError, ValueError):
        raise JobError(f"The files of “{manifest['label']}” are missing — lock the profile again", 409)


def _compatible(preset: dict, machine: str) -> bool:
    printers = preset.get("compatible_printers") or []
    for size, stock in DERIVED_NOZZLES.items():  # a derived size accepts whatever its stock size accepts
        if preset.get("type") == "filament" and machine == f"{PRINTER_FAMILY}{size[2:]} nozzle" and f"{PRINTER_FAMILY}{stock[2:]} nozzle" in printers:
            return True
    return not printers or machine in printers


def filament_compatibility(values: dict, machine: dict) -> None:
    """Validate actual material and locked machine/nozzle, not the display label."""
    material = values.get("filament_type")
    if not isinstance(material, list) or len(material) != 1 or not isinstance(material[0], str) or not material[0].strip():
        raise JobError("Filament material identity is unavailable — explicitly choose a verified preset", 409)
    nozzle = machine.get("nozzle_diameter")
    named = re.search(r" ([0-9.]+) nozzle$", machine.get("name", ""))
    try:
        valid = isinstance(nozzle, list) and len(nozzle) == 1 and math.isfinite(float(nozzle[0])) and float(nozzle[0]) > 0 and (not named or float(nozzle[0]) == float(named[1]))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise JobError("The locked machine nozzle cannot be verified — choose a compatible profile", 409)
    if values.get("type") != "filament" or not _compatible(values, machine.get("name", "")):
        raise JobError(f"“{values.get('name', 'Filament')}” isn't compatible with {machine.get('name', 'this machine')} and its nozzle", 409)
    if not values.get("compatible_printers") and values.get("compatible_printers_condition"):
        raise JobError("This Filament uses a compatibility condition that cannot be verified — choose an explicitly compatible preset", 409)


def filament_snapshot(reference: dict, machine: dict) -> tuple:
    """Verify saved content without consulting the mutable preset catalogue."""
    digest = reference.get("sha256") if isinstance(reference, dict) else None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise JobError("Filament snapshot reference is invalid — explicitly choose a verified preset", 409)
    folder = profiles_dir() / "filaments" / digest
    try:
        content = (folder / "filament.json").read_bytes()
        saved = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
        values = json.loads(content)
        if (hashlib.sha256(content).hexdigest() != digest or saved.get("sha256") != digest
                or saved.get("version") != 1 or saved.get("material") != values.get("filament_type")
                or saved.get("name") != values.get("name")):
            raise ValueError("checksum")
    except (OSError, ValueError):
        raise JobError("The saved Filament snapshot is missing or damaged — historical Slices remain available; explicitly choose a verified preset", 409)
    filament_compatibility(values, machine)
    return saved, values


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


def _machine_limits_for(record: dict) -> dict:
    bundle_id = record["data"]["profile"]["bundle_id"]
    if not bundle_id:
        raise JobError("Choose a profile first", 409)
    return machine_limits(bundle_settings(bundle_id)["machine"])


# Settings that are never changed from Spooler: identity and inheritance fields, scripts run by the slicer,
# and G-code snippets (any key ending in _gcode).
BLOCKED_SETTINGS = {
    "post_process", "inherits", "name", "from", "setting_id", "instantiation", "version", "type", "filename_format",
    "compatible_printers", "compatible_printers_condition", "compatible_prints", "compatible_prints_condition",
    "print_settings_id", "filament_settings_id", "printer_settings_id",
}

_schema_cache: dict = {}


def settings_schema() -> dict:
    """OrcaSlicer's setting definitions and tab layout (public/orca/settings.json, from tools/orca_settings.py)."""
    if "schema" not in _schema_cache:
        try:
            _schema_cache["schema"] = json.loads((APP_DIR / "public" / "orca" / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _schema_cache["schema"] = {"options": {}, "tabs": {}}
    return _schema_cache["schema"]


def _tab_keys(kind: str) -> set:
    tab = settings_schema().get("tabs", {}).get(kind, [])
    return {key for page in tab for group in page["groups"] for row in group["rows"] for key in row["options"]}


def _check_value(key: str, value, meta: dict | None) -> str:
    label = (meta or {}).get("label") or key
    if isinstance(value, (dict, list, tuple)):
        raise JobError(f"{label} needs a scalar value for each entry")
    if value is None or value == "nil":
        if (meta or {}).get("nullable"):
            return "nil"
        raise JobError(f"{label} does not support nullable inheritance; reset the override instead")
    text = ("1" if value else "0") if isinstance(value, bool) else str(value).strip()
    if len(text) > 4000:
        raise JobError(f"{label}: the value is too long")
    kind = (meta or {}).get("type")
    if kind == "bool":
        if text.lower() in ("1", "true"):
            return "1"
        if text.lower() in ("0", "false"):
            return "0"
        raise JobError(f"{label} must be on or off")
    if kind in ("int", "float", "percent", "float_or_percent"):
        if isinstance(value, (bool, dict, list)):
            raise JobError(f"{label} must be a number")
        percent = text.endswith("%")
        if percent and kind in ("int", "float"):
            raise JobError(f"{label} can't be a percentage")
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?%?", text):
            raise JobError(f"{label} must be a number with at most one trailing percent sign")
        try:
            number = float((text[:-1] if percent else text).strip())
        except ValueError:
            raise JobError(f"{label} must be a number")
        if number != number or abs(number) == float("inf") or (kind == "int" and number != int(number)):
            raise JobError(f"{label} must be a {'whole ' if kind == 'int' else ''}number")
        # Orca validates the numeric token against this field's bounds, including relative tokens.
        low, high = meta.get("min"), meta.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            bounds = f"at least {low:g}" if high is None else f"at most {high:g}" if low is None else f"between {low:g} and {high:g}"
            raise JobError(f"{label} must be {bounds}")
        if kind == "int":
            return str(int(number))
        return text
    if kind == "enum":
        choices = [choice for choice, _ in meta.get("enum", [])]
        if not choices or text not in choices:
            raise JobError(f"“{text}” isn't a valid choice for {label}")
    return text


def _structured(overrides) -> dict:
    """{"process": {…}, "filament": {…}}; a flat dict of settings means process settings."""
    if not overrides:
        return {}
    if not isinstance(overrides, dict):
        raise JobError("Profile overrides must be an object")
    if set(overrides) <= {"process", "filament"} and all(isinstance(v, dict) for v in overrides.values()):
        return overrides
    return {"process": overrides}


def normalize_overrides(overrides, base: dict, *, previous=None, check_coupled=True, scope=None, affected_id=None) -> dict:
    """Validate process and filament overrides against the flattened presets in `base` and OrcaSlicer's
    setting definitions. Returns {"process": {…}, "filament": {…}} with normalised string values."""
    options = settings_schema().get("options", {})
    out = {}
    previous = _structured(previous)
    for kind, values in _structured(overrides).items():
        preset = base.get(kind) or {}
        allowed = _tab_keys(kind)
        clean = {}
        for key, value in values.items():
            if key in previous.get(kind, {}) and json.dumps(value, sort_keys=True) == json.dumps(previous[kind][key], sort_keys=True):
                clean[key] = value
                continue
            try:
                if scope == "object" and key not in settings_schema().get("object_scope", []):
                    raise JobError(f"{key} belongs at Project or Plate scope, not Object scope" if key in allowed else f"{key} is not a recognized editable setting; remove this saved override")
                clean[key] = _normalize_setting(key, value, kind, preset, allowed, options, previous.get(kind, {}))
                _check_context(key, clean[key], {**base.get("machine", {}), **preset, **values}, options.get(key, {}))
            except JobError as error:
                error.issues = [{"code": "invalid-setting", "key": key, "kind": kind,
                                 "severity": "error", "message": str(error), "resolution_required": True}]
                if scope:
                    error.issues[0].update(scope=scope)
                if affected_id is not None:
                    error.issues[0]["affected_id"] = affected_id
                if scope == "object" and key not in settings_schema().get("object_scope", []):
                    error.issues[0].update(code="unsupported-object-setting", valid_scopes=["project", "plate"] if key in allowed else [])
                raise
        if clean:
            out[kind] = dict(sorted(clean.items()))
        before = {**base.get("machine", {}), **preset, **previous.get(kind, {})}
        after = {**base.get("machine", {}), **preset, **clean}
        old_issues = configuration_issues(before)
        new_issues = [issue for issue in configuration_issues(after) if issue not in old_issues]
        if check_coupled and new_issues:
            error = JobError(new_issues[0]["message"])
            error.issues = [{**issue, "kind": kind} for issue in new_issues]
            raise error
    return out


def _normalize_setting(key, value, kind, preset, allowed, options, previous=None):
    if key in BLOCKED_SETTINGS or key.endswith("_gcode"):
        raise JobError(f"“{key}” can't be overridden in a locked profile")
    meta = options.get(key)
    if key not in allowed or not meta or meta.get("mode") == "develop" or meta.get("readonly") or meta.get("type") not in {"int", "float", "percent", "float_or_percent", "bool", "enum", "string"}:
        raise JobError(f"“{key}” isn't a {kind} setting")
    vector = bool(meta.get("vector"))
    if vector:
        existing = (previous or {}).get(key, preset.get(key))
        existing = existing if isinstance(existing, list) and existing else [existing]
        # Single-nozzle editing changes entry zero. A complete vector must keep
        # the existing shape; never silently discard unselected variants.
        items = value if isinstance(value, list) else [value, *existing[1:]]
        if not 1 <= len(items) <= 16:
            raise JobError(f"“{key}” needs between 1 and 16 values")
        if len(items) != len(existing):
            raise JobError(f"“{key}” needs {len(existing)} value(s); preserve every stored entry. This printer edits entry 1 only")
        return [_check_value(key, item, meta) for item in items]
    else:
        if isinstance(value, (list, dict)):
            raise JobError(f"“{key}” takes a single value")
        return _check_value(key, value, meta)


def _check_context(key, value, effective, meta):
    # PrintConfig.cpp validate() and libslic3r.h, pinned v2.4.2.
    if meta.get("ratio_over") != "nozzle_diameter" or not key.endswith("line_width"):
        return
    diameters = effective.get("nozzle_diameter")
    if not isinstance(diameters, list) or not diameters:
        return  # Standalone user presets acquire nozzle context when selected in a Project.
    try:
        nozzles = [float(n) for n in diameters]
        text = str(value)
        width = float(text[:-1]) * max(nozzles) / 100 if text.endswith("%") else float(text)
        limit = min(nozzles) if key == "bridge_line_width" else 5 * max(nozzles)
    except (ValueError, TypeError):
        raise JobError(f"{key}: resolve the nozzle diameter before editing width")
    if width < 0 or width > limit:
        raise JobError(f"{key}: effective width must be between 0 and {limit:g} mm for this nozzle")


def configuration_issues(values):
    """Pinned Orca invariants; return corrections for review, never apply them.

    PrintConfig.cpp validate and ConfigManipulation.cpp update_print_fff_config,
    v2.4.2. Missing/invalid legacy values have their own typed issues.
    """
    def text(key):
        value = values.get(key)
        return str(value[0] if isinstance(value, list) and value else value)

    def number(key, ratio=None):
        try:
            value = text(key)
            return float(value[:-1]) * ratio / 100 if value.endswith("%") and ratio is not None else float(value.removesuffix("%"))
        except (ValueError, TypeError):
            return None

    issues = []
    def add(key, message, correction):
        issues.append({"code": "coupled-setting", "key": key, "severity": "error", "message": message,
                       "suggested_correction": correction, "resolution_required": True})

    for key, suggested in (("layer_height", "0.2"), ("initial_layer_print_height", "0.2"),
                           ("bridge_flow", "1"), ("internal_bridge_flow", "1"), ("filament_flow_ratio", "1")):
        actual = number(key)
        if actual is not None and actual <= 0:
            add(key, f"{key} must be greater than zero", {key: suggested})

    height = number("layer_height")
    start = number("seam_slope_start_height", height)
    if text("seam_slope_type") not in ("None", "none") and height is not None and start is not None and start >= height:
        add("seam_slope_start_height", "Scarf start height must be smaller than layer height", {"seam_slope_start_height": "0"})
    skin, lock = number("skin_infill_depth"), number("infill_lock_depth")
    if text("sparse_infill_pattern") == "lockedzag" and skin is not None and lock is not None and lock > skin:
        add("infill_lock_depth", "Infill lock depth must not exceed skin depth", {"infill_lock_depth": str(skin / 2)})
    if text("spiral_mode") in ("1", "true", "True"):
        for key, expected in (("wall_loops", 1), ("top_shell_layers", 0), ("sparse_infill_density", 0), ("enable_support", 0), ("enforce_support_layers", 0)):
            actual = number(key)
            if actual is not None and actual != expected:
                add(key, f"{key} must be {expected} when spiral vase mode is enabled", {key: str(expected)})
    low, high = number("nozzle_temperature_range_low"), number("nozzle_temperature_range_high")
    if low is not None and high is not None and low > high:
        add("nozzle_temperature_range_low", "Recommended minimum temperature must not exceed the maximum", {"nozzle_temperature_range_low": text("nozzle_temperature_range_high")})
    if text("use_firmware_retraction") in ("1", "true", "True") and text("filament_wipe" if "filament_wipe" in values else "wipe") in ("1", "true", "True"):
        add("filament_wipe" if "filament_wipe" in values else "wipe", "Firmware retraction is not compatible with wipe", {"filament_wipe" if "filament_wipe" in values else "wipe": "0"})
    return issues
