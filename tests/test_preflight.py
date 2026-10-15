import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database
import gcode_analysis
import jobs
import preflight
import state
from jobs import JobError
from tests.test_gcode_analysis import TWO_LAYERS, orca_like
from tests.test_jobs import FakePrinter

PLA_RANGE = "; nozzle_temperature_range_low = 190\n; nozzle_temperature_range_high = 240\n"


def spool(material="PLA", remaining=500.0, spool_id=7):
    return {"spool": {"id": spool_id, "remaining_weight": remaining,
                      "filament": {"material": material, "name": "Galaxy Black", "vendor": {"name": "Elegoo"}}}}


def layer(z: float, *moves: str) -> str:
    return f";LAYER_CHANGE\n;Z:{z}\n;TYPE:Outer wall\n" + "".join(f"{m}\n" for m in moves)


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_printers = state.printers
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        self.printer = FakePrinter()
        self.spare = FakePrinter("p2", "Printer 2")
        state.printers = {"p1": self.printer, "p2": self.spare}
        self.spool = spool()
        for target, attr, value in ((preflight, "HARDWARE_FILE", self.dir / "printer_hardware.json"),
                                    (preflight, "loaded_spool", lambda printer: self.spool),
                                    (jobs, "uploader", lambda *args: None)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        preflight.set_printer_hardware("p1", 0.4)

    async def asyncTearDown(self):
        for task in list(jobs._start_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)

    def tearDown(self):
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        self._tmp.cleanup()

    def add(self, body=TWO_LAYERS, config=PLA_RANGE, printer_id="p1", **changes):
        data = orca_like(body, config).encode()
        job = jobs.create_upload_job(io.BytesIO(data), len(data), "part.gcode", printer_id)
        return jobs.update_job(job["id"], changes) if changes else job

    def check(self, job):
        return preflight.check_job_id(job["id"])

    @staticmethod
    def levels(report):
        return {c["id"]: c["level"] for c in report["checks"]}

    # the happy path

    def test_a_clean_centauri_carbon_job_passes(self):
        report = self.check(self.add())

        self.assertEqual((report["status"], report["can_start"], report["needs_acknowledgement"]), ("pass", True, False))
        levels = self.levels(report)
        for check_id in ("printer", "file", "printer_model", "build_volume", "purge_zone", "nozzle", "nozzle_temp",
                         "bed_temp", "plate", "filament", "filament_amount", "commands"):
            self.assertEqual(levels[check_id], "pass", check_id)
        self.assertEqual(levels["thumbnail"], "info")
        self.assertEqual(len(report["preview"]["layers"]), 2)
        self.assertEqual(report["checks"][0]["level"], "info")  # most severe first

    # geometry

    def test_wrong_printer_model_fails(self):
        report = self.check(self.add(config=PLA_RANGE + "; printer_model = Elegoo Centauri Carbon 2\n"))
        self.assertEqual(self.levels(report)["printer_model"], "fail")
        self.assertFalse(report["can_start"])

    def test_print_past_the_bed_edge_fails(self):
        report = self.check(self.add(body=layer(0.2, "G1 X250 Y100 Z0.2 F30000", "G1 X300 Y100 E1")))
        self.assertEqual(self.levels(report)["build_volume"], "fail")
        self.assertEqual(self.levels(report)["travel"], "warn")

    def test_print_too_tall_fails(self):
        report = self.check(self.add(body=layer(260, "G1 X100 Y100 Z260", "G1 X110 Y100 E1")))
        self.assertIn("tall", next(c["title"] for c in report["checks"] if c["id"] == "build_volume"))

    def test_printing_in_the_purge_zone_fails(self):
        report = self.check(self.add(body=layer(0.2, "G1 X248 Y5 Z0.2 F30000", "G1 X252 Y5 E1")))
        levels = self.levels(report)
        self.assertEqual((levels["purge_zone"], levels["build_volume"]), ("fail", "pass"))

    # nozzle, temperatures, plate

    def test_nozzle_size_is_checked_against_the_installed_nozzle(self):
        preflight.set_printer_hardware("p1", 0.6)
        self.assertEqual(self.levels(self.check(self.add()))["nozzle"], "fail")
        self.assertEqual(self.levels(self.check(self.add(printer_id="p2")))["nozzle"], "info")  # not set up yet

    def test_temperatures_are_checked_against_printer_and_filament(self):
        self.assertEqual(self.levels(self.check(self.add(body="M104 S330\n" + TWO_LAYERS)))["nozzle_temp"], "fail")
        self.assertEqual(self.levels(self.check(self.add(body="M140 S120\n" + TWO_LAYERS)))["bed_temp"], "fail")
        above = self.check(self.add(body=TWO_LAYERS + "M104 S260\nG1 X125 Y120 E0.5\n"))
        self.assertEqual(self.levels(above)["nozzle_temp"], "warn")
        below = self.check(self.add(config="; nozzle_temperature_range_low = 215\n; nozzle_temperature_range_high = 240\n"))
        self.assertIn("Printing starts at 210", next(c["title"] for c in below["checks"] if c["id"] == "nozzle_temp"))

    def test_the_140c_preheat_is_not_mistaken_for_the_printing_temperature(self):
        report = self.check(self.add())
        self.assertEqual(report["preview"]["temperatures"]["nozzle"]["at_print_start"], 210)
        self.assertEqual(self.levels(report)["nozzle_temp"], "pass")

    def test_plate_mismatch_needs_acknowledgement(self):
        report = self.check(self.add(print_opts={"smooth_plate": True}))
        self.assertEqual(self.levels(report)["plate"], "warn")
        self.assertEqual((report["can_start"], report["needs_acknowledgement"]), (True, True))

    # filament

    def test_loaded_filament_and_remaining_weight(self):
        self.spool = spool(material="PETG")
        self.assertEqual(self.levels(self.check(self.add()))["filament"], "warn")

        self.spool = spool(material="PLA+", remaining=0.1)
        levels = self.levels(self.check(self.add()))
        self.assertEqual((levels["filament"], levels["filament_amount"]), ("pass", "warn"))

        self.spool = {"spool": None}
        self.assertEqual(self.levels(self.check(self.add()))["filament"], "info")
        self.spool = {"error": "connection refused"}
        self.assertEqual(self.levels(self.check(self.add()))["filament"], "info")

    # file contents

    def test_risky_and_unknown_commands(self):
        report = self.check(self.add(body="M500\nMY_MACRO\n" + TWO_LAYERS))
        levels = self.levels(report)
        self.assertEqual((levels["commands_fail"], levels["unknown_commands"]), ("fail", "info"))
        self.assertIn("M500", next(c["detail"] for c in report["checks"] if c["id"] == "commands_fail"))

    def test_files_stored_on_the_printer_can_only_be_partly_checked(self):
        job = jobs.create_printer_file_job("p1", "/local/benchy.gcode")
        report = self.check(job)
        self.assertEqual(self.levels(report), {"printer": "pass", "file": "warn"})
        self.assertIsNone(report["preview"])

    def test_a_file_changed_after_upload_fails(self):
        job = self.add()
        self.assertEqual(self.levels(self.check(job))["file"], "pass")
        with (self.dir / job["stored_file"]).open("ab") as fh:
            fh.write(b"G1 X110 Y110 E1\n")
        self.assertEqual(self.levels(self.check(job))["file"], "fail")

    def test_offline_or_missing_printer_fails(self):
        job = self.add()
        self.printer.connected = False
        self.assertEqual(self.levels(self.check(job))["printer"], "fail")
        missing = preflight.check_job_id(job["id"], printer_id="nope")
        self.assertEqual(next(c["title"] for c in missing["checks"] if c["id"] == "printer"), "Printer not found")

    # caching and settings

    def test_analysis_is_cached_until_the_file_changes(self):
        job = self.add()
        with mock.patch.object(preflight.gcode_analysis, "analyze", wraps=gcode_analysis.analyze) as analyze:
            self.check(job)
            self.check(job)
            self.assertEqual(analyze.call_count, 1)
        self.assertGreater(preflight.toolpath_file(job["id"]).stat().st_size, 0)

    def test_hardened_steel_nozzles(self):
        for size in (0.4, 0.6, 0.7, 0.8):
            hardware = preflight.set_printer_hardware("p2", size, "hardened steel")
            self.assertEqual((hardware["nozzle_diameter"], hardware["nozzle_material"]), (size, "hardened steel"))
        with self.assertRaises(JobError):
            preflight.set_printer_hardware("p2", 0.4, "unobtainium")

    def test_nozzle_setting_validation(self):
        self.assertEqual(preflight.set_printer_hardware("p2", "0.8")["nozzle_diameter"], 0.8)
        with self.assertRaises(JobError):
            preflight.set_printer_hardware("p1", 0.5)
        with self.assertRaises(JobError) as err:
            preflight.set_printer_hardware("nope", 0.4)
        self.assertEqual(err.exception.status, 404)

    # enforcement when starting

    async def test_start_needs_a_passing_preflight_and_acknowledged_warnings(self):
        risky = self.add(body="M500\n" + TWO_LAYERS)
        with self.assertRaisesRegex(JobError, "Preflight failed"):
            await jobs.start_job(risky["id"])
        with self.assertRaisesRegex(JobError, "Preflight failed"):
            await jobs.start_job(risky["id"], acknowledge_warnings=True)
        self.assertEqual(jobs.get_job(risky["id"])["state"], "queued")
        jobs.delete_job(risky["id"])

        mismatch = self.add(print_opts={"smooth_plate": True})
        with self.assertRaisesRegex(JobError, "Review the preflight warnings"):
            await jobs.start_job(mismatch["id"])
        started = await jobs.start_job(mismatch["id"], acknowledge_warnings=True)
        self.assertEqual(started["state"], "starting")
        preflight_event = next(e for e in jobs.job_events(mismatch["id"]) if e["event"] == "preflight")
        self.assertIn("warnings acknowledged", preflight_event["detail"])


if __name__ == "__main__":
    unittest.main()
