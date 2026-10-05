import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import spoolman
from outbox import Outbox, PermanentError


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "outbox.json"
        self.box = Outbox(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_entries_survive_a_restart(self):
        self.box.enqueue(12.34, "p1", "connection refused", spool_id=7)

        reloaded = Outbox(self.path)
        self.assertEqual(reloaded.pending_count(), 1)
        entry = reloaded.pending()[0]
        self.assertEqual((entry["spool_id"], entry["amount_g"], entry["printer_id"]), (7, 12.3, "p1"))

    def test_flush_stops_at_the_first_retryable_failure_and_keeps_order(self):
        for spool_id in (1, 2, 3):
            self.box.enqueue(10, "p1", "down", spool_id=spool_id)
        calls = []

        def send(entry):
            calls.append(entry["spool_id"])
            if entry["spool_id"] == 2:
                raise urllib.error.URLError("connection refused")
            return {"id": entry["spool_id"]}

        report = self.box.flush(send)

        self.assertEqual(calls, [1, 2])
        self.assertEqual([entry["spool_id"] for entry, _ in report["sent"]], [1])
        remaining = self.box.pending()
        self.assertEqual([entry["spool_id"] for entry in remaining], [2, 3])
        self.assertEqual(remaining[0]["attempts"], 1)

    def test_rejected_entries_are_dropped(self):
        self.box.enqueue(5, "p1", "down", spool_id=99)
        self.box.enqueue(6, "p1", "down", spool_id=1)

        def send(entry):
            if entry["spool_id"] == 99:
                raise PermanentError("HTTP 404")
            return {"id": 1}

        report = self.box.flush(send)

        self.assertEqual([entry["spool_id"] for entry in report["dropped"]], [99])
        self.assertEqual(self.box.pending_count(), 0)


class SpoolmanDeductionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.box = Outbox(Path(self._tmp.name) / "outbox.json")
        patcher = mock.patch.object(spoolman, "outbox", self.box)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unreachable_spoolman_queues_the_deduction(self):
        with mock.patch.object(spoolman, "post_use", side_effect=urllib.error.URLError("refused")):
            spoolman.spoolman_deduct_spool(3, 15.0, "p1", loop=None)

        self.assertEqual(self.box.pending()[0]["spool_id"], 3)

    def test_rejected_deduction_is_not_queued(self):
        with mock.patch.object(spoolman, "post_use", side_effect=PermanentError("HTTP 404")):
            spoolman.spoolman_deduct_spool(3, 15.0, "p1", loop=None)

        self.assertEqual(self.box.pending_count(), 0)

    def test_location_deduction_is_queued_then_resolved_when_replayed(self):
        with mock.patch.object(spoolman, "_spool_id_at_location", side_effect=OSError("refused")):
            spoolman.spoolman_deduct("p1", 20.0, loop=None)
        self.assertEqual(self.box.pending()[0]["location"], "p1")

        spool = {"id": 8, "remaining_weight": 500, "filament": {}}
        with mock.patch.object(spoolman, "_spool_id_at_location", return_value=8), \
             mock.patch.object(spoolman, "post_use", return_value=spool) as post:
            report = spoolman.flush_spoolman_outbox()

        post.assert_called_once_with(spoolman.get_spoolman_url(), 8, 20.0)
        self.assertEqual(len(report["sent"]), 1)
        self.assertEqual(self.box.pending_count(), 0)


if __name__ == "__main__":
    unittest.main()
