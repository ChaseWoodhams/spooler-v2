"""Native check of the faithful single-file export with real PETG presets and Orca 2.4.2, in disposable data directories.

Set SPOOLER_ORCA (or ORCA_SLICER_EXE) to the pinned executable. No server, saved Project or printer is used. The exported
file is sliced by a direct Orca command that is given no Spooler settings: only what the 3MF itself carries."""
import asyncio
import copy
import io
import json
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
from tests.test_slicer import cube_stl

ORCA = os.environ.get("SPOOLER_ORCA") or os.environ.get("ORCA_SLICER_EXE")


def inner_moves(text):
    """Actual Inner-wall deposition commands, not the footer's global settings."""
    role, count = "", 0
    for line in text.splitlines():
        if line.startswith(";TYPE:"):
            role = line[6:]
        elif role == "Inner wall" and re.match(r"G1 .*\b[XY]", line) and re.search(r"\bE[0-9.]", line):
            count += 1
    return count


@unittest.skipUnless(ORCA and Path(ORCA).is_file(), "Set SPOOLER_ORCA to bundled Orca 2.4.2")
class RealExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_homogeneous_petg_project_exports_what_spooler_slices(self):
        with tempfile.TemporaryDirectory(prefix="spooler-export-") as directory:
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
                project = projects.create_project("Disposable PETG export")
                for name in ("one.stl", "two.stl", "three.stl"):
                    mesh = cube_stl(20)
                    projects.import_model(project["id"], io.BytesIO(mesh), len(mesh), name)
                while await projects.process_next():
                    pass
                data = copy.deepcopy(projects.get_project(project["id"])["data"])
                data["profile"] = {**data["profile"], "bundle_id": bundle["id"],
                                   "process": {"wall_loops": "1", "wall_generator": "classic", "brim_type": "no_brim"}}
                data["plates"].append({"id": 2, "name": "Second", "overrides": {"wall_loops": "1"}})   # a no-op Plate override
                data["plates"].append({"id": 4, "name": "Third", "overrides": {}})    # a gap in the numbers: Orca counts 1..n
                first, second, third = data["objects"][:3]
                second["plate"], second["position"] = 2, [100.0, 100.0]
                third["plate"], third["position"] = 4, [150.0, 90.0]
                first["overrides"] = {"wall_loops": "4"}
                project = projects.update_project(project["id"], {"revision": projects.get_project(project["id"])["revision"], "data": data})

                plan = projects.export_plan(project["id"])
                self.assertTrue(plan["faithful"], plan)
                exported = projects.export_project(project["id"], plan["revision"], plan["fingerprint"])
                (root / "export.3mf").write_bytes(exported["body"])
                with zipfile.ZipFile(root / "export.3mf") as archive:
                    config = json.loads(archive.read("Metadata/project_settings.config"))
                self.assertEqual((config["wall_loops"], config["filament_type"], config["curr_bed_type"]), ("1", ["PETG"], "Textured PEI Plate"))

                output = root / "direct"
                output.mkdir()
                code, log = slicer.runner([ORCA, "--datadir", str(root / "direct-datadir"), "--slice", "0", "--arrange", "0",
                                           "--outputdir", str(output), str(root / "export.3mf")], 300)
                self.assertEqual(code, 0, log[-3000:])
                direct = {n: (output / f"plate_{n}.gcode").read_text(encoding="utf-8", errors="replace") for n in (1, 2, 3)}

                for plate in (1, 2, 4):
                    projects.request_slice(project["id"], plate)
                while await projects.process_next():
                    pass
                sliced = projects.get_project(project["id"])
                for plate, number in ((1, 1), (2, 2), (4, 3)):             # Spooler's Plate id, then Orca's number for it
                    self.assertEqual(sliced["slices"][str(plate)]["state"], "sliced", sliced["slices"][str(plate)].get("error"))
                    spooler = projects.plate_file(project["id"], plate, "gcode").read_text(encoding="utf-8", errors="replace")
                    self.assertIn("filament_type = PETG", direct[number])
                    self.assertEqual(inner_moves(direct[number]), inner_moves(spooler), f"plate {plate}")
                    for line in ("wall_loops", "layer_height", "nozzle_temperature"):    # the footer's own settings agree too
                        pattern = re.compile(rf"^; {line} = (.*)$", re.M)
                        self.assertEqual(pattern.search(direct[number]).group(1), pattern.search(spooler).group(1), line)
                self.assertGreater(inner_moves(direct[1]), inner_moves(direct[2]) + 100)    # the Object override reached Orca
            finally:
                for task in list(jobs._start_tasks.values()):
                    task.cancel()
                await asyncio.sleep(0)
                projects.configure()
                splits.configure()
                slicer.configure()
                jobs._data_dir = jobs.DATA_DIR
                database.configure(database.DB_FILE)


if __name__ == "__main__":
    unittest.main()
