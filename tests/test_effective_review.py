"""Settings actually used: complete, searchable review rows from the shared effective-settings operation."""
import unittest

import jobs
import projects
import slicer
from tests import test_projects, test_settings_review

MACHINE = slicer.DEFAULT_PRESETS["machine"]
PROCESS = slicer.DEFAULT_PRESETS["process"]


def row(review, key, kind=None):
    found = [r for r in review["review"]["rows"] if r["key"] == key and kind in (None, r["kind"])]
    assert len(found) == 1, (key, [r["key"] for r in review["review"]["rows"]][:20])
    return found[0]


class ReviewRows(test_settings_review.ReviewCase):
    async def project(self, **more):
        project = await self.petg_project()

        def change(data):
            data["profile"]["process"] = {"wall_loops": "4"}
            data["profile"]["filament"] = {"nozzle_temperature": ["245"]}
            data["plates"][0]["overrides"] = {"wall_loops": "6"}
            data.update(more)
        return self.edit(project, change)

    async def test_each_value_shows_inherited_requested_effective_source_chain_and_reset_parent(self):
        project = await self.project()
        review = projects.effective_settings(project["id"], 1)

        walls = row(review, "wall_loops")
        self.assertEqual(walls["effective"], "6")
        self.assertEqual(walls["requested"], "6")
        self.assertEqual(walls["source"], "plate")
        self.assertEqual([c["source"] for c in walls["chain"]], ["preset", "project", "plate"])
        self.assertTrue(walls["changed"])
        self.assertEqual(walls["reset_to"], {"value": "4", "source": "project"})
        self.assertEqual(walls["control"], {"scope": "plate", "kind": "process", "key": "wall_loops"})
        self.assertEqual(walls["inherited"], walls["chain"][0]["value"])

        temperature = row(review, "nozzle_temperature")
        self.assertEqual((temperature["inherited"], temperature["requested"], temperature["effective"]), (["240"], ["245"], ["245"]))
        self.assertEqual(temperature["reset_to"], {"value": ["240"], "source": "preset"})

        untouched = row(review, "layer_height")
        self.assertIsNone(untouched["requested"])
        self.assertFalse(untouched["changed"])
        self.assertIsNone(untouched["reset_to"])

    async def test_bed_winner_is_enforced_and_machine_values_are_read_only(self):
        project = await self.project(bed="smooth")
        review = projects.effective_settings(project["id"], 1)
        bed = row(review, "curr_bed_type")
        self.assertEqual(bed["source"], "bed")
        self.assertTrue(bed["enforced"])
        self.assertIn("bed", bed["enforced_reason"].lower())
        self.assertFalse(bed["editable"])
        nozzle = row(review, "nozzle_diameter")
        self.assertEqual(nozzle["kind"], "machine")
        self.assertTrue(nozzle["read_only"])
        self.assertFalse(nozzle["editable"])
        self.assertIn("machine", nozzle["read_only_reason"].lower())

    async def test_material_nozzle_bed_and_print_values_come_first_and_machine_values_last(self):
        project = await self.project()
        rows = projects.effective_settings(project["id"], 1)["review"]["rows"]
        keys = [r["key"] for r in rows]
        self.assertEqual(keys[0], "filament_type")
        self.assertLess(keys.index("nozzle_diameter"), keys.index("nozzle_temperature"))
        self.assertLess(keys.index("nozzle_temperature"), keys.index("layer_height"))
        self.assertLess(keys.index("curr_bed_type"), keys.index("nozzle_temperature"))
        first_machine = next(i for i, r in enumerate(rows) if r["kind"] == "machine" and r["section"] == "machine")
        self.assertTrue(all(r["kind"] == "machine" for r in rows[first_machine:]))

    async def test_review_is_labelled_expected_not_measured(self):
        project = await self.project()
        meta = projects.effective_settings(project["id"], 1)["review"]
        self.assertEqual(meta["evidence"], "expected-inputs")
        self.assertIn("will be sent to Orca", meta["label"])
        self.assertIn("not measured", meta["label"])

    async def test_search_matches_labels_and_keys_and_changed_filter_keeps_only_changed_values(self):
        project = await self.project()
        by_key = projects.effective_settings(project["id"], 1, search="wall_loops")["review"]["rows"]
        self.assertEqual([r["key"] for r in by_key], ["wall_loops"])
        label = projects.effective_settings(project["id"], 1, search="  NOZZLE temperature ")["review"]["rows"]
        self.assertIn("nozzle_temperature", [r["key"] for r in label])
        changed = projects.effective_settings(project["id"], 1, changed_only=True)["review"]["rows"]
        self.assertIn("wall_loops", [r["key"] for r in changed])
        self.assertNotIn("layer_height", [r["key"] for r in changed])
        self.assertTrue(all(r["changed"] for r in changed))
        self.assertEqual(projects.effective_settings(project["id"], 1, search="no-such-setting")["review"]["rows"], [])


class ReviewDrillDownAndExplanations(test_settings_review.ReviewCase):
    async def project(self, extra_model=False, **changes):
        project = await self.petg_project(extra_model=extra_model)
        changes = {"process": {"travel_speed": "200"}, **changes}

        def change(data):
            data["plates"][0]["overrides"] = {"wall_loops": "6"}
            data["objects"][0]["overrides"] = {"wall_loops": "8"}
            for key, value in changes.items():
                data["profile"][key] = value
        return self.edit(project, change)

    async def test_object_drill_down_uses_the_server_chain_and_resets_to_the_plate(self):
        project = await self.project()
        oid = project["data"]["objects"][0]["id"]
        review = projects.effective_settings(project["id"], 1, object_id=oid)
        self.assertEqual(review["review"]["scope"]["kind"], "object")
        self.assertEqual(review["review"]["scope"]["id"], oid)
        walls = row(review, "wall_loops")
        self.assertEqual((walls["effective"], walls["source"]), ("8", "object"))
        self.assertEqual([c["source"] for c in walls["chain"]], ["preset", "plate", "object"])
        self.assertTrue(walls["changed"])
        self.assertEqual(walls["reset_to"], {"value": "6", "source": "plate"})
        self.assertEqual(walls["control"]["scope"], "object")
        plate_review = projects.effective_settings(project["id"], 1)
        self.assertEqual(row(plate_review, "wall_loops")["effective"], "6")      # the Plate view is unaffected
        travel = row(review, "travel_speed")
        self.assertFalse(travel["editable"])
        self.assertIn("Object scope", travel["read_only_reason"])
        changed = projects.effective_settings(project["id"], 1, object_id=oid, changed_only=True)["review"]["rows"]
        self.assertEqual([r["key"] for r in changed], ["wall_loops"])
        with self.assertRaises(jobs.JobError) as missing:
            projects.effective_settings(project["id"], 1, object_id=9999)
        self.assertEqual(missing.exception.status, 404)

    async def test_bricklayers_enforced_values_name_what_was_requested_and_why(self):
        project = await self.project(bricklayers={"enabled": True}, process={"wall_generator": "arachne"})
        walls = row(projects.effective_settings(project["id"], 1), "wall_generator")
        self.assertTrue(walls["enforced"])
        self.assertEqual((walls["requested"], walls["effective"], walls["source"]), ("arachne", "classic", "bricklayers"))
        self.assertIn("BrickLayers", walls["enforced_reason"])
        self.assertIn("Requested arachne", walls["note"])
        self.assertFalse(walls["editable"])
        self.assertEqual(walls["reset_to"], {"value": None, "source": "orca-default"})      # the preset does not set it, so Orca decides

    async def test_alternate_filament_replaces_default_tuning_and_spoolman_applies_to_the_default_only(self):
        project = await self.project(extra_model=True, filament={"nozzle_temperature": ["245"]}, spool_id=7)
        default, alternate = (projects.effective_settings(project["id"], n) for n in (1, 2))
        self.assertEqual(default["review"]["filament_basis"]["kind"], "default")
        self.assertIn("Spoolman spool #7", default["review"]["filament_basis"]["explanation"])
        self.assertIn("default Filament only", default["review"]["filament_basis"]["explanation"])
        self.assertEqual(row(default, "nozzle_temperature")["effective"], ["245"])
        self.assertIn("Spoolman", row(default, "nozzle_temperature")["note"])
        self.assertEqual(alternate["review"]["filament_basis"]["kind"], "alternate")
        self.assertIn("replaces the Project default", alternate["review"]["filament_basis"]["explanation"])
        asa = row(alternate, "nozzle_temperature")
        self.assertEqual((asa["effective"], asa["source"], asa["requested"], asa["note"]), (["260"], "alternate-preset", None, None))
        self.assertEqual(row(alternate, "filament_type")["effective"], ["ASA"])

    async def test_unchecked_inherited_keys_are_read_only_and_chamber_control_is_not_claimed(self):
        project = await self.project(filament={"activate_chamber_temp_control": ["1"]})
        review = projects.effective_settings(project["id"], 1)
        schema = slicer.settings_schema()["options"]
        passthrough = [r for r in review["review"]["rows"] if r["kind"] != "machine" and r["key"] not in schema and not r["enforced"]]
        self.assertTrue(passthrough)
        for r in passthrough:
            self.assertFalse(r["editable"], r["key"])
            self.assertIsNone(r["control"])
            self.assertIn("passed through unchanged", r["read_only_reason"])
        self.assertTrue(all(not r["editable"] for r in review["review"]["rows"] if r["kind"] == "machine"))
        chamber = [r for r in review["review"]["rows"] if "chamber" in r["key"]]
        self.assertTrue(chamber)
        self.assertTrue(all("does not claim control of a chamber heater" in r["note"] for r in chamber))

    async def test_inactive_dependency_keeps_the_saved_value_and_names_the_reason(self):
        project = await self.project(filament={"filament_retraction_length": ["0"], "filament_retraction_speed": ["30"]})
        review = projects.effective_settings(project["id"], 1)
        speed = row(review, "filament_retraction_speed")
        self.assertEqual(speed["effective"], ["30"])
        self.assertEqual(speed["section"], "retraction")
        self.assertTrue(speed["inactive"])
        self.assertTrue(speed["inactive_reason"])
        self.assertFalse(row(review, "filament_retraction_length")["inactive"])

    async def test_issues_link_to_their_owning_controls(self):
        project = await self.project()
        oid = project["data"]["objects"][0]["id"]
        legacy = lambda d: d["objects"][0].update(overrides={"travel_speed": "250"})
        from unittest import mock
        with mock.patch.object(projects, "validate_data", side_effect=lambda data, record=None: data):
            self.edit(project, legacy)
        review = projects.effective_settings(project["id"], 1)
        issue = next(i for i in review["issues"] if i.get("code") == "unsupported-object-setting")
        self.assertEqual(issue["control"], {"scope": "object", "kind": "process", "key": "travel_speed", "id": oid})
