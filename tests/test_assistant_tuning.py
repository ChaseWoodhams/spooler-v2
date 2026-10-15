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

    # ── saved limits reach the next run ──
    def test_saved_values_are_read_back_and_survive_a_connection_settings_save(self):
        status, body = self.save(max_turns=7)
        self.assertEqual((status, body["tuning"]["values"]["limits"]), (200, {**LIMIT_DEFAULTS, "max_turns": 7}))
        self.assertEqual(self.call("POST", "/api/assistant/settings", {"jev_url": "http://127.0.0.1:9999"})[0], 200)
        self.assertEqual(self.values()["max_turns"], 7, "saving the connection does not drop the tuning")
        self.save(deadline_seconds=90)
        self.assertEqual(self.call("GET", "/api/assistant/settings")[1]["jev_url"], "http://127.0.0.1:9999", "nor the other way round")
        self.assertEqual(self.values(), {**LIMIT_DEFAULTS, "max_turns": 7, "deadline_seconds": 90})

    def test_the_saved_turn_cap_reaches_the_cli_of_the_next_run(self):
        self.save(max_turns=7)
        self.assertEqual(self.finished()["state"], "answer")
        self.assertEqual(self.argv_after("--max-turns"), "7")

    def test_the_saved_question_length_is_enforced_and_served_to_the_panel(self):
        self.save(question_chars=100)
        status, body = self.ask("x" * 101)
        self.assertEqual(status, 400)
        self.assertIn("100", body["error"])
        self.assertEqual(self.ask("x" * 100)[0], 200)
        _, status_body = self.call("GET", f"/api/projects/{self.project['id']}/assistant/status")
        self.assertEqual(status_body["limits"]["question_chars"], 100)

    def test_the_saved_deadline_stops_a_stalled_run_and_the_notice_and_panel_limits_follow_it(self):
        self.save(deadline_seconds=5)
        _, status_body = self.call("GET", f"/api/projects/{self.project['id']}/assistant/status")
        self.assertEqual(status_body["limits"]["deadline_seconds"], 5)
        self.scenario({"calls": [], "hang_after_init": 60})
        view = self.finished()
        self.assertEqual((view["state"], view["error"]), ("timeout", "No answer in 5 s; nothing was changed"))

    def test_the_saved_proposal_cap_is_enforced_by_the_tool_server(self):
        self.save(proposals_per_run=1)
        revision = self.project["revision"]
        self.scenario({"calls": [{"tool": "propose_arrange", "arguments": {"revision": revision}},
                                 {"tool": "propose_orientation", "arguments": {"revision": revision, "object_ids": [1, 2]}}], "answer": "done"})
        self.finished()
        first, second = read_record(self.cli_dir)["tool_results"]
        self.assertFalse(first["is_error"], first["text"])
        self.assertTrue(second["is_error"])
        self.assertIn("At most 1 Proposals per run", second["text"])

    def test_a_run_already_working_keeps_the_values_it_started_with(self):
        self.scenario({"calls": [], "hang_after_init": 4, "answer": "slow"})
        status, run = self.ask()
        self.assertEqual(status, 200)
        deadline = time.monotonic() + 20
        while not (self.cli_dir / "record.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.save(deadline_seconds=5, max_turns=3, question_chars=100)                  # edited while the run works
        view = self.poll(run)
        self.assertEqual((view["state"], view["answer"]), ("answer", "slow"))
        self.assertEqual(self.argv_after("--max-turns"), "12")
        self.assertEqual(self.finished()["state"], "answer")
        self.assertEqual(self.argv_after("--max-turns"), "3", "the next run takes the new value")

    # ── reset, damage and unknown paths ──
    def test_reset_returns_one_group_or_everything_to_the_defaults_and_keeps_the_connection(self):
        self.call("POST", "/api/assistant/settings", {"jev_url": "http://127.0.0.1:9999"})
        self.save(max_turns=7, deadline_seconds=90)
        status, body = self.call("POST", "/api/assistant/settings", {"reset_tuning": "limits"})
        self.assertEqual((status, body["tuning"]["values"]["limits"], body["jev_url"]), (200, LIMIT_DEFAULTS, "http://127.0.0.1:9999"))
        self.finished()
        self.assertEqual(self.argv_after("--max-turns"), "12")
        self.save(max_turns=7)
        self.assertEqual(self.call("POST", "/api/assistant/settings", {"reset_tuning": "all"})[1]["tuning"]["values"]["limits"], LIMIT_DEFAULTS)
        self.assertEqual(self.call("POST", "/api/assistant/settings", {"reset_tuning": "nope"})[0], 400)
        self.assertEqual(self.call("POST", "/api/assistant/settings", {"reset_tuning": "limits", "tuning": {"limits": {"max_turns": 9}}})[1]
                         ["tuning"]["values"]["limits"]["max_turns"], 9, "a reset is applied before the values sent with it")

    def test_a_damaged_or_hand_edited_file_falls_back_per_key_with_a_note_and_never_raises(self):
        path = assistant.data_dir() / assistant.SETTINGS_NAME
        cli = assistant.load_settings()["cli_path"]                  # keep the fake CLI: an empty path would find a real claude on PATH
        path.write_text(json.dumps({"cli_path": cli, "tuning": {"version": 1, "limits": {"max_turns": 500, "deadline_seconds": 90,
                                                                                           "question_chars": "lots", "mystery": 1}, "other": {}}}), encoding="utf-8")
        status, body = self.call("GET", "/api/assistant/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body["tuning"]["values"]["limits"], {**LIMIT_DEFAULTS, "deadline_seconds": 90})
        notes = " ".join(body["tuning"]["notes"])
        self.assertEqual(len(body["tuning"]["notes"]), 2, notes)
        self.assertIn("CLI turn cap", notes)
        self.assertIn("Longest question", notes)
        self.assertEqual(self.finished()["state"], "answer", "runs go on with the usable values")
        for text in ("{not json", "[]", json.dumps({"tuning": "yes"}), json.dumps({"tuning": {"version": 2, "limits": {"max_turns": 3}}}),
                     json.dumps({"tuning": {"version": 1, "limits": 5}})):
            path.write_text(text, encoding="utf-8")
            status, body = self.call("GET", "/api/assistant/settings")
            self.assertEqual((status, body["tuning"]["values"]["limits"]), (200, LIMIT_DEFAULTS), text)
            self.assertEqual(bool(body["tuning"]["notes"]), text not in ("{not json", "[]"), text)
        self.assertEqual(self.save(max_turns=4)[0], 200, "a save repairs a damaged file")
        self.assertEqual(self.values()["max_turns"], 4)

    def test_unknown_assistant_paths_answer_json_not_html(self):
        import urllib.error
        import urllib.request
        for method, path in (("GET", "/api/assistant/nope"), ("GET", "/api/assistant/settings/extra"), ("POST", "/api/assistant/nope"),
                             ("GET", "/api/assistant/tuning/preview")):
            request = urllib.request.Request(self.base + path, method=method, data=b"{}" if method == "POST" else None,
                                             headers={"Accept": "text/html", "Content-Type": "application/json"})
            try:
                base.AskTests  # noqa: B018 (keep the module import used)
                with base._opener.open(request, timeout=30) as resp:
                    self.fail(f"{method} {path} answered {resp.status}")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404, path)
                self.assertTrue(e.headers["Content-Type"].startswith("application/json"), (path, e.headers["Content-Type"]))
                self.assertIn("error", json.loads(e.read()))


@unittest.skipUnless(base.GEOMETRY, "geometry libraries not installed")
class TuningModelTests(unittest.TestCase):
    setUp, tearDown = base.AskTests.setUp, base.AskTests.tearDown
    call, make_project, scenario, ask, poll, finished = (base.AskTests.call, base.AskTests.make_project, base.AskTests.scenario, base.AskTests.ask,
                                                         base.AskTests.poll, base.AskTests.finished)

    def save(self, **model):
        return self.call("POST", "/api/assistant/settings", {"tuning": {"model": model}})

    def argv(self):
        return read_record(self.cli_dir)["argv"]

    def test_blank_by_default_and_the_cli_gets_no_model_option(self):
        status, body = self.call("GET", "/api/assistant/settings")
        self.assertEqual((status, body["tuning"]["values"]["model"]), (200, {"name": ""}))
        self.assertEqual(body["tuning"]["groups"]["model"]["fields"]["name"]["type"], "str")
        self.assertEqual(self.finished()["state"], "answer")
        self.assertNotIn("--model", self.argv())

    def test_a_saved_model_name_is_passed_to_the_cli_unchecked_and_blank_removes_it(self):
        self.assertEqual(self.save(name="  claude-sonnet-4-5 ")[1]["tuning"]["values"]["model"]["name"], "claude-sonnet-4-5")
        self.assertEqual(self.finished()["state"], "answer")
        argv = self.argv()
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-4-5")
        self.save(name="whatever-not-in-any-list")
        self.finished()
        self.assertIn("whatever-not-in-any-list", self.argv())
        self.save(name="")
        self.finished()
        self.assertNotIn("--model", self.argv())

    def test_a_name_that_is_not_text_a_single_word_or_is_too_long_is_refused_and_nothing_is_saved(self):
        for bad in (5, True, None, "two words", "x" * 101, "-p", "a\nb"):
            self.assertEqual(self.save(name=bad)[0], 400, repr(bad))
        self.assertEqual(self.call("GET", "/api/assistant/settings")[1]["tuning"]["values"]["model"]["name"], "")

    def test_a_model_the_cli_rejects_ends_the_run_with_the_cli_message_and_changes_nothing(self):
        self.save(name="no-such-model")
        self.scenario({"calls": [], "reject_model": "no-such-model"})
        revision = self.call("GET", f"/api/projects/{self.project['id']}")[1].get("revision")
        view = self.finished()
        self.assertEqual(view["state"], "error")
        self.assertIn("issue with the selected model (no-such-model)", view["error"])
        self.assertEqual(self.call("GET", f"/api/projects/{self.project['id']}")[1].get("revision"), revision)
        self.save(name="")
        self.scenario({"calls": [], "answer": "back", "reject_model": "no-such-model"})
        self.assertEqual(self.finished()["answer"], "back")

    def test_reset_model_returns_to_blank_and_leaves_limits(self):
        self.save(name="m1")
        self.call("POST", "/api/assistant/settings", {"tuning": {"limits": {"max_turns": 7}}})
        body = self.call("POST", "/api/assistant/settings", {"reset_tuning": "model"})[1]
        self.assertEqual((body["tuning"]["values"]["model"], body["tuning"]["values"]["limits"]["max_turns"]), ({"name": ""}, 7))
        self.finished()
        self.assertNotIn("--model", self.argv())


if __name__ == "__main__":
    unittest.main()
