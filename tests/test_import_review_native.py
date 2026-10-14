"""Native check of the import review with real Elegoo presets and Orca 2.4.2, in disposable data directories.

Set SPOOLER_ORCA to the pinned executable. No server, saved Project or printer is used.
"""
import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path

import database
import jobs
import projects
import slicer
import splits
from tests.orca_archives import archive

ORCA = os.environ.get("SPOOLER_ORCA")
MACHINE = "Elegoo Centauri Carbon 0.4 nozzle"
PROCESS = "0.20mm Standard @Elegoo CC 0.4 nozzle"


def project_settings(filament, material):
    return {"printer_settings_id": MACHINE, "print_settings_id": PROCESS, "filament_settings_id": [filament],
            "filament_type": [material], "curr_bed_type": "Textured PEI Plate"}


@unittest.skipUnless(ORCA and Path(ORCA).is_file(), "Set SPOOLER_ORCA to bundled Orca 2.4.2")
class RealImportReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_matched_petg_imports_after_review_and_slices_without_asking_again_while_asa_substitution_blocks(self):
        with tempfile.TemporaryDirectory(prefix="spooler-import-review-") as directory:
            root = Path(directory)
            database.configure(root / "test.db")
            jobs.init(root)
            projects.configure(root)
            splits.configure(root)
            slicer.configure(root, orca_path=Path(ORCA))
            slicer.init()
            projects.init()
            try:
                objects = [{"id": 1, "name": "Painted cube", "paint": {"paint_supports": "4"},
                            "parts": [("normal_part", {}), ("modifier_part", {"wall_loops": "9"})]},
                           {"id": 2, "name": "Plain cube", "items": [{"transform": "1 0 0 0 1 0 0 0 1 170 170 0"}]}]
                data = archive(objects=objects, project=project_settings("Elegoo PETG @ECC", "PETG"))
                review = projects.prepare_import(io.BytesIO(data), len(data), "petg.3mf")
                self.assertFalse(review["blocked"], review["report"]["blockers"])
                self.assertEqual(sorted(loss["code"] for loss in review["report"]["losses"]), ["modifier-volume", "paint-supports"])
                created = projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})
                while await projects.process_next():
                    pass
                settings = slicer.bundle_settings(created["data"]["profile"]["bundle_id"])
                self.assertEqual(settings["filament"]["filament_type"], ["PETG"])

                projects.request_slice(created["id"], 1)         # no acknowledgement is asked for again
                while await projects.process_next():
                    pass
                sliced = projects.get_project(created["id"])
                self.assertEqual(sliced["slices"]["1"]["state"], "sliced", sliced["slices"]["1"].get("error"))
                self.assertEqual(len(projects.import_records(created["id"])[0]["losses"]), 2)
                gcode = projects.plate_file(created["id"], 1, "gcode").read_text(encoding="utf-8", errors="replace")
                self.assertIn("filament_type = PETG", gcode)

                asa = archive(objects=objects, project=project_settings("Brand X ASA", "ASA"))
                blocked = projects.prepare_import(io.BytesIO(asa), len(asa), "asa.3mf")
                (blocker,) = blocked["report"]["blockers"]
                self.assertEqual((blocker["code"], blocker["requested"]["material"], blocker["proposed"]["material"]), ("unmatched-material", "ASA", "PLA"))
                with self.assertRaises(jobs.JobError):
                    projects.confirm_import(blocked["token"], {**blocked["binding"], "acknowledged": True})
                projects.cancel_import(blocked["token"])
                self.assertEqual(len(projects.list_projects(include_quick=True)), 1)
            finally:
                for task in list(jobs._start_tasks.values()):
                    task.cancel()
                await asyncio.sleep(0)
                database.configure(database.DB_FILE)
                jobs._data_dir = jobs.DATA_DIR
                slicer.configure()
                splits.configure()
                projects.configure()


if __name__ == "__main__":
    unittest.main()
