"""PROPOSABLE_SETTINGS: the Spooler-owned allow-list of settings the Assistant may propose, with per-key bounds. Pure data and
checks, so these tests need no project, no schema file and no server."""
import unittest

import proposable_settings as ps


class ClassifyTests(unittest.TestCase):
    def test_the_safe_bounded_set_is_proposable_at_project_and_plate_scope(self):
        for key in ("wall_loops", "top_shell_layers", "bottom_shell_layers", "sparse_infill_density", "sparse_infill_pattern",
                    "enable_support", "support_type", "support_style", "support_threshold_angle", "support_top_z_distance",
                    "support_interface_top_layers", "support_interface_bottom_layers", "support_interface_spacing",
                    "tree_support_branch_angle", "tree_support_branch_diameter", "brim_type", "brim_width", "skirt_loops",
                    "elefant_foot_compensation", "seam_position", "ironing_type", "outer_wall_speed", "detect_overhang_wall",
                    "enable_overhang_speed", "slowdown_for_curled_perimeters", "layer_height", "raft_layers"):
            self.assertIn(key, ps.PROPOSABLE_SETTINGS, key)
            self.assertTrue(ps.is_proposable(key, "project"), key)
            self.assertTrue(ps.is_proposable(key, "plate"), key)

    def test_cooling_keys_are_filament_settings_and_only_project_wide(self):
        self.assertEqual(ps.PROPOSABLE_SETTINGS["fan_min_speed"].kind, "filament")
        self.assertTrue(ps.is_proposable("fan_min_speed", "project"))
        self.assertFalse(ps.is_proposable("fan_min_speed", "plate"))
        self.assertIn("Filament", ps.why_not("fan_min_speed", "plate"))

    def test_object_scope_is_only_the_six_quick_print_options(self):
        quick = {"layer_height", "sparse_infill_density", "wall_loops", "enable_support", "support_type", "brim_type", "brim_width"}
        self.assertEqual(set(ps.OBJECT_SCOPE_KEYS), quick)
        for key in quick:
            self.assertTrue(ps.is_proposable(key, "object"), key)
        for key in ("top_shell_layers", "sparse_infill_pattern", "outer_wall_speed", "seam_position", "skirt_loops"):
            self.assertFalse(ps.is_proposable(key, "object"), key)
            self.assertIn("Quick print", ps.why_not(key, "object"))

    def test_the_deny_list_is_never_proposable_and_each_class_says_why(self):
        denied = {
            "nozzle_temperature": "emperature", "nozzle_temperature_initial_layer": "emperature", "hot_plate_temp": "emperature",
            "textured_plate_temp_initial_layer": "emperature", "chamber_temperature": "emperature",
            "filament_flow_ratio": "low", "pressure_advance": "low", "filament_max_volumetric_speed": "low",
            "default_acceleration": "otion", "outer_wall_acceleration": "otion", "default_jerk": "otion", "travel_speed": "otion",
            "accel_to_decel_factor": "otion", "max_volumetric_extrusion_rate_slope": "otion",
            "print_sequence": "achine", "spiral_mode": "achine", "slicing_mode": "achine", "timelapse_type": "achine",
            "exclude_object": "achine", "gcode_add_line_number": "achine", "curr_bed_type": "achine", "z_hop": "achine",
            "printable_area": "achine", "gcode_flavor": "achine", "nozzle_diameter": "achine", "filament_type": "achine",
            "post_process": "post_process", "inherits": "identity", "name": "identity", "compatible_printers": "identity",
            "print_settings_id": "identity", "filename_format": "identity",
            "machine_start_gcode": "G-code", "layer_change_gcode": "G-code", "filament_end_gcode": "G-code"}
        for key, word in denied.items():
            for scope in ("project", "plate", "object"):
                self.assertFalse(ps.is_proposable(key, scope), f"{key} at {scope}")
            self.assertIn(word, ps.why_not(key, "project"), key)
        self.assertEqual(set(ps.PROPOSABLE_SETTINGS) & set(denied), set())

    def test_an_unknown_key_is_refused_as_not_on_the_list(self):
        self.assertFalse(ps.is_proposable("made_up_key", "project"))
        self.assertIn("allow-list", ps.why_not("made_up_key", "project"))

    def test_read_only_keys_are_reported_but_never_proposable(self):
        for key in ("nozzle_temperature", "hot_plate_temp", "filament_type", "nozzle_diameter"):
            self.assertIn(key, ps.READ_ONLY_SETTINGS)
            self.assertNotIn(key, ps.PROPOSABLE_SETTINGS)


class QuickPrintParityTests(unittest.TestCase):
    """The Object-scope vocabulary is the Quick print dialog's (projects.QUICK_*); this keeps the two copies equal."""

    def test_object_scope_keys_are_exactly_what_quick_print_writes(self):
        import projects
        written = set()
        for options in ({"layer_height": 0.2, "infill": 20, "walls": 3, "supports": "tree", "brim": "custom", "brim_width": 5},
                        {"supports": "normal", "brim": "off"}, {"brim": "auto"}):
            written |= set(projects._quick_overrides(options))
        self.assertEqual(written, set(ps.OBJECT_SCOPE_KEYS))
        self.assertEqual(ps.QUICK_LAYER_HEIGHTS, tuple(f"{h:g}" for h in projects.QUICK_LAYER_HEIGHTS))
        self.assertEqual(ps.QUICK_BRIM_TYPES, ("no_brim", "auto_brim", "outer_only"))
        for supports, expected in (("normal", "normal(auto)"), ("tree", "tree(auto)")):
            self.assertEqual(projects._quick_overrides({"supports": supports})["support_type"], expected)
            self.assertIn(expected, ps.QUICK_SUPPORT_TYPES)

    def test_every_quick_print_value_is_accepted_at_object_scope(self):
        import projects
        for height in projects.QUICK_LAYER_HEIGHTS:
            self.assertIsNone(ps.refusal("layer_height", height, "object", nozzle=0.4), height)
        for key, value in projects._quick_overrides({"infill": 100, "walls": 20, "supports": "tree", "brim": "custom", "brim_width": 20}).items():
            self.assertIsNone(ps.refusal(key, value, "object"), (key, value))
        for key, value in projects._quick_overrides({"infill": 0, "walls": 1, "supports": "off", "brim": "off"}).items():
            self.assertIsNone(ps.refusal(key, value, "object"), (key, value))


class BoundsTests(unittest.TestCase):
    def refusal(self, key, value, scope="project", **context):
        return ps.refusal(key, value, scope, **context)

    def test_in_bounds_values_pass(self):
        for key, value in (("wall_loops", 4), ("wall_loops", "4"), ("sparse_infill_density", "35%"), ("sparse_infill_density", 35),
                           ("enable_support", True), ("enable_support", "0"), ("enable_support", 1), ("sparse_infill_pattern", "gyroid"),
                           ("support_type", "tree(auto)"), ("brim_width", 5), ("layer_height", 0.16), ("top_shell_layers", 6)):
            self.assertIsNone(self.refusal(key, value), (key, value))

    def test_out_of_bounds_values_are_refused_with_the_bounds_in_the_reason(self):
        cases = [("wall_loops", 0, "1"), ("wall_loops", 21, "20"), ("wall_loops", 2.5, "whole"), ("wall_loops", "many", "number"),
                 ("sparse_infill_density", "101%", "100"), ("sparse_infill_density", -5, "0"), ("top_shell_layers", 99, "20"),
                 ("brim_width", 40, "20"), ("support_threshold_angle", 120, "90"), ("skirt_loops", 50, "5"),
                 ("enable_support", "maybe", "on or off"), ("sparse_infill_pattern", "hilbertcurve", "gyroid"),
                 ("support_type", "tree(manual)", "tree(auto)"), ("brim_type", "painted", "no_brim"), ("layer_height", 0.5, "0.32"),
                 ("layer_height", 0.17, "0.16"), ("raft_layers", 9, "5"), ("elefant_foot_compensation", 2, "0.5")]
        for key, value, fragment in cases:
            reason = self.refusal(key, value)
            self.assertIsNotNone(reason, (key, value))
            self.assertIn(fragment, reason, (key, value, reason))

    def test_booleans_and_nonsense_types_are_not_numbers(self):
        self.assertIsNotNone(self.refusal("wall_loops", True))
        self.assertIsNotNone(self.refusal("wall_loops", None))
        self.assertIsNotNone(self.refusal("wall_loops", [3]))
        self.assertIsNotNone(self.refusal("wall_loops", float("nan")))

    def test_layer_height_cannot_exceed_four_fifths_of_the_nozzle(self):
        self.assertIsNone(self.refusal("layer_height", 0.32, nozzle=0.4))
        self.assertIn("nozzle", self.refusal("layer_height", 0.32, nozzle=0.2))
        self.assertIsNone(self.refusal("layer_height", 0.12, nozzle=0.2))

    def test_speeds_are_capped_at_the_profiles_value(self):
        self.assertIsNone(self.refusal("outer_wall_speed", 150, profile_value="200"))
        self.assertIsNone(self.refusal("outer_wall_speed", 200, profile_value="200"))
        reason = self.refusal("outer_wall_speed", 250, profile_value="200")
        self.assertIn("200", reason)
        self.assertIn("profile", reason)
        self.assertIn("profile", self.refusal("outer_wall_speed", 100), "no profile value to cap against: refused")
        self.assertIsNotNone(self.refusal("outer_wall_speed", 0, profile_value="200"))
        self.assertIsNotNone(self.refusal("outer_wall_speed", 2, profile_value="200"), "slower than the floor")

    def test_object_scope_applies_the_quick_print_vocabulary_not_the_wider_bounds(self):
        self.assertIsNone(self.refusal("wall_loops", 3, "object"))
        self.assertIsNone(self.refusal("sparse_infill_density", "20%", "object"))
        self.assertIsNone(self.refusal("brim_type", "outer_only", "object"))
        self.assertIn("Quick print", self.refusal("brim_type", "outer_and_inner", "object"))      # a plate may use it, a quick option may not
        self.assertIn("Quick print", self.refusal("sparse_infill_density", "20.5%", "object"))
        self.assertIn("Quick print", self.refusal("top_shell_layers", 3, "object"))
        self.assertIn("Quick print", self.refusal("layer_height", 0.15, "object"))


class BoundsReportTests(unittest.TestCase):
    def test_bounds_describe_what_would_be_accepted(self):
        self.assertEqual(ps.bounds("wall_loops", "project"), {"type": "int", "min": 1, "max": 20})
        self.assertEqual(ps.bounds("sparse_infill_density", "project")["unit"], "%")
        self.assertEqual(ps.bounds("enable_support", "project"), {"type": "bool", "choices": ["0", "1"]})
        self.assertIn("gyroid", ps.bounds("sparse_infill_pattern", "project")["choices"])
        speed = ps.bounds("outer_wall_speed", "project", profile_value="200")
        self.assertEqual((speed["max"], speed["max_is"]), (200.0, "the profile's value"))
        self.assertEqual(ps.bounds("layer_height", "object")["choices"], ["0.08", "0.12", "0.16", "0.2", "0.24", "0.28", "0.32"])
        self.assertEqual(ps.bounds("brim_type", "object")["choices"], ["no_brim", "auto_brim", "outer_only"])
        self.assertIsNone(ps.bounds("nozzle_temperature", "project"))


if __name__ == "__main__":
    unittest.main()
