"""The explicit lossy whole-Project 3MF (ticket 13), tested at the Project service and HTTP boundary.

The faithful export and its plan are ticket 11 (tests/test_project_export.py). Here the convenience alternative must
report what it would change, and only an acknowledgement bound to the reviewed plan may generate it."""
import copy
import io
import json
import unittest
from unittest import mock

import jobs
import orca_3mf
import projects
import slicer
from tests import test_projects
from tests.test_project_export import ExportCase
from tests.test_projects import template_runner

LOSSY = "lossy-3mf"


class LossyCase(ExportCase):
    async def mixed_project(self):
        """Front keeps the Project's PLA and wall_loops 5; Back is ASA with wall_loops 4: no single Orca file keeps both."""
        slicer.save_user_preset("filament", "Workshop ASA", "Elegoo PLA @ECC", {"filament_type": ["ASA"], "nozzle_temperature": ["260"]})
        project = await self.two_plate_project()
        data = copy.deepcopy(project["data"])
        data["filaments"] = {"2": {"preset": "Workshop ASA", "colour": "black"}}
        data["plates"][1]["overrides"] = {"wall_loops": "4"}
        return projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

    def acknowledgement(self, plan, **changes):
        lossy = plan["lossy"]
        return {"representation": lossy["representation"], "revision": plan["revision"], "fingerprint": plan["fingerprint"],
                "report_version": lossy["report_version"], "report_fingerprint": lossy["report_fingerprint"], "acknowledged": True, **changes}


class LossyReportTests(LossyCase):
    async def test_the_plan_offers_a_lossy_alternative_with_the_exact_differences_per_scope(self):
        project = await self.mixed_project()

        plan = projects.export_plan(project["id"])

        self.assertFalse(plan["faithful"])
        self.assertIsNone(plan["filename"], "the faithful action is still not offered")
        lossy = plan["lossy"]
        self.assertTrue(lossy["available"], lossy)
        self.assertEqual((lossy["representation"], lossy["report_version"]), (LOSSY, 1))
        self.assertEqual((lossy["filename"], lossy["content_type"]), ("Bench set (lossy).3mf", "model/3mf"))
        self.assertEqual((lossy["basis"]["plate_id"], lossy["basis"]["plate_name"], lossy["basis"]["material"]["type"]), (1, "Front", "PLA"))
        back = next(p for p in lossy["plates"] if p["id"] == 2)
        self.assertTrue(back["material_changed"])
        self.assertEqual((back["material"]["type"], back["material"]["preset"]), ("ASA", "Workshop ASA"))
        self.assertEqual((back["written_as"]["type"], back["written_as"]["preset"]), ("PLA", "Elegoo PLA @ECC"))
        self.assertFalse(next(p for p in lossy["plates"] if p["id"] == 1)["material_changed"])
        by_key = {d["key"]: d for d in lossy["differences"]}
        self.assertEqual((by_key["filament_type"]["category"], by_key["filament_type"]["written"]), ("material", ["PLA"]))
        self.assertEqual(by_key["filament_type"]["plates"], [{"id": 1, "name": "Front", "value": ["PLA"], "kept": True},
                                                            {"id": 2, "name": "Plate 2", "value": ["ASA"], "kept": False}])
        self.assertEqual((by_key["wall_loops"]["category"], by_key["wall_loops"]["written"]), ("plate-settings", "5"))
        self.assertEqual([(p["id"], p["value"], p["kept"]) for p in by_key["wall_loops"]["plates"]], [(1, "5", True), (2, "4", False)])
        self.assertEqual(lossy["faithful_alternatives"][0]["representation"], "single-3mf")
        self.assertFalse(lossy["faithful_alternatives"][0]["available"])

    async def test_a_project_one_file_can_keep_has_no_lossy_alternative(self):
        project = await self.two_plate_project()
        plan = projects.export_plan(project["id"])
        self.assertTrue(plan["faithful"])
        self.assertFalse(plan["lossy"]["available"])
        self.assertEqual(plan["lossy"]["reason"], "faithful-available")
        self.assertIsNone(plan["lossy"]["filename"])


class LossyGenerationTests(LossyCase):
    def lossy(self, project, ack=None, runner=None):
        plan = projects.export_plan(project["id"])
        with mock.patch.object(slicer, "runner", runner or template_runner()):
            return plan, projects.export_lossy(project["id"], ack if ack is not None else self.acknowledgement(plan))

    async def test_an_acknowledgement_bound_to_the_report_generates_a_truthfully_named_3mf_with_the_basis_configuration(self):
        project = await self.mixed_project()

        plan, artifact = self.lossy(project)

        self.assertEqual((artifact["filename"], artifact["content_type"]), ("Bench set (lossy).3mf", "model/3mf"))
        self.assertEqual(artifact["body"][:2], b"PK")
        parsed = orca_3mf.parse(io.BytesIO(artifact["body"]))                      # an ordinary Orca 3MF, not a package
        self.assertEqual([o["name"] for o in parsed["objects"]], ["Bench", "Lid"])
        self.assertEqual(parsed["project"]["filament_settings_id"], ["Elegoo PLA @ECC"])   # Front's material for every Plate
        self.assertEqual(parsed["project"]["filament_type"], ["PLA"])
        self.assertEqual(parsed["project"]["wall_loops"], "5")
        self.assertEqual([o["settings"] for o in parsed["objects"]], [{"wall_loops": "6"}, {}])
        self.assertEqual(artifact["plan"]["revision"], plan["revision"])

    async def test_the_faithful_routes_never_select_the_lossy_file(self):
        project = await self.mixed_project()
        plan = projects.export_plan(project["id"])
        with mock.patch.object(slicer, "runner", template_runner()):
            for operation in (lambda: projects.export_orca_project(project["id"]),
                              lambda: projects.export_project(project["id"], plan["revision"], plan["fingerprint"])):
                with self.assertRaises(jobs.JobError) as refused:
                    operation()
                self.assertEqual(refused.exception.issues[0]["code"], "export-review-required")

    async def test_no_acknowledgement_or_a_different_representation_generates_nothing(self):
        project = await self.mixed_project()
        plan = projects.export_plan(project["id"])
        runner = template_runner()
        for changes, code in ((dict(acknowledged=False), "export-acknowledgement-required"), (dict(acknowledged="yes"), "export-acknowledgement-required"),
                              (dict(representation="single-3mf"), "export-representation-mismatch"),
                              (dict(representation="package-zip"), "export-representation-mismatch")):
            with self.subTest(changes), mock.patch.object(slicer, "runner", runner):
                with self.assertRaises(jobs.JobError) as refused:
                    projects.export_lossy(project["id"], self.acknowledgement(plan, **changes))
                self.assertEqual((refused.exception.status, refused.exception.issues[0]["code"]), (409, code))
        with mock.patch.object(slicer, "runner", runner):
            with self.assertRaises(jobs.JobError) as refused:
                projects.export_lossy(project["id"], {"acknowledged": True})
        self.assertEqual(refused.exception.issues[0]["code"], "export-acknowledgement-stale")
        self.assertEqual(runner.calls, [], "nothing was generated")

    async def test_every_bound_value_makes_a_stale_acknowledgement_unusable(self):
        project = await self.mixed_project()
        plan = projects.export_plan(project["id"])
        runner = template_runner()
        for changes in (dict(revision=plan["revision"] - 1), dict(fingerprint="0" * 64), dict(report_version=2), dict(report_fingerprint="0" * 64)):
            with self.subTest(changes), mock.patch.object(slicer, "runner", runner):
                with self.assertRaises(jobs.JobError) as stale:
                    projects.export_lossy(project["id"], self.acknowledgement(plan, **changes))
                self.assertEqual((stale.exception.status, stale.exception.issues[0]["code"]), (409, "export-acknowledgement-stale"))
        self.assertEqual(runner.calls, [])

    async def test_an_edit_after_the_review_invalidates_the_acknowledgement(self):
        project = await self.mixed_project()
        plan = projects.export_plan(project["id"])
        ack = self.acknowledgement(plan)
        data = copy.deepcopy(project["data"])
        data["plates"][1]["overrides"] = {"wall_loops": "3"}
        changed = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})
        with mock.patch.object(slicer, "runner", template_runner()):
            with self.assertRaises(jobs.JobError) as stale:
                projects.export_lossy(project["id"], ack)
            self.assertEqual(stale.exception.issues[0]["code"], "export-acknowledgement-stale")
            self.assertEqual(stale.exception.issues[0]["revision"], changed["revision"])
            fresh = projects.export_plan(project["id"])
            self.assertNotEqual(fresh["lossy"]["report_fingerprint"], plan["lossy"]["report_fingerprint"])
            self.assertTrue(projects.export_lossy(project["id"], self.acknowledgement(fresh))["body"])

    async def test_blockers_stay_blockers_and_a_faithful_project_has_nothing_to_acknowledge(self):
        project = await self.mixed_project()
        with mock.patch.object(slicer, "orca_executable", return_value=None):
            plan = projects.export_plan(project["id"])
            self.assertFalse(plan["lossy"]["available"])
            self.assertEqual(plan["lossy"]["reason"], "blocked")
            with self.assertRaises(jobs.JobError) as refused:
                projects.export_lossy(project["id"], self.acknowledgement({**plan, "lossy": {**plan["lossy"], "report_fingerprint": "x"}}))
        self.assertEqual(refused.exception.issues[0]["code"], "export-template-unavailable")
        simple = await self.two_plate_project()
        plan = projects.export_plan(simple["id"])
        with mock.patch.object(slicer, "runner", template_runner()):
            with self.assertRaises(jobs.JobError) as refused:
                projects.export_lossy(simple["id"], {"representation": LOSSY, "revision": plan["revision"], "fingerprint": plan["fingerprint"],
                                                     "report_version": 1, "report_fingerprint": "x", "acknowledged": True})
        self.assertEqual(refused.exception.issues[0]["code"], "export-lossy-unavailable")


class LossyDisclosureTests(LossyCase):
    async def test_an_inherited_value_spooler_cannot_check_is_listed_and_carried_unchanged(self):
        # No fixture preset reaches this state through the Project interface (Spooler drops keys it has no definition for when
        # it snapshots a preset), so the real per-Plate reviews are given the issue the review raises for such a value.
        import export_lossy
        project = await self.mixed_project()
        record = projects.get_project(project["id"])
        plan = projects.export_plan(project["id"])
        entries = [(p, projects.effective_settings(project["id"], p["id"], record=record)) for p in record["data"]["plates"]]
        entries[1][1]["issues"].append({"code": "unresolved-inherited-setting", "scope": "preset", "kind": "filament", "key": "workshop_only_setting",
                                        "value": ["7"], "severity": "error", "resolution_required": True,
                                        "message": "workshop_only_setting: inherited setting has no pinned definition"})

        lossy = export_lossy.alternative(record, plan, entries, [])

        self.assertEqual([(i["kind"], i["key"], i["value"], i["plates"]) for i in lossy["inherited"]], [("filament", "workshop_only_setting", ["7"], ["Plate 2"])])
        self.assertTrue(lossy["available"], "an inherited locked value is disclosed, not a blocker")

    async def test_brick_layers_is_explained_and_the_slice_gcode_stays_a_separate_action(self):
        project = await self.mixed_project()
        data = copy.deepcopy(project["data"])
        data["plates"][1]["bricklayers"] = {"enabled": True}
        project = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

        lossy = projects.export_plan(project["id"])["lossy"]

        self.assertEqual(lossy["bricklayers"]["plates"], ["Plate 2"])
        for phrase in ("cannot reproduce", "G-code", "separate download"):
            self.assertIn(phrase, lossy["bricklayers"]["message"])

    async def test_without_brick_layers_there_is_no_brick_layers_claim(self):
        project = await self.mixed_project()
        self.assertIsNone(projects.export_plan(project["id"])["lossy"]["bricklayers"])

    async def test_accepted_import_losses_are_listed_with_the_record_that_keeps_them(self):
        from tests.orca_archives import archive
        from tests.test_import_review import PROJECT, TWO_PLATES
        project = await self.mixed_project()
        data = archive(project=PROJECT, **TWO_PLATES)
        review = projects.prepare_import(io.BytesIO(data), len(data), "Imported.3mf", target=project["id"])
        review = projects.resolve_import(review["token"], {"conflicts": "convert"}) if review["report"]["blockers"] else review
        projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})
        await self.run_worker()

        lossy = projects.export_plan(project["id"])["lossy"]

        (record,) = lossy["features"]
        self.assertEqual(record["record_id"], projects.import_records(project["id"])[0]["id"])
        self.assertEqual(sorted((l["code"], l["name"]) for l in record["losses"]), [("negative-volume", "Beta"), ("paint-supports", "Alpha")])
        self.assertTrue(all(l["effect"] for l in record["losses"]))


class LossyHttpTests(LossyCase):
    """The same operations over HTTP, as the Project page calls them."""
    setUp = test_projects.ProjectApiTests.setUp
    tearDown = test_projects.ProjectApiTests.tearDown
    call = test_projects.ProjectApiTests.call
    asyncTearDown = test_projects.ProjectTests.asyncTearDown
    run_worker = test_projects.ProjectTests.run_worker

    def fetch(self, method, path, body=None):
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with test_projects._opener.open(request, timeout=20) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    async def test_the_plan_offers_the_alternative_and_only_an_acknowledged_post_downloads_it(self):
        project = await self.mixed_project()
        pid = project["id"]
        with mock.patch.object(slicer, "runner", template_runner()):
            code, plan = self.call("GET", f"/api/projects/{pid}/export-plan")
            self.assertEqual((code, plan["faithful"], plan["lossy"]["available"], plan["lossy"]["filename"]), (200, False, True, "Bench set (lossy).3mf"))

            for path in (f"/api/projects/{pid}/export.3mf",
                         f"/api/projects/{pid}/export.3mf?revision={plan['revision']}&fingerprint={plan['fingerprint']}",
                         f"/api/projects/{pid}/export.3mf?representation=lossy-3mf&acknowledged=1"):
                code, refused = self.call("GET", path)
                self.assertEqual((code, refused["issues"][0]["code"]), (409, "export-review-required"), path)

            path = f"/api/projects/{pid}/export-lossy"
            for body, status, issue in (({**self.acknowledgement(plan), "acknowledged": False}, 409, "export-acknowledgement-required"),
                                        ({**self.acknowledgement(plan), "representation": "single-3mf"}, 409, "export-representation-mismatch"),
                                        ({**self.acknowledgement(plan), "revision": plan["revision"] + 1}, 409, "export-acknowledgement-stale"),
                                        ({}, 409, "export-acknowledgement-stale")):
                code, headers, raw = self.fetch("POST", path, body)
                self.assertEqual((code, json.loads(raw)["issues"][0]["code"]), (status, issue), body)

            code, headers, raw = self.fetch("POST", path, self.acknowledgement(plan))
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "model/3mf")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Bench set (lossy).3mf"')
        self.assertEqual([o["name"] for o in orca_3mf.parse(io.BytesIO(raw))["objects"]], ["Bench", "Lid"])
