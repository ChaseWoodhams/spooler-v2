import asyncio
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import auth
import health
import state
from http_handler import SPHandler

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._saved_loop = state.loop

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        state.loop = self._saved_loop

    def get(self, path):
        try:
            with _opener.open(self.base + path, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_reports_unresponsive_without_a_running_event_loop(self):
        state.loop = None
        status, body = self.get("/api/health")
        self.assertEqual((status, body["status"]), (503, "unresponsive"))

    def test_health_is_ok_with_a_running_loop_and_needs_no_login(self):
        loop = asyncio.new_event_loop()
        runner = threading.Thread(target=loop.run_forever, daemon=True)
        runner.start()
        try:
            state.loop = loop
            status, body = self.get("/api/health")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            runner.join(5)
            loop.close()
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertEqual(set(body), {"status", "uptime_s"})

    @unittest.skipUnless(auth.AUTH_ENABLED, "authentication disabled in this environment")
    def test_system_status_requires_login(self):
        status, _ = self.get("/api/system-status")
        self.assertEqual(status, 401)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_has_every_section(self):
        snap = health.snapshot()
        self.assertTrue({"spooler", "spoolman", "printers", "backups", "supervisor"} <= set(snap))
        self.assertIn("pending_deductions", snap["spoolman"])

    def test_supervisor_status_is_marked_stale_when_not_updated(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "supervisor_status.json"
            path.write_text(json.dumps({"updated_at": 1000, "services": []}))
            self.assertFalse(health.read_supervisor_status(path, now=1010)["stale"])
            self.assertTrue(health.read_supervisor_status(path, now=1000 + health.SUPERVISOR_STALE_S + 1)["stale"])


if __name__ == "__main__":
    unittest.main()
