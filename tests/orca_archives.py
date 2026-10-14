"""Hand-built Orca/Bambu 3MF archives for import-review tests.

These are written as raw XML, independently of orca_3mf.build, so an omission shared by Spooler's writer and parser cannot hide
a feature. Each loss family has a small option; `archive()` combines any of them."""

import io
import json
import zipfile
from xml.sax.saxutils import quoteattr

CUBE = [(x, y, z) for x in (0, 10) for y in (0, 10) for z in (0, 10)]
TRIS = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
        (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
NS = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
      'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"')


def mesh(z=0.0, paint=None):
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z + zz}"/>' for x, y, zz in CUBE)
    paint = paint or {}
    attrs = "".join(f" {key}={quoteattr(value)}" for key, value in paint.items())
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"{attrs if i == 0 else ""}/>' for i, (a, b, c) in enumerate(TRIS))
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"


def meta(values):
    return "".join(f'<metadata key={quoteattr(k)} value={quoteattr(str(v))}/>' for k, v in values.items())


def archive(objects=None, plates=None, project=None, extras=None, model_settings=None, sub_models=False):
    """objects: [{"id", "name", "parts": [(subtype, {metadata})], "paint": {attr: value}, "meta": {...},
                  "items": [{"transform": str|None, "printable": bool, "plate": n}], "volumes": [(firstid, lastid, {meta})]}]
    plates: [{"id", "name", "meta": {...}}]; project: dict for project_settings.config; extras: {archive path: text}.
    Returns the bytes of a 3MF."""
    objects = objects if objects is not None else [{"id": 1, "name": "Bracket"}]
    resources, build, settings_objects, instances = [], [], [], {}
    nested = {}
    for spec in objects:
        oid = spec["id"]
        parts = spec.get("parts")
        paint = spec.get("paint")
        if parts:
            components, part_xml = [], []
            for k, (subtype, part_meta) in enumerate(parts, start=1):
                part_id = oid * 100 + k
                body = f'<object id="{part_id}" type="model">{mesh(k * 20.0, paint if k == 1 else None)}</object>'
                if sub_models:
                    nested.setdefault(f"3D/Objects/object_{oid}.model", []).append(body)
                    components.append(f'<component p:path="/3D/Objects/object_{oid}.model" objectid="{part_id}"/>')
                else:
                    resources.append(body)
                    components.append(f'<component objectid="{part_id}"/>')
                part_xml.append(f'<part id="{part_id}" subtype="{subtype}">{meta({"name": f"part{k}", **part_meta})}</part>')
            resources.append(f'<object id="{oid}" type="model"><components>{"".join(components)}</components></object>')
            volumes = "".join(part_xml)
        else:
            resources.append(f'<object id="{oid}" type="model">{mesh(0.0, paint)}</object>')
            volumes = "".join(f'<volume firstid="{a}" lastid="{b}">{meta(vm)}</volume>' for a, b, vm in spec.get("volumes", []))
        settings_objects.append(f'<object id="{oid}">{meta({"name": spec.get("name", f"Object {oid}"), **spec.get("meta", {})})}{volumes}</object>')
        for index, item in enumerate(spec.get("items") or [{}]):
            transform = item.get("transform") or "1 0 0 0 1 0 0 0 1 100 100 0"
            printable = "1" if item.get("printable", True) else "0"
            build.append(f'<item objectid="{oid}" transform="{transform}" printable="{printable}"/>')
            instances.setdefault(item.get("plate", 1), []).append((oid, index))
    model = f'<?xml version="1.0"?><model unit="millimeter" {NS}><resources>{"".join(resources)}</resources><build>{"".join(build)}</build></model>'
    if model_settings is None:
        plate_specs = plates or [{"id": 1, "name": ""}]
        plate_xml = ""
        for plate in plate_specs:
            members = "".join(f'<model_instance>{meta({"object_id": o, "instance_id": i})}</model_instance>'
                              for o, i in instances.get(plate["id"], []))
            plate_xml += f'<plate>{meta({"plater_id": plate["id"], "plater_name": plate.get("name", ""), **plate.get("meta", {})})}{members}</plate>'
        model_settings = f'<?xml version="1.0"?><config>{"".join(settings_objects)}{plate_xml}</config>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", model)
        for name, bodies in nested.items():
            z.writestr(name, f'<?xml version="1.0"?><model unit="millimeter" {NS}><resources>{"".join(bodies)}</resources><build/></model>')
        z.writestr("Metadata/model_settings.config", model_settings)
        if project is not None:
            z.writestr("Metadata/project_settings.config", project if isinstance(project, str) else json.dumps(project))
        for name, text in (extras or {}).items():
            z.writestr(name, text)
    return buffer.getvalue()


# One minimal example of each loss family: (code, builder keyword arguments, expected scope, expected affected name)
MIRROR = "-1 0 0 0 1 0 0 0 1 100 100 0"
SKEW = "1 0.5 0 0 1 0 0 0 1 100 100 0"
FAMILIES = {
    "negative-volume": ({"objects": [{"id": 1, "name": "Hollow", "parts": [("normal_part", {}), ("negative_part", {})]}]}, "model", "Hollow"),
    "modifier-volume": ({"objects": [{"id": 1, "name": "Zoned", "parts": [("normal_part", {}), ("modifier_part", {"wall_loops": "6"})]}]}, "model", "Zoned"),
    "support-blocker": ({"objects": [{"id": 1, "name": "Blocked", "parts": [("normal_part", {}), ("support_blocker", {})]}]}, "model", "Blocked"),
    "support-enforcer": ({"objects": [{"id": 1, "name": "Enforced", "parts": [("normal_part", {}), ("support_enforcer", {})]}]}, "model", "Enforced"),
    "paint-supports": ({"objects": [{"id": 1, "name": "Painted", "paint": {"paint_supports": "4"}}]}, "model", "Painted"),
    "paint-seam": ({"objects": [{"id": 1, "name": "Seamed", "paint": {"paint_seam": "8"}}]}, "model", "Seamed"),
    "paint-fuzzy-skin": ({"objects": [{"id": 1, "name": "Fuzzy", "paint": {"paint_fuzzy_skin": "C"}}]}, "model", "Fuzzy"),
    "paint-color": ({"objects": [{"id": 1, "name": "Coloured", "paint": {"paint_color": "8"}}]}, "model", "Coloured"),
    "variable-layer-height": ({"objects": [{"id": 1, "name": "Smooth"}],
                               "extras": {"Metadata/layer_heights_profile.txt": "object_id=1|0 0.2 5 0.1 10 0.2\n"}}, "model", "Smooth"),
    "layer-range": ({"objects": [{"id": 1, "name": "Banded"}],
                     "extras": {"Metadata/layer_config_ranges.xml": '<objects><object id="1"><range min="0" max="2"><option opt_key="wall_loops">5</option></range></object></objects>'}},
                    "model", "Banded"),
    "part-settings": ({"objects": [{"id": 1, "name": "Mixed", "parts": [("normal_part", {"wall_loops": "7"}), ("normal_part", {})]}]}, "model", "Mixed"),
    "legacy-volume-range": ({"objects": [{"id": 1, "name": "Legacy", "volumes": [(0, 5, {"volume_type": "ModelPart"}), (6, 11, {"volume_type": "ParameterModifier"})]}]},
                            "model", "Legacy"),
    "brim-ears": ({"objects": [{"id": 1, "name": "Eared"}], "extras": {"Metadata/brim_ear_points.txt": "object_id=1|5 5 0 1\n"}}, "model", "Eared"),
    "mirrored-transform": ({"objects": [{"id": 1, "name": "Flipped", "items": [{"transform": MIRROR}]}]}, "object", "Flipped"),
    "skewed-transform": ({"objects": [{"id": 1, "name": "Sheared", "items": [{"transform": SKEW}]}]}, "object", "Sheared"),
    "disabled-copy": ({"objects": [{"id": 1, "name": "Spare", "items": [{}, {"printable": False, "transform": "1 0 0 0 1 0 0 0 1 150 150 0"}]}]},
                      "object", "Spare"),
    "custom-gcode-per-layer": ({"objects": [{"id": 1, "name": "Swap"}],
                                "extras": {"Metadata/custom_gcode_per_layer.xml": '<custom_gcodes_per_layer><plate><plate_info id="1"/>'
                                           '<code print_z="2.4" type="2" extruder="1" color="#FF0000" extra="" gcode="M600"/></plate></custom_gcodes_per_layer>'}},
                               "plate", "Plate 1"),
    "plate-metadata": ({"objects": [{"id": 1, "name": "Seq"}], "plates": [{"id": 1, "name": "Front", "meta": {"print_sequence": "by object", "spiral_mode": "true"}}]},
                       "plate", "Front"),
}
