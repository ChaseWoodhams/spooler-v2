"""Native check of the per-Plate package with real PETG/ASA presets and Orca 2.4.2, in disposable data directories.

Set SPOOLER_ORCA (or ORCA_SLICER_EXE) to the pinned executable. No server, saved Project or printer is used. Each extracted
Plate file is sliced by a direct Orca command given no Spooler settings (only what the 3MF carries), compared with Spooler's own
Slice of that Plate, then imported through the normal flow into a new Project and sliced again."""
import asyncio
import copy
import io
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import database
import jobs
import projects
import slicer
import splits
from tests.test_project_export_native import inner_moves
from tests.test_slicer import cube_stl

ORCA = os.environ.get("SPOOLER_ORCA") or os.environ.get("ORCA_SLICER_EXE")
PACKAGE = "plate-3mf-package"
FOOTER = ("wall_loops", "layer_height", "nozzle_temperature", "filament_type", "sparse_infill_density", "curr_bed_type")


def footer(text):
    out = {}
    for key in FOOTER:
        found = re.search(rf"^; {key} = (.*)$", text, re.M)
        out[key] = found.group(1) if found else None
    return out


@unittest.skipUnless(ORCA and Path(ORCA).is_file(), "Set SPOOLER_ORCA to bundled Orca 2.4.2")
class RealPackageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spooler-package-")
        self.root = Path(self._tmp.name)
        database.configure(self.root / "test.db")
        jobs.init(self.root)
        projects.configure(self.root)
        splits.configure(self.root)
        slicer.configure(self.root, orca_path=Path(ORCA))
        slicer.init()
        projects.init()

    async def asyncTearDown(self):
        for task in list(jobs._start_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)
        projects.configure()
        splits.configure()
        slicer.configure()
        jobs._data_dir = jobs.DATA_DIR
        database.configure(database.DB_FILE)
        self._tmp.cleanup()

    async def settle(self):
        while await projects.process_next():
            pass

    async def project(self, edit):
        """Two Plates with two 20 mm cubes each on PETG; `edit(data)` makes the Plates differ."""
        bundle = slicer.create_bundle("Elegoo Centauri Carbon 0.4 nozzle", "0.20mm Standard @Elegoo CC 0.4 nozzle", "Elegoo PETG @ECC")
        project = projects.create_project("Disposable package")
        for name in ("a.stl", "b.stl", "c.stl", "d.stl"):
            mesh = cube_stl(20)
            projects.import_model(project["id"], io.BytesIO(mesh), len(mesh), name)
        await self.settle()
        data = copy.deepcopy(projects.get_project(project["id"])["data"])
        data["profile"] = {**data["profile"], "bundle_id": bundle["id"],
                           "process": {"wall_loops": "1", "wall_generator": "classic", "brim_type": "no_brim", "sparse_infill_density": "10%"}}
        data["plates"].append({"id": 2, "name": "Back", "overrides": {}})
        for index, obj in enumerate(data["objects"][:4]):
            obj["plate"], obj["position"] = (1, 2, 1, 2)[index], ([80.0, 90.0], [80.0, 90.0], [150.0, 90.0], [150.0, 90.0])[index]
        edit(data)
        return projects.update_project(project["id"], {"revision": projects.get_project(project["id"])["revision"], "data": data})

    def direct(self, directory, path):
        output = directory / (path.stem + "-out")
        output.mkdir()
        code, log = slicer.runner([ORCA, "--datadir", str(self.root / "direct-datadir"), "--slice", "0", "--arrange", "0",
                                   "--outputdir", str(output), str(path)], 300)
        self.assertEqual(code, 0, log[-3000:])
        return (output / "plate_1.gcode").read_text(encoding="utf-8", errors="replace")

    async def spooler_slice(self, project_id, plate_id):
        projects.request_slice(project_id, plate_id)
        await self.settle()
        state = projects.get_project(project_id)["slices"][str(plate_id)]
        self.assertEqual(state["state"], "sliced", state.get("error"))
        return projects.plate_file(project_id, plate_id, "gcode").read_text(encoding="utf-8", errors="replace")

    async def check_package(self, project, expected):
        plan = projects.export_plan(project["id"])
        self.assertFalse(plan["faithful"], "one Orca file must refuse this Project")
        self.assertTrue(plan["package"]["default"], plan["package"])
        artifact = projects.export_project(project["id"], plan["revision"], plan["fingerprint"], PACKAGE)
        self.assertEqual((artifact["content_type"], artifact["filename"].endswith(".zip")), ("application/zip", True))
        extracted = self.root / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(io.BytesIO(artifact["body"])) as archive:
            archive.extractall(extracted)
        manifest = (extracted / "MANIFEST.txt").read_text(encoding="utf-8")
        files = {item["plate_id"]: extracted / item["filename"] for item in plan["package"]["files"]}
        for item in plan["package"]["files"]:
            self.assertIn(item["fingerprint"], manifest)
        spooler = {plate: await self.spooler_slice(project["id"], plate) for plate in (1, 2)}
        for plate, path in files.items():
            text = self.direct(self.root, path)
            self.assertEqual({k: v for k, v in footer(text).items()}, footer(spooler[plate]), f"plate {plate} direct vs Spooler footer")
            self.assertEqual(inner_moves(text), inner_moves(spooler[plate]), f"plate {plate} inner wall moves")
            for key, value in expected[plate].items():
                self.assertEqual(footer(text)[key], value, (plate, key))
        # Re-import every Plate file through the normal flow, then slice the reopened Project in Spooler.
        for plate, path in files.items():
            data = path.read_bytes()
            review = projects.prepare_import(io.BytesIO(data), len(data), path.name)
            self.assertFalse(review["blocked"], review["report"])
            done = projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})
            await self.settle()
            reopened = projects.get_project(done["id"])
            self.assertEqual(len(reopened["data"]["objects"]), 2)
            self.assertEqual({tuple(o["position"]) for o in reopened["data"]["objects"]},
                             {tuple(o["position"]) for o in project["data"]["objects"] if o["plate"] == plate})
            again = await self.spooler_slice(done["id"], reopened["data"]["plates"][0]["id"])
            self.assertEqual(footer(again), footer(spooler[plate]), f"plate {plate} after re-import")
            self.assertEqual(inner_moves(again), inner_moves(spooler[plate]), f"plate {plate} inner walls after re-import")
        return plan

    async def test_a_petg_and_an_asa_plate_keep_their_own_material_in_orca_and_after_reimport(self):
        def edit(data):
            data["filaments"] = {str(data["objects"][1]["model"]): {"preset": "Elegoo ASA @ECC", "colour": "grey"},
                                 str(data["objects"][3]["model"]): {"preset": "Elegoo ASA @ECC", "colour": "grey"}}
        project = await self.project(edit)
        plan = await self.check_package(project, {1: {"filament_type": "PETG"}, 2: {"filament_type": "ASA"}})
        self.assertEqual([p["material"]["type"] for p in plan["plates"]], ["PETG", "ASA"])

    async def test_plates_differing_in_a_non_object_setting_keep_it_per_plate_in_orca_and_after_reimport(self):
        def edit(data):
            data["plates"][1]["overrides"] = {"wall_loops": "3", "sparse_infill_density": "25%"}
            data["objects"][0]["overrides"] = {"wall_loops": "4"}
        project = await self.project(edit)
        plan = await self.check_package(project, {1: {"wall_loops": "1", "sparse_infill_density": "10%"},
                                                  2: {"wall_loops": "3", "sparse_infill_density": "25%"}})
        self.assertIn("wall_loops", [d["key"] for d in plan["differences"]])


if __name__ == "__main__":
    unittest.main()
