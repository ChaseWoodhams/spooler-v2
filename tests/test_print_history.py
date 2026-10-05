import asyncio
import csv
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import database
import jobs
import print_history
import state
from jobs import JobError
from tests.test_jobs import GCODE, FakePrinter


def spool(price=None, initial_weight=1000, filament_price=None, filament_weight=1000,
          density=1.24, material="PLA", name="Galaxy Black", vendor="Elegoo"):
    return {"price": price, "initial_weight": initial_weight, "filament": {
        "price": filament_price, "weight": filament_weight, "density": density, "material": material,
        "name": name, "vendor": {"name": vendor}, "color_hex": "111111"}}


class PrintHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_printers = state.printers
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        print_history.init()
        self.printer = FakePrinter()
        self.other = FakePrinter("p2", "Printer 2")
        state.printers = {"p1": self.printer, "p2": self.other}
        patcher = mock.patch.object(jobs, "uploader", lambda ip, path, name, progress: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        for task in list(jobs._start_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)

    def tearDown(self):
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        self._tmp.cleanup()

    def push(self, printer, code, filename="", layer=0, total=0, ticks=0):
        """Deliver a status update the way printers/base.py does: jobs first, then history."""
        printer.status = {"CurrentStatus": [0], "PrintInfo": {
            "Status": code, "Filename": filename, "CurrentLayer": layer, "TotalLayer": total, "CurrentTicks": ticks}}
        jobs.on_printer_status(printer)
        return print_history.on_printer_status(printer)

    def records(self, **filters):
        return print_history.search(**filters)["prints"]

    # following prints

    def test_manual_print_is_recorded_from_start_to_completion(self):
        self.assertIsNone(self.push(self.printer, 16, "benchy.gcode", ticks=120))
        running = self.records()[0]
        self.assertEqual((running["outcome"], running["file_name"], running["job_id"]), (None, "benchy.gcode", None))
        self.assertAlmostEqual(running["started_at"], time.time() - 120, delta=5)

        self.push(self.printer, 13, "benchy.gcode", 50, 100)
        closed = self.push(self.printer, 9, "benchy.gcode", 100, 100)

        self.assertEqual((closed["outcome"], closed["layer"], closed["total_layers"]), ("completed", 100, 100))
        self.assertGreaterEqual(closed["duration_s"], 120)
        self.assertEqual(len(self.records()), 1)

    def test_how_a_print_ends_sets_the_outcome(self):
        self.push(self.printer, 13, "a.gcode", 10, 100)
        self.assertEqual(self.push(self.printer, 8, "a.gcode", 10, 100)["outcome"], "cancelled")

        self.push(self.printer, 13, "b.gcode", 100, 100)
        self.assertEqual(self.push(self.printer, 0)["outcome"], "completed")  # idle after the last layer

        self.push(self.printer, 13, "c.gcode", 30, 100)
        unknown = self.push(self.printer, 0)
        self.assertEqual((unknown["outcome"], unknown["layer"]), ("unknown", 30))

    def test_switching_files_closes_the_previous_record(self):
        self.push(self.printer, 13, "a.gcode")
        closed = self.push(self.printer, 13, "b.gcode")

        self.assertEqual((closed["file_name"], closed["outcome"]), ("a.gcode", "unknown"))
        self.assertEqual(self.records(outcome="printing")[0]["file_name"], "b.gcode")

    def test_running_print_is_picked_up_after_a_restart(self):
        self.push(self.printer, 13, "long.gcode", 10, 100)

        print_history.init()  # Spooler restarts
        closed = self.push(self.printer, 9, "long.gcode", 100, 100)

        self.assertEqual(closed["outcome"], "completed")
        self.assertEqual(len(self.records()), 1)

    async def test_queue_job_is_linked_with_its_estimates(self):
        job = jobs.create_upload_job(io.BytesIO(GCODE), len(GCODE), "cube.gcode", "p1")
        await jobs.start_job(job["id"])
        for _ in range(200):
            if self.printer.started:
                break
            await asyncio.sleep(0.01)

        self.push(self.printer, 13, "cube.gcode", 1, 50)
        record = self.records()[0]
        self.assertEqual((record["job_id"], record["estimate_s"], record["estimate_g"]), (job["id"], 3723, 12.5))

        closed = self.push(self.printer, 9, "cube.gcode", 50, 50)
        self.assertEqual(closed["outcome"], "completed")
        self.assertEqual(jobs.get_job(job["id"])["state"], "completed")
        detail = print_history.get_print(record["id"])
        self.assertIn("printing", [e["event"] for e in detail["job_events"]])

    # usage, spools and cost

    def test_usage_records_spools_material_and_cost(self):
        self.push(self.printer, 13, "two-colour.gcode")
        spools = {7: spool(price=20), 9: spool(filament_price=30, density=1.27, material="PETG", name="Blue")}

        record = print_history.record_usage("p1", 100000.0, 1.24, {7: 60000.0, 9: 40000.0}, lookup=spools.get)

        self.assertAlmostEqual(record["filament_g"], 301.2, places=1)
        self.assertAlmostEqual(record["cost"], 7.25, places=2)
        self.assertEqual((record["spool_id"], record["material"], record["vendor"]), (7, "PLA", "Elegoo"))
        self.assertEqual([s["spool_id"] for s in record["spools"]], [7, 9])
        self.assertEqual(record["spools"][1]["price_per_kg"], 30.0)

    def test_usage_without_spoolman_falls_back_to_density(self):
        self.push(self.printer, 13, "offline.gcode")

        def unreachable(_):
            raise OSError("connection refused")

        with mock.patch.object(print_history, "_spool_id_at_location", side_effect=OSError("refused")):
            record = print_history.record_usage("p1", 1000.0, 1.24, {}, lookup=unreachable)

        self.assertAlmostEqual(record["filament_g"], 3.0, places=1)
        self.assertIsNone(record["cost"])
        self.assertEqual(record["spools"], [])

    def test_usage_after_the_record_closed_still_attaches(self):
        self.push(self.printer, 13, "late.gcode")
        self.push(self.printer, 9, "late.gcode")

        record = print_history.record_usage("p1", 500.0, 1.24, {3: 500.0}, lookup=lambda _: spool(price=25))
        self.assertEqual((record["file_name"], record["spool_id"]), ("late.gcode", 3))

    # reading, notes, export, repeat

    def test_search_filters_and_summary(self):
        self.push(self.printer, 13, "benchy.gcode", 100, 100)
        self.push(self.printer, 9, "benchy.gcode", 100, 100)
        self.push(self.other, 13, "bracket_50%.gcode")
        self.push(self.other, 8, "bracket_50%.gcode")
        print_history.update_notes(self.records(printer_id="p2")[0]["id"], "layer shift at 3 cm")

        self.assertEqual([r["file_name"] for r in self.records(q="BENCHY")], ["benchy.gcode"])
        self.assertEqual(len(self.records(q="layer shift")), 1)
        self.assertEqual([r["file_name"] for r in self.records(q="50%")], ["bracket_50%.gcode"])
        self.assertEqual(self.records(q="%"), self.records(q="50%"))  # % is matched literally
        self.assertEqual(len(self.records(outcome="cancelled")), 1)
        result = print_history.search(printer_id="p1")
        self.assertEqual((result["total"], result["summary"]["success_rate"]), (1, 1.0))
        self.assertEqual(print_history.search()["summary"]["success_rate"], 0.5)
        with self.assertRaises(JobError):
            print_history.search(outcome="exploded")

    def test_notes_and_csv_export(self):
        self.push(self.printer, 13, "benchy.gcode")
        self.push(self.printer, 9, "benchy.gcode")
        record_id = self.records()[0]["id"]

        self.assertEqual(print_history.update_notes(record_id, "perfect, keep these settings")["notes"],
                         "perfect, keep these settings")
        rows = list(csv.DictReader(io.StringIO(print_history.export_csv())))
        self.assertEqual((rows[0]["file_name"], rows[0]["outcome"], rows[0]["notes"]),
                         ("benchy.gcode", "completed", "perfect, keep these settings"))
        with self.assertRaises(JobError):
            print_history.update_notes(999, "x")

    async def test_repeat_copies_the_queue_job_or_queues_the_printer_file(self):
        job = jobs.create_upload_job(io.BytesIO(GCODE), len(GCODE), "cube.gcode", "p1")
        jobs.update_job(job["id"], {"notes": "PETG", "print_opts": {"leveling": True}})
        await jobs.start_job(job["id"])
        for _ in range(200):
            if self.printer.started:
                break
            await asyncio.sleep(0.01)
        self.push(self.printer, 13, "cube.gcode")
        self.push(self.printer, 9, "cube.gcode")
        queued_print = self.records()[0]

        copy = print_history.repeat_print(queued_print["id"])
        self.assertNotEqual(copy["id"], job["id"])
        self.assertEqual((copy["state"], copy["notes"], copy["print_opts"]["leveling"]), ("queued", "PETG", True))
        self.assertEqual((self.dir / copy["stored_file"]).read_bytes(), GCODE)

        self.push(self.other, 13, "benchy.gcode")
        self.push(self.other, 9, "benchy.gcode")
        manual = self.records(printer_id="p2")[0]
        again = print_history.repeat_print(manual["id"])
        self.assertEqual((again["source"], again["printer_file"], again["printer_id"]),
                         ("printer", "/local/benchy.gcode", "p2"))

    def test_legacy_history_is_imported_once(self):
        legacy = self.dir / "history.json"
        legacy.write_text(json.dumps([
            {"timestamp": "2026-09-01T10:00:00", "printer_id": "p1", "printer_name": "Printer 1",
             "filename": "old.gcode", "filament_mm": 1000, "filament_g": 3.0, "print_time_s": 600, "completed": True},
            {"timestamp": "2026-09-02T10:00:00", "printer_id": "p1", "printer_name": "Printer 1",
             "filename": "stopped.gcode", "filament_mm": 10, "filament_g": 0.1, "print_time_s": 60, "completed": False},
            {"broken": True},
        ]))

        self.assertEqual(print_history.import_legacy_history(legacy), 2)
        self.assertEqual(print_history.import_legacy_history(legacy), 0)
        self.assertTrue((self.dir / "history.json.imported").exists())
        outcomes = {r["file_name"]: (r["outcome"], r["duration_s"], r["source"]) for r in self.records()}
        self.assertEqual(outcomes, {"old.gcode": ("completed", 600, "imported"),
                                    "stopped.gcode": ("cancelled", 60, "imported")})


if __name__ == "__main__":
    unittest.main()
