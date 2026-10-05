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
    return {"objects": list(objects.values()), "plates": plates, "project": project, "warnings": warnings}


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


def _first(value):
    return value[0] if isinstance(value, list) and value else value


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


def _numbers(values) -> str:
    return " ".join(f"{v:.9g}" for v in values)


def _mesh_xml(verts: np.ndarray, tris: np.ndarray) -> str:
    vertices = "".join(f'<vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>' for x, y, z in verts)
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
                config.append(f'    <metadata key={_attr(key)} value={_attr(value)}/>\n')
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
