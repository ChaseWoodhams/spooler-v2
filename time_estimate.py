"""
A rough print time for a plate before it is sliced, and the features logged with every slice so a better model can be fitted.

    time = a x layers + b x deposited volume + c x first-layer footprint

The seeds below are the research figures (about 50 s per layer and about 160 s per cm3, each fitted on its own on the one real
Spooler-sliced plate, research/time-estimate.md) halved, because used together they doubled that plate's time. The footprint term has no seed yet.

Checked on that plate (project 1 plate 1, two rear_base parts, OrcaSlicer 15260 s, 310 layers, 94.9 cm3 deposited): the estimate
finds 310 layers and 96.2 cm3 (+1.3%) and, with the halved seeds, about 15446 s (+1.2%). With the unhalved seeds it was 30886 s
(+102%). That is one plate, so it says nothing yet about other plates. Fitting `COEFFICIENTS` from the log is what makes the seconds usable. Nothing here
is trustworthy as a time until the log (table `slice_log`) holds enough slices to fit `COEFFICIENTS`; until then the rank
(layers, then volume) is the useful part, and every figure is marked approximate.
"""

import math
import time

import database
import support_measure

# seconds per layer, per cm3 deposited, per cm2 of first-layer footprint
COEFFICIENTS = (25.0, 80.0, 0.0)
DEFAULT_LAYER_HEIGHT, DEFAULT_LINE_WIDTH, DEFAULT_WALLS, DEFAULT_SHELL_LAYERS, DEFAULT_INFILL = 0.2, 0.42, 2, 8, 0.15


def _number(value, default: float) -> float:
    """A slicer setting ("0.2", "15%", ["0.4"]) as a number."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return default


def settings_from(process: dict) -> dict:
    """The few process settings the estimate reads, with defaults for anything the profile leaves out."""
    shells = _number(process.get("top_shell_layers"), 5) + _number(process.get("bottom_shell_layers"), 3)
    return {"layer_height": _number(process.get("layer_height"), DEFAULT_LAYER_HEIGHT) or DEFAULT_LAYER_HEIGHT,
            "walls": _number(process.get("wall_loops"), DEFAULT_WALLS),
            "line_width": _number(process.get("line_width"), DEFAULT_LINE_WIDTH) or DEFAULT_LINE_WIDTH,
            "shell_layers": shells or DEFAULT_SHELL_LAYERS,
            "infill": min(1.0, max(0.0, _number(process.get("sparse_infill_density"), DEFAULT_INFILL * 100) / 100))}


def deposited_cm3(volume_cm3: float, surface_cm2: float, height_mm: float, settings: dict) -> float:
    """The plastic an object takes: its walls and top/bottom skin as solid, the rest at the infill density."""
    walls = surface_cm2 * settings["walls"] * settings["line_width"] / 10
    skin = (volume_cm3 / (height_mm / 10) if height_mm else 0.0) * settings["shell_layers"] * settings["layer_height"] / 10
    shell = min(volume_cm3, walls + skin)
    return shell + settings["infill"] * (volume_cm3 - shell)


def layer_count(height_mm: float, layer_height: float) -> int:
    return math.ceil(round(height_mm / layer_height, 6)) if height_mm > 0 else 0


def predict_seconds(layers: int, volume_cm3: float, first_layer_cm2: float, *, models=None, support_volume_cm3: float = 0.0,
                    settings_hash: str | None = None, nozzle: float | None = None) -> int:
    """Plate seconds: the fitted model (`calibration.fit_models()`) when given, else the seeds."""
    if models is not None:
        return models.predict_seconds(layers, volume_cm3, first_layer_cm2, support_volume_cm3, settings_hash=settings_hash, nozzle=nozzle)
    a, b, c = COEFFICIENTS
    return round(a * layers + b * volume_cm3 + c * first_layer_cm2)


def rank_key(estimate: dict) -> tuple:
    """Longest-first ordering that needs no fitted model: layers, then deposited volume."""
    return (estimate["layers"], estimate["volume_cm3"])


def longest_first(items: list, time_of=lambda item: item) -> list:
    """`items` longest print first. `time_of(item)` is the item's time ({"seconds", "source", "layers", "volume_cm3"}) or None.
    Seconds are compared only while every timed item has the same source, since the model's seconds run high next to a real
    slicer figure; with mixed sources the ranking falls back to rank_key. Untimed items go last; ties keep their order."""
    times = [time_of(item) for item in items]
    known = [t for t in times if t]
    by_seconds = len({t.get("source") for t in known}) <= 1 or not all("layers" in t and "volume_cm3" in t for t in known)
    keys = [(0,) if not t else (1, t.get("seconds") or 0) if by_seconds else (1, *rank_key(t)) for t in times]
    return [items[i] for i in sorted(range(len(items)), key=lambda i: keys[i], reverse=True)]


def deposited_from_filament(filament_mm, diameter_mm) -> float | None:
    """cm3 of plastic from the filament length the slicer reports."""
    if not filament_mm:
        return None
    return round(float(filament_mm) * math.pi * (_number(diameter_mm, 1.75) / 2) ** 2 / 1000, 2)


def record_slice(project_id: int, plate_id: int, features: dict | None, analysis: dict, *, bundle_id: str | None = None,
                settings_hash: str | None = None, nozzle: float | None = None, kind: str = "full", gcode_path=None,
                support_volume_cm3: float | None = None, object_support_volumes: dict | None = None) -> None:
    """Log a finished slice: the features its time was estimated from, what the slicer made of it, what the rough
    estimate said, and what the calibration needs (bundle, settings hash, nozzle, kind, Orca version, measured support grams).
    With the G-code, one `support_log` row per Object that has support is added. The planner's proxy support volume (cm3) goes
    with it: for the plate in `support_volume_cm3`, and per Object in `object_support_volumes` (keyed by the Object's name in the
    3MF, which G-code names extend with `_id_N_copy_M`). Rows are only ever added."""
    features = features or {}
    stats = analysis.get("feature_stats") or {}
    estimates = analysis.get("estimates") or {}
    model_s = (predict_seconds(features["layers"], features["volume_cm3"], features["first_layer_cm2"]) if features else None)
    measured = support_measure.measure(gcode_path) if gcode_path else None
    now = time.time()
    with database.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO slice_log (project_id, plate_id, slicer, layers, volume_cm3, first_layer_cm2, support, brim, estimate_s, "
            "estimate_g, deposited_cm3, model_s, created_at, bundle_id, settings_hash, nozzle, kind, orca_version, support_g, "
            "support_volume_cm3) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, plate_id, analysis.get("slicer"), features.get("layers"), features.get("volume_cm3"),
             features.get("first_layer_cm2"), int(any(name.startswith("Support") for name in stats)), int("Brim" in stats),
             estimates.get("estimate_s"), estimates.get("estimate_g"),
             deposited_from_filament(estimates.get("estimate_mm"), (analysis.get("config") or {}).get("filament_diameter")),
             model_s, now, bundle_id, settings_hash, nozzle, kind, analysis.get("slicer"),
             measured["support_g"] if measured else None, support_volume_cm3))
        for name, found in ((measured or {}).get("objects") or {}).items():
            proxy = (object_support_volumes or {}).get(name, (object_support_volumes or {}).get(name.rsplit("_id_", 1)[0]))
            conn.execute("INSERT INTO support_log (slice_log_id, object_name, bundle_hash, support_volume_cm3, support_g, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (cursor.lastrowid, name, settings_hash, proxy, found["support_g"], now))


def slice_log(project_id: int | None = None) -> dict:
    """The logged slices, oldest first, optionally for one project."""
    where, args = ("WHERE project_id = ?", (project_id,)) if project_id is not None else ("", ())
    with database.transaction() as conn:
        rows = conn.execute(f"SELECT * FROM slice_log {where} ORDER BY id", args).fetchall()
        ids = [row["id"] for row in rows]
        objects = conn.execute(f"SELECT * FROM support_log WHERE slice_log_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
    return {"slices": [{**dict(row), "support": bool(row["support"]), "brim": bool(row["brim"])} for row in rows],
            "support_log": [dict(row) for row in objects]}
