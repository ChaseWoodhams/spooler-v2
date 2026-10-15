"""Spooler has no login: every route and the live connection answer without credentials."""
import asyncio
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import projects
import state
from http_handler import SPHandler
from tests.test_jobs import GCODE, FakePrinter, isolate_preflight

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

REMOVED_GET = ["/login", "/api/auth-status"]
REMOVED_POST = ["/api/login", "/api/logout", "/api/setup", "/api/change-password"]


class StubPrinter(FakePrinter):
    def to_dict(self):
        return {"id": self.id, "name": self.name}

    def link_status(self, now=None):
        return {"id": self.id, "name": self.name, "connected": self.connected}


class OpenAccessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        projects.configure(self.dir)
        projects.init()
        self._saved = (state.printers, state.loop)
        state.printers = {"p1": StubPrinter()}
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        state.loop = self.loop
        patcher = mock.patch.multiple(jobs, uploader=lambda ip, path, name, progress: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        isolate_preflight(self, self.dir)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(5)
        self.loop.close()
        state.printers, state.loop = self._saved
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        self._tmp.cleanup()

    def call(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"}
        h.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with _opener.open(request, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_formerly_protected_routes_need_no_credentials(self):
        for path in ("/api/dashboard", "/api/jobs", "/api/projects", "/api/system-status", "/api/printers"):
            with self.subTest(path=path):
                status, body = self.call("GET", path)
                self.assertEqual(status, 200)
                json.loads(body)

    def test_a_mutating_post_needs_no_credentials(self):
        status, body = self.call("POST", "/api/jobs/upload?filename=cube.gcode&printer_id=p1", raw=GCODE)
        self.assertEqual((status, json.loads(body)["state"]), (200, "queued"))
        self.assertEqual(len(json.loads(self.call("GET", "/api/jobs")[1])["jobs"]), 1)

    def test_removed_auth_endpoints_are_not_found(self):
        for path in REMOVED_GET:
            with self.subTest(method="GET", path=path):
                self.assertEqual(self.call("GET", path)[0], 404)
        for path in REMOVED_POST:
            with self.subTest(method="POST", path=path):
                self.assertEqual(self.call("POST", path, {"username": "a", "password": "b"})[0], 404)

    def test_a_stale_session_cookie_has_no_effect(self):
        cookie = {"Cookie": "spooler_sid=stale-token"}
        self.assertEqual(self.call("GET", "/api/jobs", headers=cookie)[0], 200)
        self.assertEqual(self.call("GET", "/api/auth-status", headers=cookie)[0], 404)
        self.assertNotIn("Set-Cookie", self._headers("/api/jobs", cookie))

    def _headers(self, path, headers):
        request = urllib.request.Request(self.base + path, headers=headers)
        with _opener.open(request, timeout=10) as resp:
            return dict(resp.headers)


class BrowserSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_without_cookie_gets_the_initial_messages(self):
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve
        from ws_handler import browser_handler
        saved = state.printers
        state.printers = {"p1": StubPrinter()}
        try:
            async with serve(browser_handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}", additional_headers={"Cookie": "spooler_sid=stale"}) as ws:
                    kinds = [json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] for _ in range(4)]
        finally:
            state.printers = saved
        self.assertEqual(kinds, ["printer_update", "printers_listed", "tray_map", "system_status"])


if __name__ == "__main__":
    unittest.main()
