"""Assistant tuning (Limits), through the app's own routes with the fake `claude`."""
import json
import time
import unittest

import assistant
import assistant_runner
from tests import test_assistant_ask as base
from tests.fake_claude import read_record

LIMIT_DEFAULTS = {"deadline_seconds": 60, "max_turns": 12, "proposals_per_run": 3, "question_chars": 2000}


@unittest.skipUnless(base.GEOMETRY, "geometry libraries not installed")
class TuningLimitsTests(unittest.TestCase):
    setUp, tearDown = base.AskTests.setUp, base.AskTests.tearDown
    call, make_project, scenario, ask, poll, finished = (base.AskTests.call, base.AskTests.make_project, base.AskTests.scenario, base.AskTests.ask,
                                                         base.AskTests.poll, base.AskTests.finished)

    def save(self, **limits):
        return self.call("POST", "/api/assistant/settings", {"tuning": {"limits": limits}})

    def values(self):
        status, body = self.call("GET", "/api/assistant/settings")
        self.assertEqual(status, 200, body)
        return body["tuning"]["values"]["limits"]

    def argv_after(self, flag):
        argv = read_record(self.cli_dir)["argv"]
        return argv[argv.index(flag) + 1]

    def test_with_nothing_saved_the_limits_are_todays_numbers_and_the_server_describes_each(self):
        status, body = self.call("GET", "/api/assistant/settings")
        tuning = body["tuning"]
        self.assertEqual((status, tuning["version"], tuning["values"]["limits"], tuning["notes"]), (200, 1, LIMIT_DEFAULTS, []))
        fields = tuning["groups"]["limits"]["fields"]
        self.assertEqual({k: (f["default"], f["min"], f["max"]) for k, f in fields.items()},
                         {"deadline_seconds": (60, 5, 600), "max_turns": (12, 1, 50), "proposals_per_run": (3, 1, 10), "question_chars": (2000, 100, 10000)})
        self.assertTrue(all(f["label"] and f["hint"] for f in fields.values()))
        view = self.finished()                                  # untouched install: the CLI gets today's turn cap
        self.assertEqual(view["state"], "answer")
        self.assertEqual(self.argv_after("--max-turns"), "12")

    def test_an_out_of_bounds_value_is_refused_with_the_bound_and_nothing_is_saved_or_clamped(self):
        for limits, bound in (({"deadline_seconds": 4}, "5"), ({"deadline_seconds": 601}, "600"), ({"max_turns": 0}, "1"), ({"max_turns": 51}, "50"),
                              ({"proposals_per_run": 11}, "10"), ({"question_chars": 99}, "100"), ({"question_chars": 10001}, "10000")):
            status, body = self.save(**limits)
            self.assertEqual(status, 400, limits)
            self.assertIn(bound, body["error"], limits)
        for bad in ("ten", True, 1.5, None):
            self.assertEqual(self.save(max_turns=bad)[0], 400, bad)
        self.assertEqual(self.save(max_turns=8, deadline_seconds=2)[0], 400)
        self.assertEqual(self.values(), LIMIT_DEFAULTS, "a refused save changes nothing, not even its valid keys")
        self.assertEqual(self.call("POST", "/api/assistant/settings", {"tuning": {"limits": {"nope": 1}}})[0], 400)
        self.assertEqual(self.call("POST", "/api/assistant/settings", {"tuning": {"nope": {}}})[0], 400)


if __name__ == "__main__":
    unittest.main()
