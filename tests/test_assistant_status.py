"""Assistant availability and its Settings, seen through the HTTP API. The Jev check is faked; no real CLI runs."""
import copy
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import assistant
import assistant_runner
import database
import jobs
import projects
import slicer
import splits
from tests.test_slicer import make_fake_orca

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AssistantStatusTests(unittest.TestCase):
    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        splits.configure(self.dir)
        projects.configure(self.dir)
        projects.init()
        assistant.configure(self.dir)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.project = projects.create_project("Assist")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        assistant.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def status(self, fresh=False):
        return self.call("GET", f"/api/projects/{self.project['id']}/assistant/status" + ("?fresh=1" if fresh else ""))

    def use_cli(self, path=sys.executable):
        self.call("POST", "/api/assistant/settings", {"cli_path": path})

    def test_available_when_the_cli_resolves_and_jev_answers_ok(self):
        self.use_cli()
        with mock.patch.object(assistant, "probe_jev", return_value=None):
            code, body = self.status()
        self.assertEqual(code, 200)
        self.assertTrue(body["available"])
        self.assertIsNone(body["reason"])

    def test_the_status_carries_the_limits_the_panel_shows_so_it_does_not_hard_code_them(self):
        self.use_cli()
        self.call("POST", "/api/assistant/settings", {"tuning": {"limits": {"deadline_seconds": 7}}})
        with mock.patch.object(assistant, "probe_jev", return_value=None):
            _, body = self.status()
        self.assertEqual(body["limits"], {"deadline_seconds": 7, "question_chars": 2000, "history_turns": assistant_runner.MAX_HISTORY_TURNS,
                                          "history_chars": assistant_runner.MAX_HISTORY_CHARS, "turn_chars": assistant_runner.MAX_TURN_CHARS})

    def test_a_missing_cli_is_unavailable_with_the_reason(self):
        self.use_cli(str(self.dir / "nope" / "claude.exe"))
        with mock.patch.object(assistant, "probe_jev", return_value=None):
            code, body = self.status()
        self.assertEqual(code, 200)
        self.assertFalse(body["available"])
        self.assertIn("CLI", body["reason"])

    def test_no_setting_and_nothing_on_path_is_unavailable(self):
        with mock.patch.object(assistant.shutil, "which", return_value=None), \
                mock.patch.object(assistant, "probe_jev", return_value=None):
            _, body = self.status()
        self.assertFalse(body["available"])
        self.assertIn("PATH", body["reason"])

    def test_the_cli_falls_back_to_path(self):
        with mock.patch.object(assistant.shutil, "which", return_value=sys.executable), \
                mock.patch.object(assistant, "probe_jev", return_value=None):
            _, body = self.status()
        self.assertTrue(body["available"])

    def test_a_failed_jev_check_is_unavailable_and_other_routes_still_answer(self):
        self.use_cli()
        with mock.patch.object(assistant, "probe_jev", side_effect=OSError("connection refused")):
            _, body = self.status()
            code, project = self.call("GET", f"/api/projects/{self.project['id']}")
        self.assertFalse(body["available"])
        self.assertIn("connection refused", body["reason"])
        self.assertEqual(code, 200)
        self.assertEqual(project["id"], self.project["id"])

    def test_a_crashing_check_does_not_break_the_route(self):
        self.use_cli()
        with mock.patch.object(assistant, "probe_jev", side_effect=RuntimeError("boom")):
            code, body = self.status()
        self.assertEqual(code, 200)
        self.assertFalse(body["available"])

    def test_the_result_is_cached_for_a_few_seconds_and_fresh_rechecks(self):
        self.use_cli()
        with mock.patch.object(assistant, "probe_jev", return_value=None) as probe:
            self.status()
            self.status()
            self.assertEqual(probe.call_count, 1)
            self.status(fresh=True)
            self.assertEqual(probe.call_count, 2)

    def test_a_slow_jev_does_not_hold_the_request_past_its_budget(self):
        self.use_cli()
        release = threading.Event()
        with mock.patch.object(assistant, "probe_jev", side_effect=lambda *a, **k: release.wait(10)), \
                mock.patch.object(assistant, "WAIT_S", 0.3):
            started = time.monotonic()
            code, body = self.status()
            took = time.monotonic() - started
            release.set()
        self.assertEqual(code, 200)
        self.assertLess(took, 2)
        self.assertFalse(body["available"])
        self.assertTrue(body["checking"])

    def test_saving_settings_clears_the_cache(self):
        self.use_cli()
        with mock.patch.object(assistant, "probe_jev", return_value=None) as probe:
            self.status()
            self.call("POST", "/api/assistant/settings", {"jev_url": "http://127.0.0.1:4000"})
            self.status()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args.args[0], "http://127.0.0.1:4000")

    def test_settings_have_working_defaults_and_validate(self):
        code, body = self.call("GET", "/api/assistant/settings")
        self.assertEqual(code, 200)
        self.assertEqual(body["cli_path"], "")
        self.assertEqual(body["jev_url"], "http://127.0.0.1:3455")
        self.assertEqual(body["defaults"]["jev_url"], "http://127.0.0.1:3455")
        self.assertNotIn("api_key", body)
        code, _ = self.call("POST", "/api/assistant/settings", {"jev_url": "ftp://x"})
        self.assertEqual(code, 400)
        code, body = self.call("POST", "/api/assistant/settings", {"cli_path": "C:/tools/claude.exe", "jev_url": "http://127.0.0.1:3455/"})
        self.assertEqual(code, 200)
        self.assertEqual(body["jev_url"], "http://127.0.0.1:3455")
        self.assertEqual(self.call("GET", "/api/assistant/settings")[1]["cli_path"], "C:/tools/claude.exe")

    def test_blank_settings_return_to_the_defaults(self):
        self.call("POST", "/api/assistant/settings", {"cli_path": "x", "jev_url": "http://h:1"})
        _, body = self.call("POST", "/api/assistant/settings", {"cli_path": "", "jev_url": ""})
        self.assertEqual(body["cli_path"], "")
        self.assertEqual(body["jev_url"], "http://127.0.0.1:3455")

    def test_the_status_carries_the_purpose_summary(self):
        self.use_cli()
        with mock.patch.object(assistant, "probe_jev", return_value=None):
            self.assertEqual(self.status()[1]["purpose"], {"default": {"value": "general", "note": ""}, "models": []})
            record = projects.get_project(self.project["id"])
            data = copy.deepcopy(record["data"])
            data["purpose"] = {"value": "functional", "note": "carries thrust"}
            code, _ = self.call("PATCH", f"/api/projects/{record['id']}", {"data": data, "revision": record["revision"]})
            self.assertEqual(code, 200)
            self.assertEqual(self.status()[1]["purpose"]["default"], {"value": "functional", "note": "carries thrust"})

    def test_the_check_also_answers_without_a_project(self):
        self.use_cli(str(self.dir / "nope.exe"))
        code, body = self.call("GET", "/api/assistant/status?fresh=1")
        self.assertEqual(code, 200)
        self.assertFalse(body["available"])

    def test_an_unknown_project_is_404(self):
        code, _ = self.call("GET", "/api/projects/99999/assistant/status")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
