import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import brick_post
import gcode_analysis
import jobs

# A 10 mm cube sliced by OrcaSlicer 2.4.2 with the Centauri Carbon profile: 0.2 mm layers, 3 classic walls
# printed inner then outer, no arcs.
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "orca_cube_10mm.gcode"


class SettingTests(unittest.TestCase):
    def test_validate_fills_defaults_and_checks_limits(self):
        self.assertIsNone(brick_post.validate(None))
        self.assertEqual(brick_post.validate({"enabled": True}),
                         {"enabled": True, "extrusion_multiplier": 1.05, "start_at_layer": 3})
        self.assertEqual(brick_post.validate({"enabled": 1, "extrusion_multiplier": "1.1", "start_at_layer": 5.0}),
                         {"enabled": True, "extrusion_multiplier": 1.1, "start_at_layer": 5})
        for raw, message in (({"extrusion_multiplier": 2}, "between 0.8 and 1.5"), ({"extrusion_multiplier": "lots"}, "a number"),
                             ({"start_at_layer": 0}, "between 1 and 1000"), ({"start_at_layer": 2.5}, "whole number"),
                             ("on", "must be an object")):
            with self.assertRaisesRegex(jobs.JobError, message):
                brick_post.validate(raw)

    def test_a_plate_setting_overrides_the_project(self):
        on, off = {"enabled": True, "extrusion_multiplier": 1.1, "start_at_layer": 4}, {"enabled": False}
        self.assertIsNone(brick_post.effective(None, None))
        self.assertEqual(brick_post.effective(on, None), on)
        self.assertIsNone(brick_post.effective(on, off))
        self.assertEqual(brick_post.effective(off, {"enabled": True})["extrusion_multiplier"], 1.05)


class VendoredScriptTests(unittest.TestCase):
    def test_the_vendored_script_matches_its_recorded_source(self):
        recorded = re.search(r"sha256: ([0-9a-f]{64})", (brick_post.SCRIPT.parent / "SOURCE.md").read_text()).group(1)
        self.assertEqual(hashlib.sha256(brick_post.SCRIPT.read_bytes()).hexdigest(), recorded)
        self.assertTrue((brick_post.SCRIPT.parent / "LICENSE").is_file())

    def test_it_interlocks_the_inner_walls_of_real_orca_gcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "plate.orca.gcode", Path(tmp) / "plate.gcode"
            shutil.copyfile(FIXTURE, source)
            args, _ = brick_post.run(source, target, {**brick_post.DEFAULTS, "enabled": True, "start_at_layer": 3})
            self.assertIn("-noLogging", args)
            text = target.read_text(encoding="utf-8")
            self.assertEqual(source.read_bytes(), FIXTURE.read_bytes())  # OrcaSlicer's output is kept as it was
            self.assertIn(";== BrickLayers Post-Processed", text)
            self.assertIn(";== Starting at Layer: 3", text)

            before, _ = gcode_analysis.analyze(source)
            after, _ = gcode_analysis.analyze(target)
            whole = {round(layer["z"], 2) for layer in before["layers"]}
            raised = sorted({round(layer["z"], 2) for layer in after["layers"]} - whole)
            self.assertTrue(raised, "no walls were raised")
            self.assertAlmostEqual(raised[0], 0.7)  # half a 0.2 mm layer above layer 3, the first one it changes
            self.assertEqual(max(layer["z"] for layer in after["layers"]), max(whole))
            self.assertEqual([f for f in after["findings"] if f["level"] == "fail"], [])
            self.assertGreater(after["stats"]["extruded_mm"], before["stats"]["extruded_mm"])  # the 1.05 multiplier

    def test_a_failure_reports_the_error_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.gcode"
            source.write_text("G28\n")
            failing = mock.Mock(return_value=(1, "Traceback …\nValueError: bad line\n\nSee 'bricklayers_error_log.txt' for a saved copy.\n"))
            with mock.patch.object(brick_post, "runner", failing), \
                    self.assertRaisesRegex(brick_post.BrickLayersError, "BrickLayers failed: ValueError: bad line"):
                brick_post.run(source, Path(tmp) / "out.gcode", {**brick_post.DEFAULTS, "enabled": True})
            with mock.patch.object(brick_post, "runner", mock.Mock(return_value=(0, ""))), \
                    self.assertRaisesRegex(brick_post.BrickLayersError, "without writing G-code"):
                brick_post.run(source, Path(tmp) / "out.gcode", {**brick_post.DEFAULTS, "enabled": True})


if __name__ == "__main__":
    unittest.main()
