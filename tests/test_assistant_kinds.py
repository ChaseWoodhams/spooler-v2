"""Assistant tuning: Proposal kinds on or off, through the app's routes with the fake `claude`, and the tool server over stdio."""
import json
import sys
import unittest

import assistant_runner
import spooler_mcp
from tests import test_assistant_ask as base
from tests.fake_claude import read_record
from tests.test_spooler_mcp import McpSession, SERVER

KINDS = ("orientation", "arrange", "plan", "settings")
READ = [n for n in spooler_mcp.TOOL_NAMES if not n.startswith("propose_")]


def tool_of(kind):
    return f"mcp__spooler__propose_{kind}"


@unittest.skipUnless(base.GEOMETRY, "geometry libraries not installed")
class ProposalKindTests(unittest.TestCase):
    setUp, tearDown = base.AskTests.setUp, base.AskTests.tearDown
    call, make_project, scenario, ask, poll, finished = (base.AskTests.call, base.AskTests.make_project, base.AskTests.scenario, base.AskTests.ask,
                                                         base.AskTests.poll, base.AskTests.finished)

    def save(self, **kinds):
        return self.call("POST", "/api/assistant/settings", {"tuning": {"proposal_kinds": kinds}})

    def record(self):
        return read_record(self.cli_dir)

    def allowed(self):
        argv = self.record()["argv"]
        start = argv.index("--allowedTools") + 1
        end = next(i for i in range(start, len(argv)) if argv[i].startswith("--"))
        return argv[start:end]

    def test_everything_is_on_by_default_and_a_run_equals_today(self):
        body = self.call("GET", "/api/assistant/settings")[1]["tuning"]
        self.assertEqual(body["values"]["proposal_kinds"], {k: True for k in KINDS})
        self.assertEqual(body["defaults"]["proposal_kinds"], {k: True for k in KINDS})
        self.assertEqual(self.finished()["state"], "answer")
        self.assertEqual(self.allowed(), list(assistant_runner.ALLOWED_TOOLS))
        self.assertNotIn("switched off", self.record()["system_prompt"])
        self.assertEqual(len(self.record()["init"]["tools"]), 11)

    def test_a_switched_off_kind_is_not_allowed_not_listed_and_named_in_the_fixed_block(self):
        self.assertEqual(self.save(arrange=False, plan=False)[0], 200)
        self.assertEqual(self.finished()["state"], "answer")
        record = self.record()
        self.assertEqual(self.allowed(), [t for t in assistant_runner.ALLOWED_TOOLS if not t.endswith(("propose_arrange", "propose_plan"))])
        self.assertEqual(sorted(record["init"]["tools"]), sorted(self.allowed()))
        self.assertNotIn(tool_of("arrange"), record["init"]["tools"])
        prompt = record["system_prompt"]
        fixed = prompt[prompt.index("Fixed by Spooler"):]
        self.assertIn("Arrange", fixed)
        self.assertIn("Plan plates", fixed)
        self.assertNotIn("Orientation Proposals", fixed)
        self.assertIn("switched off", fixed)

    def test_reading_tools_still_work_with_every_kind_off(self):
        self.save(**{k: False for k in KINDS})
        self.scenario({"calls": [{"tool": "get_project"}], "answer": "Read only."})
        view = self.finished()
        self.assertEqual((view["state"], view["answer"]), ("answer", "Read only."))
        self.assertEqual(self.allowed(), [f"mcp__spooler__{n}" for n in READ])
        self.assertFalse(self.record()["tool_results"][0]["is_error"])

    def test_a_call_to_a_switched_off_kind_is_refused_naming_the_setting_and_stages_nothing(self):
        self.save(orientation=False)
        self.scenario({"calls": [{"tool": "propose_orientation", "arguments": {"revision": 1}}], "answer": "Cannot."})
        self.assertEqual(self.finished()["proposals"], [])
        result = self.record()["tool_results"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("Proposal kinds", result["text"])
        self.assertIn("orientation", result["text"])

    def test_a_non_bool_value_is_refused(self):
        self.assertEqual(self.save(plan="no")[0], 400)
        self.assertEqual(self.save(bogus=False)[0], 400)

    def test_reset_returns_all_kinds_on(self):
        self.save(settings=False)
        body = self.call("POST", "/api/assistant/settings", {"reset_tuning": "proposal_kinds"})[1]
        self.assertEqual(body["tuning"]["values"]["proposal_kinds"], {k: True for k in KINDS})


@unittest.skipUnless(base.GEOMETRY, "geometry libraries not installed")
class ToolServerKindTests(unittest.TestCase):
    setUp, tearDown = base.AskTests.setUp, base.AskTests.tearDown
    call, make_project, scenario = base.AskTests.call, base.AskTests.make_project, base.AskTests.scenario

    def session(self, *extra):
        command = [sys.executable, str(SERVER), "--project", str(self.project["id"]), "--data-dir", str(self.dir), *extra]
        s = McpSession(self.dir, self.project["id"], command=command)
        self.addCleanup(lambda: (s.proc.kill(), s.proc.wait(), s.proc.stdin.close(), s.proc.stdout.close(), s.proc.stderr.close()))
        s.start()
        return s

    def names(self, s):
        return [t["name"] for t in s.request("tools/list")["result"]["tools"]]

    def test_flag_hides_and_refuses_a_kind(self):
        s = self.session("--kinds-off", "plan,settings")
        names = self.names(s)
        self.assertNotIn("propose_plan", names)
        self.assertNotIn("propose_settings", names)
        self.assertIn("propose_arrange", names)
        self.assertIn("get_project", names)
        reply = s.call("propose_plan", {"revision": 1})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("Proposal kinds", reply["result"]["content"][0]["text"])
        self.assertFalse(s.call("get_project")["result"]["isError"])

    def test_terminal_session_reads_the_saved_switches_from_the_data_dir(self):
        self.call("POST", "/api/assistant/settings", {"tuning": {"proposal_kinds": {"orientation": False}}})
        names = self.names(self.session())
        self.assertNotIn("propose_orientation", names)
        self.assertEqual(len(names), 10)
        reply = self.session().call("propose_orientation", {"revision": 1})
        self.assertTrue(reply["result"]["isError"])

    def test_terminal_session_with_nothing_saved_lists_all_eleven(self):
        self.assertEqual(sorted(self.names(self.session())), sorted(spooler_mcp.TOOL_NAMES))

    def test_a_damaged_settings_file_leaves_everything_on(self):
        (self.dir / "assistant_settings.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(len(self.names(self.session())), 11)

    def test_a_terminal_session_applies_the_apps_rule_unknown_version_or_damaged_section_means_all_kinds_on(self):
        path = self.dir / "assistant_settings.json"
        off = {"orientation": False}
        for tuning in ({"version": 2, "proposal_kinds": off}, {"version": 1, "proposal_kinds": 5}, "yes", {"version": "1", "proposal_kinds": off}):
            path.write_text(json.dumps({"tuning": tuning}), encoding="utf-8")
            self.assertEqual(len(self.names(self.session())), 11, tuning)
        path.write_text(json.dumps({"tuning": {"version": 1, "proposal_kinds": {"orientation": False, "plan": "no"}}}), encoding="utf-8")
        names = self.names(self.session())
        self.assertNotIn("propose_orientation", names)
        self.assertIn("propose_plan", names, "a value that is not on or off falls back to on, as in the app")
        path.write_text(json.dumps({"tuning": {"proposal_kinds": off}}), encoding="utf-8")
        self.assertNotIn("propose_orientation", self.names(self.session()), "no version written means the current one, as in the app")


if __name__ == "__main__":
    unittest.main()
