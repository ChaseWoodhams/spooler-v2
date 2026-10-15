"""
OrcaSlicer / BambuStudio project 3MF files, in both directions.

Reading (`parse`) turns a project into plain data Spooler can import: each object's mesh (its parts merged in the
object's own coordinates), where every copy of it stands (position, rotation, scale), which plate it is on, its
per-object setting changes, and the project's settings (`Metadata/project_settings.config`).  A plain 3MF from
another program works too: every build item becomes an object on one plate.

Writing (`build`) makes a project OrcaSlicer opens like its own: one plate per Spooler plate, every object with its
placement, the per-object settings, and the project settings, so the desktop app shows the same job.

3MF stores a transform as 12 numbers for a row vector: x' = x·M + t, M being the first nine numbers in rows.
"""

import io
import json
import math
import re
import zipfile
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import numpy as np

from values import first as _first

MODEL_PATH    = "3D/3dmodel.model"
SETTINGS_PATH = "Metadata/model_settings.config"
PROJECT_PATH  = "Metadata/project_settings.config"
UNITS         = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
# metadata on an <object> that describes the object rather than changing a setting
OBJECT_INFO   = {"name", "extruder", "matrix", "source_file", "source_object_id", "source_volume_id", "source_offset_x",
                 "source_offset_y", "source_offset_z", "source_in_inches", "source_in_meters", "volume_type", "face_count",
                 "layer_height_profile", "sla_support_points", "sla_drain_holes"}
IGNORED_PARTS = {"negative_part": "negative volumes", "modifier_part": "modifier volumes", "support_blocker": "support blockers",
                 "support_enforcer": "support enforcers"}
MAX_PLATES    = 50


class ThreeMFError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attrs(element) -> dict:
    return {_local(k): v for k, v in element.attrib.items()}


def _transform(text: str | None) -> tuple:
    """(3×3 matrix, translation) from the 12 numbers of a 3MF transform; the identity when absent."""
    try:
        values = [float(v) for v in (text or "").split()]
    except ValueError:
        values = []
    if len(values) != 12:
        return np.eye(3), np.zeros(3)
    return np.array(values[:9]).reshape(3, 3), np.array(values[9:])


def is_project(source) -> bool:
    """True for a 3MF written by OrcaSlicer/BambuStudio: it carries plates and settings, not just a mesh."""
    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return SETTINGS_PATH in names or PROJECT_PATH in names


# ── Reading ────────────────────────────────────────────────────────────────────

def _read_model(archive: zipfile.ZipFile, name: str) -> dict:
    """{object id: {"mesh": (verts, tris) | None, "components": [(path, object id, matrix, translation)]}}, unit scale."""
    objects, unit = {}, 1.0
    current = None
    verts, tris = [], []
    with archive.open(name) as fh:
        for event, element in ElementTree.iterparse(fh, events=("start", "end")):
            tag = _local(element.tag)
            if event == "start":
                if tag == "model":
                    unit = UNITS.get(element.attrib.get("unit", "millimeter"), 1.0)
                elif tag == "object":
                    current = {"id": element.attrib.get("id"), "mesh": None, "components": [], "type": element.attrib.get("type", "model")}
                    verts, tris = [], []
                continue
            if tag == "vertex":
                verts.append((float(element.attrib["x"]), float(element.attrib["y"]), float(element.attrib["z"])))
                element.clear()
            elif tag == "triangle":
                tris.append((int(element.attrib["v1"]), int(element.attrib["v2"]), int(element.attrib["v3"])))
                element.clear()
            elif tag == "component" and current is not None:
                a = _attrs(element)
                matrix, shift = _transform(a.get("transform"))
                current["components"].append((a.get("path"), a.get("objectid"), matrix, shift))
            elif tag == "mesh" and current is not None:
                current["mesh"] = (np.array(verts, dtype=float).reshape(-1, 3) * unit, np.array(tris, dtype=np.int64).reshape(-1, 3))
                verts, tris = [], []
            elif tag == "object" and current is not None:
                objects[current["id"]] = current
                current = None
                element.clear()
    return {"objects": objects, "unit": unit}


def _settings(archive: zipfile.ZipFile) -> dict:
    """Object settings, part kinds and plates from Metadata/model_settings.config (empty for a plain 3MF)."""
    out = {"objects": {}, "plates": []}
    if SETTINGS_PATH not in archive.namelist():
        return out
    try:
        root = ElementTree.fromstring(archive.read(SETTINGS_PATH))
    except ElementTree.ParseError:
        return out
    for element in root:
        tag = _local(element.tag)
        meta = lambda node: {m.attrib.get("key"): m.attrib.get("value", "") for m in node if _local(m.tag) == "metadata"}
        if tag == "object":
            parts = {p.attrib.get("id"): {"subtype": p.attrib.get("subtype", "normal_part"), **meta(p)}
                     for p in element if _local(p.tag) == "part"}
            out["objects"][element.attrib.get("id")] = {"meta": meta(element), "parts": parts}
        elif tag == "plate":
            info = meta(element)
            instances = [(meta(i).get("object_id"), int(meta(i).get("instance_id", 0) or 0))
                         for i in element if _local(i.tag) == "model_instance"]
            try:
                number = int(info.get("plater_id", len(out["plates"]) + 1))
            except ValueError:
                number = len(out["plates"]) + 1
            out["plates"].append({"id": number, "name": info.get("plater_name", "").strip(), "instances": instances})
    return out


def _merge_object(archive, cache: dict, obj: dict, wanted_parts: dict | None, warnings: list, label: str) -> tuple:
    """One top-level object's mesh in its own coordinates: an inline mesh, or its components merged."""
    if obj["mesh"] is not None and not obj["components"]:
        return obj["mesh"]
    all_verts, all_tris, offset = [], [], 0
    skipped = set()
    for path, object_id, matrix, shift in obj["components"]:
        part = (wanted_parts or {}).get(object_id, {})
        kind = part.get("subtype", "normal_part")
        if kind != "normal_part":
            skipped.add(IGNORED_PARTS.get(kind, kind))
            continue
        name = (path or "").lstrip("/") or MODEL_PATH
        if name not in cache:
            if name not in archive.namelist():
                raise ThreeMFError(f"“{label}” refers to {name}, which isn't in the file")
            cache[name] = _read_model(archive, name)
        target = cache[name]["objects"].get(object_id)
        if target is None or target["mesh"] is None:
            raise ThreeMFError(f"“{label}” refers to a missing part")
        verts, tris = target["mesh"]
        all_verts.append(verts @ matrix + shift)
        all_tris.append(tris + offset)
        offset += len(verts)
    if skipped:
        warnings.append(f"{label}: {', '.join(sorted(skipped))} were left out")
    if not all_verts:
        raise ThreeMFError(f"“{label}” has no printable part")
    return np.concatenate(all_verts), np.concatenate(all_tris)


def summary(source) -> dict:
    """What a 3MF holds, counted without reading its meshes: {"objects": printable instances, "plates": plates, "orca": bool}.
    Every printable build item is one object on the bed (a repeated object counts once per copy; a disabled one, printable="0", is not counted); an Orca project lists its plates,
    anything else has the one. Raises ThreeMFError for a file that isn't a 3MF archive."""
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError):
        raise ThreeMFError("That isn't a valid 3MF file")
    with archive:
        if MODEL_PATH not in archive.namelist():
            raise ThreeMFError("The 3MF has no model")
        objects = 0
        with archive.open(MODEL_PATH) as fh:
            for _, element in ElementTree.iterparse(fh):
                if _local(element.tag) == "item" and _attrs(element).get("printable", "1") != "0":
                    objects += 1
                element.clear()
        plates = len(_settings(archive)["plates"]) or 1
        names = set(archive.namelist())
    return {"objects": objects, "plates": plates, "orca": SETTINGS_PATH in names or PROJECT_PATH in names}


# ── Inspecting ─────────────────────────────────────────────────────────────────

REPORT_VERSION = 1
PAINT_ATTRS = {"paint_supports": "paint-supports", "paint_seam": "paint-seam", "paint_fuzzy_skin": "paint-fuzzy-skin", "paint_color": "paint-color"}
SUBTYPE_CODES = {"negative_part": "negative-volume", "modifier_part": "modifier-volume", "support_blocker": "support-blocker",
                 "support_enforcer": "support-enforcer"}
VOLUME_CODES = {"NegativeVolume": "negative-volume", "ParameterModifier": "modifier-volume", "SupportBlocker": "support-blocker",
                "SupportEnforcer": "support-enforcer"}
PART_INFO = OBJECT_INFO | {"subtype", "mesh_stat", "modifier", "text_information", "volume_type", "matrix"}
PLATE_SETTINGS = ("bed_type", "print_sequence", "first_layer_print_sequence", "other_layers_print_sequence",
                  "other_layers_print_sequence_nums", "spiral_mode", "filament_map_mode", "filament_maps", "limit_filament_maps")
# code → (feature, scope, what the import does about it)
LOSSES = {
    "negative-volume":        ("Negative volume", "model", "The hole is not cut: the Model imports as a solid without it."),
    "modifier-volume":        ("Modifier volume", "model", "The modifier region and its settings are left out."),
    "support-blocker":        ("Support blocker", "model", "The blocker is left out, so supports can appear where it was."),
    "support-enforcer":       ("Support enforcer", "model", "The enforcer is left out, so supports may be missing where it was."),
    "paint-supports":         ("Painted supports", "model", "The painted support areas are not kept."),
    "paint-seam":             ("Painted seam", "model", "The painted seam positions are not kept."),
    "paint-fuzzy-skin":       ("Painted fuzzy skin", "model", "The painted fuzzy skin areas are not kept."),
    "paint-color":            ("Painted colour", "model", "The painted colour regions are not kept."),
    "variable-layer-height":  ("Variable layer height", "model", "The layer height profile is not kept; the Plate's layer height is used throughout."),
    "layer-range":            ("Layer-range settings", "model", "Settings that apply only to a range of heights are not kept."),
    "part-settings":          ("Part settings", "model", "Settings stored on individual parts are not kept; the parts merge into one Model."),
    "legacy-volume-range":    ("Volumes inside one mesh", "model", "The volumes are not separated, so their triangles import as one solid Model."),
    "brim-ears":              ("Brim ears", "model", "The manual brim ear points are not kept."),
    "mirrored-transform":     ("Mirrored placement", "object", ""),
    "skewed-transform":       ("Skewed placement", "object", ""),
    "disabled-copy":          ("Disabled copy", "object", "The disabled copy is left out; only enabled copies are imported."),
    "custom-gcode-per-layer": ("G-code at layers", "plate", "Colour changes, pauses and other layer G-code are not kept, so they will not appear in a Slice."),
    "plate-metadata":         ("Plate settings", "plate", "The Plate's own bed type, print sequence and similar settings are not kept."),
}
TRANSFORM_EFFECT = {
    "single": {"mirrored-transform": "The mirrored shape is baked into the Model's geometry; its position is chosen by Spooler.",
               "skewed-transform": "The skewed shape is baked into the Model's geometry; its position is chosen by Spooler."},
    "project": {"mirrored-transform": "The Object is placed unmirrored.", "skewed-transform": "The Object is placed unskewed."},
}
BLOCKERS = {"malformed-settings": "Metadata/model_settings.config can't be read, so the Plates and Object settings are unresolved.",
            "malformed-project-settings": "Metadata/project_settings.config can't be read, so the project's material and settings are unresolved.",
            "nothing-printable": "Every copy in the file is disabled, so there is nothing to print."}


def _object_label(settings: dict, object_id) -> str:
    label = settings.get("meta", {}).get("name") or f"Object {object_id}"
    return re.sub(r"\.(stl|obj|3mf|step|stp)$", "", label.replace("\\", "/").rsplit("/", 1)[-1], flags=re.I) or f"Object {object_id}"


def _scan_model(archive: zipfile.ZipFile, name: str) -> dict:
    """What one model file holds without reading its meshes: {"objects": {id: {"components", "paint"}}, "items"}."""
    objects, items, current = {}, [], None
    try:
        with archive.open(name) as fh:
            for event, element in ElementTree.iterparse(fh, events=("start", "end")):
                tag = _local(element.tag)
                if event == "start":
                    if tag == "object":
                        current = {"components": [], "paint": set()}
                        objects[element.attrib.get("id")] = current
                    continue
                if tag == "triangle" and current is not None:
                    for key, code in PAINT_ATTRS.items():
                        if element.attrib.get(key, "").strip():
                            current["paint"].add(code)
                    element.clear()
                elif tag == "component" and current is not None:
                    a = _attrs(element)
                    current["components"].append(((a.get("path") or "").lstrip("/") or name, a.get("objectid")))
                elif tag == "item":
                    a = _attrs(element)
                    matrix, _ = _transform(a.get("transform"))
                    items.append({"object": a.get("objectid"), "matrix": matrix, "printable": a.get("printable", "1") != "0"})
                elif tag == "object":
                    current = None
    except ElementTree.ParseError:
        raise ThreeMFError("The 3MF's model can't be read")
    return {"objects": objects, "items": items}


def _text_lines(archive: zipfile.ZipFile, name: str) -> list:
    """(1-based object position, payload) for each `object_id=N|payload` line that has a payload."""
    if name not in archive.namelist():
        return []
    out = []
    for line in archive.read(name).decode("utf-8", "replace").splitlines():
        match = re.match(r"\s*object_id\s*=\s*(\d+)\s*\|(.*)$", line)
        if match and match[2].strip():
            out.append((int(match[1]), match[2].strip()))
    return out


def _xml(archive: zipfile.ZipFile, name: str):
    """The root element of an optional metadata file, or None when it is absent or unreadable."""
    if name not in archive.namelist():
        return None
    try:
        return ElementTree.fromstring(archive.read(name))
    except ElementTree.ParseError:
        return None


def _skewed(matrix: np.ndarray) -> bool:
    a = matrix.T
    scale = np.linalg.norm(a, axis=0)
    scale[scale < 1e-9] = 1.0
    rotation = a / scale
    return bool(np.abs(rotation.T @ rotation - np.eye(3)).max() > 1e-3)


def inspect(source, conversion: str = "project") -> dict:
    """What importing this 3MF would drop, change or leave unresolved, read from the original archive before anything is
    flattened: {"version", "objects", "plates", "orca", "project", "losses": [...], "blockers": [...]}. Each loss names its
    `scope` (model, object, plate), the affected `name` and a stable `code`; a blocker is data that must be replaced before
    any import. Detecting a feature never means it is kept. `conversion` ("single" or "project") says how the placement is
    converted. Raises ThreeMFError for a file that isn't a readable 3MF."""
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError):
        raise ThreeMFError("That isn't a valid 3MF file")
    with archive:
        names = set(archive.namelist())
        if MODEL_PATH not in names:
            raise ThreeMFError("The 3MF has no model")
        main = _scan_model(archive, MODEL_PATH)
        cache = {MODEL_PATH: main}

        def painted(path, object_id, seen=()):
            if (path, object_id) in seen:
                return set()
            if path not in cache:
                cache[path] = _scan_model(archive, path) if path in names else {"objects": {}, "items": []}
            entry = cache[path]["objects"].get(object_id)
            if entry is None:
                return set()
            found = set(entry["paint"])
            for sub_path, sub_id in entry["components"]:
                found |= painted(sub_path, sub_id, seen + ((path, object_id),))
            return found

        blockers = []
        root = None
        if SETTINGS_PATH in names:
            root = _xml(archive, SETTINGS_PATH)
            if root is None:
                blockers.append({"code": "malformed-settings", "scope": "project", "name": SETTINGS_PATH, "message": BLOCKERS["malformed-settings"]})
        info = _settings(archive) if root is not None else {"objects": {}, "plates": []}
        project = None
        if PROJECT_PATH in names:
            try:
                project = json.loads(archive.read(PROJECT_PATH).decode("utf-8", "replace"))
            except ValueError:
                blockers.append({"code": "malformed-project-settings", "scope": "project", "name": PROJECT_PATH,
                                 "message": BLOCKERS["malformed-project-settings"]})
        items = main["items"]
        if items and not any(i["printable"] for i in items):
            blockers.append({"code": "nothing-printable", "scope": "project", "name": "Build", "message": BLOCKERS["nothing-printable"]})

        order = list(info["objects"]) or list(dict.fromkeys(i["object"] for i in items))
        by_position = {n: object_id for n, object_id in enumerate(order, start=1)}
        referenced = list(dict.fromkeys(i["object"] for i in items if i["object"] in main["objects"]))
        found = {object_id: {} for object_id in referenced}     # object id → {code: detail}
        for object_id in referenced:
            entry, settings = found[object_id], info["objects"].get(object_id, {})
            for code in sorted(painted(MODEL_PATH, object_id)):
                entry[code] = None
            for part in settings.get("parts", {}).values():
                code = SUBTYPE_CODES.get(part.get("subtype", "normal_part"))
                if code:
                    entry[code] = None
                elif set(part) - PART_INFO:
                    entry["part-settings"] = sorted(set(part) - PART_INFO)
            if settings.get("meta", {}).get("layer_height_profile", "").strip():
                entry["variable-layer-height"] = None
        for node in (root if root is not None else []):
            if _local(node.tag) != "object" or node.attrib.get("id") not in found:
                continue
            entry = found[node.attrib["id"]]
            volumes = [v for v in node if _local(v.tag) == "volume"]
            for volume in volumes:
                values = {m.attrib.get("key"): m.attrib.get("value", "") for m in volume if _local(m.tag) == "metadata"}
                code = VOLUME_CODES.get(values.get("volume_type", ""))
                if code:
                    entry[code] = None
                if set(values) - PART_INFO:
                    entry.setdefault("part-settings", sorted(set(values) - PART_INFO))
            if len(volumes) > 1 and any(v.attrib.get("firstid") is not None for v in volumes):
                entry["legacy-volume-range"] = None
        for file, code in (("Metadata/layer_heights_profile.txt", "variable-layer-height"), ("Metadata/brim_ear_points.txt", "brim-ears")):
            for position, payload in _text_lines(archive, file):
                if by_position.get(position) in found and re.search(r"\d", payload):
                    found[by_position[position]][code] = None
        ranges = _xml(archive, "Metadata/layer_config_ranges.xml")
        for node in (ranges if ranges is not None else []):
            if _local(node.tag) == "object" and any(_local(r.tag) == "range" for r in node):
                target = node.attrib.get("id")
                target = target if target in found else by_position.get(int(target)) if (target or "").isdigit() else None
                if target in found:
                    found[target]["layer-range"] = None

        losses = []

        def add(code, ident, name, detail=None):
            feature, scope, text = LOSSES[code]
            losses.append({"code": code, "feature": feature, "scope": scope, "id": ident, "name": name, "detail": detail,
                           "effect": TRANSFORM_EFFECT.get(conversion, {}).get(code) or text})

        for object_id in referenced:
            name = _object_label(info["objects"].get(object_id, {}), object_id)
            copies = [i for i in items if i["object"] == object_id]
            for code in LOSSES:
                if code in found[object_id]:
                    add(code, object_id, name, found[object_id][code])
            for code, count in (("mirrored-transform", sum(float(np.linalg.det(i["matrix"])) < 0 for i in copies)),
                                ("skewed-transform", sum(_skewed(i["matrix"]) for i in copies)),
                                ("disabled-copy", sum(not i["printable"] for i in copies))):
                if count:
                    add(code, object_id, name, {"copies": count})

        plate_names = {str(p["id"]): p["name"] or f"Plate {p['id']}" for p in info["plates"]}
        for node in (root if root is not None else []):
            if _local(node.tag) == "plate":
                values = {m.attrib.get("key"): m.attrib.get("value", "") for m in node if _local(m.tag) == "metadata"}
                keys = [k for k in PLATE_SETTINGS if values.get(k, "").strip()]
                if keys:
                    ident = values.get("plater_id", "")
                    add("plate-metadata", ident, plate_names.get(ident) or f"Plate {ident}", {"keys": keys})
        custom = _xml(archive, "Metadata/custom_gcode_per_layer.xml")
        for node in (custom if custom is not None else []):
            if _local(node.tag) == "plate":
                ident = next((c.attrib.get("id", "") for c in node if _local(c.tag) == "plate_info"), "")
                count = sum(_local(c.tag) == "code" for c in node)
                if count:
                    add("custom-gcode-per-layer", ident, plate_names.get(ident) or f"Plate {ident}", {"changes": count})
        return {"version": REPORT_VERSION, "orca": SETTINGS_PATH in names or PROJECT_PATH in names, "losses": losses, "blockers": blockers,
                "objects": sum(i["printable"] for i in items), "plates": len(info["plates"]) or 1, "project": project}


def parse(source) -> dict:
    """Read a project (path or file object). Raises ThreeMFError for a file that can't be read."""
    warnings = []
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError):
        raise ThreeMFError("That isn't a valid 3MF file")
    with archive:
        if MODEL_PATH not in archive.namelist():
            raise ThreeMFError("The 3MF has no model")
        main = _read_model(archive, MODEL_PATH)
        cache = {MODEL_PATH: main}
        info = _settings(archive)

        # build items, in order; a repeated object is another instance of it
        build = []
        with archive.open(MODEL_PATH) as fh:
            for _, element in ElementTree.iterparse(fh):
                if _local(element.tag) == "item":
                    a = _attrs(element)
                    matrix, shift = _transform(a.get("transform"))
                    build.append({"object": a.get("objectid"), "matrix": matrix, "shift": shift, "printable": a.get("printable", "1") != "0"})
        if not build:
            raise ThreeMFError("The 3MF has nothing to print")

        plate_of, plates = {}, []
        for plate in info["plates"]:
            plates.append({"id": plate["id"], "name": plate["name"]})
            for key in plate["instances"]:
                plate_of[key] = plate["id"]
        if not plates:
            plates = [{"id": 1, "name": ""}]
        if len(plates) > MAX_PLATES:
            raise ThreeMFError(f"A project has at most {MAX_PLATES} plates")

        objects, seen = {}, {}
        for item in build:
            object_id = item["object"]
            source_object = main["objects"].get(object_id)
            if source_object is None:
                warnings.append(f"A build item refers to missing object {object_id}")
                continue
            index = seen[object_id] = seen.get(object_id, -1) + 1
            entry = objects.get(object_id)
            settings = info["objects"].get(object_id, {})
            if entry is None:
                label = settings.get("meta", {}).get("name") or f"Object {object_id}"
                label = re.sub(r"\.(stl|obj|3mf|step|stp)$", "", label.replace("\\", "/").rsplit("/", 1)[-1], flags=re.I) or f"Object {object_id}"
                verts, tris = _merge_object(archive, cache, source_object, settings.get("parts"), warnings, label)
                overrides = {k: v for k, v in settings.get("meta", {}).items() if k not in OBJECT_INFO}
                entry = objects[object_id] = {"name": label[:100], "verts": verts, "tris": tris, "settings": overrides, "instances": []}
            plate = plate_of.get((object_id, index), plates[0]["id"])
            entry["instances"].append({"matrix": item["matrix"], "shift": item["shift"], "plate": plate, "printable": item["printable"]})

        project = None
        if PROJECT_PATH in archive.namelist():
            try:
                project = json.loads(archive.read(PROJECT_PATH).decode("utf-8", "replace"))
            except ValueError:
                warnings.append("The project's settings couldn't be read")
        if not objects:
            raise ThreeMFError("The 3MF has nothing to print")
        _remove_plate_origins(list(objects.values()), plates, project)
    return {"objects": list(objects.values()), "plates": plates, "project": project, "warnings": warnings}


def plate_origin(number: int, count: int, width: float, depth: float) -> tuple:
    """Where Orca puts Plate `number` (1-based) of `count` in its Plate grid: columns grow to the square root of the count and
    the Plates are a fifth of a bed apart (PartPlateList::compute_origin)."""
    root = count ** 0.5
    columns = round(root) + (1 if root > round(root) else 0)
    row, column = divmod(number - 1, columns)
    return (column * width * 1.2, -row * depth * 1.2)


def _bed_size(project) -> tuple:
    points = []
    for item in (project or {}).get("printable_area") or []:
        try:
            x, y = str(item).lower().split("x")
            points.append((float(x), float(y)))
        except ValueError:
            continue
    if not points:
        return 256.0, 256.0
    return max(p[0] for p in points) - min(p[0] for p in points), max(p[1] for p in points) - min(p[1] for p in points)


def _remove_plate_origins(objects: list, plates: list, project) -> None:
    """Orca stores every Object where its Plate stands in the Plate grid; Spooler places Objects on the bed. An instance that
    lies inside its own Plate's cell (other than the first, which sits at the origin) is moved back onto the bed. One placed
    elsewhere, such as in a file Spooler wrote before this, is left where it is."""
    width, depth = _bed_size(project)
    numbers = {p["id"]: n for n, p in enumerate(sorted(plates, key=lambda p: p["id"] if isinstance(p["id"], int) else 0), start=1)}
    for obj in objects:
        for instance in obj["instances"]:
            number = instance["plate"] if isinstance(instance["plate"], int) else numbers.get(instance["plate"], 1)
            if number < 2:
                continue
            x, y = plate_origin(number, len(plates), width, depth)
            world = obj["verts"] @ instance["matrix"] + instance["shift"]
            low, high = world.min(axis=0), world.max(axis=0)
            centre = ((low[0] + high[0]) / 2, (low[1] + high[1]) / 2)
            if x <= centre[0] <= x + width and y <= centre[1] <= y + depth:
                instance["shift"] = instance["shift"] - np.array([x, y, 0.0])


def pose(verts: np.ndarray, matrix: np.ndarray, shift: np.ndarray) -> dict:
    """Spooler's placement (position, rotation about X/Y/Z in degrees, scale) that stands `verts` where the
    transform does. Spooler centres the model, scales, rotates X then Y then Z, sets it on the bed and centres its footprint."""
    a = matrix.T
    scale = np.linalg.norm(a, axis=0)
    scale[scale < 1e-9] = 1.0
    rotation = a / scale
    notes = []
    if np.linalg.det(rotation) < 0:
        notes.append("mirrored")
    if np.abs(rotation.T @ rotation - np.eye(3)).max() > 1e-3:
        notes.append("skewed")
    if abs(rotation[2, 0]) < 0.999999:
        ry = -math.asin(max(-1.0, min(1.0, rotation[2, 0])))
        rx = math.atan2(rotation[2, 1], rotation[2, 2])
        rz = math.atan2(rotation[1, 0], rotation[0, 0])
    else:  # looking straight along an axis: any split of the turn between X and Z gives the same shape
        ry = -math.copysign(math.pi / 2, rotation[2, 0])
        rx = math.atan2(-rotation[1, 2], rotation[1, 1])
        rz = 0.0
    world = verts @ matrix + shift
    low, high = world.min(axis=0), world.max(axis=0)
    return {"position": [round(float((low[0] + high[0]) / 2), 3), round(float((low[1] + high[1]) / 2), 3)],
            "rotation": [round(math.degrees(v) % 360, 4) for v in (rx, ry, rz)],
            "scale": [round(float(min(max(s, 0.01), 100)), 6) for s in scale], "notes": notes}


# ── Project settings → Spooler's setting changes ───────────────────────────────

def _same(a, b) -> bool:
    if a == b:
        return True
    try:
        return math.isclose(float(str(a).rstrip("%")), float(str(b).rstrip("%")), rel_tol=1e-9, abs_tol=1e-9) and str(a).endswith("%") == str(b).endswith("%")
    except ValueError:
        return False


def settings_changes(project: dict, base: dict, allowed: dict) -> dict:
    """{"process": {…}, "filament": {…}}: the project's values that differ from the flattened presets in `base`,
    limited to the keys in `allowed` ({"process": set of keys, "filament": …})."""
    changes = {"process": {}, "filament": {}}
    for kind in changes:
        preset = base.get(kind) or {}
        for key in allowed.get(kind, ()):
            if key not in project or key not in preset:
                continue
            new, old = project[key], preset[key]
            if isinstance(old, list) or isinstance(new, list):
                new = new if isinstance(new, list) else [new]
                old = old if isinstance(old, list) else [old]
                if len(new) == len(old) and all(_same(x, y) for x, y in zip(new, old)):
                    continue
                changes[kind][key] = [str(v) for v in new[:1]] if kind == "filament" else new
            elif not _same(new, old):
                changes[kind][key] = new
    return changes


# ── Writing ────────────────────────────────────────────────────────────────────

CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                 ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                 ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                 ' <Default Extension="png" ContentType="image/png"/>\n'
                 ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>')
ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
             ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
             '</Relationships>')
NAMESPACES = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
              'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"')


def _attr(value) -> str:
    return '"' + escape(str(value), {'"': "&quot;"}) + '"'


def scalar(value) -> str:
    """One typed setting value as Orca writes it: booleans 1/0, a nullable's null as nil, numbers without float noise."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.15g}"
    return str(value)


def metadata_value(value) -> str:
    """A setting in Object metadata: a vector is its elements joined by ';'."""
    return ";".join(scalar(v) for v in value) if isinstance(value, (list, tuple)) else scalar(value)


def config_value(value):
    """A setting in project_settings.config (JSON): vectors stay lists, every element a canonical string."""
    return [config_value(v) for v in value] if isinstance(value, (list, tuple)) else scalar(value)


def _numbers(values) -> str:
    return " ".join(f"{v:.12g}" for v in values)


def _mesh_xml(verts: np.ndarray, tris: np.ndarray) -> str:
    vertices = "".join(f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>' for x, y, z in verts)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>"


def build(objects: list, plates: list, project_config: dict, app_version: str = "2.4.2") -> bytes:
    """A project 3MF. `objects`: [{"name", "verts", "tris" (the mesh as imported), "matrix", "shift" (its placement on the
    bed), "plate" (id), "settings" ({key: value} per-object changes)}]; `plates`: [{"id", "name"}]."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        main = [f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NAMESPACES}>\n',
                ' <metadata name="Application">BambuStudio-02.06.00.51</metadata>\n',
                f' <metadata name="OrcaSlicer">{escape(app_version)}</metadata>\n',
                ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <metadata name="Title">Spooler project</metadata>\n <resources>\n']
        rels, config, build_items = [], ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'], []
        for index, obj in enumerate(objects):
            mesh_id, top_id = 2 * index + 1, 2 * index + 2      # each object: a mesh file, and the object that places it
            path = f"3D/Objects/object_{mesh_id}.model"
            archive.writestr(path, f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NAMESPACES}>\n'
                                   f' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
                                   f'  <object id="{mesh_id}" type="model">{_mesh_xml(obj["verts"], obj["tris"])}</object>\n </resources>\n <build/>\n</model>\n')
            rels.append(f' <Relationship Target="/{path}" Id="rel-{mesh_id}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n')
            main.append(f'  <object id="{top_id}" type="model"><components>'
                        f'<component p:path="/{path}" objectid="{mesh_id}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>\n')
            transform = _numbers([*np.asarray(obj["matrix"]).reshape(-1), *obj["shift"]])
            build_items.append(f'  <item objectid="{top_id}" transform="{transform}" printable="1"/>\n')
            config.append(f'  <object id="{top_id}">\n    <metadata key="name" value={_attr(obj["name"])}/>\n    <metadata key="extruder" value="1"/>\n')
            for key, value in sorted((obj.get("settings") or {}).items()):
                config.append(f'    <metadata key={_attr(key)} value={_attr(metadata_value(value))}/>\n')
            config.append(f'    <part id="{mesh_id}" subtype="normal_part">\n      <metadata key="name" value={_attr(obj["name"])}/>\n'
                          '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n    </part>\n  </object>\n')
        for plate in plates:
            config.append(f'  <plate>\n    <metadata key="plater_id" value="{plate["id"]}"/>\n'
                          f'    <metadata key="plater_name" value={_attr(plate.get("name") or "")}/>\n    <metadata key="locked" value="false"/>\n')
            for index, obj in enumerate(objects):
                if obj["plate"] == plate["id"]:
                    config.append(f'    <model_instance>\n      <metadata key="object_id" value="{2 * index + 2}"/>\n'
                                  f'      <metadata key="instance_id" value="0"/>\n      <metadata key="identify_id" value="{100 + index}"/>\n    </model_instance>\n')
            config.append("  </plate>\n")
        config.append("  <assemble>\n  </assemble>\n</config>\n")
        main.append(" </resources>\n <build>\n" + "".join(build_items) + " </build>\n</model>\n")
        archive.writestr(MODEL_PATH, "".join(main))
        archive.writestr("3D/_rels/3dmodel.model.rels",
                         '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                         + "".join(rels) + "</Relationships>")
        archive.writestr(SETTINGS_PATH, "".join(config))
        archive.writestr(PROJECT_PATH, json.dumps(project_config, indent=4))
        archive.writestr("Metadata/slice_info.config",
                         '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <header>\n    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
                         f'    <header_item key="X-BBL-Client-Version" value="02.06.00.51"/>\n    <header_item key="OrcaSlicer-Version" value="{escape(app_version)}"/>\n'
                         '  </header>\n</config>\n')
    return out.getvalue()
