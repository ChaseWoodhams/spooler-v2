"""
BrickLayers interlocking walls: an optional post-processing step for sliced project plates.

BrickLayers (vendor/bricklayers, GPL-3.0, by Geek Detour) raises every other inner wall loop by half a layer, so
walls interlock like bricks and are stronger across layer lines. It runs on OrcaSlicer's G-code in its own process.

Setting (project data)
  profile.bricklayers  {"enabled", "extrusion_multiplier", "start_at_layer"} — the project's setting
  plate.bricklayers    null to use the project's setting, or the same shape to override it for one plate

BrickLayers can't handle arcs and needs classic walls printed inner then outer, so those process settings are
forced while it is on (only in the plate's own process.json; locked profiles are never changed).
"""

import hashlib
import json
import sys
from pathlib import Path

import jobs  # used as a module: jobs.JobError

APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "vendor" / "bricklayers" / "bricklayers.py"
TIMEOUT_S = 30 * 60

DEFAULTS = {"enabled": False, "extrusion_multiplier": 1.05, "start_at_layer": 3}
MULTIPLIER_RANGE = (0.8, 1.5)
START_RANGE = (1, 1000)
REQUIRED_PROCESS = {"enable_arc_fitting": "0", "wall_generator": "classic", "wall_sequence": "inner wall/outer wall"}


class BrickLayersError(Exception):
    exit_code = None


def _number(raw: dict, key: str, low: float, high: float, whole: bool) -> float | int:
    value = raw.get(key, DEFAULTS[key])
    label = "BrickLayers start layer" if whole else "BrickLayers extrusion multiplier"
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise jobs.JobError(f"{label} must be a number")
    if number != number or (whole and number != int(number)):
        raise jobs.JobError(f"{label} must be a {'whole ' if whole else ''}number")
    if not low <= number <= high:
        raise jobs.JobError(f"{label} must be between {low:g} and {high:g}")
    return int(number) if whole else round(number, 3)


def validate(raw) -> dict | None:
    """A clean setting, or None when `raw` is empty (use the defaults / the project's setting)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise jobs.JobError("The BrickLayers setting must be an object")
    return {"enabled": bool(raw.get("enabled", False)),
            "extrusion_multiplier": _number(raw, "extrusion_multiplier", *MULTIPLIER_RANGE, whole=False),
            "start_at_layer": _number(raw, "start_at_layer", *START_RANGE, whole=True)}


def effective(project_setting: dict | None, plate_setting: dict | None) -> dict | None:
    """The setting a plate slices with, or None when BrickLayers is off for it."""
    setting = plate_setting if plate_setting is not None else project_setting
    return {**DEFAULTS, **setting} if setting and setting.get("enabled") else None


def command(source: Path, target: Path, setting: dict) -> list:
    return [sys.executable, str(SCRIPT), str(source), "-outputFile", str(target),
            "-extrusionMultiplier", f"{setting['extrusion_multiplier']:g}", "-startAtLayer", str(setting["start_at_layer"]),
            "-verbosity", "0", "-noLogging"]


def _run_process(args: list, timeout: float) -> tuple:
    import slicer  # slicer's runner already handles the timeout and hides the console window
    return slicer._run_process(args, timeout)


runner = _run_process  # replaced in tests


def run(source: Path, target: Path, setting: dict) -> tuple:
    """Post-process `source` into `target`. Returns (the command, its output). Blocking."""
    args = command(source, target, setting)
    target.unlink(missing_ok=True)
    code, text = runner(args, TIMEOUT_S)
    if code != 0:
        last = next((line.strip() for line in reversed(text.splitlines())  # skip its "see the error log" pointer
                     if line.strip() and not line.startswith("See '")), "")
        error = BrickLayersError(f"BrickLayers failed{': ' + last if last else f' with code {code}'}")
        error.exit_code = code
        raise error
    if not target.is_file() or target.stat().st_size == 0:
        raise BrickLayersError("BrickLayers finished without writing G-code")
    return args, text


_identity: dict = {}


def identity() -> str:
    """Which post-processor a BrickLayers Slice used: the hash of the vendored script plus the settings it forces on Orca."""
    if "value" not in _identity:
        digest = hashlib.sha256()
        try:
            digest.update(SCRIPT.read_bytes())
        except OSError:
            digest.update(b"missing")
        digest.update(json.dumps(REQUIRED_PROCESS, sort_keys=True).encode())
        _identity["value"] = digest.hexdigest()
    return _identity["value"]
