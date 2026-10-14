"""Native Object effect check at the disposable public Project boundary.

Set SPOOLER_ORCA to the pinned executable. No server, user data or printer is used.
"""
import asyncio
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

import database
import jobs
import projects
import slicer
import splits
from tests.test_slicer import cube_stl

ORCA = os.environ.get("SPOOLER_ORCA")


@unittest.skipUnless(ORCA and Path(ORCA).is_file(), "Set SPOOLER_ORCA to bundled Orca 2.4.2")
class RealObjectScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_petg_object_walls_change_toolpaths_and_match_independent_orca_reference(self):
        with tempfile.TemporaryDirectory(prefix="spooler-object-scope-") as directory:
            root = Path(directory)
            database.configure(root / "test.db")
            jobs.init(root)
            projects.configure(root)
            splits.configure(root)
            slicer.configure(root, orca_path=Path(ORCA))
            slicer.init()
            projects.init()
            try:
                bundle = slicer.create_bundle("Elegoo Centauri Carbon 0.4 nozzle", "0.20mm Standard @Elegoo CC 0.4 nozzle", "Elegoo PETG @ECC")
                project = projects.create_project("Disposable PETG Object effect")
                mesh = cube_stl(20)
                projects.import_model(project["id"], io.BytesIO(mesh), len(mesh), "cube.stl")
                while await projects.process_next():
                    pass
                project = projects.get_project(project["id"])
                data = project["data"]
                data["profile"] = {**data["profile"], "bundle_id": bundle["id"], "process": {"wall_loops": "1", "wall_generator": "classic", "brim_type": "no_brim"}}
                project = projects.update_project(project["id"], {"revision": project["revision"], "data": data})
                gcode = []
                for walls in (None, "4"):
                    if walls:
                        data = project["data"]
                        data["objects"][0]["overrides"] = {"wall_loops": walls}
                        project = projects.update_project(project["id"], {"revision": project["revision"], "data": data})
                    review = projects.effective_settings(project["id"], 1)
                    self.assertEqual(review["filament"]["filament_type"], ["PETG"])
                    projects.request_slice(project["id"], 1)
                    while await projects.process_next():
                        pass
                    project = projects.get_project(project["id"])
                    self.assertEqual(project["slices"]["1"]["state"], "sliced", project["slices"]["1"].get("error"))
                    gcode.append(projects.plate_file(project["id"], 1, "gcode").read_text(encoding="utf-8", errors="replace"))
                # Count actual Inner-wall deposition commands, not footer/global settings.
                def inner_moves(text):
                    role, count = "", 0
                    for line in text.splitlines():
                        if line.startswith(";TYPE:"):
                            role = line[6:]
                        elif role == "Inner wall" and re.match(r"G1 .*\b[XY]", line) and re.search(r"\bE[0-9.]", line):
                            count += 1
                    return count
                self.assertGreater(inner_moves(gcode[1]), inner_moves(gcode[0]) + 100)
                # Independent reference uses a common 4-wall process and the same cube geometry,
                # with Object metadata removed; it never calls the effective-settings resolver.
                work = projects.plate_dir(project["id"], 1)
                import zipfile
                from xml.etree import ElementTree as ET
                removed_overrides = 0
                with zipfile.ZipFile(work / "plate.3mf") as source, zipfile.ZipFile(root / "reference.3mf", "w") as target:
                    for name in source.namelist():
                        content = source.read(name)
                        if name == "Metadata/model_settings.config":
                            tree = ET.fromstring(content)
                            for obj in tree.findall("object"):
                                for metadata in list(obj.findall("metadata")):
                                    if metadata.get("key") == "wall_loops":
                                        obj.remove(metadata)
                                        removed_overrides += 1
                            content = ET.tostring(tree)
                        target.writestr(name, content)
                self.assertEqual(removed_overrides, 1)
                process = json.loads((work / "profile/process.json").read_text(encoding="utf-8"))
                process["wall_loops"] = "4"
                (root / "reference-process.json").write_text(json.dumps(process), encoding="utf-8")
                output = root / "reference-output"
                output.mkdir()
                args = [ORCA, "--datadir", str(root / "reference-datadir"), "--slice", "0", "--arrange", "0", "--load-settings",
                        f"{work / 'profile/machine.json'};{root / 'reference-process.json'}", "--load-filaments", str(work / "profile/filament.json"),
                        "--outputdir", str(output), str(root / "reference.3mf")]
                code, log = slicer.runner(args, 180)
                self.assertEqual(code, 0, log[-4000:])
                reference = next(output.glob("plate_*.gcode")).read_text(encoding="utf-8", errors="replace")
                self.assertEqual(inner_moves(gcode[1]), inner_moves(reference))
            finally:
                for task in list(jobs._start_tasks.values()):
                    task.cancel()
                await asyncio.sleep(0)
                projects.configure()
                splits.configure()
                slicer.configure()
                jobs._data_dir = jobs.DATA_DIR
                database.configure(database.DB_FILE)
