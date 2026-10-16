"""Arrange and Plan plates lay out the ready objects, skip those of failed models and say so, through the project HTTP API."""
import json
import unittest

import database
import projects
from tests import test_arrange
from tests.test_plan_plates import legacy_plan
from tests.test_slicer import cube_stl

_Base = test_arrange.ArrangeApiTests


@unittest.skipUnless(test_arrange.GEOMETRY, "geometry libraries not installed")
class ArrangeSkipsFailedModelsTests(unittest.TestCase):
    setUp, tearDown, call, project_with, save, lay, arrange = (_Base.setUp, _Base.tearDown, _Base.call, _Base.project_with,
                                                               _Base.save, _Base.lay, _Base.arrange)

    def fail_model(self, project_id, model_id):
        with database.transaction() as conn:
            models = json.loads(conn.execute("SELECT models FROM projects WHERE id = ?", (project_id,)).fetchone()["models"])
            for m in models:
                if m["id"] == model_id:
                    m.update(state="failed", error="The mesh has no solid volume")
            conn.execute("UPDATE projects SET models = ? WHERE id = ?", (json.dumps(models), project_id))

    def two_models_one_failed(self):
        """Model 1 (ready cube) has objects 1, 2 stacked at the same spot; model 2 (failed) has object 3, on plate 1."""
        project = self.project_with(cube_stl(20), cube_stl(30))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 1, (50, 50), False), (2, 1, (120, 120), False)])
        self.fail_model(project["id"], 2)
        return projects.get_project(project["id"])

    def test_arrange_one_plate_lays_out_the_ready_objects_and_reports_the_skipped_one(self):
        project = self.two_models_one_failed()
        status, result = self.arrange(project, plate=1)
        self.assertEqual(status, 200, result)
        objects = {o["id"]: o for o in result["data"]["objects"]}
        self.assertNotEqual(objects[1]["position"], objects[2]["position"])
        self.assertEqual((objects[3]["plate"], objects[3]["position"]), (1, [120.0, 120.0]))    # the failed one stays put
        self.assertIn("1 object", result["outcome"])
        self.assertIn("failed", result["outcome"])

    def test_arrange_all_does_the_same(self):
        project = self.two_models_one_failed()
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        objects = {o["id"]: o for o in result["data"]["objects"]}
        self.assertNotEqual(objects[1]["position"], objects[2]["position"])
        self.assertEqual(objects[3]["plate"], 1)
        self.assertIn("skipped", result["outcome"])
        self.assertIn("failed", result["outcome"])

    def test_a_plate_holding_a_failed_models_object_is_never_removed(self):
        project = self.project_with(cube_stl(20), cube_stl(30))
        project = self.lay(project, [(1, 1, (50, 50), False), (2, 2, (120, 120), False)])
        self.fail_model(project["id"], 2)
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertEqual({o["id"]: o["plate"] for o in result["data"]["objects"]}[2], 2)
        self.assertIn(2, [p["id"] for p in result["data"]["plates"]])

    def test_with_nothing_ready_the_plate_refusal_keeps_its_reason(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False)])
        self.fail_model(project["id"], 1)
        status, result = self.arrange(project, plate=1)
        self.assertEqual(status, 409)
        self.assertIn("nothing ready", result["error"].lower())
        self.assertIn("failed", result["error"])

    def test_arrange_all_with_nothing_ready_still_says_so(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False)])
        self.fail_model(project["id"], 1)
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertIn("Nothing", result["outcome"])
        self.assertIn("failed", result["outcome"])

    def test_every_arrange_response_has_an_outcome_even_when_nothing_changed(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (128, 128), False)])
        status, first = self.arrange(project)
        self.assertEqual(status, 200, first)
        self.assertTrue(first["outcome"])
        status, second = self.arrange(first)
        self.assertEqual(status, 200, second)
        self.assertTrue(second["outcome"])
        self.assertTrue(self.arrange(second, plate=1)[1]["outcome"])

    def test_plan_plates_plans_the_ready_objects_and_reports_the_skipped_one(self):
        project = self.two_models_one_failed()
        status, plan = legacy_plan(project["id"])
        self.assertEqual(status, 200, plan)
        planned = [o["id"] for p in plan["plates"] for o in p["objects"]]
        self.assertEqual(sorted(planned), [1, 2])
        self.assertIn("skipped", plan["summary"])
        self.assertIn("failed", plan["summary"])
        status, accepted = self.call("POST", f"/api/projects/{project['id']}/plan/accept",
                                     {"revision": plan["revision"], "fingerprint": plan["fingerprint"]})
        self.assertEqual(status, 200, accepted)
        self.assertEqual({o["id"]: o["plate"] for o in accepted["data"]["objects"]}[3], 1)
        self.assertIn("skipped", accepted["plan"]["summary"])

    def test_plan_plates_with_nothing_ready_says_why(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False)])
        self.fail_model(project["id"], 1)
        status, plan = legacy_plan(project["id"])
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["plates"], [])
        self.assertIn("failed", plan["summary"])


del _Base   # so the borrowed test class isn't collected here too

if __name__ == "__main__":
    unittest.main()
