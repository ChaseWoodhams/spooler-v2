import unittest
from unittest import mock

import farm
import jobs
import preflight
import slicer
import state
from tests.test_slicer import SlicerTestCase

PLA = {"id": 3, "remaining_weight": 640.0, "initial_weight": 1000.0, "price": 20.0, "location": "Printer 1", "archived": False,
       "filament": {"material": "PLA", "name": "Silk Blue", "density": 1.24, "diameter": 1.75, "color_hex": "3366FF",
                    "settings_extruder_temp": 215, "settings_bed_temp": 60, "vendor": {"name": "Elegoo"}}}
PETG = {"id": 4, "remaining_weight": 900.0, "initial_weight": 1000.0, "location": "", "archived": False,
        "filament": {"material": "PETG", "name": "Clear", "density": 1.27, "diameter": 1.75, "vendor": {"name": "Sunlu"}}}
OLD = {"id": 5, "archived": True, "filament": {"material": "PLA"}}
PROCESS = "0.20mm Standard @Elegoo CC 0.4 nozzle"


class SpoolTests(SlicerTestCase):
    def spoolman(self, spools=(PLA, PETG, OLD)):
        def get(path):
            if path.startswith("/api/v1/spool?"):
                return list(spools)
            return next(s for s in spools if path.endswith(f"/spool/{s['id']}"))
        patcher = mock.patch.object(farm, "_get", get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_spools_are_listed_with_the_printer_that_holds_each(self):
        self.spoolman()
        listing = farm.list_spools()

        self.assertEqual([s["id"] for s in listing["spools"]], [3, 4])   # archived spools are left out
        self.assertEqual(listing["printers"], {"p1": 3})
        pla = listing["spools"][0]
        self.assertEqual((pla["name"], pla["remaining_weight"], pla["price_per_kg"]), ("Elegoo PLA Silk Blue", 640.0, 20.0))

    def test_a_spool_becomes_a_matching_preset_and_filament_changes(self):
        self.spoolman()
        bundle = self.bundle()

        pla = farm.spool_match(3, bundle["id"], "textured")
        self.assertEqual(pla["preset"], "Elegoo PLA @ECC")
        self.assertEqual(pla["changes"]["nozzle_temperature"], ["215"])
        self.assertEqual(pla["changes"]["filament_density"], ["1.24"])
        self.assertEqual(pla["changes"]["textured_plate_temp"], ["60"])
        self.assertNotIn("hot_plate_temp", pla["changes"])
        self.assertEqual(farm.spool_match(3, bundle["id"], "smooth")["changes"]["hot_plate_temp"], ["60"])

        # the changes describe the spool whether or not this printer has a stock PETG preset to suggest
        petg = farm.spool_match(4, bundle["id"])
        stock = "Elegoo PETG @ECC" in slicer._system_presets("filament", slicer.require_orca())
        self.assertEqual(petg["preset"], "Elegoo PETG @ECC" if stock else None)
        self.assertEqual(petg["changes"]["filament_density"], ["1.27"])
        self.assertNotIn("nozzle_temperature", petg["changes"])

    def test_spoolman_being_down_is_a_clear_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with self.assertRaisesRegex(jobs.JobError, "Spoolman isn't reachable"):
                farm.list_spools()


class PlateFitTests(SlicerTestCase):
    def record(self, printer_id="p1"):
        return {"id": 1, "data": {"plates": [{"id": 1, "name": "Plate 1", "printer_id": printer_id}],
                                  "profile": {"bundle_id": self.bundle()["id"]}},
                "slices": {"1": {"estimate_g": 500.0}}}

    def fit(self, hardware=None, spool=None, printer_id="p1", connected=True):
        state.printers["p1"].connected = connected
        with mock.patch.object(preflight, "printer_hardware", return_value={"nozzle_diameter": hardware}), \
                mock.patch.object(preflight, "loaded_spool", return_value=spool if spool is not None else {"spool": None}):
            return farm.plate_fit(self.record(printer_id), 1, printer_id)["warnings"]

    def test_no_printer_no_warnings(self):
        self.assertEqual(farm.plate_fit(self.record(None), 1)["warnings"], [])

    def test_offline_nozzle_material_and_amount_are_flagged(self):
        warnings = self.fit(hardware=0.6, connected=False, spool={"spool": PETG})
        texts = [w["text"] for w in warnings]
        self.assertTrue(any("offline" in t for t in texts))
        self.assertTrue(any("0.4 mm nozzle" in t and "0.6 mm nozzle" in t for t in texts))
        self.assertTrue(any("PLA" in t and "PETG" in t for t in texts))

        thin = {**PLA, "remaining_weight": 200.0}
        self.assertTrue(any("200 g left" in w["text"] for w in self.fit(hardware=0.4, spool={"spool": thin})))
        self.assertEqual(self.fit(hardware=0.4, spool={"spool": PLA}), [])   # right nozzle, PLA, 640 g for a 500 g plate

    def test_unknown_things_are_information_not_failures(self):
        levels = {w["level"] for w in self.fit(hardware=None, spool={"error": "down"})}
        self.assertEqual(levels, {"info"})


if __name__ == "__main__":
    unittest.main()
