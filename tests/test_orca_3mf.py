import io
import json
import math
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

import orca_3mf

CUBE = np.array([(x, y, z) for x in (-10, 10) for y in (-5, 5) for z in (-2, 2)], float)
TRIS = np.array([(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
                 (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)])


def rotation_z(degrees):
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def obj(name, plate, matrix=None, shift=(100, 100, 2), settings=None):
    return {"name": name, "verts": CUBE, "tris": TRIS, "matrix": np.eye(3) if matrix is None else matrix,
            "shift": np.array(shift, float), "plate": plate, "settings": settings or {}}


def written(objects, plates, config=None):
    return io.BytesIO(orca_3mf.build(objects, plates, config or {"layer_height": "0.2"}))


class ThreeMFTests(unittest.TestCase):
    def test_a_written_project_reads_back_with_plates_placement_and_settings(self):
        turned = (rotation_z(30) @ np.diag([1.5, 1.5, 1.5])).T
        source = written([obj("Bracket", 1, settings={"wall_loops": "4"}), obj("Lid", 2, matrix=turned, shift=(60, 70, 3))],
                         [{"id": 1, "name": "Front"}, {"id": 2, "name": ""}], {"layer_height": "0.16", "printer_settings_id": "X"})

        self.assertTrue(orca_3mf.is_project(source))
        parsed = orca_3mf.parse(source)
        self.assertEqual([(p["id"], p["name"]) for p in parsed["plates"]], [(1, "Front"), (2, "")])
        self.assertEqual(parsed["project"]["layer_height"], "0.16")
        bracket, lid = parsed["objects"]
        self.assertEqual((bracket["name"], bracket["settings"], bracket["verts"].shape, bracket["tris"].shape),
                         ("Bracket", {"wall_loops": "4"}, (8, 3), (12, 3)))
        self.assertEqual([i["plate"] for i in bracket["instances"] + lid["instances"]], [1, 2])

        placed = orca_3mf.pose(lid["verts"], lid["instances"][0]["matrix"], lid["instances"][0]["shift"])
        self.assertEqual(placed["notes"], [])
        self.assertAlmostEqual(placed["rotation"][2], 30, places=3)
        self.assertEqual(placed["scale"], [1.5, 1.5, 1.5])
        world = lid["verts"] @ lid["instances"][0]["matrix"] + lid["instances"][0]["shift"]
        self.assertEqual(placed["position"], [round(float((world[:, 0].min() + world[:, 0].max()) / 2), 3),
                                              round(float((world[:, 1].min() + world[:, 1].max()) / 2), 3)])

    def test_pose_recovers_any_turn_about_the_three_axes(self):
        from projects import rotation_matrix
        for angles in ([0, 0, 0], [90, 0, 0], [0, 45, 30], [20, 300, 200], [0, 90, 10]):
            matrix = (rotation_matrix(angles) @ np.diag([2.0, 1.0, 0.5])).T
            placed = orca_3mf.pose(CUBE, matrix, np.zeros(3))
            again = (rotation_matrix(placed["rotation"]) @ np.diag(placed["scale"])).T
            self.assertTrue(np.allclose(CUBE @ again, CUBE @ matrix, atol=1e-2), angles)

    def test_mirrored_objects_are_flagged(self):
        self.assertEqual(orca_3mf.pose(CUBE, np.diag([-1.0, 1.0, 1.0]), np.zeros(3))["notes"], ["mirrored"])

    def test_parts_are_merged_and_modifiers_left_out(self):
        model = ('<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
                 'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"><resources>'
                 '<object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>'
                 '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
                 '<object id="3" type="model"><mesh><vertices><vertex x="0" y="0" z="5"/><vertex x="1" y="0" z="5"/><vertex x="0" y="1" z="5"/></vertices>'
                 '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
                 '<object id="2"><components><component objectid="1"/><component objectid="3" transform="1 0 0 0 1 0 0 0 1 10 0 0"/></components></object>'
                 '</resources><build><item objectid="2"/></build></model>')
        settings = ('<config><object id="2"><metadata key="name" value="Two part.stl"/><metadata key="wall_loops" value="5"/>'
                    '<part id="1" subtype="normal_part"/><part id="3" subtype="modifier_part"/></object>'
                    '<plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="2"/>'
                    '<metadata key="instance_id" value="0"/></model_instance></plate></config>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(orca_3mf.MODEL_PATH, model)
            archive.writestr(orca_3mf.SETTINGS_PATH, settings)

        parsed = orca_3mf.parse(buffer)
        (part,) = parsed["objects"]
        self.assertEqual((part["name"], part["settings"], len(part["verts"]), len(part["tris"])), ("Two part", {"wall_loops": "5"}, 3, 1))
        self.assertTrue(any("modifier volumes" in w for w in parsed["warnings"]))

    def test_plain_3mf_files_are_not_projects_but_still_parse(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(orca_3mf.MODEL_PATH, '<model unit="inch"><resources><object id="1"><mesh><vertices>'
                             '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>'
                             '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
                             '<build><item objectid="1"/><item objectid="1" transform="1 0 0 0 1 0 0 0 1 50 0 0"/></build></model>')
        self.assertFalse(orca_3mf.is_project(buffer))
        parsed = orca_3mf.parse(buffer)
        (only,) = parsed["objects"]
        self.assertEqual(len(only["instances"]), 2)   # the second item is another copy
        self.assertAlmostEqual(float(only["verts"][1][0]), 25.4)  # inches become millimetres

    def test_settings_changes_lists_only_real_differences(self):
        base = {"process": {"layer_height": "0.2", "wall_loops": "2", "sparse_infill_density": "15%"},
                "filament": {"nozzle_temperature": ["210"], "filament_density": ["1.24"]}}
        project = {"layer_height": "0.20", "wall_loops": "4", "sparse_infill_density": "15%", "nozzle_temperature": ["215"],
                   "filament_density": ["1.24"], "not_in_preset": "1"}
        allowed = {"process": {"layer_height", "wall_loops", "sparse_infill_density", "not_in_preset"},
                   "filament": {"nozzle_temperature", "filament_density"}}
        self.assertEqual(orca_3mf.settings_changes(project, base, allowed),
                         {"process": {"wall_loops": "4"}, "filament": {"nozzle_temperature": ["215"]}})

    def test_broken_files_are_refused(self):
        with self.assertRaises(orca_3mf.ThreeMFError):
            orca_3mf.parse(io.BytesIO(b"not a zip"))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "x")
        with self.assertRaises(orca_3mf.ThreeMFError):
            orca_3mf.parse(buffer)


if __name__ == "__main__":
    unittest.main()
