"""Dependency rules evaluated in Python (setting_rules.py): which settings OrcaSlicer hides, greys out or makes inert, and why.

Expectations are written by hand from OrcaSlicer's behaviour (ConfigManipulation::toggle_print_fff_options), not recomputed from
the rule data, so a wrong transcription fails here and not only in the browser parity check (test_rules_parity.py)."""
import unittest

import setting_rules as rules


class VisibleGreyedInertTests(unittest.TestCase):
    def test_a_setting_nothing_gates_is_visible_and_live(self):
        state = rules.explain({}, "layer_height")
        self.assertEqual((state.visible, state.greyed, state.inert, state.reason), (True, False, False, None))

    def test_support_settings_are_greyed_while_supports_are_off_and_say_why(self):
        state = rules.explain({"enable_support": "0", "raft_layers": "0"}, "support_style")
        self.assertEqual((state.visible, state.greyed, state.inert), (True, True, True))
        self.assertIn("enable_support", state.reason)
        self.assertIn("no effect", state.reason.lower())

    def test_support_settings_come_alive_when_supports_turn_on_or_a_raft_is_used(self):
        self.assertFalse(rules.explain({"enable_support": "1"}, "support_style").inert)
        self.assertFalse(rules.explain({"raft_layers": "2"}, "support_style").inert)

    def test_true_and_1_both_count_as_on(self):
        self.assertFalse(rules.explain({"enable_support": "true"}, "support_top_z_distance").inert)

    def test_zero_infill_hides_the_infill_pattern(self):
        state = rules.explain({"sparse_infill_density": "0%"}, "sparse_infill_pattern")
        self.assertEqual((state.visible, state.inert), (False, True))
        self.assertIn("sparse_infill_density", state.reason)

    def test_percent_values_read_like_the_browser_reads_them(self):
        self.assertTrue(rules.explain({"sparse_infill_density": "15%"}, "sparse_infill_pattern").visible)

    def test_spiral_mode_hides_overhang_reverse(self):
        self.assertFalse(rules.explain({"spiral_mode": "1"}, "overhang_reverse").visible)
        self.assertTrue(rules.explain({"spiral_mode": "0"}, "overhang_reverse").visible)

    def test_slope_limit_greys_out_arc_fitting_a_cross_effect(self):
        self.assertTrue(rules.explain({"max_volumetric_extrusion_rate_slope": "5"}, "enable_arc_fitting").greyed)
        self.assertFalse(rules.explain({"max_volumetric_extrusion_rate_slope": "0"}, "enable_arc_fitting").greyed)

    def test_zero_walls_grey_out_wall_settings(self):
        self.assertTrue(rules.explain({"wall_loops": "0"}, "seam_position").greyed)
        self.assertFalse(rules.explain({"wall_loops": "3"}, "seam_position").greyed)

    def test_one_wall_loop_is_enough_to_keep_wall_settings_live(self):
        self.assertFalse(rules.explain({"wall_loops": "1"}, "seam_position").greyed)

    def test_an_unset_value_counts_as_absent_like_the_browser(self):
        self.assertTrue(rules.explain({}, "brim_object_gap").greyed is False)   # brim_type undefined is not "no_brim"
        self.assertTrue(rules.explain({"brim_type": "no_brim"}, "brim_object_gap").greyed)

    def test_a_later_rule_overrides_an_earlier_one_for_the_same_key(self):
        # infill_direction is allowed by the broad shell rule but the last rule greys it for adaptive cubic infill
        live = {"sparse_infill_density": "20%", "sparse_infill_pattern": "grid"}
        self.assertFalse(rules.explain(live, "infill_direction").greyed)
        self.assertTrue(rules.explain({**live, "sparse_infill_pattern": "adaptivecubic"}, "infill_direction").greyed)

    def test_klipper_hides_the_marlin_only_junction_deviation_always(self):
        self.assertFalse(rules.explain({}, "default_junction_deviation").visible)

    def test_unknown_keys_are_visible_and_live(self):
        self.assertFalse(rules.explain({}, "not_a_real_setting").inert)

    def test_evaluate_returns_the_hidden_and_greyed_sets(self):
        toggles = rules.evaluate({"spiral_mode": "1", "enable_support": "0"})
        self.assertIn("overhang_reverse", toggles.hidden)
        self.assertIn("support_style", toggles.disabled)
        self.assertNotIn("layer_height", toggles.hidden | toggles.disabled)

    def test_values_may_be_given_as_numbers(self):
        self.assertTrue(rules.explain({"wall_loops": 0}, "seam_position").greyed)


class ProposalNoteTests(unittest.TestCase):
    def test_a_change_that_would_do_nothing_is_reported_with_its_reason(self):
        inert = rules.inert_changes({"enable_support": "0"}, {"support_style": "tree_slim"})
        self.assertEqual(list(inert), ["support_style"])
        self.assertIn("enable_support", inert["support_style"])

    def test_the_same_proposal_turning_supports_on_is_not_inert(self):
        self.assertEqual(rules.inert_changes({"enable_support": "0"}, {"enable_support": "1", "support_style": "tree_slim"}), {})

    def test_the_change_that_gates_others_is_never_itself_inert(self):
        self.assertEqual(rules.inert_changes({}, {"enable_support": "1"}), {})


class SingleSourceTests(unittest.TestCase):
    def test_the_rules_are_read_from_the_file_the_browser_imports(self):
        from pathlib import Path
        self.assertEqual(rules.DATA_FILE, Path(__file__).resolve().parent.parent / "public" / "slicer" / "rules-data.js")
        self.assertGreater(len(rules.RULES), 80)


if __name__ == "__main__":
    unittest.main()
