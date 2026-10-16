"""The Settings Preview, through the app's routes with the fake `claude`: it equals what the next run sends and starts nothing."""
import unittest
from pathlib import Path

import assistant_runner
from tests import test_assistant_ask as base
from tests.fake_claude import RECORD, read_record


@unittest.skipUnless(base.GEOMETRY, "geometry libraries not installed")
class PreviewTests(unittest.TestCase):
    setUp, tearDown = base.AskTests.setUp, base.AskTests.tearDown
    call, make_project, scenario, ask, poll, finished = (base.AskTests.call, base.AskTests.make_project, base.AskTests.scenario, base.AskTests.ask,
                                                         base.AskTests.poll, base.AskTests.finished)

    def preview(self, message="Make the brackets stronger", history=None, project=None):
        project = project or self.project
        return self.call("POST", f"/api/projects/{project['id']}/assistant/preview", {"message": message, "history": history or []})

    def save(self, tuning):
        status, body = self.call("POST", "/api/assistant/settings", {"tuning": tuning})
        self.assertEqual(status, 200, body)

    def test_the_preview_equals_what_the_next_run_sends(self):
        self.save({"instructions": {"text": "Answer in one word."}, "prompt_context": {"send_purpose_notes": False, "send_default_purpose": False},
                   "limits": {"proposals_per_run": 2}})
        history = [{"role": "user", "text": "Hi"}, {"role": "assistant", "text": "Hello"}]
        status, preview = self.preview(history=history)
        self.assertEqual(status, 200, preview)
        self.finished(history=history)
        record = read_record(self.cli_dir)
        self.assertEqual(preview["instructions"], record["system_prompt"])
        self.assertEqual(preview["prompt"], record["stdin"])
        self.assertIn("Answer in one word.", preview["instructions"])
        self.assertIn("At most 2 Proposals per run.", preview["instructions"])
        self.assertNotIn(base.NOTE, preview["prompt"])

    def test_the_built_in_instructions_and_default_context_match_a_run(self):
        status, preview = self.preview("Why two plates?")
        self.assertEqual(status, 200, preview)
        self.finished("Why two plates?")
        record = read_record(self.cli_dir)
        self.assertEqual((preview["instructions"], preview["prompt"]), (record["system_prompt"], record["stdin"]))

    def test_history_off_leaves_it_out_of_the_preview_like_a_run(self):
        self.save({"prompt_context": {"history_enabled": False}})
        history = [{"role": "user", "text": "EARLIER-TURN"}]
        status, preview = self.preview(history=history)
        self.assertNotIn("EARLIER-TURN", preview["prompt"])
        self.finished(history=history)
        self.assertEqual(preview["prompt"], read_record(self.cli_dir)["stdin"])

    def test_a_preview_starts_nothing(self):
        before = dict(assistant_runner._runs)
        status, preview = self.preview()
        self.assertEqual(status, 200, preview)
        self.assertFalse((Path(self.cli_dir) / RECORD).exists(), "the fake claude was never started")
        self.assertEqual(assistant_runner._runs, before)
        self.assertEqual(assistant_runner._active, {})
        self.assertEqual(self.call("GET", "/api/assistant/run-log")[1]["entries"], [])

    def test_the_preview_follows_saved_settings_only(self):
        _, first = self.preview()
        rejected = self.call("POST", "/api/assistant/settings", {"tuning": {"instructions": {"text": "x" * 9000}}})
        self.assertEqual(rejected[0], 400)
        self.assertEqual(self.preview()[1], first, "a refused save changes nothing")
        self.save({"instructions": {"text": "New words."}})
        self.assertIn("New words.", self.preview()[1]["instructions"])

    def test_a_blank_question_or_unknown_project_is_refused(self):
        self.assertEqual(self.preview("  ")[0], 400)
        self.assertEqual(self.call("POST", "/api/projects/99999/assistant/preview", {"message": "hi"})[0], 404)

    def test_the_question_length_limit_applies(self):
        self.save({"limits": {"question_chars": 100}})
        self.assertEqual(self.preview("x" * 101)[0], 400)


if __name__ == "__main__":
    unittest.main()
