"""Fast tests of the parity comparison itself: no Orca, server or printer. They prove the workflow would notice each regression
the ticket names, and that it leaves readable diagnostics when it does."""
import tempfile
import unittest
from pathlib import Path

from tests import parity

FIXTURE = Path(__file__).parent / "fixtures" / "orca_cube_10mm.gcode"


def gcode(*, walls=40, infill=30, x0=80.0, layers=50, seconds=270, grams=0.74, **config):
    """A tiny stand-in for an Orca file: role-tagged moves, estimates and a config footer."""
    settings = {"filament_type": "PETG", "nozzle_temperature": "250", "wall_loops": "3", "sparse_infill_density": "15%",
                "layer_height": "0.2", "curr_bed_type": "Textured PEI Plate", "filament_settings_id": "Elegoo PETG @ECC", **config}
    lines = [f"; total layer number: {layers}"]
    for role, count in (("Inner wall", walls), ("Sparse infill", infill)):
        lines.append(f";TYPE:{role}")
        lines += [f"G1 X{x0 + i * 0.5:.3f} Y{90 + (i % 7):.3f} E0.2" for i in range(count)]
    lines += [f"; total layers count = {layers}", f"; total filament used [g] = {grams}",
              f"; estimated printing time (normal mode) = {seconds // 60}m {seconds % 60}s", "; CONFIG_BLOCK_START"]
    lines += [f"; {k} = {v}" for k, v in settings.items()]
    lines.append("; CONFIG_BLOCK_END")
    return "\n".join(lines)


EXPECT = {"filament_type": "PETG", "nozzle_temperature": "250", "wall_loops": "3", "_moves": {"Inner wall": 1}, "_layers": 50}


class ParityLogicTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spooler-parity-logic-")
        self.addCleanup(self._tmp.cleanup)
        self.diagnostics = Path(self._tmp.name)

    def check(self, other, expected=EXPECT, label="case"):
        return parity.assert_parity(label, gcode(), other, expected, self.diagnostics)

    def fails(self, other, expected=EXPECT, label="case"):
        with self.assertRaises(parity.ParityFailure) as caught:
            self.check(other, expected, label)
        return str(caught.exception)

    def test_identical_outputs_that_match_the_independent_expectation_pass(self):
        self.assertEqual(self.check(gcode())["layers"], 50)
        self.assertEqual(list(self.diagnostics.iterdir()), [])

    def test_the_real_orca_fixture_parses_layers_time_weight_and_config(self):
        parsed = parity.parse(FIXTURE.read_text(encoding="utf-8", errors="replace"))
        self.assertEqual((parsed["layers"], parsed["time_s"], parsed["weight_g"]), (50, 270, 0.74))
        self.assertEqual(parsed["config"]["filament_type"], "PLA")
        self.assertGreater(sum(parsed["moves"].values()), 100)

    def test_time_drift_is_bounded_by_the_larger_of_one_second_or_a_tenth_of_a_percent(self):
        self.check(gcode(seconds=271))                              # 1 s on a 270 s print
        self.assertIn("time_s", self.fails(gcode(seconds=273)))     # 3 s: over both bounds
        long = parity.parse(gcode(seconds=36000))
        near = parity.parse(gcode(seconds=36030))                   # 30 s is 0.08% of ten hours
        far = parity.parse(gcode(seconds=36060))                    # 60 s is 0.17%
        self.assertEqual([d for d in parity.compare(long, near) if d["what"] == "time_s"], [])
        self.assertTrue([d for d in parity.compare(long, far) if d["what"] == "time_s"])

    def test_weight_drift_is_bounded_by_the_larger_of_two_hundredths_of_a_gram_or_a_tenth_of_a_percent(self):
        self.check(gcode(grams=0.75))
        self.assertIn("weight_g", self.fails(gcode(grams=0.80)))
        heavy, near, far = (parity.parse(gcode(grams=g)) for g in (100.0, 100.09, 100.3))
        self.assertEqual([d for d in parity.compare(heavy, near) if d["what"] == "weight_g"], [])
        self.assertTrue([d for d in parity.compare(heavy, far) if d["what"] == "weight_g"])

    def test_layer_counts_must_match_exactly(self):
        self.assertIn("layer counts must match", self.fails(gcode(layers=51)))

    def test_plate_walls_disappearing_fails_and_keeps_readable_diagnostics(self):
        text = self.fails(gcode(walls=0), label="walls gone")
        self.assertIn("toolpath Inner wall", text)
        folder = self.diagnostics / "walls_gone"
        self.assertTrue((folder / "report.txt").read_text(encoding="utf-8").startswith("PARITY FAILURE: walls gone"))
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["expected.json", "other.gcode", "other.json", "reference.gcode", "reference.json", "report.txt"])

    def test_infill_disappearing_fails(self):
        self.assertIn("toolpath Sparse infill", self.fails(gcode(infill=0)))

    def test_petg_changing_to_pla_fails_even_when_the_wall_counts_still_agree(self):
        text = self.fails(gcode(filament_type="PLA", nozzle_temperature="220"))
        self.assertIn("filament_type", text)
        self.assertIn("nozzle_temperature", text)

    def test_asa_identity_or_settings_changing_fails(self):
        asa = {"filament_type": "ASA", "nozzle_temperature": "260", "_layers": 50}
        wrong_temperature = gcode(filament_type="ASA", nozzle_temperature="250")
        with self.assertRaises(parity.ParityFailure):
            parity.assert_parity("asa", gcode(filament_type="ASA", nozzle_temperature="260"), wrong_temperature, asa, self.diagnostics)

    def test_a_moved_object_fails_on_placement(self):
        self.assertIn("placement", self.fails(gcode(x0=95.0)))

    def test_a_resolver_bug_shared_by_both_paths_is_caught_by_the_independent_expectation(self):
        both_wrong = gcode(wall_loops="1")
        with self.assertRaises(parity.ParityFailure) as caught:
            parity.assert_parity("shared", both_wrong, both_wrong, EXPECT, self.diagnostics)
        self.assertIn("wall_loops", str(caught.exception))
        self.assertIn("not what was asked for", str(caught.exception))

    def test_review_differing_from_the_inputs_sent_to_orca_fails(self):
        self.assertIn("sparse_infill_density", self.fails(gcode(sparse_infill_density="25%"), {**EXPECT, "sparse_infill_density": "15%"}))

    def test_only_documented_cosmetic_differences_are_ignored(self):
        self.check(gcode(filament_settings_id="Another name", thumbnails="48x48/PNG"))
        self.assertIn("layer_height", self.fails(gcode(layer_height="0.16")))
        self.assertIn("curr_bed_type", self.fails(gcode(curr_bed_type="Cool Plate")))

    def test_a_setting_reported_by_only_one_output_is_a_difference(self):
        self.assertIn("reported by only one output", self.fails(gcode(extra_setting="1")))


if __name__ == "__main__":
    unittest.main()
