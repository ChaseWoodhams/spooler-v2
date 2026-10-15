"""Settings Proposals through the project API with the fake `claude` that starts the real Spooler MCP server: ask -> propose_settings
-> staged -> review view (settings rows) -> Accept as one revision with the prior data restorable (the client's Undo) -> 409."""
import hashlib
import json
import unittest

import datapaths
import profile_read
import projects
from tests import test_assistant_proposals as proposals
from tests.fake_claude import read_record

PRESET = {"sparse_infill_density": "15%", "sparse_infill_pattern": "gyroid", "enable_support": "0", "top_shell_layers": "4",
          "outer_wall_speed": "200", "brim_type": "auto_brim"}


class SettingsApiCase(proposals.ProposalCase):
    def setUp(self):
        super().setUp()
        bundle = self.current()["data"]["profile"]["bundle_id"]
        self.assertTrue(bundle, "the test project has a locked profile")
        path = datapaths.profiles_dir() / bundle / "process.json"
        process = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**process, **PRESET}), encoding="utf-8")        # the MCP server reads the preset as it is on disk
        manifest = path.parent / "bundle.json"                                      # keep the locked profile verifiable for slicing
        record = json.loads(manifest.read_text(encoding="utf-8"))
        record["presets"]["process"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(record), encoding="utf-8")

    def settings_call(self, scope="project", changes=None, scope_id=None, revision=None, **extra):
        arguments = {"revision": self.project["revision"] if revision is None else revision, "scope": scope, "changes": changes or {}, **extra}
        if scope_id is not None:
            arguments["id"] = scope_id
        return {"tool": "propose_settings", "arguments": arguments}

    def propose(self, *calls):
        view = self.run_scenario(list(calls) or [self.settings_call(changes={"wall_loops": 5, "sparse_infill_density": "40%"}, reason="Stronger arms")])
        self.assertEqual(view["state"], "proposal", view)
        return view, view["proposals"][0]["token"]

    def path(self, token, action=""):
        return f"/api/projects/{self.project['id']}/assistant/proposals/{token}{action}"


class SettingsProposalApiTests(SettingsApiCase):
    def test_the_card_summary_shows_key_old_to_new_with_the_layer_of_the_old_value(self):
        view, token = self.propose()
        card, = view["proposals"]
        self.assertEqual((card["kind"], card["review"], card["scope"], card["reason"]), ("settings", "settings", "Whole project", "Stronger arms"))
        self.assertEqual([(line["label"], line["text"]) for line in card["diff"]],
                         [("wall_loops:", "3 → 5 (was the profile's value)"), ("sparse_infill_density:", "15% → 40% (was the profile's value)")])
        self.assertNotIn("data", card)
        self.assertNotIn("settings", card, "the table is for the review sheet, the card has the diff")
        (is_error, result), = self.results()
        self.assertFalse(is_error, result)
        self.assertEqual(self.staged(token)["base_revision"], self.project["revision"])
        self.assertEqual(self.current()["revision"], self.project["revision"], "proposing saves nothing")

    def test_the_review_view_has_settings_rows_and_no_placement_sides(self):
        _, token = self.propose(self.settings_call(changes={"wall_loops": 5, "top_shell_layers": 6, "enable_support": 0, "support_threshold_angle": 40}))
        status, review = self.call("GET", self.path(token))
        self.assertEqual(status, 200, review)
        self.assertEqual((review["kind"], review["review"], review["revision"]), ("settings", "settings", self.project["revision"]))
        self.assertNotIn("before", review)
        self.assertNotIn("after", review)
        rows = {r["key"]: r for r in review["settings"]}
        self.assertEqual({k: (r["old"], r["new"], r["layer"]) for k, r in rows.items() if k != "enable_support"},
                         {"wall_loops": ("3", "5", "preset"), "top_shell_layers": ("4", "6", "preset"), "support_threshold_angle": (rows["support_threshold_angle"]["old"], "40", "preset")})
        self.assertNotIn("enable_support", rows, "already off, so dropped as a no-op")
        self.assertEqual(rows["wall_loops"]["label"], "Wall loops")
        self.assertEqual(rows["wall_loops"]["notes"], [])
        self.assertTrue(rows["support_threshold_angle"]["notes"][0].startswith("No effect because"), rows["support_threshold_angle"])
        self.assertNotIn("data", review)
        self.assertEqual(self.current()["revision"], self.project["revision"], "reviewing changes nothing")

    def test_a_plate_proposal_reviews_with_the_plates_name_as_scope(self):
        _, token = self.propose(self.settings_call("plate", {"wall_loops": 6}, scope_id=1))
        status, review = self.call("GET", self.path(token))
        self.assertEqual(status, 200, review)
        self.assertEqual(review["scope"], self.current()["data"]["plates"][0]["name"])
        self.assertEqual(review["settings"][0]["layer"], "preset")

    def test_accept_saves_one_revision_with_the_overrides_and_the_prior_data_can_be_put_back(self):
        _, token = self.propose()
        prior = self.current()
        status, result = self.call("POST", self.path(token, "/accept"), {"revision": prior["revision"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["revision"], prior["revision"] + 1, "one revision")
        self.assertEqual(result["data"]["profile"]["process"], {**prior["data"]["profile"]["process"], "wall_loops": "5", "sparse_infill_density": "40%"})
        self.assertEqual(result["proposal"]["kind"], "settings")
        self.assertEqual(self.current()["data"], result["data"])
        self.assertEqual(self.staging(), [], "staging is swept on accept")
        status, undone = self.call("PATCH", f"/api/projects/{prior['id']}", {"data": prior["data"], "revision": result["revision"]})
        self.assertEqual(status, 200, undone)
        self.assertEqual(self.current()["data"], prior["data"])

    def test_accept_of_an_object_proposal_writes_the_object_override(self):
        _, token = self.propose(self.settings_call("object", {"wall_loops": 6, "brim_type": "outer_only"}, scope_id=1))
        prior = self.current()
        status, result = self.call("POST", self.path(token, "/accept"), {"revision": prior["revision"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["data"]["objects"][0]["overrides"], {"wall_loops": "6", "brim_type": "outer_only"})
        self.assertEqual(result["data"]["objects"][1]["overrides"], {})

    def test_accept_makes_the_plates_slice_outdated(self):
        projects.request_slices(self.project["id"], [1])
        self.settle()
        self.project = self.current()
        self.assertIs(self.project["slices"]["1"]["outdated"], False)
        _, token = self.propose()
        status, result = self.call("POST", self.path(token, "/accept"), {"revision": self.project["revision"]})
        self.assertEqual(status, 200, result)
        self.assertIs(self.current()["slices"]["1"]["outdated"], True)

    def test_accept_after_any_edit_is_409_and_superseded_and_the_edit_stands(self):
        _, token = self.propose()
        edited = self.edit(self.current(), lambda d: d["objects"][2].update(position=[30, 30]))
        status, refused = self.call("POST", self.path(token, "/accept"), {"revision": edited["revision"]})
        self.assertEqual(status, 409, refused)
        self.assertIs(refused["superseded"], True)
        self.assertEqual(self.current()["data"], edited["data"])
        self.assertEqual(self.staging(), [])
        self.assertEqual(self.call("GET", self.path(token))[0], 404)

    def test_review_of_a_superseded_proposal_is_409(self):
        _, token = self.propose()
        self.edit(self.current(), lambda d: d["objects"][2].update(position=[30, 30]))
        status, refused = self.call("GET", self.path(token))
        self.assertEqual(status, 409, refused)
        self.assertIs(refused["superseded"], True)

    def test_dismiss_throws_it_away_and_changes_nothing(self):
        _, token = self.propose()
        self.assertEqual(self.call("POST", self.path(token, "/dismiss"), {})[0], 200)
        self.assertEqual(self.staging(), [])
        self.assertEqual(self.current()["data"], self.project["data"])

    def test_a_refused_key_ends_the_run_as_an_answer_with_nothing_staged(self):
        view = self.run_scenario([self.settings_call(changes={"nozzle_temperature": 300, "outer_wall_speed": 400})])
        (is_error, result), = self.results()
        self.assertFalse(is_error)
        self.assertIsNone(result["proposal"])
        self.assertEqual(sorted(r["key"] for r in result["refused"]), ["nozzle_temperature", "outer_wall_speed"])
        self.assertEqual(view["state"], "answer")
        self.assertEqual(self.staging(), [])

    def test_a_valid_and_a_refused_key_stage_the_valid_one_and_show_the_refusal_on_the_card(self):
        view, token = self.propose(self.settings_call(changes={"wall_loops": 4, "default_acceleration": 9000}))
        card, = view["proposals"]
        self.assertEqual([line["label"] for line in card["diff"]], ["wall_loops:"])
        self.assertTrue(any("default_acceleration" in p for p in card["problems"]), card["problems"])

    def test_the_settings_tools_run_through_the_cli_and_the_system_prompt_scopes_a_stated_purpose_to_one_proposal(self):
        self.run_scenario([{"tool": "get_settings", "arguments": {"scope": "project"}},
                           {"tool": "describe_setting", "arguments": {"key": "wall_loops"}}])
        results = self.results()
        self.assertEqual([is_error for is_error, _ in results], [False, False])
        self.assertTrue(any(row["key"] == "wall_loops" and row["proposable"] for row in results[0][1]["settings"]))
        self.assertIn("applies to that Proposal only", read_record(self.cli_dir)["system_prompt"])


if __name__ == "__main__":
    unittest.main()
