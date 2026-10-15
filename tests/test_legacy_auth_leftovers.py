"""An install upgraded from the login era carries old settings and files; they must be inert."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server

OLD_ENV = (
    "AUTH_ENABLED=true\n"
    "SPOOLER_USERNAME=admin\n"
    "SPOOLER_PW_HASH=$2b$12$abcdefghijklmnopqrstuv\n"
    "SECURE_COOKIES=true\n"
    "HTTP_PORT=8123\n"
)
OLD_KEYS = ("AUTH_ENABLED", "SPOOLER_USERNAME", "SPOOLER_PW_HASH", "SECURE_COOKIES")


class LegacyAuthLeftoverTests(unittest.TestCase):
    def test_old_auth_variables_in_env_load_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(OLD_ENV)
            (Path(tmp) / "auth.json").write_text('{"username": "admin", "pw_hash": "x"}')
            (Path(tmp) / "sessions.json").write_text("{}")
            env = {k: v for k, v in os.environ.items() if k not in OLD_KEYS and k != "HTTP_PORT"}
            env["DATA_DIR"] = tmp
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                server._load_dotenv()
                self.assertEqual(os.environ["HTTP_PORT"], "8123")  # the file was read
            self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_leftover_account_and_session_files_cause_no_error_on_data_load(self):
        import persistence
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "auth.json").write_text("not even json")
            (Path(tmp) / "sessions.json").write_text("{}")
            with mock.patch.object(persistence, "PRINTERS_FILE", Path(tmp) / "printers.json"):
                self.assertEqual(persistence.load_printers(), [])


if __name__ == "__main__":
    unittest.main()
