"""Opening the Assembly guide from a project: one entry per split with its guide state (the Print page's list)."""
import json
import unittest
import urllib.request
from unittest import mock

import database
import jobs
import projects
import splits
from tests.test_assembly_guide import GuideCase
from tests.test_assembly_plan import GEOMETRY, grid_split, stack_split


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ProjectGuidesTests(GuideCase):
    def setUp(self):
        super().setUp()
        projects.configure(self.dir)
        projects.init()
        self.addCleanup(projects.configure)

    def project_with(self, *split_ids):
        project = projects.create_project("Chair", split_id=split_ids[0])
        with database.transaction() as conn:
            for extra in split_ids[1:]:
                conn.execute("INSERT INTO project_splits (project_id, split_id) VALUES (?, ?)", (project["id"], extra))
        return project["id"]

    def test_one_split_gives_one_entry(self):
        one = self.make_split(*stack_split(3))
        entries = projects.guides(self.project_with(one))
        self.assertEqual([e["split_id"] for e in entries], [one])
        self.assertEqual(entries[0]["guide"]["state"], "none")
        self.assertEqual(entries[0]["name"], "model")
        self.assertEqual(entries[0]["download_name"], "model - assembly guide.pdf")

    def test_several_splits_each_list_their_own_guide_state(self):
        ready, fresh, stale = (self.make_split(*stack_split(3)) for _ in range(3))
        self.generate(ready)
        self.generate(stale)
        (splits.work_dir(stale) / "cuts.json").write_text(json.dumps({"mode": "smart", "cuts": []}), encoding="utf-8")
        with mock.patch.object(splits, "assembly_plan", side_effect=lambda i: {"ok": True, "code": "ffffff"} if i == stale else splits.assembly_plan.__wrapped__(i) if hasattr(splits.assembly_plan, "__wrapped__") else None):
            pass
        entries = projects.guides(self.project_with(ready, fresh, stale))
        self.assertEqual([e["split_id"] for e in entries], [ready, fresh, stale])
        states = {e["split_id"]: e["guide"] for e in entries}
        self.assertEqual(states[ready]["state"], "ready")
        self.assertFalse(states[ready]["out_of_date"])
        self.assertEqual(states[fresh]["state"], "none")
        self.assertEqual(states[stale]["state"], "ready")
        self.assertTrue(states[stale]["out_of_date"])

    def test_a_generating_guide_shows_as_generating_and_there_is_no_combined_entry(self):
        a, b = self.make_split(*stack_split(3)), self.make_split(*grid_split())
        splits.start_guide(a, {})
        entries = projects.guides(self.project_with(a, b))
        self.assertEqual([e["guide"]["state"] for e in entries], ["generating", "none"])
        self.assertEqual(len(entries), 2)

    def test_the_project_api_serves_the_list(self):
        import threading
        from http.server import ThreadingHTTPServer
        from http_handler import SPHandler
        a, b = self.make_split(*stack_split(3)), self.make_split(*stack_split(2))
        project_id = self.project_with(a, b)
        with mock.patch("http_handler._auth_ok", return_value=True):
            server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{server.server_address[1]}/api/projects/{project_id}/guides", timeout=10) as resp:
                body = json.loads(resp.read())
        self.assertEqual([e["split_id"] for e in body["guides"]], [a, b])

    def test_an_unknown_project_is_a_404(self):
        with self.assertRaises(jobs.JobError):
            projects.guides(9999)


if __name__ == "__main__":
    unittest.main()
