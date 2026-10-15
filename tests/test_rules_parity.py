"""Parity of the dependency rules: setting_rules.py (server) against rules.js (the module the browser runs).

Both read public/slicer/rules-data.js, so a rule edit reaches both sides; this test catches a difference in how each side
EVALUATES the data (string/number coercion, unset values, last-rule-wins) over many generated settings states, not only the
proposable keys. Prior art: parity.py, test_parity_logic.py, test_parity_native.py. Needs Node; without it the test is skipped,
which means NOT RUN."""
import json
import random
import shutil
import subprocess
import unittest
from pathlib import Path

import setting_rules as rules

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
# Keys from research/02-settings-surface.md section 7 ("safe, bounded") that a settings Proposal may touch.
PROPOSABLE = ["wall_loops", "top_shell_layers", "bottom_shell_layers", "sparse_infill_density", "sparse_infill_pattern",
              "enable_support", "support_type", "support_style", "support_threshold_angle", "support_top_z_distance",
              "support_interface_top_layers", "support_interface_bottom_layers", "support_interface_spacing",
              "tree_support_branch_angle", "tree_support_branch_diameter", "brim_type", "brim_width", "skirt_loops",
              "elefant_foot_compensation", "seam_position", "ironing_type", "outer_wall_speed", "detect_overhang_wall",
              "enable_overhang_speed", "slowdown_for_curled_perimeters", "layer_height", "raft_layers"]
GENERIC = ["0", "1", "2", "3", "true", "false", "none", "disabled_fuzzy", "15%", "0%", "100%", "0.2", "0.6", "50", "abc", ""]


def literals(expr, found):
    """String literals compared with each key, so generated states can reach every branch of every rule."""
    if isinstance(expr, list):
        if expr[0] in ("eq", "ne") and expr[1][0] == "val" and isinstance(expr[2], str):
            found.setdefault(expr[1][1], set()).add(expr[2])
        if expr[0] == "in" and expr[1][0] == "val":
            found.setdefault(expr[1][1], set()).update(expr[2][1])
        for part in expr[1:]:
            literals(part, found)


def controlled_keys():
    drivers, found = set(), {}
    for rule in rules.RULES:
        drivers.update(rules._drivers(rule.when))
        literals(rule.when, found)
    return sorted(drivers), found


def generated_states(count=400, seed=20261015):
    drivers, found = controlled_keys()
    pools = {k: GENERIC + sorted(found.get(k, ())) + ["tree(auto)", "normal(auto)", "tree_slim(auto)"] * (k == "support_type") for k in drivers}
    rng = random.Random(seed)
    states = [{}]
    for _ in range(count):
        density = rng.choice((0.1, 0.3, 0.6))
        states.append({k: rng.choice(pools[k]) for k in drivers if rng.random() < density * 3 or k in ("enable_support", "support_type")})
    for key in drivers:                                    # every driver alone, at every value it can take
        states += [{key: v} for v in pools[key]]
    return states


@unittest.skipUnless(NODE, "Node.js not found: browser parity NOT RUN")
class RulesParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.states = generated_states()
        done = subprocess.run([NODE, str(ROOT / "tests" / "rules_eval.mjs"), str(ROOT / "public" / "slicer" / "rules.js")],
                              input=json.dumps(cls.states), capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode:
            raise AssertionError(f"node failed:\n{done.stderr}")
        cls.browser = json.loads(done.stdout)

    def test_python_and_browser_agree_on_hidden_and_greyed_for_every_state(self):
        self.assertGreater(len(self.states), 500)
        for state, browser in zip(self.states, self.browser):
            toggles = rules.evaluate(state)
            self.assertEqual((sorted(toggles.hidden), sorted(toggles.disabled)), (browser["hidden"], browser["disabled"]),
                             f"disagree for {state}")

    def test_the_states_exercise_both_outcomes_for_every_proposable_key_that_has_a_rule(self):
        gated = [k for k in PROPOSABLE if any(k in r.keys for r in rules.RULES)]
        self.assertIn("support_style", gated)
        for key in gated:
            seen = {rules.explain(s, key).inert for s in self.states}
            self.assertEqual(seen, {True, False}, f"{key} never both inert and live across the generated states")

    def test_explain_matches_the_browser_for_the_proposable_keys(self):
        for state, browser in zip(self.states, self.browser):
            for key in PROPOSABLE:
                got = rules.explain(state, key)
                self.assertEqual((not got.visible, got.greyed), (key in browser["hidden"], key in browser["disabled"]),
                                 f"{key} for {state}")


if __name__ == "__main__":
    unittest.main()
