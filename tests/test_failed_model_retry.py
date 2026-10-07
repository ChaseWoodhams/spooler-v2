"""A model that fails to import says why, and Retry re-queues it without deleting and re-uploading."""
import unittest
import urllib.error
from unittest import mock

import jobs
import projects
from tests.test_import_claim import FIXTURES, GEOMETRY
from tests import test_import_claim
_Base = test_import_claim.ImportClaimTests


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class FailedModelRetryTests(unittest.TestCase):
    setUp, tearDown, call, run_workers, new_project = (_Base.setUp, _Base.tearDown, _Base.call, _Base.run_workers,
                                                       _Base.new_project)
    upload = _Base.upload

    def call_status(self, method, path):
        try:
            return self.call(method, path)
        except urllib.error.HTTPError as e:
            return e.code, None

    def model(self, project_id):
        return self.call("GET", f"/api/projects/{project_id}")[1]["models"][0]

    def test_a_failed_import_says_why_in_plain_words_and_retry_makes_it_ready(self):
        project_id = self.new_project(sorted(FIXTURES.glob("*.stl"))[:1])
        with mock.patch.object(projects, "_run_import", side_effect=jobs.JobError("The mesh has no solid volume")):
            self.run_workers(slots=2)
        failed = self.model(project_id)
        self.assertEqual((failed["state"], failed["error"]), ("failed", "The mesh has no solid volume"))

        status, project = self.call("POST", f"/api/projects/{project_id}/models/{failed['id']}/retry")
        self.assertEqual(status, 200)
        requeued = project["models"][0]
        self.assertEqual((requeued["state"], requeued["error"]), ("importing", None))
        self.assertNotIn("claimed_at", requeued)

        self.run_workers(slots=2)
        ready = self.model(project_id)
        self.assertEqual((ready["state"], ready["error"]), ("ready", None))

    def test_a_file_that_is_not_a_mesh_fails_with_a_readable_reason(self):
        project_id = self.call("POST", "/api/projects", {"name": "Broken"})[1]["id"]
        status, _ = self.call("POST", f"/api/projects/{project_id}/models/upload?filename=broken.stl", raw=b"this is not a mesh at all")
        self.assertEqual(status, 200)
        self.run_workers(slots=1)
        failed = self.model(project_id)
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["error"] and "Traceback" not in failed["error"] and "\n" not in failed["error"].strip(), failed["error"])

    def test_only_a_failed_model_can_be_retried(self):
        project_id = self.new_project(sorted(FIXTURES.glob("*.stl"))[:1])
        self.assertEqual(self.call_status("POST", f"/api/projects/{project_id}/models/1/retry")[0], 409)  # still importing
        self.run_workers(slots=1)
        self.assertEqual(self.call_status("POST", f"/api/projects/{project_id}/models/1/retry")[0], 409)  # ready
        self.assertEqual(self.call_status("POST", f"/api/projects/{project_id}/models/99/retry")[0], 404)

    def test_a_failed_model_can_still_be_deleted(self):
        project_id = self.new_project(sorted(FIXTURES.glob("*.stl"))[:1])
        with mock.patch.object(projects, "_run_import", side_effect=jobs.JobError("nope")):
            self.run_workers(slots=1)
        status, project = self.call("DELETE", f"/api/projects/{project_id}/models/1")
        self.assertEqual((status, project["models"]), (200, []))


del _Base
if __name__ == "__main__":
    unittest.main()
