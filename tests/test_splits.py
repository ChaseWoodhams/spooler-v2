import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database
import jobs
import slicer
import splits
import state
from tests.test_jobs import FakePrinter
from tests.test_slicer import fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
if GEOMETRY:
    import trimesh

    from tests.test_split_engine import channel, export


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class SplitWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_printers = state.printers
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        splits.configure(self.dir)
        splits.init()
        state.printers = {"p1": FakePrinter()}

    async def asyncTearDown(self):
        for task in list(jobs._start_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)

    def tearDown(self):
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        self._tmp.cleanup()

    def model_bytes(self, model=None) -> bytes:
        path = self.dir / "upload.stl"
        export(model or channel(), path, soup=True)
        return path.read_bytes()

    def upload(self, data=None, name="channel.stl", **settings):
        data = self.model_bytes() if data is None else data
        return splits.create_split(io.BytesIO(data), len(data), name, settings)

    async def run_stages(self):
        while await splits.process_next():
            pass

    async def test_upload_plan_edit_split_and_queue(self):
        record = self.upload()
        self.assertEqual(record["state"], splits.ANALYZING)
        await self.run_stages()
        detail = splits.get_split(record["id"], detail=True)
        self.assertEqual(detail["state"], splits.READY)
        self.assertEqual(detail["analysis"]["size"], [520.0, 140.0, 90.0])
        self.assertTrue(all(p["fits"] for p in detail["check"]["pieces"]))

        cuts = detail["cuts"]["cuts"]
        cuts[0]["offset"] += 5
        self.assertEqual(splits.save_cuts(record["id"], {"cuts": cuts})["state"], splits.CHECKING)
        with self.assertRaises(jobs.JobError) as busy:
            splits.request_split(record["id"])
        self.assertEqual(busy.exception.status, 409)
        await self.run_stages()
        detail = splits.get_split(record["id"], detail=True)
        self.assertEqual((detail["state"], detail["check"]["cuts"][0]["offset"]), (splits.READY, cuts[0]["offset"]))

        splits.update_settings(record["id"], {"connectors": {"diameter": 1.75, "kind": "pin", "fallback_diameter": None}})
        self.assertEqual(splits.request_split(record["id"])["state"], splits.SPLITTING)
        await self.run_stages()
        detail = splits.get_split(record["id"], detail=True)
        parts = detail["result"]["parts"]
        self.assertEqual((detail["state"], detail["parts"]), (splits.SPLIT, len(parts)))
        self.assertEqual(list(detail["result"]["summary"]["rods"]), ["1.75 mm pin"])
        self.assertEqual([c["id"] for c in detail["result"]["cuts"]], [c["id"] for c in detail["check"]["cuts"]])
        plan = splits.assembly_plan(record["id"])   # the real engine's result plans cleanly
        self.assertTrue(plan["ok"], plan.get("failure"))
        self.assertEqual(plan["summary"]["parts"], len(parts))
        path, name = splits.part_path(record["id"], 1)
        self.assertTrue(path.is_file())
        self.assertEqual(name, "channel_part01.stl")

        plain = slicer.list_bundles()[0]
        tree = slicer.derive_bundle(plain["id"], {"enable_support": "1", "support_type": "tree(auto)"})
        self.assertNotEqual(tree["id"], plain["id"])
        self.assertTrue(tree["label"].endswith("tree supports"))
        processed = json.loads((slicer.profiles_dir() / tree["id"] / "process.json").read_text())
        self.assertEqual((processed["enable_support"], processed["support_type"]), ("1", "tree(auto)"))

        queued = splits.queue_parts(record["id"], {"bundle_id": plain["id"], "support_bundle_id": tree["id"], "printer_id": "p1"})
        self.assertEqual((queued["state"], len(queued["slice_ids"])), (splits.QUEUED, len(parts)))
        records = [slicer.get_slice(i) for i in queued["slice_ids"]]
        self.assertEqual([r["source_name"] for r in records], [f"channel_part{p['number']:02d}.stl" for p in parts])
        self.assertTrue(all(r["split_id"] == record["id"] for r in records))
        self.assertIn(f"part 1 of {len(parts)}", records[0]["notes"])
        for part, sliced in zip(parts, records):
            self.assertEqual(sliced["bundle_id"], tree["id"] if part["needs_supports"] else plain["id"])

        with mock.patch.object(slicer, "runner", fake_runner()):
            while await slicer.process_next():
                pass
        summary = splits.get_split(record["id"])["queue"]
        self.assertEqual((summary["slices"], summary["queued"]), (len(parts), len(parts)))

    async def test_requests_are_validated(self):
        with self.assertRaisesRegex(jobs.JobError, "STL, OBJ or 3MF"):
            self.upload(name="model.step")
        with self.assertRaisesRegex(jobs.JobError, "empty"):
            self.upload(data=b"")
        with self.assertRaisesRegex(jobs.JobError, "Scale"):
            self.upload(scale=-1)
        with self.assertRaisesRegex(slicer.jobs.JobError, "can't be overridden"):
            slicer.derive_bundle(slicer.list_bundles()[0]["id"], {"start_gcode": "M112"})
        record = self.upload()
        for call in (lambda: splits.request_split(record["id"]), lambda: splits.retry(record["id"]),
                     lambda: splits.save_cuts(record["id"], {"cuts": []})):
            with self.assertRaises(jobs.JobError) as refused:
                call()
            self.assertEqual(refused.exception.status, 409)
        await self.run_stages()
        with self.assertRaisesRegex(jobs.JobError, "list"):
            splits.save_cuts(record["id"], {"cuts": "none"})

        splits.save_cuts(record["id"], {"cuts": []})  # no cuts: the channel is too big for one part
        await self.run_stages()
        with self.assertRaisesRegex(jobs.JobError, "too big"):
            splits.request_split(record["id"])
        with self.assertRaises(jobs.JobError) as not_split:
            splits.queue_parts(record["id"], {"bundle_id": slicer.list_bundles()[0]["id"]})
        self.assertEqual(not_split.exception.status, 409)

    async def test_a_failed_stage_can_be_retried_and_the_split_deleted(self):
        flat = b"solid x\nfacet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 1 0 0\n vertex 0 1 0\n endloop\nendfacet\nendsolid x\n"
        record = self.upload(data=flat, name="flat.stl")
        await self.run_stages()
        failed = splits.get_split(record["id"])
        self.assertEqual((failed["state"], failed["stage"]), (splits.FAILED, "analyze"))
        self.assertTrue(failed["error"])
        self.assertEqual(splits.retry(record["id"])["state"], splits.ANALYZING)
        await self.run_stages()

        splits.delete_split(record["id"])
        self.assertFalse(splits.work_dir(record["id"]).exists())
        with self.assertRaises(jobs.JobError) as gone:
            splits.get_split(record["id"])
        self.assertEqual(gone.exception.status, 404)

    async def test_a_failed_auto_fix_is_a_note_and_leaves_the_split_ready_with_its_cuts(self):
        record = self.upload()
        await self.run_stages()
        before = (splits.work_dir(record["id"]) / "cuts.json").read_text()
        message = "Auto-fix couldn't remove the parts that are too small to print. The cuts are unchanged: move or remove the cut that makes them."
        splits.request_plan(record["id"], {"mode": "smart", "keep_cuts": True})
        with mock.patch.object(splits, "_run_stage", side_effect=jobs.JobError(message)):
            await self.run_stages()
        detail = splits.get_split(record["id"], detail=True)
        self.assertEqual(detail["state"], splits.READY, "the split is not failed")
        self.assertIsNone(detail["error"])
        self.assertEqual(detail["plan_note"], message)
        self.assertEqual((splits.work_dir(record["id"]) / "cuts.json").read_text(), before)
        # the next edit clears the note
        splits.save_cuts(record["id"], {"cuts": detail["cuts"]["cuts"]})
        self.assertIsNone(splits.get_split(record["id"], detail=True)["plan_note"])

    async def test_a_failed_plan_without_auto_fix_is_still_a_failed_split(self):
        record = self.upload()
        await self.run_stages()
        splits.request_plan(record["id"], {"mode": "smart"})
        with mock.patch.object(splits, "_run_stage", side_effect=jobs.JobError("boom")):
            await self.run_stages()
        detail = splits.get_split(record["id"], detail=True)
        self.assertEqual((detail["state"], detail["error"]), (splits.FAILED, "boom"))
        self.assertIsNone(detail["plan_note"])

    async def test_a_failed_split_reports_the_parts_it_is_about(self):
        record = self.upload()
        await self.run_stages()
        work = splits.work_dir(record["id"])
        (work / "error.json").write_text(json.dumps({"stage": "split", "error": "Part 2 is too small to print — move or remove Cut 1", "parts": [2]}))
        splits._set_state(record["id"], splits.FAILED, stage="split", error="Part 2 is too small to print — move or remove Cut 1")
        self.assertEqual(splits.get_split(record["id"], detail=True)["error_parts"], [2])
        splits.retry(record["id"])
        self.assertEqual(splits.get_split(record["id"], detail=True)["error_parts"], [], "nothing to highlight once it is retried")

    async def test_new_scale_analyses_again(self):
        record = self.upload(data=self.model_bytes(channel(52.0)), name="small.stl", scale=1)
        await self.run_stages()
        self.assertTrue(splits.get_split(record["id"], detail=True)["analysis"]["fits_whole"])
        updated = splits.update_settings(record["id"], {"scale": 10})
        self.assertEqual(updated["state"], splits.ANALYZING)
        await self.run_stages()
        detail = splits.get_split(record["id"], detail=True)
        self.assertEqual(detail["analysis"]["size"][0], 520.0)
        self.assertEqual(detail["state"], splits.READY)


if __name__ == "__main__":
    unittest.main()
