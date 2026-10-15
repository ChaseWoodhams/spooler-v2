"""The queue and review deep links the project page uses (/?review=<job>, /?queue=1) serve the app, not the Spoolman proxy."""
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests import test_jobs_api


class DeepLinkTests(unittest.TestCase):
    def setUp(self):
        self.api = test_jobs_api.JobsApiTests("test_queue_workflow")
        self.api.setUp()
        self.addCleanup(self.api.tearDown)
        patcher = mock.patch("http_handler.PROXY_SPOOLMAN", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, path):
        request = urllib.request.Request(self.api.base + path, headers={"Accept": "text/html"})
        try:
            with test_jobs_api._opener.open(request, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_the_review_link_opens_the_spooler_app(self):
        status, body = self.page("/?review=5")
        self.assertEqual(status, 200)
        self.assertIn('id="modal-review"', body)

    def test_the_queue_link_opens_the_spooler_app(self):
        status, body = self.page("/?queue=1")
        self.assertEqual(status, 200)
        self.assertIn('id="modal-review"', body)

    def test_fixed_spoolman_nav_link_redirects_to_spoolman_when_proxy_is_off(self):
        import http.client
        import urllib.parse
        with mock.patch("http_handler.PROXY_SPOOLMAN", False),              mock.patch("http_handler.get_spoolman_url", return_value="http://spoolman.example:7912"):
            conn = http.client.HTTPConnection(urllib.parse.urlparse(self.api.base).netloc, timeout=10)
            try:
                conn.request("GET", "/spoolman/")
                resp = conn.getresponse()
                resp.read()
                self.assertIn(resp.status, (301, 302, 303, 307))
                self.assertEqual(resp.getheader("Location"), "http://spoolman.example:7912/")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
