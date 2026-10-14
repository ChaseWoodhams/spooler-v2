"""Native check of the explicit lossy whole-Project 3MF with real PETG and ASA presets and Orca 2.4.2, in disposable data.

Set SPOOLER_ORCA (or ORCA_SLICER_EXE) to the pinned executable. A PETG Plate and an ASA Plate with a different wall count
cannot share one Orca file; the acknowledged lossy file carries the first Plate's PETG configuration for both, and a direct
Orca command given no Spooler settings shows exactly that, while Spooler's own Slice of the ASA Plate keeps its material and walls."""
import asyncio
import copy
import io
import json
import os
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


@unittest.skipUnless(ORCA and Path(ORCA).is_file(), "Set SPOOLER_ORCA to bundled Orca 2.4.2")
class RealLossyExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_petg_and_asa_project_exports_one_petg_file_only_after_acknowledgement(self):
        with tempfile.TemporaryDirectory(prefix="spooler-lossy-") as directory:
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
                project = projects.create_project("Disposable PETG and ASA")
                for name in ("petg.stl", "asa.stl"):
                    mesh = cube_stl(20)
                    projects.import_model(project["id"], io.BytesIO(mesh), len(mesh), name)
                while await projects.process_next():
                    pass
                record = projects.get_project(project["id"])
                data = copy.deepcopy(record["data"])
                data["profile"] = {**data["profile"], "bundle_id": bundle["id"],
                                   "process": {"wall_loops": "1", "wall_generator": "classic", "brim_type": "no_brim"}}
                data["plates"].append({"id": 2, "name": "ASA plate", "overrides": {"wall_loops": "3"}})
                data["objects"][1]["plate"], data["objects"][1]["position"] = 2, [100.0, 100.0]
                data["filaments"] = {str(data["objects"][1]["model"]): {"preset": "Elegoo ASA @ECC", "colour": ""}}
                project = projects.update_project(project["id"], {"revision": record["revision"], "data": data})

                plan = projects.export_plan(project["id"])
                self.assertFalse(plan["faithful"])
                with self.assertRaises(jobs.JobError):
                    projects.export_orca_project(project["id"])                                   # the direct route stays faithful-only
                lossy = plan["lossy"]
                self.assertTrue(lossy["available"], lossy)
                asa = next(p for p in lossy["plates"] if p["name"] == "ASA plate")
                self.assertEqual((asa["material"]["type"], asa["written_as"]["type"], asa["material_changed"]), ("ASA", "PETG", True))
                self.assertIn(("wall_loops", "plate-settings"), [(d["key"], d["category"]) for d in lossy["differences"]])

                exported = projects.export_lossy(project["id"], {
                    "representation": lossy["representation"], "revision": plan["revision"], "fingerprint": plan["fingerprint"],
                    "report_version": lossy["report_version"], "report_fingerprint": lossy["report_fingerprint"], "acknowledged": True})
                self.assertEqual(exported["filename"], "Disposable PETG and ASA (lossy).3mf")
                (root / "lossy.3mf").write_bytes(exported["body"])
                with zipfile.ZipFile(root / "lossy.3mf") as archive:
                    config = json.loads(archive.read("Metadata/project_settings.config"))
                self.assertEqual((config["filament_type"], config["wall_loops"]), (["PETG"], "1"))

                output = root / "direct"
                output.mkdir()
                code, log = slicer.runner([ORCA, "--datadir", str(root / "direct-datadir"), "--slice", "0", "--arrange", "0",
                                           "--outputdir", str(output), str(root / "lossy.3mf")], 300)
                self.assertEqual(code, 0, log[-3000:])
                direct = {n: (output / f"plate_{n}.gcode").read_text(encoding="utf-8", errors="replace") for n in (1, 2)}
                self.assertIn("filament_type = PETG", direct[2], "the ASA Plate comes out as PETG in the lossy file")
                self.assertEqual(inner_moves(direct[1]), inner_moves(direct[2]))                 # and without its own wall count

                projects.request_slice(project["id"], 2)
                while await projects.process_next():
                    pass
                spooler = projects.plate_file(project["id"], 2, "gcode").read_text(encoding="utf-8", errors="replace")
                self.assertIn("filament_type = ASA", spooler)
                self.assertGreater(inner_moves(spooler), inner_moves(direct[2]) + 50, "Spooler's own Slice keeps the ASA Plate's walls")
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
