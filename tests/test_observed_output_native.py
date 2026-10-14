"""Opt-in pinned Orca check of the observed-output comparison. Set ORCA_SLICER_EXE to the bundled 2.4.2 executable.

Only generated cubes, an isolated Project database and Orca datadirs are used; no printer command is sent.
"""
import io
import json
import os
import re
from pathlib import Path
import unittest

import observed_output
import projects
import slicer
from tests import test_projects
from tests.test_slicer import cube_stl

ORCA = Path(os.environ.get("ORCA_SLICER_EXE", "missing-orca-executable"))


@unittest.skipUnless(ORCA.is_file(), "Set ORCA_SLICER_EXE for the bundled native check")
class NativeObservedOutputTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_projects.ProjectTests.setUp
    asyncTearDown = test_projects.ProjectTests.asyncTearDown
    tearDown = test_projects.ProjectTests.tearDown
    run_worker = test_projects.ProjectTests.run_worker
    edit = test_projects.ProjectTests.edit

    async def slice(self, material, plate=None, profile=None):
        slicer.configure(self.dir, orca_path=ORCA)
        bundle = slicer.create_bundle("Elegoo Centauri Carbon 0.4 nozzle", "0.20mm Standard @Elegoo CC 0.4 nozzle", f"Elegoo {material} @ECC")
        project = projects.create_project(f"Disposable {material} observed output")
        cube = cube_stl(10)
        projects.import_model(project["id"], io.BytesIO(cube), len(cube), "cube.stl")
        await self.run_worker()
        project = projects.get_project(project["id"])
        project = self.edit(project, profile={**project["data"]["profile"], "bundle_id": bundle["id"], **(profile or {})},
                            plates=[{**project["data"]["plates"][0], "overrides": plate or {}}])
        projects.request_slice(project["id"], 1)
        await self.run_worker()
        project = projects.get_project(project["id"])
        self.assertEqual(project["slices"]["1"]["state"], "sliced", project["slices"]["1"])
        return project

    async def test_real_petg_and_asa_output_matches_the_inputs_it_was_prepared_with(self):
        for material in ("PETG", "ASA"):
            with self.subTest(material=material):
                project = await self.slice(material, plate={"wall_loops": "4", "sparse_infill_density": "25%"},
                                           profile={"filament": {"nozzle_temperature": ["245"], "filament_z_hop": ["0.4"],
                                                                 "filament_retraction_length": ["nil"]}})
                review = projects.slice_inputs(project["id"], 1)["observed"]
                self.assertEqual(review["differences"], [], review["differences"])
                self.assertIn(review["state"], ("consistent", "normalized"))
                self.assertTrue(project["slices"]["1"]["observed"]["trusted"])
                self.assertGreater(review["compared"], 200)
                self.assertEqual(review["source"], "plate.gcode")
                # what is compared is Orca's account of the inputs, not an assumption: the footer carries the chosen values
                gcode = projects.plate_file(project["id"], 1, "gcode").read_text(errors="replace")
                footer = dict(re.findall(r"^; (\w+) = (.*)$", gcode, re.M))
                self.assertEqual((footer["filament_type"], footer["wall_loops"], footer["nozzle_temperature"]), (material, "4", "245"))
                self.assertEqual(footer["z_hop"], "0.4")
                # the stock Elegoo preset names an infill pattern Orca 2.4.2 does not know: recorded, not hidden
                names = {n["key"] for n in review["normalized"]}
                print(f"\n[{material}] compared {review['compared']}, normalized {sorted(names)}, "
                projects.queue_plate(project["id"], 1, {"printer_id": "p1"})

    async def test_a_real_footer_that_was_altered_is_reported_as_a_mismatch_by_setting_and_scope(self):
        project = await self.slice("PETG", plate={"wall_loops": "4"})
        work = projects.plate_dir(project["id"], 1)
        saved = json.loads((work / "inputs.json").read_text(encoding="utf-8"))
        altered = work / "altered.gcode"
        text = (work / "plate.gcode").read_text(errors="replace")
        text = re.sub(r"^; nozzle_temperature = .*$", "; nozzle_temperature = 200", text, flags=re.M)
        text = re.sub(r"^; wall_loops = .*$", "; wall_loops = 2", text, flags=re.M)
        altered.write_text(text)
        report = observed_output.check(saved, altered, slicer.settings_schema())
        self.assertEqual(report["state"], "mismatch")
        found = {d["key"]: d for d in report["differences"]}
        self.assertEqual(set(found), {"nozzle_temperature", "wall_loops"})
        self.assertEqual((found["wall_loops"]["scope"], found["wall_loops"]["expected"], found["wall_loops"]["observed"]), ("plate", "4", "2"))
        self.assertEqual(found["nozzle_temperature"]["scope"], "preset")

    async def test_real_output_without_its_footer_is_unavailable_not_verified(self):
        project = await self.slice("PETG")
        work = projects.plate_dir(project["id"], 1)
        stripped = work / "stripped.gcode"
        stripped.write_text((work / "plate.gcode").read_text(errors="replace").split("; CONFIG_BLOCK_START")[0])
        report = observed_output.check(json.loads((work / "inputs.json").read_text(encoding="utf-8")), stripped, slicer.settings_schema())
        self.assertEqual((report["state"], report["trusted"], report["compared"]), ("unavailable", False, 0))


if __name__ == "__main__":
    unittest.main()
