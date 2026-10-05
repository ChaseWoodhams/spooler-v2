import json
import unittest

import state
from printers.protocol import CMD_HOME_AXES, CMD_LIGHT, CMD_MOVE_AXES
from ws_handler import handle_browser_message


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class FakePrinter:
    def __init__(self):
        self.id = "printer-1"
        self.printer_type = "cc1"
        self.connected = True
        self.status = {"PrintInfo": {"Status": 0}, "CurrentStatus": [0]}
        self.commands = []

    async def send_cmd(self, command, data):
        self.commands.append((command, data))
        return True


class PrinterControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.printer = FakePrinter()
        self.ws = FakeWebSocket()
        state.printers = {self.printer.id: self.printer}

    def tearDown(self):
        state.printers = {}

    async def send(self, **message):
        message.setdefault("printer_id", self.printer.id)
        await handle_browser_message(self.ws, json.dumps(message))

    async def test_idle_printer_accepts_supported_jog(self):
        await self.send(action="move_axis", axis="x", step=-10)
        self.assertEqual(self.printer.commands, [(CMD_MOVE_AXES, {"Axis": "X", "Step": -10.0})])

    async def test_motion_is_rejected_during_print(self):
        self.printer.status["PrintInfo"]["Status"] = 3
        await self.send(action="move_axis", axis="X", step=10)
        self.assertEqual(self.printer.commands, [])
        self.assertIn("only available while the printer is idle", self.ws.messages[-1]["message"])

    async def test_large_jog_is_rejected(self):
        await self.send(action="move_axis", axis="X", step=100)
        self.assertEqual(self.printer.commands, [])
        self.assertIn("0.1, 1, or 10 mm", self.ws.messages[-1]["message"])

    async def test_home_command_validates_axis(self):
        await self.send(action="home_axes", axis="xy")
        self.assertEqual(self.printer.commands, [(CMD_HOME_AXES, {"Axis": "XY"})])

        self.printer.commands.clear()
        await self.send(action="home_axes", axis="E")
        self.assertEqual(self.printer.commands, [])
        self.assertEqual(self.ws.messages[-1]["message"], "Invalid homing axis")

    async def test_fan_payload_is_scoped_and_bounded(self):
        await self.send(action="set_fan", fan="auxiliary", speed=45)
        self.assertEqual(
            self.printer.commands,
            [(CMD_LIGHT, {"TargetFanSpeed": {"AuxiliaryFan": 45}})],
        )

        self.printer.commands.clear()
        await self.send(action="set_fan", fan="model", speed=101)
        self.assertEqual(self.printer.commands, [])

    async def test_temperature_payload_is_scoped_and_bounded(self):
        await self.send(action="set_temperature", target="nozzle", value=215)
        self.assertEqual(self.printer.commands, [(CMD_LIGHT, {"TempTargetNozzle": 215})])

        self.printer.commands.clear()
        await self.send(action="set_temperature", target="bed", value=111)
        self.assertEqual(self.printer.commands, [])
        self.assertIn("between 0 and 110", self.ws.messages[-1]["message"])


if __name__ == "__main__":
    unittest.main()
