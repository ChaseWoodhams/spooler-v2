"""The browser app has no login: the served shell, scripts and service worker never send the user to a login page."""
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from http_handler import SPHandler

PUBLIC = Path(__file__).resolve().parent.parent / "public"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Login page, sign-out and password UI, and the removed auth endpoints.
FORBIDDEN = re.compile(
    r"/login|/api/logout|/api/setup|/api/change-password|/api/auth-status|setup_required"
    r"|Sign out|Signed out|signout|Change password",
    re.IGNORECASE,
)


def served_paths():
    """Every page and script the app ships, as URL paths (third-party vendor bundles excluded)."""
    files = [p for ext in ("*.html", "*.js") for p in PUBLIC.rglob(ext) if "vendor" not in p.relative_to(PUBLIC).parts]
    return sorted("/" + p.relative_to(PUBLIC).as_posix() for p in files)


class NoLoginUiTests(unittest.TestCase):
    def setUp(self):
        # Unknown asset paths are otherwise handed to a Spoolman proxy, which answers for itself.
        patcher = mock.patch("http_handler.PROXY_SPOOLMAN", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        try:
            with _opener.open(self.base + path, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_login_page_is_gone(self):
        self.assertEqual(self.get("/login.html")[0], 404)

    def test_served_shell_scripts_and_service_worker_have_no_login_ui(self):
        paths = ["/"] + served_paths()
        self.assertIn("/sw.js", paths)
        self.assertIn("/app.js", paths)
        for path in paths:
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(FORBIDDEN.findall(body), [])

    def test_printer_access_code_inputs_remain(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertRegex(body, r'type="password"')


if __name__ == "__main__":
    unittest.main()
