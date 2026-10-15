"""The Assistant's Proposal pipeline, through the project HTTP API with the fake `claude` that starts the real Spooler MCP server:
propose (orientation) -> staged under projects/.assistant/<token>/ -> review -> Accept / Dismiss / Superseded -> sweep."""
import asyncio
import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import assistant
import assistant_runner
import database
import jobs
import proposal_store
import projects
import slicer
import splits
from tests.fake_claude import make_fake_claude, read_record, set_scenario
from tests.test_orient import l_bracket, mushroom
from tests.test_slicer import fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ProposalCase(unittest.TestCase):
    """A project of three parts: two mushrooms standing on their posts (they should lie on their discs) and a bracket already
    on a good face. Subclasses ask the Assistant and read what it staged."""

    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.cli_dir = self.dir / "cli"
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        splits.configure(self.dir)
        projects.configure(self.dir)
        projects.init()
        assistant.configure(self.dir)
        assistant_runner.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        probe = mock.patch.object(assistant, "probe_jev", return_value=None)
        probe.start()
        self.addCleanup(probe.stop)
        self.project = self.make_project()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        assistant.configure()
        assistant_runner.reset()
        self._tmp.cleanup()

    # ── helpers ──
    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def settle(self):
        async def work():
            with mock.patch.object(slicer, "runner", fake_runner()):
                while await projects.process_next():
                    pass
        asyncio.run(work())

    def make_project(self, stls=None, rotations=((180, 0, 0), (180, 0, 0), (0, 0, 0))):
        project = projects.create_project("Brackets")
        for number, data in enumerate(stls or [mushroom(), mushroom(), l_bracket()], start=1):
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"Part {number}.stl")
        self.settle()
        record = projects.get_project(project["id"])
        data = copy.deepcopy(record["data"])
        for obj, rotation in zip(data["objects"], rotations):
            obj["rotation"] = list(rotation)
        return self.edit(record, lambda d: d.update(data))

    def edit(self, record, change):
        data = copy.deepcopy(record["data"])
        change(data)
        status, saved = self.call("PATCH", f"/api/projects/{record['id']}", {"data": data, "revision": record["revision"]})
        self.assertEqual(status, 200, saved)
        return saved

    def current(self):
        return projects.get_project(self.project["id"])

    def scenario(self, scenario):
        cli = make_fake_claude(self.cli_dir, scenario)
        status, _ = self.call("POST", "/api/assistant/settings", {"cli_path": str(cli)})
        self.assertEqual(status, 200)

    def ask(self, message="Orient these better"):
        status, run = self.call("POST", f"/api/projects/{self.project['id']}/assistant/ask", {"message": message, "history": []})
        self.assertEqual(status, 200, run)
        return run

    def poll(self, run, timeout=60):
        end = time.monotonic() + timeout
        while True:
            status, view = self.call("GET", f"/api/projects/{run['project_id']}/assistant/runs/{run['id']}")
            self.assertEqual(status, 200, view)
            if view["state"] != "thinking" or time.monotonic() > end:
                return view
            time.sleep(0.05)

    def propose_call(self, object_ids=None, revision=None, **extra):
        arguments = {"revision": self.project["revision"] if revision is None else revision, **extra}
        if object_ids is not None:
            arguments["object_ids"] = object_ids
        return {"tool": "propose_orientation", "arguments": arguments}

    def run_scenario(self, calls, answer="Done."):
        self.scenario({"calls": calls, "answer": answer})
        return self.poll(self.ask())

    def results(self):
        return [(r["is_error"], json.loads(r["text"]) if not r["is_error"] else r["text"]) for r in read_record(self.cli_dir)["tool_results"]]

    def staging(self):
        root = proposal_store.staging_root(projects.projects_dir())
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def staged(self, token):
        return proposal_store.read(projects.projects_dir(), token)


class OrientationProposalTests(ProposalCase):
    def test_propose_orientation_stages_what_the_deterministic_helper_returns_and_changes_no_project_data(self):
        before = self.current()
        view = self.run_scenario([self.propose_call([1, 2, 3])])
        (is_error, result), = self.results()
        self.assertFalse(is_error, result)
        self.assertEqual(view["state"], "proposal")
        token = result["token"]
        staged = self.staged(token)
        self.assertEqual((staged["project_id"], staged["base_revision"], staged["kind"]), (before["id"], before["revision"], "orientation"))
        # the resulting data is exactly the non-saving helper's rotations on a copy of the data, nothing invented
        options = projects._orient_options(before, {"mode": "least-support"})
        expected = copy.deepcopy(before["data"])
        for obj in expected["objects"]:
            suggestion = projects._orientation_for(before["id"], obj, options)
            obj["rotation"] = suggestion["rotation"]
        self.assertEqual(staged["data"], expected)
        self.assertNotEqual(staged["data"]["objects"][0]["rotation"], [180, 0, 0])
        self.assertEqual(staged["data"]["objects"][2]["rotation"], [0, 0, 0], "a part already on a good face is not touched")
        # the model gets a readable diff, not the resulting data
        self.assertNotIn("data", result)
        self.assertEqual(len(result["diff"]), 2)
        # nothing was saved
        after = self.current()
        self.assertEqual((after["revision"], after["data"]), (before["revision"], before["data"]))
        self.assertEqual(self.staging(), [token])

    def test_a_propose_made_at_a_behind_revision_says_stale_and_stages_nothing(self):
        view = self.run_scenario([self.propose_call([1, 2], revision=self.project["revision"] - 1)])
        (is_error, result), = self.results()
        self.assertFalse(is_error)
        self.assertIs(result["stale"], True)
        self.assertEqual(result["revision"], self.project["revision"])
        self.assertIn("changed", result)
        self.assertNotIn("token", result)
        self.assertEqual(self.staging(), [])
        self.assertEqual(view["state"], "answer", "no Proposal, so the run ends as an answer")
        self.assertEqual(view["proposals"], [])

    def test_the_server_refuses_a_fourth_proposal_in_one_run(self):
        calls = [self.propose_call([1]), self.propose_call([2]), self.propose_call([1, 2]), self.propose_call([1, 2])]
        view = self.run_scenario(calls)
        results = self.results()
        self.assertEqual([is_error for is_error, _ in results], [False, False, False, True])
        self.assertIn("At most 3", results[3][1])
        self.assertEqual(view["state"], "proposal")
        third = results[2][1]["token"]
        self.assertEqual(self.staging(), [third], "only the latest Proposal of a kind is kept; the two it replaced are swept")
        self.assertEqual([p["token"] for p in view["proposals"]], [third])

    def test_pinned_objects_are_reported_and_skipped(self):
        self.project = self.edit(self.current(), lambda d: d["objects"][0].update(pinned=True))
        self.run_scenario([self.propose_call()])
        (_, result), = self.results()
        self.assertEqual([p["object"] for p in result["pinned_skipped"]], [1])
        staged = self.staged(result["token"])
        self.assertEqual(staged["pinned"][0]["object"], 1)
        self.assertEqual(staged["data"]["objects"][0]["rotation"], [180, 0, 0], "a Pinned object is left alone")
        self.assertNotEqual(staged["data"]["objects"][1]["rotation"], [180, 0, 0])
        self.assertEqual([line["object"] for line in staged["diff"]], [2])

    def test_objects_of_a_model_still_importing_cannot_be_targeted(self):
        data = mushroom()
        projects.import_model(self.project["id"], io.BytesIO(data), len(data), "Late.stl")        # not settled: still importing
        self.project = self.current()
        importing = next(m["id"] for m in self.project["models"] if m["state"] == "importing")
        late = next(o["id"] for o in self.project["data"]["objects"] if o["model"] == importing)
        self.run_scenario([self.propose_call([1, late])])
        (_, result), = self.results()
        self.assertTrue(any(f"Object {late}" in p and "importing" in p for p in result["problems"]), result["problems"])
        staged = self.staged(result["token"])
        self.assertEqual(staged["targets"]["objects"], [1])
        self.assertEqual(next(o for o in staged["data"]["objects"] if o["id"] == late)["rotation"], [0, 0, 0])

    def test_when_nothing_would_change_nothing_is_staged(self):
        view = self.run_scenario([self.propose_call([3])])            # the bracket is already on a good face
        (is_error, result), = self.results()
        self.assertFalse(is_error)
        self.assertIsNone(result["proposal"])
        self.assertEqual(result["unchanged"], [3])
        self.assertEqual(self.staging(), [])
        self.assertEqual(view["state"], "answer")

    def test_bad_arguments_are_errors_the_model_can_read(self):
        self.run_scenario([self.propose_call([1], mode="looks"), self.propose_call("1"), self.propose_call([True]), {"tool": "propose_orientation", "arguments": {}}])
        self.assertEqual([is_error for is_error, _ in self.results()], [True, True, True, True])
        self.assertEqual(self.staging(), [])

    def test_the_result_the_model_sees_is_a_readable_diff_notes_and_problems_not_the_resulting_data(self):
        self.run_scenario([self.propose_call(reason="Stands the mushrooms on their wide discs")])
        (_, result), = self.results()
        self.assertEqual(set(result), {"revision", "token", "kind", "title", "diff", "notes", "pinned_skipped", "problems", "proposals_left_this_run", "next"})
        self.assertEqual(result["kind"], "orientation")
        self.assertIn("rotation 180, 0, 0 → ", result["diff"][0]["text"])
        self.assertEqual(result["diff"][0]["label_quoted"], "Part 1")
        self.assertEqual(result["proposals_left_this_run"], 2)
        self.assertLess(len(json.dumps(result)), 3000)
        self.assertEqual(self.staged(result["token"])["reason"], "Stands the mushrooms on their wide discs")


class ProposalRouteTests(ProposalCase):
    def propose(self, **extra):
        view = self.run_scenario([self.propose_call([1, 2], **extra)])
        self.assertEqual(view["state"], "proposal", view)
        return view, view["proposals"][0]["token"]

    def path(self, token, action=""):
        return f"/api/projects/{self.project['id']}/assistant/proposals/{token}{action}"

    def test_the_run_view_carries_a_card_summary_but_never_the_resulting_data(self):
        view, token = self.propose(reason="Lie them on their discs")
        card, = view["proposals"]
        self.assertEqual((card["token"], card["kind"], card["title"], card["reason"], card["base_revision"]),
                         (token, "orientation", "Better orientation for 2 objects", "Lie them on their discs", self.project["revision"]))
        self.assertEqual(card["scope"], "2 objects on Plate 1")
        self.assertEqual([line["label"] for line in card["diff"]], ["Part 1", "Part 2"])
        self.assertNotIn("data", card)
        self.assertNotIn("emptied", card)

    def test_the_review_view_shows_the_plates_before_and_after_in_the_shape_the_plan_sheet_renders(self):
        _, token = self.propose()
        status, review = self.call("GET", self.path(token))
        self.assertEqual(status, 200, review)
        self.assertEqual((review["token"], review["kind"], review["review"], review["revision"]), (token, "orientation", "placement", self.project["revision"]))
        self.assertEqual(len(review["bed"]), 4)
        before, after = review["before"]["plates"], review["plates"]
        self.assertEqual(review["after"]["plates"], after)
        self.assertEqual([p["id"] for p in before], [1])
        self.assertEqual([p["id"] for p in after], [1])
        for plate in before + after:
            self.assertEqual(set(plate) >= {"id", "name", "new", "filament", "parts", "turned", "objects"}, True)
            self.assertEqual(plate["parts"], 3, "the whole plate is drawn so the change is seen in place")
            self.assertTrue(all(len(o["outline"]) > 2 for o in plate["objects"]))
        self.assertEqual([o["rotation"] for o in before[0]["objects"]], [[180, 0, 0], [180, 0, 0], [0, 0, 0]])
        self.assertNotEqual(after[0]["objects"][0]["rotation"], [180, 0, 0])
        self.assertEqual(after[0]["turned"], 2)
        self.assertEqual(review["kept"], [])
        self.assertIn("Better orientation", review["title"])
        self.assertNotIn("data", review)
        self.assertEqual(self.current()["revision"], self.project["revision"], "reviewing changes nothing")

    def test_accept_applies_the_staged_data_as_one_revision_and_the_prior_data_can_be_put_back(self):
        _, token = self.propose()
        staged = self.staged(token)
        prior = self.current()
        status, result = self.call("POST", self.path(token, "/accept"), {"revision": prior["revision"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["revision"], prior["revision"] + 1, "one revision")
        self.assertEqual(result["data"], staged["data"])
        self.assertEqual(self.current()["data"], staged["data"])
        self.assertEqual(self.staging(), [], "staging is swept on accept")
        # what the client's Undo does: save the prior data over the accepted revision
        status, undone = self.call("PATCH", f"/api/projects/{prior['id']}", {"data": prior["data"], "revision": result["revision"]})
        self.assertEqual(status, 200, undone)
        self.assertEqual(self.current()["data"], prior["data"])

    def test_accept_makes_the_slice_of_a_changed_plate_outdated(self):
        projects.request_slices(self.project["id"], [1])
        self.settle()
        sliced = self.current()
        self.assertEqual((sliced["slices"]["1"]["state"], sliced["slices"]["1"]["outdated"]), ("sliced", False))
        self.project = sliced
        _, token = self.propose()
        status, result = self.call("POST", self.path(token, "/accept"), {"revision": sliced["revision"]})
        self.assertEqual(status, 200, result)
        self.assertIs(self.current()["slices"]["1"]["outdated"], True)

    def test_accept_after_any_edit_is_refused_409_the_proposal_is_superseded_and_nothing_is_rebased(self):
        _, token = self.propose()
        edited = self.edit(self.current(), lambda d: d["objects"][2].update(position=[30, 30]))
        status, refused = self.call("POST", self.path(token, "/accept"), {"revision": edited["revision"]})
        self.assertEqual(status, 409, refused)
        self.assertIs(refused["superseded"], True)
        self.assertEqual(self.current()["data"], edited["data"], "the edit stands and the proposal was not merged into it")
        self.assertEqual(self.current()["revision"], edited["revision"])
        self.assertEqual(self.staging(), [], "a superseded Proposal is swept")
        self.assertEqual(self.call("GET", self.path(token))[0], 404)
        self.assertEqual(self.call("POST", self.path(token, "/accept"), {"revision": edited["revision"]})[0], 404)

    def test_reviewing_a_proposal_the_project_has_moved_past_says_superseded(self):
        _, token = self.propose()
        self.edit(self.current(), lambda d: d["objects"][2].update(position=[30, 30]))
        status, refused = self.call("GET", self.path(token))
        self.assertEqual((status, refused.get("superseded")), (409, True), refused)
        self.assertEqual(self.staging(), [])

    def test_accept_from_a_page_that_is_behind_is_refused_too(self):
        _, token = self.propose()
        status, refused = self.call("POST", self.path(token, "/accept"), {"revision": self.project["revision"] - 1})
        self.assertEqual((status, refused.get("superseded")), (409, True), refused)
        self.assertEqual(self.current()["data"], self.project["data"])

    def test_accept_needs_the_revision(self):
        _, token = self.propose()
        status, _ = self.call("POST", self.path(token, "/accept"), {})
        self.assertEqual(status, 400)
        self.assertEqual(self.staging(), [token], "a malformed accept does not consume the Proposal")

    def test_dismiss_changes_nothing_and_sweeps_the_staging(self):
        _, token = self.propose()
        status, _ = self.call("POST", self.path(token, "/dismiss"), {})
        self.assertEqual(status, 200)
        self.assertEqual(self.staging(), [])
        self.assertEqual(self.current()["data"], self.project["data"])
        self.assertEqual(self.current()["revision"], self.project["revision"])
        self.assertEqual(self.call("POST", self.path(token, "/dismiss"), {})[0], 200, "dismissing twice is harmless")

    def test_a_token_that_is_unknown_malformed_or_from_another_project_is_404(self):
        _, token = self.propose()
        other = projects.create_project("Other")
        for bad in ("0" * 32, "..%2F..%2Fspooler", "zz", "A" * 32):
            self.assertEqual(self.call("GET", self.path(bad))[0], 404, bad)
            self.assertEqual(self.call("POST", self.path(bad, "/accept"), {"revision": 1})[0], 404, bad)
        self.assertEqual(self.call("GET", f"/api/projects/{other['id']}/assistant/proposals/{token}")[0], 404)
        self.assertEqual(self.call("POST", f"/api/projects/{other['id']}/assistant/proposals/{token}/accept", {"revision": 1})[0], 404)
        self.assertEqual(self.staging(), [token], "the other project's attempts did not consume it")
