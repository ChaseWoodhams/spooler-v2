"""
Preflight checks: a job's G-code measured against the printer it is about to run on.

Static facts come from gcode_analysis, cached next to the job's G-code; live facts
(printer state, the spool assigned in Spoolman, the installed nozzle) are read when
the checks run. Every check is pass, info, warn or fail. A job with a failing check
can't start, and warnings have to be acknowledged when starting.
"""

import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

import gcode_analysis
import jobs  # used as a module: jobs imports preflight lazily inside start_job()
import print_history
import spoolman
import state
from persistence import DATA_DIR, _atomic_write

# Elegoo Centauri Carbon (CC1): build volume and purge zone from the stock OrcaSlicer
# machine preset; heater limits from Elegoo's published specifications.
PRINTER_LIMITS = {
    "cc1": {
        "model":        "Elegoo Centauri Carbon",
        "area":         (0.0, 0.0, 256.0, 256.0),
        "height":       256.0,
        "exclude":      [(246.0, 0.0, 256.0, 20.0)],   # nozzle purge/wipe zone, front right
        "travel":       (-5.0, -5.0, 262.0, 270.0),    # purge lines and parking reach slightly past the bed
        "nozzle_max_c": 320,
        "bed_max_c":    110,
    },
}
NOZZLE_SIZES      = (0.2, 0.4, 0.6, 0.8)
EDGE_TOLERANCE_MM = 0.5
RUNOUT_MARGIN     = 1.05
PLATE_BED_TYPE    = {False: "Textured PEI Plate", True: "High Temp Plate"}
PLATE_LABEL       = {False: "textured side (A)", True: "smooth side (B)"}
LEVEL_ORDER       = {"pass": 0, "info": 1, "warn": 2, "fail": 3}

HARDWARE_FILE = DATA_DIR / "printer_hardware.json"
_hardware_lock = threading.Lock()
_analysis_locks: defaultdict = defaultdict(threading.Lock)


# ── Installed hardware ─────────────────────────────────────────────────────────

def _load_hardware() -> dict:
    try:
        data = json.loads(HARDWARE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def printer_hardware(printer_id: str) -> dict:
    return {"nozzle_diameter": None, **_load_hardware().get(printer_id, {})}


def set_printer_hardware(printer_id: str, nozzle_diameter) -> dict:
    if printer_id not in state.printers:
        raise jobs.JobError("Printer not found", 404)
    try:
        nozzle = float(nozzle_diameter)
    except (TypeError, ValueError):
        raise jobs.JobError("nozzle_diameter must be a number")
    if nozzle not in NOZZLE_SIZES:
        raise jobs.JobError(f"Nozzle must be one of {', '.join(f'{n:g}' for n in NOZZLE_SIZES)} mm")
    with _hardware_lock:
        data = _load_hardware()
        data[printer_id] = {**data.get(printer_id, {}), "nozzle_diameter": nozzle}
        _atomic_write(HARDWARE_FILE, json.dumps(data, indent=2))
    return printer_hardware(printer_id)


# ── Facts ──────────────────────────────────────────────────────────────────────

def job_files(job: dict) -> tuple:
    """(G-code, cached analysis, toolpath blob) paths for an uploaded job."""
    gcode = jobs._data_dir / job["stored_file"]
    return gcode, gcode.parent / "analysis.json", gcode.parent / "toolpath.bin"


def job_analysis(job: dict) -> dict | None:
    """Analysis of an uploaded job's G-code, cached until the file changes. None for files on a printer. Blocking."""
    if job["source"] != "upload" or not job.get("stored_file"):
        return None
    gcode, cache, blob = job_files(job)
    if not gcode.is_file():
        raise jobs.JobError("The G-code file for this job is missing", 410)
    with _analysis_locks[job["id"]]:
        stat = gcode.stat()
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if (cached.get("version") == gcode_analysis.ANALYSIS_VERSION and blob.is_file()
                    and cached.get("size") == stat.st_size and cached.get("mtime") == stat.st_mtime):
                return cached
        except (OSError, ValueError):
            pass
        analysis, toolpath = gcode_analysis.analyze(gcode)
        analysis["mtime"] = stat.st_mtime
        staging = blob.with_suffix(".tmp")
        staging.write_bytes(toolpath)
        os.replace(staging, blob)
        _atomic_write(cache, json.dumps(analysis))
        return analysis


def loaded_spool(printer) -> dict:
    """{"spool": Spoolman spool or None} for the spool at the printer's location, or {"error": …}. Blocking."""
    try:
        spool_id = spoolman._spool_id_at_location(printer.name)
        return {"spool": print_history.fetch_spool(spool_id) if spool_id is not None else None}
    except Exception as e:
        return {"error": str(e) or type(e).__name__}


def _number(value) -> float | None:
    try:
        return float(str(value).split(",")[0])
    except (TypeError, ValueError):
        return None


def material_family(value: str | None) -> str | None:
    """'PLA+' / 'PLA-CF' → PLA, 'PETG PRO' / 'PET' → PETG, 'PAHT-CF' → PA."""
    if not value:
        return None
    base = value.upper().replace("+", "").replace("-", " ").split()[0]
    return {"PET": "PETG", "PAHT": "PA", "PA6": "PA", "PA12": "PA"}.get(base, base)


# ── Checks ─────────────────────────────────────────────────────────────────────

def evaluate(job: dict, printer, analysis: dict | None, spool_info: dict, hardware: dict,
             printer_missing: bool = False) -> list:
    checks: list = []

    def check(check_id: str, level: str, title: str, detail: str | None = None) -> None:
        checks.append({"id": check_id, "level": level, "title": title, "detail": detail})

    if printer is None:
        check("printer", "fail", "Printer not found" if printer_missing else "Choose a printer for this job")
    else:
        reason = jobs.printer_block_reason(printer)
        active = jobs.active_job_for(printer.id)
        if active and active["id"] == job["id"]:
            reason = None
        check("printer", "fail" if reason else "pass", reason or f"{printer.name} is online and ready")

    if analysis is None:
        check("file", "warn", "The G-code is stored on the printer, so its contents can't be checked",
              "Only the printer checks ran.")
        return checks
    if job.get("file_sha256") and analysis["sha256"] != job["file_sha256"]:
        check("file", "fail", "The G-code file has changed since it was added to the queue")
    elif not analysis["layers"]:
        check("file", "fail", "The file contains no printable moves")
    else:
        check("file", "pass", f"{len(analysis['layers'])} layers, {analysis['stats']['extrusion_moves']:,} extrusion moves",
              f"Generated by {analysis['slicer']}" if analysis.get("slicer") else None)

    limits = PRINTER_LIMITS.get(getattr(printer, "printer_type", None))
    config = analysis["config"]
    if limits is not None:
        model = config.get("printer_model")
        if not model:
            check("printer_model", "warn", "The file doesn't say which printer it was sliced for")
        elif model != limits["model"]:
            check("printer_model", "fail", f"Sliced for the {model}, not the {limits['model']}")
        else:
            check("printer_model", "pass", f"Sliced for the {model}")

        _check_geometry(check, analysis, limits)
        _check_temperatures(check, analysis, limits)

    file_nozzle = _number(config.get("nozzle_diameter"))
    installed = hardware.get("nozzle_diameter")
    if file_nozzle is None:
        check("nozzle", "warn", "The file doesn't state a nozzle size")
    elif printer is None or installed is None:
        check("nozzle", "info", f"Sliced for a {file_nozzle:g} mm nozzle",
              f"Set the nozzle installed in {printer.name} to check this automatically." if printer else None)
    elif abs(file_nozzle - installed) > 0.01:
        check("nozzle", "fail", f"Sliced for a {file_nozzle:g} mm nozzle, but {printer.name} has a {installed:g} mm nozzle")
    else:
        check("nozzle", "pass", f"Nozzle size matches ({installed:g} mm)")

    smooth = bool((job.get("print_opts") or {}).get("smooth_plate"))
    sliced_for = config.get("curr_bed_type")
    if not sliced_for:
        check("plate", "info", "The file doesn't say which build plate it was sliced for")
    elif sliced_for != PLATE_BED_TYPE[smooth]:
        check("plate", "warn", f"Sliced for the {sliced_for}, but the job is set to the {PLATE_LABEL[smooth]}",
              "Bed temperature and first-layer settings may not suit the plate. Change the job's build plate or re-slice.")
    else:
        check("plate", "pass", f"Build plate matches ({PLATE_LABEL[smooth]})")

    if printer is not None:
        _check_filament(check, analysis, job, printer, spool_info)
    _check_commands(check, analysis)
    return checks


def _check_geometry(check, analysis: dict, limits: dict) -> None:
    bounds = analysis["bounds"]["model"]
    tol = EDGE_TOLERANCE_MM
    if bounds:
        (x0, y0, _), (x1, y1, z1) = bounds["min"], bounds["max"]
        ax0, ay0, ax1, ay1 = limits["area"]
        if x0 < ax0 - tol or y0 < ay0 - tol or x1 > ax1 + tol or y1 > ay1 + tol:
            check("build_volume", "fail", "The print extends past the edge of the bed",
                  f"Printed area X {x0:g}–{x1:g} mm, Y {y0:g}–{y1:g} mm; the bed is {ax1 - ax0:g} × {ay1 - ay0:g} mm.")
        elif z1 > limits["height"] + tol:
            check("build_volume", "fail", f"The print is {z1:g} mm tall; the printer's maximum is {limits['height']:g} mm")
        else:
            check("build_volume", "pass", f"Fits the build volume ({x1 - x0:.1f} × {y1 - y0:.1f} × {z1:.1f} mm)")

        intrusions = [area for area in limits["exclude"]
                      if gcode_analysis.grid_cells_in(analysis, area[0] + tol, area[1] + tol, area[2] - tol, area[3] - tol)]
        if intrusions:
            ex0, ey0, ex1, ey1 = intrusions[0]
            check("purge_zone", "fail", "The print reaches into the nozzle purge zone",
                  f"X {ex0:g}–{ex1:g} mm, Y {ey0:g}–{ey1:g} mm at the front-right corner is kept clear for nozzle cleaning.")
        else:
            check("purge_zone", "pass", "Keeps clear of the purge zone")

    travel = analysis["bounds"]["travel"]
    if travel:
        tx0, ty0, tx1, ty1 = limits["travel"]
        (mx0, my0), (mx1, my1) = travel["min"], travel["max"]
        if mx0 < tx0 or my0 < ty0 or mx1 > tx1 or my1 > ty1:
            check("travel", "warn", "Some moves go beyond the printer's normal travel range",
                  f"The head is sent to X {mx0:g}–{mx1:g} mm, Y {my0:g}–{my1:g} mm.")


def _check_temperatures(check, analysis: dict, limits: dict) -> None:
    config = analysis["config"]
    nozzle, bed = analysis["temperatures"]["nozzle"], analysis["temperatures"]["bed"]
    if not nozzle:
        check("nozzle_temp", "warn", "The file never sets a nozzle temperature")
    elif nozzle["max"] > limits["nozzle_max_c"]:
        check("nozzle_temp", "fail", f"Nozzle temperature {nozzle['max']:g} °C is above the printer's "
                                     f"{limits['nozzle_max_c']} °C limit")
    else:
        low, high = _number(config.get("nozzle_temperature_range_low")), _number(config.get("nozzle_temperature_range_high"))
        printing = nozzle.get("at_print_start")
        if high and nozzle["max"] > high:
            check("nozzle_temp", "warn", f"Nozzle reaches {nozzle['max']:g} °C, above the filament's {high:g} °C maximum")
        elif low and printing and printing < low:
            check("nozzle_temp", "warn", f"Printing starts at {printing:g} °C, below the filament's {low:g} °C minimum")
        else:
            check("nozzle_temp", "pass", f"Nozzle up to {nozzle['max']:g} °C")
    if bed and bed["max"] > limits["bed_max_c"]:
        check("bed_temp", "fail", f"Bed temperature {bed['max']:g} °C is above the printer's {limits['bed_max_c']} °C limit")
    elif bed:
        check("bed_temp", "pass", f"Bed up to {bed['max']:g} °C")


def _check_filament(check, analysis: dict, job: dict, printer, spool_info: dict) -> None:
    sliced = analysis["config"].get("filament_type")
    if "error" in spool_info:
        check("filament", "info", "Spoolman couldn't be reached, so the loaded filament wasn't checked", spool_info["error"])
        return
    spool = spool_info.get("spool")
    if spool is None:
        check("filament", "info", f"No spool is assigned to {printer.name} in Spoolman",
              "Assign one to check the material and how much filament is left.")
        return
    filament = spool.get("filament") or {}
    loaded = filament.get("material")
    described = " ".join(filter(None, [(filament.get("vendor") or {}).get("name"), loaded, filament.get("name")]))
    if sliced and loaded and material_family(sliced) != material_family(loaded):
        check("filament", "warn", f"Sliced for {sliced}, but {printer.name} has {loaded} loaded",
              f"Spool #{spool.get('id')}: {described}")
    else:
        check("filament", "pass", f"Loaded filament matches ({described or loaded or 'spool #' + str(spool.get('id'))})")

    needed = (analysis.get("estimates") or {}).get("estimate_g") or job.get("estimate_g")
    remaining = spool.get("remaining_weight")
    if needed and remaining is not None:
        if remaining < needed * RUNOUT_MARGIN:
            check("filament_amount", "warn", f"Spool #{spool.get('id')} has {remaining:.0f} g left; this print needs about {needed:.0f} g")
        else:
            check("filament_amount", "pass", f"Enough filament: about {needed:.0f} g needed, {remaining:.0f} g left")


def _check_commands(check, analysis: dict) -> None:
    for level in ("fail", "warn"):
        found = [f for f in analysis["findings"] if f["level"] == level]
        if found:
            check(f"commands_{level}", level, f"{len(found)} risky command{'s' if len(found) != 1 else ''} in the file",
                  "; ".join(f"line {f['line']}: {f['command']} {f['reason']}" for f in found[:6]))
    if not analysis["findings"]:
        check("commands", "pass", "No risky commands")
    if analysis["unknown_commands"]:
        check("unknown_commands", "info", "Commands Spooler doesn't recognise",
              ", ".join(f"{name} ×{count}" for name, count in sorted(analysis["unknown_commands"].items())))
    if not analysis.get("thumbnail"):
        check("thumbnail", "info", "No preview image in the file", "The printer's screen won't show a thumbnail for it.")


# ── Reports ────────────────────────────────────────────────────────────────────

def _preview(analysis: dict | None) -> dict | None:
    if analysis is None:
        return None
    keep = ("sha256", "size", "slicer", "thumbnail", "config", "estimates", "features", "bounds", "temperatures", "stats")
    return {**{k: analysis[k] for k in keep},
            "layers": [{k: layer[k] for k in ("z", "height", "extruded_mm", "offset", "length")} for layer in analysis["layers"]]}


def check_job(job: dict, printer, printer_missing: bool = False) -> dict:
    """Run every preflight check for a job on a printer. Blocking (G-code analysis, Spoolman)."""
    analysis = job_analysis(job)
    spool_info = loaded_spool(printer) if printer is not None and analysis is not None else {"spool": None}
    hardware = printer_hardware(printer.id) if printer is not None else {"nozzle_diameter": None}
    checks = evaluate(job, printer, analysis, spool_info, hardware, printer_missing)
    worst = max((c["level"] for c in checks), key=LEVEL_ORDER.get, default="pass")
    return {
        "job_id":                job["id"],
        "printer_id":            printer.id if printer is not None else None,
        "checked_at":            time.time(),
        "status":                worst if worst in ("fail", "warn") else "pass",
        "can_start":             worst != "fail",
        "needs_acknowledgement": worst == "warn",
        "checks":                sorted(checks, key=lambda c: -LEVEL_ORDER[c["level"]]),
        "hardware":              hardware,
        "preview":               _preview(analysis),
    }


def check_job_id(job_id: int, printer_id: str | None = None) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise jobs.JobError("Job not found", 404)
    printer_id = printer_id or job["printer_id"]
    printer = state.printers.get(printer_id) if printer_id else None
    return check_job(job, printer, printer_missing=bool(printer_id) and printer is None)


def toolpath_file(job_id: int) -> Path:
    job = jobs.get_job(job_id)
    if job is None:
        raise jobs.JobError("Job not found", 404)
    if job_analysis(job) is None:
        raise jobs.JobError("Files stored on the printer have no preview", 404)
    return job_files(job)[2]
