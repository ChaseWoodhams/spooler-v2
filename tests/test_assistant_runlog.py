"""The Assistant run log, through the app's own routes with the fake `claude`."""
import contextlib
import io
import json
import time
import unittest
from unittest import mock

import assistant
import assistant_runlog
import assistant_runner
from tests import test_assistant_ask as base
from tests.fake_claude import read_record
from tests.test_assistant_proposals import ProposalCase

LOG = "/api/assistant/run-log"


@unittest.skipUnless(base.GEOMETRY, "geometry libraries not installed")
class RunLogTests(unittest.TestCase):
    setUp, tearDown = base.AskTests.setUp, base.AskTests.tearDown
    call, make_project, scenario, ask, poll, finished = (base.AskTests.call, base.AskTests.make_project, base.AskTests.scenario, base.AskTests.ask,
                                                         base.AskTests.poll, base.AskTests.finished)

    def entries(self, expect=None, timeout=10):
        """The log as the Settings page reads it; waits until `expect` entries exist (the entry is written just after the run is published)."""
        end = time.monotonic() + timeout
        while True:
            status, body = self.call("GET", LOG)
            self.assertEqual(status, 200, body)
            if expect is None or len(body["entries"]) >= expect or time.monotonic() > end:
                return body["entries"]
            time.sleep(0.05)

    def test_an_empty_log_answers_an_empty_list_and_a_cap(self):
        status, body = self.call("GET", LOG)
        self.assertEqual((status, body["entries"]), (200, []))
        self.assertEqual(body["cap"], assistant_runlog.CAP)

    def test_an_answered_run_is_logged_with_everything_the_page_shows(self):
        self.finished("Why two plates?")
        entry, = self.entries(1)
        self.assertEqual((entry["outcome"], entry["project_id"], entry["project_name"], entry["question"]),
                         ("answered", self.project["id"], "Brackets", "Why two plates?"))
        self.assertEqual((entry["model"], entry["tool_calls"], entry["steps"], entry["proposals"]), ("fake-model", 1, ["Reading project"], []))
        self.assertIsNone(entry["reason"])
        self.assertGreaterEqual(entry["duration_seconds"], 0)
        self.assertTrue(entry["time"])
        self.assertEqual(entry["tuning"], self.call("GET", "/api/assistant/settings")[1]["tuning"]["values"], "the tuning in force is the whole snapshot")

    def test_the_logged_prompt_is_exactly_what_the_cli_received(self):
        self.finished("Why two plates?")
        entry, = self.entries(1)
        self.assertEqual(entry["prompt"], read_record(self.cli_dir)["stdin"])
        self.assertIn("Why two plates?", entry["prompt"])

    def test_the_logged_system_prompt_is_exactly_the_text_the_cli_was_given(self):
        self.call("POST", "/api/assistant/settings", {"tuning": {"instructions": {"text": "Be terse."}, "model": {"name": "my-model"},
                                                                  "proposal_kinds": {"plan": False}}})
        self.finished("Why two plates?")
        entry, = self.entries(1)
        record = read_record(self.cli_dir)
        self.assertEqual(entry["system_prompt"], record["system_prompt"])
        self.assertTrue(entry["system_prompt"].startswith("Be terse."))
        self.assertIn("At most 3 Proposals per run.", entry["system_prompt"])
        self.assertIn("Plan plates", entry["system_prompt"], "the fixed block is part of it")
        self.assertEqual(entry["prompt"], record["stdin"], "the question prompt is still logged apart from it")

    def test_a_failed_run_is_logged_with_the_reason_shown_to_the_person(self):
        self.scenario({"calls": [], "no_result": True, "exit_code": 3})
        view = self.finished()
        entry, = self.entries(1)
        self.assertEqual((entry["outcome"], entry["reason"]), ("failed", view["error"]))
        self.assertIn("exit code 3", entry["reason"])

    def test_a_cancelled_run_is_logged_with_its_reason(self):
        self.scenario({"calls": [{"tool": "get_project"}], "hang_after_init": 60})
        status, run = self.ask()
        while not (read_record_safe(self.cli_dir) or {}).get("server_pid"):
            time.sleep(0.05)
        self.call("POST", f"/api/projects/{run['project_id']}/assistant/runs/{run['id']}/cancel")
        entry, = self.entries(1)
        self.assertEqual((entry["outcome"], entry["reason"]), ("cancelled", assistant_runner.CANCELLED_MESSAGE))

    def test_a_timed_out_run_is_logged_with_its_reason(self):
        self.scenario({"calls": [{"tool": "get_project"}], "hang_after_init": 60})
        self.call("POST", "/api/assistant/settings", {"tuning": {"limits": {"deadline_seconds": 5}}})
        view = self.poll(self.ask()[1])
        entry, = self.entries(1)
        self.assertEqual((view["state"], entry["outcome"], entry["reason"]), ("timeout", "timed out", assistant_runner.timeout_message(5)))
        self.assertEqual(entry["tuning"]["limits"]["deadline_seconds"], 5)

    def test_the_log_is_newest_first_and_capped(self):
        with mock.patch.object(assistant_runlog, "CAP", 3):
            for number in range(5):
                self.finished(f"Question {number}")
                self.entries(min(number + 1, 3))
                time.sleep(0.05)
            entries = self.entries(3)
        self.assertEqual([e["question"] for e in entries], ["Question 4", "Question 3", "Question 2"])

    def test_the_log_survives_a_restart_and_clear_empties_it(self):
        self.finished("Remember me")
        self.entries(1)
        assistant_runner.reset()                       # a restart forgets every run in memory; the log is a file
        entry, = self.entries(1)
        self.assertEqual(entry["question"], "Remember me")
        self.assertTrue(assistant_runlog.log_file().is_file())
        status, body = self.call("POST", LOG + "/clear")
        self.assertEqual((status, body["entries"]), (200, []))
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.call("GET", LOG)[1]["entries"], [])

    def test_a_damaged_log_file_reads_as_empty_and_the_next_run_starts_a_new_log(self):
        assistant_runlog.log_file().write_text("{not json", encoding="utf-8")
        self.assertEqual(self.entries(), [])
        self.finished("After damage")
        self.assertEqual([e["question"] for e in self.entries(1)], ["After damage"])

    def test_the_one_line_server_log_still_appears(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.finished()
            self.entries(1)
        self.assertRegex(out.getvalue(), r"\[Assistant\] time=.* project=\d+ duration=.* state=answer tool_calls=1 proposals=none")

    def test_a_log_write_failure_never_stops_the_run_from_ending(self):
        with mock.patch.object(assistant_runlog, "record", side_effect=OSError("disk full")):
            self.assertEqual(self.finished()["state"], "answer")

    def test_the_log_routes_answer_json_for_unknown_paths(self):
        self.assertEqual(self.call("POST", LOG + "/nope")[0], 404)


class RunLogProposalTests(ProposalCase):
    def test_a_run_that_made_a_proposal_is_logged_as_a_proposal_with_its_token(self):
        view = self.run_scenario([self.propose_call([1, 2, 3])])
        end = time.monotonic() + 10
        while time.monotonic() < end:
            status, body = self.call("GET", LOG)
            if body["entries"]:
                break
            time.sleep(0.05)
        entry, = body["entries"]
        self.assertEqual((view["state"], entry["outcome"], entry["proposals"]), ("proposal", "proposal", [p["token"] for p in view["proposals"]]))
        self.assertEqual(len(entry["proposals"]), 1)
        self.assertEqual(entry["tool_calls"], 1)


def read_record_safe(folder):
    try:
        return read_record(folder)
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    unittest.main()
