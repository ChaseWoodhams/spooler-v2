import contextlib
import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import http_handler
import supervisor as sv


def _env(**kw):
    """Environment with BIND_HOST set (or removed when None)."""
    env = {k: v for k, v in os.environ.items() if k != "BIND_HOST"}
    env.update({k: v for k, v in kw.items() if v is not None})
    return mock.patch.dict(os.environ, env, clear=True)


class BindAddressTests(unittest.TestCase):
    def _bound(self, factory):
        server = factory(0)
        try:
            return server.server_address[0]
        finally:
            server.server_close()

    def test_http_listener_binds_loopback_by_default(self):
        with _env():
            self.assertEqual(self._bound(http_handler.make_http_server), "127.0.0.1")

    def test_blank_setting_still_means_loopback(self):
        with _env(BIND_HOST="  "):
            self.assertEqual(self._bound(http_handler.make_http_server), "127.0.0.1")

    def test_opt_in_setting_chooses_the_bind_address(self):
        with _env(BIND_HOST="0.0.0.0"):
            self.assertEqual(self._bound(http_handler.make_http_server), "0.0.0.0")

    def test_no_warning_on_loopback(self):
        for value in (None, "127.0.0.1", "localhost", "::1"):
            with _env(BIND_HOST=value):
                self.assertIsNone(http_handler.bind_warning(), value)

    def test_warning_when_exposed_says_there_is_no_login(self):
        with _env(BIND_HOST="0.0.0.0"):
            warning = http_handler.bind_warning()
        self.assertIn("no login", warning.lower())
        self.assertIn("0.0.0.0", warning)

    def test_listeners_log_the_address_they_bound(self):
        out = io.StringIO()
        with _env(), contextlib.redirect_stdout(out):
            self._bound(http_handler.make_http_server)
        self.assertIn("127.0.0.1", out.getvalue())


class SupervisorProbeTests(unittest.TestCase):
    def _health_url(self, bind_host):
        with TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            env = {"PATH": ""}
            if bind_host is not None:
                env["BIND_HOST"] = bind_host
            (_, spooler), _ = sv.build_services(app, base_env=env)
            return spooler.health_url

    def test_probe_uses_loopback_by_default(self):
        self.assertEqual(self._health_url(None), "http://127.0.0.1:8080/api/health")

    def test_probe_uses_loopback_when_listening_on_all_interfaces(self):
        self.assertEqual(self._health_url("0.0.0.0"), "http://127.0.0.1:8080/api/health")

    def test_probe_follows_a_specific_bind_address(self):
        self.assertEqual(self._health_url("192.168.1.5"), "http://192.168.1.5:8080/api/health")


if __name__ == "__main__":
    unittest.main()
