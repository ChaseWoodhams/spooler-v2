"""The explicit lossy whole-Project 3MF: a report of exactly what one Orca file would change, bound to the plan it came from.

Pure; I/O is in projects.py. The alternative exists only when the Project cannot be written faithfully (export_plan.py) and
nothing blocks it. The file carries one machine/process/filament configuration, that of the first nonempty Plate (the
"basis"); every other Plate is written with it. Spooler never picks this representation for the faithful download: it is
generated only for an acknowledgement that names this report's version, fingerprint and representation.

Report version 1:
  available, reason   reason is None, "faithful-available" (nothing is lost, so nothing to acknowledge) or "blocked"
  representation      "lossy-3mf"
  report_version, report_fingerprint   what an acknowledgement must repeat, with the plan's revision and fingerprint
  filename/content_type   the artifact: an ordinary Orca 3MF whose name says it is lossy
  basis               the Plate whose configuration is written for all of them
  plates              [{id, name, objects, material, written_as, material_changed}]
  differences         [{kind, category, key, written, plates: [{id, name, value, kept}]}]; category is material, bed,
                      plate-settings or machine
  object_overrides    eligible Object settings that are still carried (compared with the basis)
  inherited           locked-preset values Spooler cannot validate or edit; they are carried unchanged
  features            accepted import losses (advanced Orca features) recorded with the Project; none is preserved
  bricklayers         null, or the Plates and the explanation that Orca configuration cannot reproduce the transformed G-code
"""
import export_plan as planner
from values import digest

VERSION = 1
REPRESENTATION = "lossy-3mf"
BED_KEYS = {"curr_bed_type", "bed_type", "printable_area", "printable_height", "bed_exclude_area"}
BRICKLAYERS = ("BrickLayers changes the G-code after Orca slices it, and Orca configuration and geometry cannot reproduce that "
               "transformation. This file re-slices in Orca without it. The Plate's actual Slice G-code, with BrickLayers "
               "applied, stays a separate download.")


def category(kind: str, key: str) -> str:
    if key in BED_KEYS or "plate_temp" in key or key.startswith("bed_"):
        return "bed"
    return {"filament": "material", "machine": "machine"}.get(kind, "plate-settings")


def filename(project_name: str) -> str:
    return planner.filename(project_name)[:-len(".3mf")] + " (lossy).3mf"


def _inherited(entries: list) -> list:
    found = {}
    for plate, review in entries:
        for issue in review["issues"]:
            if issue.get("scope") == "preset" and issue.get("key"):
                item = found.setdefault((issue.get("kind"), issue["key"]), {"kind": issue.get("kind"), "key": issue["key"], "value": issue.get("value"),
                                                                          "message": issue.get("message"), "plates": []})
                item["plates"].append(plate["name"])
    return [found[k] for k in sorted(found, key=lambda k: (str(k[0]), k[1]))]


def _features(records: list) -> list:
    return [{"record_id": r["id"], "filename": r.get("filename"),
             "losses": [{k: loss.get(k) for k in ("code", "feature", "scope", "name", "effect")} for loss in r["losses"]]}
            for r in records if r.get("losses")]


def alternative(record: dict, plan: dict, entries: list, import_records: list) -> dict:
    """The lossy alternative for `plan` (built from `entries`, the same [(plate, effective_settings)] it used)."""
    base = {"representation": REPRESENTATION, "report_version": VERSION, "available": False, "reason": None,
            "filename": None, "content_type": None, "report_fingerprint": None, "basis": None, "plates": [], "differences": [],
            "object_overrides": [], "inherited": [], "features": [], "bricklayers": None,
            "faithful_alternatives": [{"representation": planner.SINGLE_FILE, "available": plan["faithful"],
                                       "why": None if plan["faithful"] else "Plates differ in material or settings that one Orca file cannot hold"},
                                      {"representation": planner.PACKAGE, "available": plan["package"]["available"],
                                       "why": None if plan["package"]["available"] else "Something blocks the export"}]}
    if plan["faithful"]:
        return {**base, "reason": "faithful-available"}
    if plan["blockers"] or not entries:
        return {**base, "reason": "blocked"}
    basis_plate, basis = entries[0]
    written = planner.material(basis)
    plates = []
    for plate, review in entries:
        own = planner.material(review)
        plates.append({"id": plate["id"], "name": plate["name"], "objects": [o["name"] for o in review["objects"]], "material": own,
                       "written_as": written, "material_changed": (own["type"], own["preset"]) != (written["type"], written["preset"])})
    differences = []
    for d in plan["differences"]:
        reference = d["plates"][0]["value"]
        differences.append({"kind": d["kind"], "category": category(d["kind"], d["key"]), "key": d["key"], "written": reference,
                            "plates": [{**p, "kept": p["value"] == reference} for p in d["plates"]]})
    bricks = next((l for l in plan["limitations"] if l["code"] == "bricklayers"), None)
    report = {"version": VERSION, "basis": basis_plate["id"], "plates": [(p["id"], p["material"], p["written_as"]) for p in plates],
              "differences": differences, "object_overrides": [row for plate, review in entries for row in planner.override_rows(review, plate, basis["process"])], "inherited": _inherited(entries),
              "features": _features(import_records), "bricklayers": bricks["plates"] if bricks else None}
    return {**base, "available": True, "filename": filename(record["name"]), "content_type": planner.CONTENT_TYPE,
            "basis": {"plate_id": basis_plate["id"], "plate_name": basis_plate["name"], "material": written},
            "plates": plates, "differences": differences, "object_overrides": report["object_overrides"], "inherited": report["inherited"],
            "features": report["features"],
            "bricklayers": {"plates": bricks["plates"], "message": BRICKLAYERS} if bricks else None,
            "report_fingerprint": digest(report, default=str)}
