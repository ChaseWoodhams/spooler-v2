"""
Print-farm hooks for the slicer: choosing a Spoolman spool as the filament, and checking a plate against the printer
it is meant for.

A spool becomes filament-setting changes on the project (density, diameter, temperatures, cost) on top of the closest
stock filament preset, so the slice, its fingerprint and the queue all see the real material. Nothing here talks to a
printer: it reads Spoolman and the printer state Spooler already holds.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import jobs
import preflight
import slicer
import spoolman
import state

BED_KEYS = {"textured": ("textured_plate_temp", "textured_plate_temp_initial_layer"),
            "smooth": ("hot_plate_temp", "hot_plate_temp_initial_layer")}   # Orca's plate keys for the two CC plate sides


def _get(path: str):
    try:
        with urllib.request.urlopen(f"{spoolman.get_spoolman_url()}{path}", timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise jobs.JobError(f"Spoolman isn't reachable ({getattr(e, 'reason', e)})", 502)


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def price_per_kg(spool: dict) -> float | None:
    filament = spool.get("filament") or {}
    weight = spool.get("initial_weight") or filament.get("weight")
    if spool.get("price") and weight:
        return spool["price"] / weight * 1000
    if filament.get("price") and filament.get("weight"):
        return filament["price"] / filament["weight"] * 1000
    return None


def summary(spool: dict) -> dict:
    filament = spool.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name") or ""
    return {
        "id":               spool.get("id"),
        "name":             " ".join(filter(None, [vendor, filament.get("material"), filament.get("name")])) or f"Spool {spool.get('id')}",
        "vendor":           vendor,
        "material":         filament.get("material"),
        "color":            filament.get("color_hex"),
        "remaining_weight": spool.get("remaining_weight"),
        "initial_weight":   spool.get("initial_weight") or filament.get("weight"),
        "location":         spool.get("location") or "",
        "density":          filament.get("density"),
        "diameter":         filament.get("diameter"),
        "temp":             filament.get("settings_extruder_temp"),
        "bed_temp":         filament.get("settings_bed_temp"),
        "price_per_kg":     price_per_kg(spool),
    }


def list_spools() -> dict:
    """{"spools": [summary], "printers": {printer id: spool id}} — every spool in use, and which printer holds which."""
    spools = [summary(s) for s in _get("/api/v1/spool?allow_archived=false") if not s.get("archived")]
    at = {}
    for printer in state.printers.values():
        held = next((s["id"] for s in spools if s["location"] == printer.name), None)
        if held is not None:
            at[printer.id] = held
    return {"spools": sorted(spools, key=lambda s: (s["name"].lower(), s["id"])), "printers": at}


def suggest_preset(spool: dict, machine: str) -> str | None:
    """The stock filament preset for `machine` that matches the spool's material, preferring its vendor's."""
    material = preflight.material_family(spool.get("material"))
    if not material:
        return None
    orca = slicer.require_orca()
    names = slicer.stock_presets()["filaments"].get(machine) or []
    matches = []
    for name in names:
        try:
            flat, _ = slicer.flatten_preset(slicer._presets("filament", orca), name)
        except jobs.JobError:
            continue
        if preflight.material_family((flat.get("filament_type") or [None])[0]) == material:
            vendor = (spool.get("vendor") or "").lower()
            matches.append((0 if vendor and vendor in name.lower() else 1, len(name), name))
    return min(matches)[2] if matches else None


SPOOL_KEYS = ("filament_density", "filament_diameter", "nozzle_temperature", "nozzle_temperature_initial_layer", "textured_plate_temp",
              "textured_plate_temp_initial_layer", "hot_plate_temp", "hot_plate_temp_initial_layer", "filament_cost")   # what a spool sets


def filament_proposal(spool: dict, bed: str, base: dict) -> tuple:
    """(changes, skipped): the Filament settings that describe the spool, limited to what the preset `base` and
    OrcaSlicer accept, and the properties that were left out with the reason."""
    wanted = {}
    if _number(spool.get("density")):
        wanted["filament_density"] = [spool["density"]]
    if _number(spool.get("diameter")):
        wanted["filament_diameter"] = [spool["diameter"]]
    if _number(spool.get("temp")):
        wanted["nozzle_temperature"] = wanted["nozzle_temperature_initial_layer"] = [int(spool["temp"])]
    if _number(spool.get("bed_temp")):
        for key in BED_KEYS.get(bed, ()):
            wanted[key] = [int(spool["bed_temp"])]
    if _number(spool.get("price_per_kg")):
        wanted["filament_cost"] = [round(spool["price_per_kg"], 2)]
    changes, skipped = {}, []
    for key, value in wanted.items():
        try:
            changes.update(slicer.normalize_overrides({"filament": {key: value}}, base).get("filament", {}))
        except jobs.JobError as exc:
            skipped.append({"key": key, "value": value, "reason": str(exc)})
    return changes, skipped


def filament_changes(spool: dict, bed: str, base: dict) -> dict:
    """Filament-setting changes that describe the spool, limited to what the preset `base` and OrcaSlicer accept."""
    return filament_proposal(spool, bed, base)[0]


def spool_match(spool_id: int, bundle_id: str, bed: str = "textured") -> dict:
    """What choosing this spool does: its summary, the closest stock filament preset, and the filament changes."""
    spool = summary(_get(f"/api/v1/spool/{int(spool_id)}"))
    base = slicer.bundle_settings(bundle_id)
    machine = base["bundle"]["presets"]["machine"]["name"]
    return {"spool": spool, "preset": suggest_preset(spool, machine), "changes": filament_changes(spool, bed, base),
            "current_preset": base["bundle"]["presets"]["filament"]["name"]}


def plate_fit(record: dict, plate_id: int, printer_id: str | None = None) -> dict:
    """Warnings about sending a plate to a printer: offline, another nozzle, other material, not enough filament."""
    data = record["data"]
    plate = next((p for p in data["plates"] if p["id"] == plate_id), None)
    if plate is None:
        raise jobs.JobError("Plate not found", 404)
    printer_id = printer_id or plate.get("printer_id")
    if not printer_id:
        return {"printer_id": None, "warnings": []}
    printer = jobs._require_printer(printer_id)
    warnings = []
    if not printer.connected:
        warnings.append({"level": "warn", "text": f"{printer.name} is offline"})
    if data["profile"].get("bundle_id"):
        settings = slicer.bundle_settings(data["profile"]["bundle_id"])
        nozzle = _number((settings["machine"].get("nozzle_diameter") or [None])[0])
        installed = preflight.printer_hardware(printer.id).get("nozzle_diameter")
        if nozzle and installed and abs(nozzle - installed) > 0.01:
            warnings.append({"level": "fail", "text": f"This profile slices for a {nozzle:g} mm nozzle, but {printer.name} has a {installed:g} mm nozzle"})
        material = (settings["filament"].get("filament_type") or [None])[0]
    else:
        material = None
    spool = preflight.loaded_spool(printer)
    if "error" in spool:
        warnings.append({"level": "info", "text": "Spoolman couldn't be reached, so the loaded filament wasn't checked"})
    elif spool["spool"] is None:
        warnings.append({"level": "info", "text": f"No spool is assigned to {printer.name} in Spoolman"})
    else:
        held = summary(spool["spool"])
        if material and held["material"] and preflight.material_family(material) != preflight.material_family(held["material"]):
            warnings.append({"level": "warn", "text": f"Sliced for {material}, but {printer.name} has {held['material']} loaded ({held['name']})"})
        needed = (record["slices"].get(str(plate_id)) or {}).get("estimate_g")
        if needed and held["remaining_weight"] is not None and held["remaining_weight"] < needed * preflight.RUNOUT_MARGIN:
            warnings.append({"level": "warn", "text": f"Spool #{held['id']} has {held['remaining_weight']:.0f} g left; this plate needs about {needed:.0f} g"})
    return {"printer_id": printer.id, "printer": printer.name, "warnings": warnings}
