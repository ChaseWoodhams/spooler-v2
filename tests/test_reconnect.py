import asyncio
import json
import time
import unittest
from unittest import mock

from printers import base
from printers.base import PrinterConnection, reconnect_delay
from printers.cc1 import CC1Connection


class ScriptedPrinter(PrinterConnection):
    """connect() outcomes: True = the link comes up and later drops, False = connection refused."""

    def __init__(self, outcomes=()):
        super().__init__("p1", "10.0.0.2", "Test printer")
        self.outcomes = list(outcomes)

    async def connect(self):
        if self.outcomes.pop(0):
            self.connected = True
            self.connected = False
        else:
            self.last_error = "refused"


class StopLoop(Exception):
    pass


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


class ReconnectBackoffTests(unittest.IsolatedAsyncioTestCase):
    def test_delay_grows_exponentially_and_is_capped(self):
        no_jitter = lambda: 0.5
        self.assertEqual(reconnect_delay(0, rand=no_jitter), base.RECONNECT_BASE_S)
        self.assertEqual([reconnect_delay(n, rand=no_jitter) for n in (1, 2, 3)], [4, 8, 16])
        self.assertEqual(reconnect_delay(12, rand=no_jitter), base.RECONNECT_MAX_S)

    def test_jitter_stays_within_twenty_percent(self):
        low, high = (reconnect_delay(2, rand=lambda: r) for r in (0.0, 1.0))
        self.assertAlmostEqual(low, 8 * 0.8)
        self.assertAlmostEqual(high, 8 * 1.2)

    async def test_backoff_resets_after_a_successful_connection(self):
        printer = ScriptedPrinter([False, False, True, False])
        waits = []

        async def fake_sleep(seconds):
            waits.append((seconds, printer.reconnect_attempts))
            if len(waits) == 4:
                raise asyncio.CancelledError

        with mock.patch("asyncio.sleep", fake_sleep), mock.patch("random.random", return_value=0.5):
            await printer.start()

        self.assertEqual(waits, [(4, 1), (8, 2), (2, 0), (4, 1)])

    def test_connected_property_tracks_link_timestamps(self):
        printer = ScriptedPrinter()
        self.assertIsNone(printer.connected_since)
        self.assertIsNotNone(printer.disconnected_since)

        printer.last_error = "refused"
        printer.connected = True
        self.assertIsNotNone(printer.connected_since)
        self.assertIsNone(printer.disconnected_since)
        self.assertIsNone(printer.last_error)

        printer.connected = False
        self.assertIsNone(printer.connected_since)
        self.assertIsNotNone(printer.disconnected_since)

    def test_link_is_stale_only_when_connected_and_silent(self):
        printer = ScriptedPrinter()
        now = time.time()
        self.assertFalse(printer.link_is_stale(now + 1000))  # an offline link is never "stale"

        printer.connected = True
        printer.last_message_at = now
        self.assertFalse(printer.link_is_stale(now + 30))
        self.assertTrue(printer.link_is_stale(now + base.STALE_LINK_S + 5))


class CC1WatchdogTests(unittest.IsolatedAsyncioTestCase):
    def make_printer(self, silent_for: float) -> CC1Connection:
        printer = CC1Connection("p1", "10.0.0.2", "Test CC1")
        printer.ws = FakeSocket()
        printer.connected = True
        printer.connected_since = printer.last_message_at = time.time() - silent_for
        return printer

    async def test_keepalive_drops_a_silent_connection(self):
        printer = self.make_printer(silent_for=500)

        async def no_wait(_):
            return None

        with mock.patch("asyncio.sleep", no_wait):
            await asyncio.wait_for(printer._keepalive_poller(), timeout=2)

        self.assertTrue(printer.ws.closed)
        self.assertIn("No response from printer", printer.last_error)

    async def test_keepalive_polls_a_live_connection(self):
        printer = self.make_printer(silent_for=5)
        calls = 0

        async def one_round(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise StopLoop

        with mock.patch("asyncio.sleep", one_round), self.assertRaises(StopLoop):
            await printer._keepalive_poller()

        self.assertFalse(printer.ws.closed)
        self.assertEqual(len(printer.ws.sent), 1)


class CC1CameraTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.printer = CC1Connection("p1", "10.0.0.2", "Test CC1")
        self.printer.ws = FakeSocket()
        self.printer.connected = True

    def camera_cmds(self):
        return [m for m in self.printer.ws.sent if '"Cmd": 386' in m]

    async def push_status(self, code):
        async def record_transition():
            self.printer._last_print_status = code

        msg = json.dumps({"Status": {"PrintInfo": {"Status": code}}})
        with mock.patch.object(self.printer, "_check_print_transition", record_transition), \
             mock.patch.object(self.printer, "_broadcast_state", mock.AsyncMock()):
            await self.printer._handle_message(msg)

    async def test_print_state_change_re_enables_the_camera(self):
        await self.push_status(0)
        self.printer._camera_enabled_at = 0.0  # outside the throttle window
        await self.push_status(16)             # print starting: firmware drops the feed
        self.assertEqual(len(self.camera_cmds()), 2)

    async def test_unchanged_print_state_does_not_resend(self):
        await self.push_status(13)
        self.printer._camera_enabled_at = 0.0
        await self.push_status(13)
        self.assertEqual(len(self.camera_cmds()), 1)

    async def test_bursts_of_enable_requests_are_throttled(self):
        self.assertTrue(await self.printer.enable_camera())
        self.assertTrue(await self.printer.enable_camera())
        self.assertEqual(len(self.camera_cmds()), 1)


if __name__ == "__main__":
    unittest.main()
