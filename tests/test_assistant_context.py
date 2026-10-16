"""Assistant tuning, Prompt context: what each question sends along, through the app's own routes with the fake `claude`."""
import unittest

from tests import test_assistant_ask as base
from tests.fake_claude import read_record

CONTEXT_DEFAULTS = {"send_purposes": True, "send_purpose_notes": True, "send_default_purpose": True, "history_enabled": True,
                    "history_turns": 12, "history_chars": 6000, "turn_chars": 1500}
HISTORY = [{"role": "user" if n % 2 == 0 else "assistant", "text": f"turn {n:02d} " + "z" * 400} for n in range(40)]


@unittest.skipUnless(base.GEOMETRY, "geometry libraries not installed")
class PromptContextTests(unittest.TestCase):
    setUp, tearDown = base.AskTests.setUp, base.AskTests.tearDown
    call, make_project, scenario, ask, poll, finished = (base.AskTests.call, base.AskTests.make_project, base.AskTests.scenario, base.AskTests.ask,
                                                         base.AskTests.poll, base.AskTests.finished)

    def save(self, **values):
        return self.call("POST", "/api/assistant/settings", {"tuning": {"prompt_context": values}})

    def stdin(self, history=None):
        self.assertEqual(self.finished("latest question", history)["state"], "answer")
        return read_record(self.cli_dir)["stdin"]

    def test_defaults_are_todays_numbers_and_the_prompt_is_unchanged(self):
        status, body = self.call("GET", "/api/assistant/settings")
        self.assertEqual((status, body["tuning"]["values"]["prompt_context"]), (200, CONTEXT_DEFAULTS))
        stdin = self.stdin(HISTORY)
        self.assertIn("Project default Purpose:", stdin)
        self.assertIn("Purpose functional (", stdin)
        self.assertIn(base.NOTE.replace('"', '\\"'), stdin)
        self.assertIn("turn 39", stdin)
        self.assertNotIn("turn 27", stdin)                       # 12 turns kept
        _, st = self.call("GET", f"/api/projects/{self.project['id']}/assistant/status")
        self.assertEqual({k: st["limits"][k] for k in ("history_turns", "history_chars", "turn_chars")},
                         {"history_turns": 12, "history_chars": 6000, "turn_chars": 1500})

    def test_purposes_off_leaves_the_model_list_and_framing_but_no_purpose(self):
        self.save(send_purposes=False)
        stdin = self.stdin()
        self.assertIn('Project "Brackets", revision', stdin)
        self.assertIn("Models:", stdin)
        self.assertIn('"Arm bracket"', stdin)
        self.assertIn("Everything inside quotation marks", stdin)
        self.assertNotIn("functional", stdin.lower())
        self.assertNotIn("Purpose functional", stdin)
        self.assertIn("Project default Purpose", stdin, "the default Purpose has its own switch")

    def test_notes_off_drops_every_note_but_keeps_the_purposes(self):
        self.save(send_purpose_notes=False)
        stdin = self.stdin()
        self.assertNotIn("; note", stdin)
        self.assertNotIn("motor", stdin)
        self.assertIn("Purpose functional", stdin)

    def test_default_purpose_off_drops_only_that_line(self):
        self.save(send_default_purpose=False)
        stdin = self.stdin()
        self.assertNotIn("Project default Purpose", stdin)
        self.assertIn("Purpose functional", stdin)
        self.assertIn("Models:", stdin)

    def test_history_off_sends_none_and_tells_the_panel_to_send_none(self):
        self.save(history_enabled=False)
        stdin = self.stdin(HISTORY)
        self.assertNotIn("turn 39", stdin)
        self.assertNotIn("Earlier in this chat", stdin)
        self.assertIn("latest question", stdin)
        _, st = self.call("GET", f"/api/projects/{self.project['id']}/assistant/status")
        self.assertEqual(st["limits"]["history_turns"], 0)

    def test_history_numbers_change_what_is_kept(self):
        self.save(history_turns=2)
        stdin = self.stdin(HISTORY)
        self.assertIn("turn 38", stdin)
        self.assertNotIn("turn 37", stdin)
        self.save(history_turns=50, history_chars=600, turn_chars=100)
        stdin = self.stdin(HISTORY)
        self.assertIn("turn 39", stdin)
        self.assertNotIn("turn 33", stdin, "600 characters at 100 per turn keeps six turns")
        self.assertNotIn("z" * 101, stdin)
        _, st = self.call("GET", f"/api/projects/{self.project['id']}/assistant/status")
        self.assertEqual((st["limits"]["history_turns"], st["limits"]["history_chars"], st["limits"]["turn_chars"]), (50, 600, 100))

    def test_bounds_are_refused_naming_the_bound_and_reset_restores_defaults(self):
        for values, bound in (({"history_turns": 0}, "1"), ({"history_turns": 51}, "50"), ({"history_chars": 199}, "200"),
                              ({"history_chars": 20001}, "20000"), ({"turn_chars": 99}, "100"), ({"turn_chars": 5001}, "5000")):
            status, body = self.save(**values)
            self.assertEqual(status, 400, values)
            self.assertIn(bound, body["error"], values)
        for bad in ("yes", 1, None):
            self.assertEqual(self.save(send_purposes=bad)[0], 400, bad)
        self.assertEqual(self.save(send_purposes=False, history_turns=3)[0], 200)
        status, body = self.call("POST", "/api/assistant/settings", {"reset_tuning": "prompt_context"})
        self.assertEqual((status, body["tuning"]["values"]["prompt_context"]), (200, CONTEXT_DEFAULTS))
        self.assertIn("Purpose functional", self.stdin())
