"""Guards for text encoding of the static UI: every shipped file is UTF-8 and is served with a charset."""
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from http_handler import SPHandler

PUBLIC = Path(__file__).resolve().parent.parent / "public"
TEXT_SUFFIXES = {".js", ".css", ".html", ".json", ".svg"}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def own_text_files():
    return [p for p in PUBLIC.rglob("*")
            if p.is_file() and p.suffix in TEXT_SUFFIXES and "vendor" not in p.relative_to(PUBLIC).parts]


class PublicEncodingTests(unittest.TestCase):
    def test_public_text_files_are_valid_utf8(self):
        files = own_text_files()
        self.assertTrue(files)
        bad = []
        for p in files:
            try:
                p.read_bytes().decode("utf-8")
            except UnicodeDecodeError as e:
                bad.append(f"{p.relative_to(PUBLIC)} (byte {e.start})")
        self.assertEqual(bad, [], "not valid UTF-8 (save as UTF-8 or use escapes)")


class StaticCharsetTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def content_type(self, path):
        with _opener.open(self.base + path, timeout=5) as resp:
            return resp.headers["Content-Type"]

    def test_js_and_css_carry_utf8_charset(self):
        self.assertIn("charset=utf-8", self.content_type("/history.js").lower())
        self.assertIn("charset=utf-8", self.content_type("/style.css").lower())

    def test_json_and_svg_carry_utf8_charset(self):
        for suffix in (".json", ".svg"):
            path = next((p for p in own_text_files() if p.suffix == suffix), None)
            if path is None:
                continue
            ctype = self.content_type("/" + path.relative_to(PUBLIC).as_posix())
            self.assertIn("charset=utf-8", ctype.lower(), path.name)


if __name__ == "__main__":
    unittest.main()
