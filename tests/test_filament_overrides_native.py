"""Opt-in pinned Orca checks. Set ORCA_SLICER_EXE to the bundled executable.

Only generated cubes, isolated Project data and Orca datadirs are used.
"""
import io
import json
import os
import re
from pathlib import Path
import unittest

import projects
import slicer
from tests import test_projects
from tests.test_slicer import cube_stl


ORCA = Path(os.environ.get("ORCA_SLICER_EXE", "missing-orca-executable"))


@unittest.skipUnless(ORCA.is_file(), "Set ORCA_SLICER_EXE for the bundled native check")
class NativeFilamentOverrideTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_projects.ProjectTests.setUp
    asyncTearDown = test_projects.ProjectTests.asyncTearDown
    tearDown = test_projects.ProjectTests.tearDown
    run_worker = test_projects.ProjectTests.run_worker
    edit = test_projects.ProjectTests.edit

    async def test_petg_and_asa_nullable_overrides_reach_real_orca(self):
        slicer.configure(self.dir, orca_path=ORCA)
        for material in ("PETG", "ASA"):
            with self.subTest(material=material):
                bundle = slicer.create_bundle("Elegoo Centauri Carbon 0.4 nozzle",
                    "0.20mm Standard @Elegoo CC 0.4 nozzle", f"Elegoo {material} @ECC")
                project = projects.create_project(f"Disposable {material} nullable check")
                cube = cube_stl(10)
                projects.import_model(project["id"], io.BytesIO(cube), len(cube), "cube.stl")
                await self.run_worker()
                project = projects.get_project(project["id"])
                values = {"filament_retraction_length": [None], "filament_z_hop": ["0.4"],
                          "filament_wipe": ["nil"], "filament_ironing_flow": ["nil"]}
                project = self.edit(project, profile={**project["data"]["profile"],
                    "bundle_id": bundle["id"], "filament": values})
                review = projects.effective_settings(project["id"], 1)
                self.assertEqual(review["filament"]["filament_type"], [material])
                projects.request_slice(project["id"], 1)
                await self.run_worker()
                result = projects.get_project(project["id"])["slices"]["1"]
                self.assertEqual(result["state"], "sliced", result)
                work = projects.plate_dir(project["id"], 1)
                supplied = json.loads((work / "profile" / "filament.json").read_text())
                self.assertEqual(supplied["filament_retraction_length"], ["nil"])
                gcode = projects.plate_file(project["id"], 1, "gcode").read_text(errors="replace")
                footer = dict(re.findall(r"^; (\w+) = (.*)$", gcode, re.M))
                # The pinned CLI omits nil overrides from its footer, while the
                # ordinary keys hold the resolved printer/process values.
                for key in ("filament_retraction_length", "filament_wipe", "filament_ironing_flow"):
                    self.assertEqual(supplied[key], ["nil"])
                    self.assertNotIn(key, footer)
                for key, value in (("filament_type", material), ("retraction_length", "0.8"),
                                   ("filament_z_hop", "0.4"), ("z_hop", "0.4"),
                                   ("wipe", "1"), ("ironing_flow", "10%")):
                    self.assertEqual(footer.get(key), value, key)
