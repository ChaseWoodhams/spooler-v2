"""PROPOSABLE_SETTINGS: the settings the Assistant may propose, owned by Spooler (not by OrcaSlicer's schema), with per-key bounds.

`profile_read.normalize_overrides` accepts nearly any key in the flattened preset and has no upper bound on speeds, so it cannot be
the safety net. This table is: a key that is not here is refused, and the deny-list below says why for the classes the person
will ask about (temperatures, flow, motion limits, machine behaviour, identity keys, `post_process`, `*_gcode`). A Proposal only
ever changes slicing-quality choices, and the person reviews every one before it is applied.

Pure data and checks, standard library only (plus the block list of `profile_read`), so the MCP server and the tests can use it
without a project. Public API:

    PROPOSABLE_SETTINGS     {key: Spec}      what may be proposed, with its bounds
    OBJECT_SCOPE_KEYS       the keys the six Quick print options write; the only keys an Object may be given
    READ_ONLY_SETTINGS      keys the Assistant may read (get_settings reports them) but never propose
    is_readable(key)        whether get_settings / describe_setting may return the key at all (the allow-list for reading)
    why_not(key, scope)     None when `key` may be proposed at `scope`, otherwise the reason it may not
    refusal(key, value, scope, profile_value=, nozzle=)    None when the value is acceptable, otherwise why not
    bounds(key, scope, profile_value=)                     what would be accepted, for get_settings and describe_setting

`scope` is "project", "plate" or "object". Speeds are capped at the profile's own value (`profile_value`, the locked preset's)."""
import math
import re
from collections import namedtuple

import profile_read

Spec = namedtuple("Spec", "kind type min max choices unit")


def _spec(kind, type_, minimum=None, maximum=None, choices=None, unit=None):
    return Spec(kind, type_, minimum, maximum, tuple(choices) if choices else None, unit)


# The six Quick print options (projects.QUICK_*), as the process keys they write. Kept equal by tests/test_assistant_settings.py.
QUICK_LAYER_HEIGHTS = ("0.08", "0.12", "0.16", "0.2", "0.24", "0.28", "0.32")
QUICK_SUPPORT_TYPES = ("normal(auto)", "tree(auto)")
QUICK_BRIM_TYPES = ("no_brim", "auto_brim", "outer_only")
OBJECT_SCOPE_KEYS = ("layer_height", "sparse_infill_density", "wall_loops", "enable_support", "support_type", "brim_type", "brim_width")
SPEED_FLOOR = 5.0                                    # mm/s: a speed below this is a typo, not a choice

_SPEEDS = ("outer_wall_speed", "inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed", "top_surface_speed",
           "bridge_speed", "support_speed", "initial_layer_speed")

PROPOSABLE_SETTINGS = {
    # shells and strength
    "wall_loops": _spec("process", "int", 1, 20),
    "top_shell_layers": _spec("process", "int", 0, 20, unit="layers"),
    "bottom_shell_layers": _spec("process", "int", 0, 20, unit="layers"),
    "sparse_infill_density": _spec("process", "percent", 0, 100, unit="%"),
    "sparse_infill_pattern": _spec("process", "enum", choices=("rectilinear", "grid", "line", "triangles", "cubic", "adaptivecubic",
                                                               "quartercubic", "honeycomb", "gyroid", "lightning", "crosshatch")),
    # supports
    "enable_support": _spec("process", "bool"),
    "support_type": _spec("process", "enum", choices=QUICK_SUPPORT_TYPES),
    "support_style": _spec("process", "enum", choices=("default", "grid", "snug", "organic", "tree_slim", "tree_strong", "tree_hybrid")),
    "support_threshold_angle": _spec("process", "int", 0, 90, unit="°"),
    "support_top_z_distance": _spec("process", "float", 0, 0.4, unit="mm"),
    "support_interface_top_layers": _spec("process", "int", 0, 5, unit="layers"),
    "support_interface_bottom_layers": _spec("process", "int", 0, 5, unit="layers"),
    "support_interface_spacing": _spec("process", "float", 0, 2, unit="mm"),
    "tree_support_branch_angle": _spec("process", "float", 10, 60, unit="°"),
    "tree_support_branch_diameter": _spec("process", "float", 1, 10, unit="mm"),
    # adhesion
    "brim_type": _spec("process", "enum", choices=("no_brim", "auto_brim", "outer_only", "inner_only", "outer_and_inner")),
    "brim_width": _spec("process", "float", 0, 20, unit="mm"),
    "skirt_loops": _spec("process", "int", 0, 5, unit="loops"),
    "raft_layers": _spec("process", "int", 0, 5, unit="layers"),
    "elefant_foot_compensation": _spec("process", "float", 0, 0.5, unit="mm"),
    # quality
    "layer_height": _spec("process", "choice", choices=QUICK_LAYER_HEIGHTS, unit="mm"),
    "seam_position": _spec("process", "enum", choices=("nearest", "aligned", "aligned_back", "back", "random")),
    "ironing_type": _spec("process", "enum", choices=("no ironing", "top", "topmost")),
    "detect_overhang_wall": _spec("process", "bool"),
    "enable_overhang_speed": _spec("process", "bool"),
    "slowdown_for_curled_perimeters": _spec("process", "bool"),
    # speeds: never above the profile's own value
    **{key: _spec("process", "speed", SPEED_FLOOR, None, unit="mm/s") for key in _SPEEDS},
    # cooling (filament settings: project-wide only)
    "fan_min_speed": _spec("filament", "float", 0, 100, unit="%"),
    "fan_max_speed": _spec("filament", "float", 0, 100, unit="%"),
    "overhang_fan_speed": _spec("filament", "int", 0, 100, unit="%"),
    "slow_down_layer_time": _spec("filament", "float", 0, 60, unit="s"),
    "close_fan_the_first_x_layers": _spec("filament", "int", 0, 10, unit="layers"),
}

# What get_settings reports with `proposable: false` so the model can read the whole picture without being able to change it.
READ_ONLY_SETTINGS = ("nozzle_temperature", "nozzle_temperature_initial_layer", "hot_plate_temp", "hot_plate_temp_initial_layer",
                      "textured_plate_temp", "textured_plate_temp_initial_layer", "filament_type", "nozzle_diameter")

# Plainly safe process and filament keys the Assistant may read besides the proposable and read-only ones.
READABLE_EXTRA = ("default_acceleration", "travel_speed", "outer_wall_acceleration", "inner_wall_acceleration", "initial_layer_acceleration",
                  "initial_layer_print_height", "line_width", "outer_wall_line_width", "sparse_infill_line_width", "seam_gap",
                  "reduce_crossing_wall", "spiral_mode", "print_sequence", "enable_prime_tower", "filament_diameter",
                  "filament_flow_ratio", "pressure_advance", "enable_pressure_advance")

_PRIVATE_WORDS = frozenset(("host", "apikey", "api", "password", "passwd", "secret", "token", "user", "username", "auth", "authorization",
                            "cafile", "cert", "ssl", "url", "upload", "path", "dir", "directory", "folder", "webui", "login", "credentials"))


def is_private(key: str) -> bool:
    """G-code that runs on the printer, post-processing scripts, print host / API key / upload settings, file naming and anything
    path- or credential-like. Such a key is never read, whatever else is true of it."""
    words = key.lower().split("_")
    return (key.endswith("_gcode") or key in ("post_process", "filename_format") or any(w.startswith("printhost") for w in words)
            or any(w in _PRIVATE_WORDS for w in words))


def is_readable(key: str) -> bool:
    return (key in PROPOSABLE_SETTINGS or key in READ_ONLY_SETTINGS or key in READABLE_EXTRA) and not is_private(key)


_TEMPERATURE = re.compile(r"(^|_)temp(erature)?($|_)")
_MOTION = re.compile(r"acceleration|jerk|travel_speed|accel_to_decel|junction_deviation|rate_slope")
_FLOW = re.compile(r"flow_ratio|pressure_advance|volumetric")
_MACHINE = re.compile(r"retract|z_hop|prime_tower|wipe_tower|flush|printable|gcode_flavor|extruder_clearance|^machine_|filament_diameter|bed_exclude|thumbnail")
_MACHINE_KEYS = frozenset(("print_sequence", "spiral_mode", "slicing_mode", "timelapse_type", "exclude_object", "gcode_label_objects",
                           "gcode_add_line_number", "reduce_infill_retraction", "curr_bed_type", "filament_type", "nozzle_diameter",
                           "ooze_prevention", "support_filament", "support_interface_filament", "wall_filament", "sparse_infill_filament",
                           "solid_infill_filament", "wipe_tower_x", "enable_arc_fitting", "gcode_comments"))


def _denied(key: str):
    """The reason a key is on the deny-list, or None when it is not (it may still be missing from the allow-list)."""
    if key.endswith("_gcode"):
        return f"{key} is G-code that runs on the printer, so G-code settings can never be proposed"
    if key == "post_process":
        return "post_process runs scripts after slicing, so it can never be proposed"
    if key in profile_read.BLOCKED_SETTINGS:
        return f"{key} is an identity setting of the profile (names, inheritance, compatibility), so identity settings can never be proposed"
    if _TEMPERATURE.search(key):
        return f"{key} is a temperature. Temperatures come from the printer and filament profile and are never proposed"
    if _MOTION.search(key):
        return f"{key} is a motion limit (acceleration, jerk, travel speed). Motion limits are never proposed"
    if _FLOW.search(key):
        return f"{key} is a flow setting (flow ratio, pressure advance, volumetric speed). Flow settings are never proposed"
    if key in _MACHINE_KEYS or _MACHINE.search(key):
        return f"{key} changes how the machine behaves or what the material is. Machine and printer settings are never proposed"
    return None


def why_not(key: str, scope: str):
    """None when `key` may be proposed at `scope`; otherwise the reason it may not, in plain words."""
    denied = _denied(key)
    if denied:
        return denied
    spec = PROPOSABLE_SETTINGS.get(key)
    if spec is None:
        return f"{key} is not on the Assistant's allow-list of settings it may propose"
    if scope == "object" and key not in OBJECT_SCOPE_KEYS:
        return (f"{key} cannot be set on a single Object. At Object scope only the Quick print options can be proposed: "
                "layer height, infill, walls, supports, brim and brim width")
    if spec.kind == "filament" and scope != "project":
        return f"{key} is a Filament setting, and Filament settings can only be proposed for the whole project"
    return None


def is_proposable(key: str, scope: str) -> bool:
    return why_not(key, scope) is None


# ── Values ────────────────────────────────────────────────────────────────────

def _number(value):
    """The finite number a value says ("35%" is 35 only where a percentage is wanted), or None."""
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
        return None
    try:
        number = float(str(value).strip().rstrip("%")) if isinstance(value, str) else float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower() if isinstance(value, (str, int)) else None
    return {"1": True, "true": True, "on": True, "0": False, "false": False, "off": False}.get(text)


def _g(number) -> str:
    return f"{number:g}"


def _range_text(spec: Spec) -> str:
    unit = f" {spec.unit}" if spec.unit and spec.unit != "%" else "%" if spec.unit == "%" else ""
    return f"from {_g(spec.min)} to {_g(spec.max)}{unit}"


def _quick(key: str, value):
    """The Quick print vocabulary at Object scope: the same closed lists and ranges projects._quick_overrides enforces."""
    number = _number(value)
    if key == "layer_height":
        if number is None or not any(abs(number - float(c)) < 1e-6 for c in QUICK_LAYER_HEIGHTS):
            return "Quick print only offers layer heights " + ", ".join(QUICK_LAYER_HEIGHTS) + " mm"
    elif key == "sparse_infill_density":
        if number is None or number != int(number) or not 0 <= number <= 100:
            return "Quick print infill is a whole percentage from 0 to 100"
    elif key == "wall_loops":
        if number is None or number != int(number) or not 1 <= number <= 20:
            return "Quick print walls is a whole number from 1 to 20"
    elif key == "enable_support":
        if _bool(value) is None:
            return "Quick print supports are on or off"
    elif key == "support_type":
        if value not in QUICK_SUPPORT_TYPES:
            return "Quick print supports are one of: " + ", ".join(QUICK_SUPPORT_TYPES)
    elif key == "brim_type":
        if value not in QUICK_BRIM_TYPES:
            return "Quick print brim is one of: " + ", ".join(QUICK_BRIM_TYPES)
    elif key == "brim_width":
        if number is None or not 0 < number <= 20:
            return "Quick print brim width is above 0 and at most 20 mm"
    return None


def refusal(key: str, value, scope: str, *, profile_value=None, nozzle=None):
    """None when `value` is acceptable for `key` at `scope`; otherwise why not. `profile_value` is the locked profile's own value
    (the cap for speeds); `nozzle` is its nozzle diameter in mm (a layer is at most 80% of it)."""
    reason = why_not(key, scope)
    if reason:
        return reason
    spec = PROPOSABLE_SETTINGS[key]
    if scope == "object":
        reason = _quick(key, value)
        if reason:
            return reason
    kind = spec.type
    if kind in ("int", "percent"):
        number = _number(value)
        whole = "a whole percentage" if kind == "percent" else "a whole number"
        if number is None or number != int(number):
            return f"{key} must be {whole} {_range_text(spec)}"
        if not spec.min <= number <= spec.max:
            return f"{key} must be {_range_text(spec)}"
    elif kind == "float":
        number = _number(value)
        if number is None:
            return f"{key} must be a number {_range_text(spec)}"
        if not spec.min <= number <= spec.max:
            return f"{key} must be {_range_text(spec)}"
    elif kind == "bool":
        if _bool(value) is None:
            return f"{key} must be on or off (1 or 0)"
    elif kind == "enum":
        if not isinstance(value, str) or value not in spec.choices:
            return f"{key} must be one of: {', '.join(spec.choices)}"
    elif kind == "choice":
        number = _number(value)
        if number is None or not any(abs(number - float(c)) < 1e-6 for c in spec.choices):
            return f"{key} must be one of: {', '.join(spec.choices)} mm"
        if nozzle and number > 0.8 * float(nozzle) + 1e-9:
            return f"{key} can be at most 80% of the nozzle diameter ({_g(0.8 * float(nozzle))} mm for a {_g(float(nozzle))} mm nozzle)"
    elif kind == "speed":
        number = _number(value)
        cap = _number(profile_value)
        if cap is None:
            return f"{key} has no value in the locked profile to cap it at, so it cannot be proposed"
        if number is None or number < spec.min:
            return f"{key} must be a speed of at least {_g(spec.min)} mm/s"
        if number > cap:
            return f"{key} can be at most the profile's value, {_g(cap)} mm/s (proposed {_g(number)} mm/s). Speeds are never raised above the profile"
    return None


def bounds(key: str, scope: str, *, profile_value=None):
    """What would be accepted for `key` at `scope`, for the model to read; None when it may not be proposed there."""
    if why_not(key, scope):
        return None
    spec = PROPOSABLE_SETTINGS[key]
    out = {"type": {"percent": "percent", "choice": "float", "speed": "float"}.get(spec.type, spec.type)}
    if spec.type == "bool":
        out["choices"] = ["0", "1"]
        return out
    if spec.type == "speed":
        out["min"] = spec.min
        cap = _number(profile_value)
        if cap is not None:
            out.update(max=cap, max_is="the profile's value")
    elif spec.min is not None:
        out.update(min=spec.min, max=spec.max)
    choices = spec.choices
    if scope == "object":
        choices = {"brim_type": QUICK_BRIM_TYPES}.get(key, choices)
        if key == "brim_width":
            out["min"] = 0
    if choices:
        out["choices"] = list(choices)
    if spec.unit:
        out["unit"] = spec.unit
    return out
