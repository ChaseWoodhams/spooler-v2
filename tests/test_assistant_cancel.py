"""Cancel, the 60 s deadline and failure states of an Assistant run, through the project API with the fake `claude`."""
import contextlib
import io
import subprocess
import time
import unittest
from unittest import mock

import assistant_runner
import datapaths
import proposal_store
from tests.fake_claude import read_record
from tests import test_assistant_ask as base          # a module, so its tests are not collected twice


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out.split()


@unittest.skipUnless(base.GEOMETRY, "geometry libraries not installed")
class CancelTests(unittest.TestCase):
    setUp, tearDown = base.AskTests.setUp, base.AskTests.tearDown
    call, make_project, scenario, ask, poll, finished = (base.AskTests.call, base.AskTests.make_project, base.AskTests.scenario, base.AskTests.ask,
                                                         base.AskTests.poll, base.AskTests.finished)

    def cancel(self, run):
        return self.call("POST", f"/api/projects/{run['project_id']}/assistant/runs/{run['id']}/cancel")

    def wait_for_server(self, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                record = read_record(self.cli_dir)
                if record.get("server_pid"):
                    return record
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        self.fail("the fake claude never started its MCP server")

    def assert_nothing_left(self, record, run):
        for pid in (record["pid"], record["server_pid"]):
            end = time.monotonic() + 5
            while pid_alive(pid) and time.monotonic() < end:
                time.sleep(0.1)
            self.assertFalse(pid_alive(pid), f"process {pid} is still running")
        self.assertFalse((assistant_runner.run_root() / run["id"]).exists(), "the run folder is gone")

    def revision(self):
        return self.call("GET", f"/api/projects/{self.project['id']}")[1]["revision"]

    # ── the deadline ──
    def test_the_deadline_ends_a_stalled_run_as_timeout_and_leaves_no_process(self):
        self.scenario({"calls": [{"tool": "get_project"}], "hang_after_init": 60, "answer": "partial"})
        before = self.revision()
        self.call("POST", "/api/assistant/settings", {"tuning": {"limits": {"deadline_seconds": 5}}})
        status, run = self.ask()
        record = self.wait_for_server()
        view = self.poll(run)
        self.assertEqual(status, 200)
        self.assertEqual(view["state"], "timeout")
        self.assertIsNone(view["answer"])
        self.assertIn("nothing was changed", view["error"].lower())
        self.assert_nothing_left(record, run)
        self.assertEqual(self.revision(), before)
        self.scenario({"calls": [], "answer": "back"})                     # the project is free again
        self.assertEqual(self.finished()["answer"], "back")

    def test_the_default_deadline_is_sixty_seconds_and_the_notice_says_so(self):
        _, body = self.call("GET", f"/api/projects/{self.project['id']}/assistant/status")
        self.assertEqual(body["limits"]["deadline_seconds"], 60)
        self.assertEqual(assistant_runner.timeout_message(60), "No answer in 60 s; nothing was changed")

    # ── cancel ──
    def test_cancel_ends_the_run_at_once_discards_partial_output_and_leaves_no_process(self):
        self.scenario({"calls": [{"tool": "get_project"}], "hang_after_init": 60, "answer": "partial answer"})
        before = self.revision()
        status, run = self.ask()
        self.assertEqual(status, 200)
        record = self.wait_for_server()
        started = time.monotonic()
        status, view = self.cancel(run)
        self.assertEqual(status, 200, view)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(view["state"], "cancelled")
        self.assertIsNone(view["answer"])
        self.assertEqual(view["error"], "Cancelled; nothing was changed")
        self.assertEqual(self.poll(run)["state"], "cancelled")
        self.assert_nothing_left(record, run)
        self.assertEqual(self.revision(), before)
        self.scenario({"calls": [], "answer": "again"})
        self.assertEqual(self.finished()["answer"], "again")

    def test_cancelling_a_finished_or_unknown_run_changes_nothing(self):
        status, run = self.ask()
        view = self.poll(run)
        self.assertEqual(view["state"], "answer")
        status, body = self.cancel(view)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "answer")
        self.assertEqual(body["answer"], view["answer"])
        status, _ = self.cancel({"project_id": self.project["id"], "id": "nope"})
        self.assertEqual(status, 404)

    def test_a_run_of_another_project_cannot_be_cancelled_through_this_one(self):
        self.scenario({"calls": [], "hang_after_init": 3})
        status, run = self.ask()
        other = self.make_project("Other", ["only.stl"])
        status, _ = self.cancel({"project_id": other["id"], "id": run["id"]})
        self.assertEqual(status, 404)
        self.cancel(run)

    # ── failure states ──
    def test_a_rate_limit_result_is_usage_limit(self):
        for text in ("You've hit your usage limit. Resets at 5pm", "API Error: 429 rate_limit_error: Number of request tokens has exceeded"):
            self.scenario({"calls": [], "result": {"is_error": True, "subtype": "success", "result": text}})
            view = self.finished()
            self.assertEqual(view["state"], "usage_limit", text)
            self.assertEqual(view["error"], "AI unavailable: usage limit reached")
            self.assertIsNone(view["answer"])

    def test_other_failures_stay_errors_in_plain_words(self):
        self.scenario({"calls": [], "no_result": True, "exit_code": 3})
        view = self.finished()
        self.assertEqual((view["state"], bool(view["error"])), ("error", True))
        self.scenario({"calls": [], "result": {"is_error": True, "subtype": "error_during_execution", "result": "Something broke"}})
        self.assertEqual(self.finished()["state"], "error")

    # ── the log line ──
    def test_each_run_writes_one_log_line_with_no_prompt_answer_or_tool_output(self):
        self.scenario({"calls": [{"tool": "get_project"}], "answer": "SECRET-ANSWER-TEXT"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            view = self.finished("SECRET-QUESTION-TEXT please")
            time.sleep(0.2)
        lines = [line for line in out.getvalue().splitlines() if line.startswith("[Assistant]")]
        self.assertEqual(len(lines), 1, out.getvalue())
        line = lines[0]
        for part in (f"project={self.project['id']}", "state=answer", "tool_calls=1", "duration=", "proposals=", "time="):
            self.assertIn(part, line)
        for text in ("SECRET-ANSWER-TEXT", "SECRET-QUESTION-TEXT", "Brackets", "Arm bracket"):
            self.assertNotIn(text, out.getvalue())
        self.assertEqual(view["state"], "answer")

    def test_a_cancelled_run_and_a_timed_out_run_each_log_exactly_once(self):
        self.scenario({"calls": [], "hang_after_init": 60})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status, run = self.ask()
            self.wait_for_server()
            self.cancel(run)
            self.assertEqual(self.poll(run)["state"], "cancelled")
            time.sleep(0.3)
            self.call("POST", "/api/assistant/settings", {"tuning": {"limits": {"deadline_seconds": 5}}})
            self.assertEqual(self.finished()["state"], "timeout")
            time.sleep(0.3)
        lines = [line for line in out.getvalue().splitlines() if line.startswith("[Assistant]")]
        self.assertEqual(len(lines), 2, out.getvalue())
        self.assertIn("state=cancelled", lines[0])
        self.assertIn("state=timeout", lines[1])

    # ── staged Proposals ──
    def test_a_run_that_did_not_end_in_a_proposal_has_its_staged_proposals_discarded(self):
        self.scenario({"calls": [], "hang_after_init": 60})
        status, run = self.ask()
        self.wait_for_server()
        root = datapaths.projects_dir()
        proposal_store.write(root, {"project_id": self.project["id"], "run": run["id"], "kind": "orientation", "base_revision": 1})
        self.assertEqual(len(proposal_store.listing(root, run=run["id"])), 1)
        self.cancel(run)
        self.assertEqual(self.poll(run)["state"], "cancelled")
        self.assertEqual(proposal_store.listing(root, run=run["id"]), [])


if __name__ == "__main__":
    unittest.main()
