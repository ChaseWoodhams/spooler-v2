"""Arrange and plan Proposals (ticket 16), through the project HTTP API with the fake `claude` that starts the real Spooler MCP
server: the staged data is exactly what the deterministic, non-saving helpers return; a plan Spooler would refuse stages nothing;
Superseded, Accept (one revision) and Undo go through the generic pipeline."""
import copy
import json
import unittest
from unittest import mock

import database
import placement_read
import project_read
import projects
import spooler_mcp
from tests.test_assistant_proposals import GEOMETRY, ProposalCase


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PlacementProposalCase(ProposalCase):
    def spread(self, plates=3):
        """The three parts on plates of their own, so that Arrange and Plan have something to fold together."""
        def change(data):
            for number in range(2, plates + 1):
                data["plates"].append({"id": number, "name": f"Plate {number}"})
            for obj, plate in zip(data["objects"], range(1, plates + 1)):
                obj["plate"], obj["position"] = plate, [60.0, 60.0]
        self.project = self.edit(self.current(), change)
        return self.project

    def ledger(self, record):
        with database.transaction() as conn:
            return project_read.read_ledger(conn, record["id"])

    def propose(self, tool, **arguments):
        arguments = {"revision": self.project["revision"], **arguments}
        view = self.run_scenario([{"tool": tool, "arguments": arguments}])
        (is_error, result), = self.results()
        return view, is_error, result

    def path(self, token, action=""):
        return f"/api/projects/{self.project['id']}/assistant/proposals/{token}{action}"

    def accept(self, token):
        return self.call("POST", self.path(token, "/accept"), {"revision": self.current()["revision"]})


class ArrangeProposalTests(PlacementProposalCase):
    def test_propose_arrange_stages_what_arrange_layout_and_place_layout_return_and_saves_nothing(self):
        self.spread()
        before = self.current()
        view, is_error, result = self.propose("propose_arrange")
        self.assertFalse(is_error, result)
        self.assertEqual(view["state"], "proposal")
        staged = self.staged(result["token"])
        self.assertEqual((staged["kind"], staged["review"], staged["base_revision"]), ("arrange", "placement", before["revision"]))
        order = placement_read._arrange_plate_ids(before, None)
        layout = placement_read.arrange_layout(before["id"], before, order)
        expected = copy.deepcopy(before["data"])
        used = placement_read._place_layout(expected, order, layout, lambda group: json.loads(group)["overrides"])
        emptied = [p for p in order if p not in used]
        expected["plates"] = [p for p in expected["plates"] if p["id"] not in emptied]
        self.assertEqual(staged["data"], expected)
        self.assertEqual(staged["emptied"], emptied)
        self.assertEqual(emptied, [2, 3], "three small parts fold onto one plate")
        self.assertNotIn("data", result)
        self.assertEqual(self.current()["data"], before["data"])
        self.assertEqual(self.staging(), [result["token"]])

    def test_plate_ids_limit_the_arrange_and_pinned_objects_stay_put(self):
        self.spread()
        self.project = self.edit(self.current(), lambda d: d["objects"][0].update(pinned=True))
        _, is_error, result = self.propose("propose_arrange", plate_ids=[1, 2])
        self.assertFalse(is_error, result)
        staged = self.staged(result["token"])
        by_id = {o["id"]: o for o in staged["data"]["objects"]}
        self.assertEqual((by_id[1]["plate"], by_id[1]["position"]), (1, [60.0, 60.0]), "pinned stays")
        self.assertEqual((by_id[3]["plate"], by_id[3]["position"]), (3, [60.0, 60.0]), "plate 3 was not asked for")
        self.assertEqual(by_id[2]["plate"], 1, "plate 2's part joins the pinned part's plate")
        self.assertEqual(staged["targets"]["plates"], [1, 2])

    def test_when_nothing_would_move_nothing_is_staged(self):
        self.project = self.edit(self.current(), lambda d: [o.update(pinned=True) for o in d["objects"]])
        _, is_error, result = self.propose("propose_arrange")
        self.assertFalse(is_error, result)
        self.assertIsNone(result["proposal"])
        self.assertEqual(self.staging(), [])

    def test_a_behind_revision_is_superseded_and_stages_nothing(self):
        self.spread()
        _, _, result = self.propose("propose_arrange", revision=self.project["revision"] - 1)
        self.assertIs(result["superseded"], True)
        self.assertEqual(self.staging(), [])

    def test_bad_plate_ids_are_an_error_the_model_can_read(self):
        self.spread()
        _, is_error, _ = self.propose("propose_arrange", plate_ids=["one"])
        self.assertTrue(is_error)
        self.assertEqual(self.staging(), [])

    def test_review_shows_before_and_after_and_accept_is_one_revision_with_one_undo(self):
        self.spread()
        _, _, result = self.propose("propose_arrange")
        status, review = self.call("GET", self.path(result["token"]))
        self.assertEqual(status, 200, review)
        self.assertEqual([p["id"] for p in review["before"]["plates"]], [1, 2, 3])
        self.assertEqual([len(p["objects"]) for p in review["before"]["plates"]], [1, 1, 1])
        self.assertEqual(sum(len(p["objects"]) for p in review["after"]["plates"]), 3)
        self.assertEqual(review["plates"], review["after"]["plates"])
        prior = self.current()
        status, accepted = self.accept(result["token"])
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["revision"], prior["revision"] + 1)
        self.assertEqual([p["id"] for p in accepted["data"]["plates"]], [1])
        self.assertEqual(self.staging(), [])
        status, undone = self.call("PATCH", f"/api/projects/{prior['id']}", {"data": prior["data"], "revision": accepted["revision"]})
        self.assertEqual(status, 200, undone)
        self.assertEqual(self.current()["data"], prior["data"])

    def test_an_edit_after_proposing_supersedes_it(self):
        self.spread()
        _, _, result = self.propose("propose_arrange")
        self.edit(self.current(), lambda d: d["plates"][0].update(name="Mine"))
        status, body = self.accept(result["token"])
        self.assertEqual((status, body.get("superseded")), (409, True))
        self.assertEqual(self.staging(), [])

    def test_a_newer_arrange_replaces_the_older_when_the_run_ends(self):
        self.spread()
        rev = self.project["revision"]
        view = self.run_scenario([{"tool": "propose_arrange", "arguments": {"revision": rev}},
                                  {"tool": "propose_arrange", "arguments": {"revision": rev, "plate_ids": [1, 2]}}])
        self.assertEqual(len(view["proposals"]), 1)
        self.assertEqual(len(self.staging()), 1)
        self.assertEqual(view["proposals"][0]["token"], self.results()[1][1]["token"])


class PlanProposalTests(PlacementProposalCase):
    def test_propose_plan_stages_what_plan_data_returns_and_saves_nothing(self):
        self.spread()
        before = self.current()
        view, is_error, result = self.propose("propose_plan", density="roomy")
        self.assertFalse(is_error, result)
        staged = self.staged(result["token"])
        self.assertEqual((view["state"], staged["kind"], staged["review"]), ("proposal", "plan", "placement"))
        planned = placement_read._plan_data(before["id"], before, False, {"preset": "roomy"}, ledger=self.ledger(before))
        self.assertEqual(staged["data"], planned["data"])
        self.assertEqual(staged["emptied"], planned["emptied"])
        self.assertEqual(staged["data"]["pack"]["preset"], "roomy")
        self.assertNotIn("data", result)
        self.assertEqual(self.current()["data"], before["data"])

    def test_without_a_density_the_project_density_is_kept(self):
        self.spread()
        before = self.current()
        _, _, result = self.propose("propose_plan")
        planned = placement_read._plan_data(before["id"], before, False, None, ledger=self.ledger(before))
        self.assertEqual(self.staged(result["token"])["data"], planned["data"])

    def test_bad_density_is_an_error(self):
        self.spread()
        _, is_error, _ = self.propose("propose_plan", density="huge")
        self.assertTrue(is_error)
        self.assertEqual(self.staging(), [])

    def test_a_behind_revision_is_superseded(self):
        self.spread()
        _, _, result = self.propose("propose_plan", revision=self.project["revision"] - 1)
        self.assertIs(result["superseded"], True)
        self.assertEqual(self.staging(), [])

    def test_accept_moves_objects_across_plates_as_one_save_with_one_undo_and_the_sheet_renders_it(self):
        self.spread()
        _, _, result = self.propose("propose_plan")
        status, review = self.call("GET", self.path(result["token"]))
        self.assertEqual(status, 200, review)
        self.assertEqual([p["id"] for p in review["before"]["plates"]][:3], [1, 2, 3])
        self.assertTrue(all(len(o["outline"]) > 2 for p in review["after"]["plates"] for o in p["objects"]))
        self.assertEqual(review["plates"], review["after"]["plates"])
        prior = self.current()
        saves = []
        real = projects.save_data

        def counting(*args, **kwargs):
            saves.append(1)
            return real(*args, **kwargs)
        with mock.patch.object(projects, "save_data", counting):
            status, accepted = self.accept(result["token"])
        self.assertEqual(status, 200, accepted)
        self.assertEqual(len(saves), 1)
        self.assertEqual(accepted["revision"], prior["revision"] + 1)
        self.assertEqual(len(accepted["data"]["plates"]), 1)
        self.assertEqual({o["plate"] for o in accepted["data"]["objects"]}, {accepted["data"]["plates"][0]["id"]})
        status, undone = self.call("PATCH", f"/api/projects/{prior['id']}", {"data": prior["data"], "revision": accepted["revision"]})
        self.assertEqual(status, 200, undone)
        self.assertEqual(self.current()["data"], prior["data"])

    def test_a_plan_spooler_would_refuse_returns_the_refusal_and_stages_nothing(self):
        self.spread()
        record = self.current()
        refusal = {"data": record["data"], "plates": [], "kept": [], "emptied": [], "problems": [{"key": 1}],
                   "refused": "Part 1 would overlap another object", "fingerprint": "x"}
        ctx = spooler_mcp.Context(record["id"], str(self.dir), run="refusal-run")
        with mock.patch.object(placement_read, "_plan_data", return_value=refusal):
            result = spooler_mcp._TOOLS["propose_plan"]["fn"](ctx, revision=record["revision"])
        self.assertIs(result["refused"], True)
        self.assertEqual(result["refused_quoted"], "Part 1 would overlap another object")
        self.assertIsNone(result["proposal"])
        self.assertEqual(self.staging(), [])

    def test_when_the_plan_would_change_nothing_nothing_is_staged(self):
        self.project = self.edit(self.current(), lambda d: [o.update(pinned=True) for o in d["objects"]])
        _, is_error, result = self.propose("propose_plan")
        self.assertFalse(is_error, result)
        self.assertIsNone(result["proposal"])
        self.assertEqual(self.staging(), [])

    def test_a_plan_and_an_arrange_in_one_run_are_both_kept_one_per_kind(self):
        self.spread()
        rev = self.project["revision"]
        view = self.run_scenario([{"tool": "propose_plan", "arguments": {"revision": rev}},
                                  {"tool": "propose_arrange", "arguments": {"revision": rev}},
                                  {"tool": "propose_plan", "arguments": {"revision": rev, "density": "tight"}}])
        self.assertEqual(sorted(c["kind"] for c in view["proposals"]), ["arrange", "plan"])
        self.assertEqual(len(self.staging()), 2)


if __name__ == "__main__":
    unittest.main()
