"""Ticket 18: Proposals staged by a terminal-started Spooler MCP server (no app run) show up for their project in the app and go
through review / accept / dismiss / Superseded like any other. The server is the real one, started over stdio from the test."""
import sys
import time
import unittest
from pathlib import Path

import datapaths
import proposal_store
import projects
from tests.test_assistant_proposals import ProposalCase, GEOMETRY
from tests.test_spooler_mcp import McpSession

SERVER = Path(__file__).resolve().parent.parent / "spooler_mcp.py"


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class TerminalCase(ProposalCase):
    def terminal(self, project=None):
        session = McpSession(self.dir, (project or self.project)["id"])
        self.addCleanup(self.close, session)
        session.start()
        return session

    @staticmethod
    def close(session):
        try:
            session.close()
        except Exception:
            session.proc.kill()

    def stage(self, session, revision=None, **extra):
        reply = session.call("propose_orientation", {"revision": self.project["revision"] if revision is None else revision, **extra})
        self.assertFalse(reply["result"]["isError"], reply)
        import json
        return json.loads(reply["result"]["content"][0]["text"])

    def listing(self, project=None):
        status, body = self.call("GET", f"/api/projects/{(project or self.project)['id']}/assistant/proposals")
        self.assertEqual(status, 200, body)
        return body["proposals"]

    def test_terminal_proposal_is_listed_and_can_be_accepted(self):
        staged = self.stage(self.terminal())
        cards = self.listing()
        self.assertEqual([c["token"] for c in cards], [staged["token"]])
        self.assertEqual(cards[0]["kind"], "orientation")
        self.assertNotIn("data", cards[0])
        self.assertTrue(proposal_store.read(datapaths.projects_dir(), staged["token"])["run"].startswith("terminal-"))
        before = self.current()["revision"]
        status, view = self.call("GET", f"/api/projects/{self.project['id']}/assistant/proposals/{staged['token']}")
        self.assertEqual(status, 200, view)
        status, saved = self.call("POST", f"/api/projects/{self.project['id']}/assistant/proposals/{staged['token']}/accept", {"revision": before})
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.current()["revision"], before + 1)
        self.assertEqual(self.listing(), [])

    def test_terminal_proposal_can_be_dismissed_and_changes_nothing(self):
        staged = self.stage(self.terminal())
        before = self.current()
        status, _ = self.call("POST", f"/api/projects/{self.project['id']}/assistant/proposals/{staged['token']}/dismiss", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.current()["revision"], before["revision"])

    def test_terminal_proposal_is_superseded_by_an_edit(self):
        staged = self.stage(self.terminal())
        self.edit(self.current(), lambda d: d["plates"][0].update(name="Renamed"))
        self.assertEqual(self.listing(), [])                          # a superseded one is never offered
        status, body = self.call("GET", f"/api/projects/{self.project['id']}/assistant/proposals/{staged['token']}")
        self.assertIn(status, (404, 409))

    def test_review_of_a_superseded_terminal_proposal_is_superseded(self):
        staged = self.stage(self.terminal())
        self.edit(self.current(), lambda d: d["plates"][0].update(name="Renamed"))
        status, body = self.call("POST", f"/api/projects/{self.project['id']}/assistant/proposals/{staged['token']}/accept", {"revision": self.current()["revision"]})
        self.assertIn(status, (404, 409), body)

    def test_cap_is_three_per_terminal_session_and_latest_of_a_kind_is_listed(self):
        session = self.terminal()
        for _ in range(3):
            self.stage(session)
            time.sleep(0.01)
        reply = session.call("propose_orientation", {"revision": self.project["revision"]})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("At most 3", reply["result"]["content"][0]["text"])
        self.assertEqual(len(self.listing()), 1)                      # the three were all orientation: only the newest stays
        self.assertEqual(len(proposal_store.listing(datapaths.projects_dir())), 1)    # and the older ones were swept

    def test_other_projects_and_app_runs_are_not_listed(self):
        other = self.make_project()
        self.stage(self.terminal(other), revision=other["revision"])
        self.assertEqual(self.listing(), [])
        self.assertEqual(len(self.listing(other)), 1)
        self.assertEqual(self.call("GET", f"/api/projects/{self.project['id']}/assistant/proposals/{self.listing(other)[0]['token']}")[0], 404)
        # a Proposal of an app-started run belongs to that run's thread, not to this listing
        session = McpSession(self.dir, self.project["id"], command=[sys.executable, str(SERVER), "--project", str(self.project["id"]),
                                                                   "--data-dir", str(self.dir), "--run", "a" * 32])
        self.addCleanup(self.close, session)
        session.start()
        self.stage(session)
        self.assertEqual(self.listing(), [])

    def test_expiry_and_startup_sweeps_cover_terminal_proposals(self):
        staged = self.stage(self.terminal())
        proposal_store.sweep(datapaths.projects_dir(), time.time() + 1000)
        self.assertEqual(len(self.listing()), 1)
        proposal_store.sweep(datapaths.projects_dir(), time.time() + proposal_store.TTL_S + 60)
        self.assertEqual(self.listing(), [])
        self.stage(self.terminal())
        projects.sweep_assistant_staging(time.time(), startup=True)
        self.assertEqual(self.listing(), [])

    def test_unknown_project_listing_is_404(self):
        self.assertEqual(self.call("GET", "/api/projects/99999/assistant/proposals")[0], 404)


if __name__ == "__main__":
    unittest.main()
