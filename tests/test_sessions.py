import json
import tempfile
import time
import unittest
from pathlib import Path

import auth


class PersistentSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved = (auth._AUTH_FILE, auth._SESSIONS_FILE, dict(auth._sessions))
        auth.set_auth_file(self.dir / "auth.json")

    def tearDown(self):
        auth._AUTH_FILE, auth._SESSIONS_FILE, sessions = self._saved
        auth._sessions.clear()
        auth._sessions.update(sessions)
        self._tmp.cleanup()

    def restart(self):
        auth._sessions.clear()
        auth._load_sessions()

    def test_session_survives_a_restart_without_storing_the_raw_token(self):
        token = auth._create_session()

        self.assertNotIn(token, (self.dir / "sessions.json").read_text())
        self.restart()
        self.assertTrue(auth._validate_session(token))

    def test_sign_out_is_persisted(self):
        token = auth._create_session()
        auth._invalidate_session(token)

        self.restart()
        self.assertFalse(auth._validate_session(token))

    def test_expired_sessions_are_not_restored(self):
        (self.dir / "sessions.json").write_text(json.dumps({auth._token_key("old"): time.time() - 10}))

        self.restart()
        self.assertFalse(auth._validate_session("old"))
        self.assertEqual(auth._sessions, {})

    def test_corrupt_sessions_file_is_ignored(self):
        (self.dir / "sessions.json").write_text("{not json")

        self.restart()
        self.assertEqual(auth._sessions, {})


if __name__ == "__main__":
    unittest.main()
