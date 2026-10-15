"""Shapes measured against the real services in the live check (ticket 19), pinned so a refactor cannot drift from them.
No network: the Jev answer is replayed through a fake opener."""
import io
import json
import unittest
from unittest import mock

import assistant
import assistant_runner

# GET http://127.0.0.1:3455/jev/status as measured on 2026-10-15 (stats trimmed): 200, application/json, "ok": true, no "status" key.
JEV_STATUS = {"ok": True, "listen": "http://127.0.0.1:3455", "upstream": "http://127.0.0.1:3456",
              "stats": {"proxiedRequests": 5122, "jevErrors": 1}, "models": {}, "routingPolicy": {"defaultTier": "balanced"}}


class _Opener:
    def __init__(self, body: bytes):
        self.body, self.urls = body, []

    def open(self, url, timeout=None):
        self.urls.append(url)
        return io.BytesIO(self.body)


class JevStatusShapeTests(unittest.TestCase):
    def probe(self, payload):
        opener = _Opener(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        with mock.patch("urllib.request.build_opener", return_value=opener):
            assistant.probe_jev("http://127.0.0.1:3455/", timeout=1)
        return opener.urls

    def test_the_real_jev_status_answer_counts_as_ok(self):
        self.assertEqual(self.probe(JEV_STATUS), ["http://127.0.0.1:3455/jev/status"])

    def test_ok_false_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.probe({**JEV_STATUS, "ok": False})


class UsageLimitWordingTests(unittest.TestCase):
    """Wordings found in the installed CLI (claude 2.1.300) for a usage or rate limit; the spec maps all of them to usage_limit."""

    def test_cli_usage_and_rate_limit_wordings_map_to_usage_limit(self):
        for text in ("Claude AI usage limit reached|1760550000", "You've hit your limit · resets 5pm", "You're out of usage credits",
                     "spend limit reached", "Server is temporarily limiting requests (not your usage limit)",
                     'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
                     "Credit balance is too low"):
            state, answer, error = assistant_runner._from_result({"type": "result", "subtype": "success", "is_error": True, "result": text})
            self.assertEqual((state, error), ("usage_limit", assistant_runner.USAGE_LIMIT_ERROR), text)

    def test_other_errors_stay_errors(self):
        state, _, _ = assistant_runner._from_result({"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 500 overloaded"})
        self.assertEqual(state, "error")


if __name__ == "__main__":
    unittest.main()
