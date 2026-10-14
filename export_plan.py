"""Revision-bound export plans: what an Orca export of a Project can faithfully represent. Pure; I/O is in projects.py.

A plan is computed from the per-Plate `effective_settings` results (the same calculation a Slice uses) and never from the
raw override dictionaries. Generation consumes exactly this plan, so a representation that is not offered cannot be
produced. Representations (`representation`) are named so later packages and an explicit lossy alternative can sit beside
the single file: "single-3mf" and, beside it, "plate-3mf-package" (the `package` entry, ticket 12).

Plan version 1:
  faithful        True only when one Orca project file represents every nonempty Plate
  plates          every nonempty Plate: id, name, objects, material {type, preset, colour}, fingerprint
  settings        {"machine": n, "process": n, "filament": n}: values sent in the shared project configuration
  object_overrides eligible Object settings carried as Object metadata (no-op overrides left out)
  differences     [{kind, key, plates: [{id, name, value}]}]: effective values that are not common to every Plate
  blockers        [{code, message, ...}]: nothing can be exported until resolved
  limitations     [{code, message, plates}]: faithful but not reproduced by an Orca file (e.g. post-processing)
  filename/content_type: what the download is called and is, None when not faithful
  package         {available, default, representation, filename, content_type, files, note}: one self-contained 3MF per nonempty
                  Plate in a ZIP with a manifest. Available whenever nothing blocks the export; `default` when the single file
                  is not faithful. Each file lists plate_id, name, filename, material, fingerprint.
"""
import hashlib
import json
import re

import orca_3mf

VERSION = 1
SINGLE_FILE = "single-3mf"
CONTENT_TYPE = "model/3mf"
PACKAGE = "plate-3mf-package"
PACKAGE_CONTENT_TYPE = "application/zip"
MANIFEST = "MANIFEST.txt"
NL = chr(10)
PACKAGE_NOTE = ("A set of independent Orca project files, one per Plate, in an ordinary ZIP. It is not a Spooler backup and not an "
                "Orca-native format: extract it and open each 3MF in Orca. Spooler does not import the ZIP itself; import the "
                "extracted files one at a time.")
KINDS = ("machine", "process", "filament")
# keys naming a preset rather than carrying a setting; the project file states these itself
IDENTITY_KEYS = {"name", "from", "instantiation", "type", "inherits", "setting_id", "compatible_printers", "filament_id"}
BRICKLAYERS = ("BrickLayers post-processing changes the G-code after Orca slices it. An Orca project file cannot reproduce "
               "that; the Plate's actual Slice G-code remains a separate download.")


def filename(project_name: str) -> str:
    return (re.sub(r"[^\w .-]+", "_", project_name or "").strip() or "project") + ".3mf"


def package_filename(project_name: str) -> str:
    return (re.sub(r"[^\w .-]+", "_", project_name or "").strip() or "project") + " - Plate files.zip"


def plate_filenames(plates: list) -> list:
    """One safe, distinct file name per Plate: its number then its name (the number keeps equal names apart)."""
    out = []
    for plate in plates:
        label = re.sub(r"[^\w .-]+", "_", plate["name"] or "").strip(" .") or "Plate"
        out.append(f"Plate {plate['id']} - {label}.3mf")
    return out


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def material(review: dict) -> dict:
    choice = review.get("filament_choice")
    return {"type": _first(review["filament"].get("filament_type")), "preset": choice["preset"] if choice else review["filament"].get("name"),
            "colour": (choice or {}).get("colour", "")}


def _object_overrides(review: dict, plate: dict) -> list:
    """Object settings that change what Orca receives for this Plate; one equal to the Plate's effective value changes nothing."""
    out = []
    for obj in review["objects"]:
        for key, value in sorted((obj.get("overrides") or {}).items()):
            if review["process"].get(key) != value:
                out.append({"plate": plate["id"], "object_id": obj["id"], "name": obj["name"], "key": key, "value": value})
    return out


def _differences(entries: list) -> list:
    out = []
    for kind in KINDS:
        for key in sorted(set().union(*(set(review[kind]) for _, review in entries))):
            if key in IDENTITY_KEYS:
                continue
            values = [(plate, review[kind].get(key)) for plate, review in entries]
            if any(value != values[0][1] for _, value in values):
                out.append({"kind": kind, "key": key, "plates": [{"id": p["id"], "name": p["name"], "value": v} for p, v in values]})
    names = [(plate, material(review)["preset"]) for plate, review in entries]
    if any(name != names[0][1] for _, name in names) and not any(d["kind"] == "filament" for d in out):
        out.append({"kind": "filament", "key": "filament_settings_id",
                    "plates": [{"id": p["id"], "name": p["name"], "value": n} for p, n in names]})
    return out


def plan(record: dict, entries: list, unavailable: list, waiting: list, template_available: bool) -> dict:
    """`entries`: [(plate, effective_settings result)] for every nonempty Plate; `unavailable`: blockers raised while
    resolving a Plate; `waiting`: names of Objects whose Model is not ready."""
    data = record["data"]
    blockers = list(unavailable)
    if not data["objects"]:
        blockers.append({"code": "nothing-to-export", "message": "The Project has no Objects to export"})
    if waiting:
        blockers.append({"code": "model-not-ready", "message": f"Still importing: {', '.join(waiting)}", "names": waiting})
    seen = set()
    for plate, review in entries:
        for issue in review["issues"]:
            if issue.get("resolution_required") and issue.get("scope") != "preset":
                key = (issue.get("code"), issue.get("scope"), issue.get("key"), issue.get("affected_id"), plate["id"])
                if key not in seen:
                    seen.add(key)
                    blockers.append({**issue, "plate_id": plate["id"]})
    if not template_available:
        blockers.append({"code": "export-template-unavailable",
                         "message": "OrcaSlicer is needed to write the complete project configuration, and it was not found"})
    differences = _differences(entries) if entries else []
    overrides = [o for plate, review in entries for o in _object_overrides(review, plate)]
    limitations = []
    bricks = [plate["name"] for plate, review in entries if review.get("bricklayers")]
    if bricks:
        limitations.append({"code": "bricklayers", "message": BRICKLAYERS, "plates": bricks})
    faithful = not blockers and not differences and bool(entries)
    plates = [{"id": plate["id"], "name": plate["name"], "objects": len(review["objects"]), "material": material(review),
               "fingerprint": review["fingerprint"]} for plate, review in entries]
    bound = {"version": VERSION, "representation": SINGLE_FILE, "name": record["name"], "bed": data["bed"],
             "plates": [(p["id"], p["name"]) for p in data["plates"]], "inputs": [(p["id"], p["fingerprint"]) for p in plates],
             "objects": [(o["id"], o["name"], o["plate"]) for o in data["objects"]]}
    common = entries[0][1] if entries else None
    names = plate_filenames([p for p, _ in entries])
    available = not blockers and bool(entries)
    package = {"available": available, "default": available and not faithful, "representation": PACKAGE,
               "filename": package_filename(record["name"]) if available else None,
               "content_type": PACKAGE_CONTENT_TYPE if available else None, "note": PACKAGE_NOTE,
               "files": [{"plate_id": p["id"], "name": p["name"], "filename": n, "material": q["material"], "fingerprint": q["fingerprint"]}
                         for (p, _), n, q in zip(entries, names, plates)] if available else []}
    return {"version": VERSION, "project_id": record["id"], "revision": record["revision"],
            "fingerprint": hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "representation": SINGLE_FILE, "faithful": faithful, "plates": plates,
            "settings": {kind: len(common[kind]) for kind in KINDS} if common and faithful else None,
            "package": package, "object_overrides": overrides, "differences": differences, "blockers": blockers, "limitations": limitations,
            "filename": filename(record["name"]) if faithful else None, "content_type": CONTENT_TYPE if faithful else None,
            "label": "Settings that will be written to the Orca project file (expected inputs, not measured output)"}


def project_config(review: dict, template: dict, authored: set) -> tuple:
    """(project_settings.config content, keys Orca's template has no place for). Effective values come first and are
    typed canonically; the template only supplies Orca defaults for everything the Project does not state."""
    merged = {}
    for kind in KINDS:
        for key, value in review[kind].items():
            if key not in IDENTITY_KEYS:
                merged[key] = orca_3mf.config_value(value)
    config, dropped = dict(template), []
    for key, value in merged.items():
        if key not in template:
            dropped.append(key)
        elif isinstance(template[key], list) and not isinstance(value, list):
            config[key] = [value]
        elif not isinstance(template[key], list) and isinstance(value, list):
            config[key] = value[0] if value else ""
        else:
            config[key] = value
    names = {"printer_settings_id": review["machine"].get("name"), "print_settings_id": review["process"].get("name"),
             "filament_settings_id": [material(review)["preset"]]}
    config.update({"from": "project", "name": "project_settings", **{k: v for k, v in names.items() if v}})
    return config, sorted(set(dropped) & authored)


def manifest(record_name: str, plan: dict, entries: list) -> str:
    """The readable MANIFEST.txt of a package: which file is which Plate, its material and fingerprint, what an Orca file cannot keep."""
    package = plan["package"]
    lines = [f"Spooler Plate files for the Project \"{record_name}\"", "",
             "This ZIP holds one independent Orca project (3MF) per Plate. Each file contains that Plate's own machine, process and",
             "filament configuration, its Objects with their placement, and any Object-specific settings. It is not a backup of the",
             "Spooler Project (queue, history and Split data are not included) and it is not an Orca-native ZIP format.", "",
             "To use a file: Extract the ZIP, then in Orca choose File > Open Project (or drag the extracted .3mf onto Orca).",
             "Open each file separately; the Plates do not share settings. To bring a file back into Spooler, import the extracted",
             "3MF through the normal import; Spooler shows what it will keep before changing anything.", "",
             f"Planned from Project revision {plan['revision']}; plan fingerprint {plan['fingerprint']}.", "", "Files", "-----"]
    for item, (_, review) in zip(package["files"], entries):
        material = item["material"]
        lines += [item["filename"], f"  Plate: {item['name'] or '(unnamed)'} (id {item['plate_id']})",
                  f"  Material: {material['type'] or '?'}, preset {material['preset'] or '?'}" + (f", colour {material['colour']}" if material["colour"] else ""),
                  f"  Objects: {len(review['objects'])}", f"  Input fingerprint: {item['fingerprint']}"]
        for limit in plan["limitations"]:
            if item["name"] in limit["plates"]:
                lines.append(f"  Limitation: {limit['message']}")
        lines.append("")
    if plan["differences"]:
        lines += ["Why a single file was not used", "------------------------------",
                  "These effective values differ between Plates, and one Orca project holds only one configuration:"]
        for d in plan["differences"]:
            lines.append(f"  {d['key']}: " + "; ".join(f"{p['name'] or p['id']} = {p['value'] if not isinstance(p['value'], list) else ' / '.join(map(str, p['value']))}" for p in d["plates"]))
        lines.append("")
    if plan["limitations"]:
        lines += ["Limitations", "-----------"] + [f"  {l['message']} ({', '.join(l['plates'])})" for l in plan["limitations"]] + [""]
    lines += ["These are the settings Spooler expects Orca to receive, not measured output."]
    return NL.join(lines) + NL
