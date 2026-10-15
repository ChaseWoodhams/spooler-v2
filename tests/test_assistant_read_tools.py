"""The Assistant's project read tools (get_plate, get_model_geometry, get_printer_capabilities, get_slice_summary): direct over the
MCP stdio protocol where that is cheap, and through the project API with the fake `claude` for a real project with real meshes."""
import asyncio
import io
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import assistant
import database
import jobs
import project_read
import projects
import slicer
import spooler_mcp
import splits
from tests.fake_claude import make_fake_claude, read_record
from tests import test_assistant_ask as ask_module
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca
from tests.test_spooler_mcp import McpDataCase, McpSession

GEOMETRY = not splits.missing_modules()
READ_TOOLS = ("get_plate", "get_model_geometry", "get_printer_capabilities", "get_slice_summary")
BUNDLE = "0123456789ab"
PRIVATE = ["printer-xyz", "192.168.1.50", "access-code-123", "C:\\", "C:/", "/Users/", "AppData", "plate.gcode", "G1 X", "M104 S",
           "access_code", "ip_address", "spoolman", "spool_id", "job_id", "queue", "vertices", "\"verts\""]
SUPPORTS_ON = {"enable_support": "1"}


def write_bundle(data_dir: Path, machine=None, bundle_id=BUNDLE):
    folder = Path(data_dir) / "profiles" / bundle_id
    folder.mkdir(parents=True, exist_ok=True)
    machine = machine or {"name": "Elegoo Centauri Carbon 0.4 nozzle", "printable_area": ["0x0", "256x256"], "printable_height": "250",
                          "bed_exclude_area": ["246x0", "256x0", "256x20", "246x20"], "nozzle_diameter": ["0.4"]}
    (folder / "bundle.json").write_text(json.dumps({"id": bundle_id, "label": "Locked profile", "presets": {}}), encoding="utf-8")
    (folder / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
    (folder / "process.json").write_text(json.dumps({"name": "0.20mm"}), encoding="utf-8")
    (folder / "filament.json").write_text(json.dumps({"name": "PETG"}), encoding="utf-8")


def assert_clean(test, text):
    lowered = text.lower()
    for private in PRIVATE:
        test.assertNotIn(private.lower(), lowered, f"{private!r} must not reach the model")
    test.assertIsNone(re.search(r"[A-Za-z]:[\\/]", text), "no drive path")


class ReadToolsMcpTests(McpDataCase):
    """The tools over stdio, on hand-made rows (no meshes, so what needs geometry is reported as unknown, not guessed)."""

    def project(self, **extra):
        data = {"profile": {"bundle_id": BUNDLE, "process": {}, "filament": {}}, "bed": "smooth",
                "plates": [{"id": 1, "name": "Plate \"one\"", "printer_id": "printer-xyz", "overrides": {"layer_height": "0.12"}},
                           {"id": 2, "name": "Empty"}],
                "objects": [{"id": 1, "model": 11, "plate": 1, "name": "Arm", "position": [10.123, 20], "rotation": [0, 90, 0],
                             "scale": [1, 1, 2], "pinned": True, "overrides": {"sparse_infill_density": "40%"}},
                            {"id": 2, "model": 11, "plate": 1, "name": "Arm 2", "position": [60, 20], "rotation": [0, 0, 0], "scale": [1, 1, 1],
                             "overrides": {}}]}
        data.update(extra)
        models = [{"id": 11, "name": "Arm", "file": "C:/Users/me/arm.stl", "state": "ready", "size": [80, 20, 10], "volume_cm3": 12.5}]
        self.add_project(1, "Shelf", 7, data, models, extra.pop("slices", None))
        write_bundle(self.dir)

    def ask(self, name, arguments=None, project=1):
        session = McpSession(self.dir, project)
        try:
            session.start()
            tools = session.request("tools/list")["result"]["tools"]
            reply = session.call(name, arguments)
        finally:
            err = session.close()
        self.assertEqual(err.strip(), "")
        return reply, tools

    def result(self, name, arguments=None):
        reply, _ = self.ask(name, arguments)
        self.assertFalse(reply["result"].get("isError"), reply)
        text = reply["result"]["content"][0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), 20_000)
        assert_clean(self, text)
        return json.loads(text)

    def test_the_read_tools_are_listed_with_their_argument_schemas(self):
        self.project()
        _, tools = self.ask("get_project")
        by_name = {t["name"]: t for t in tools}
        self.assertEqual(sorted(by_name), sorted(("get_project",) + READ_TOOLS + ("propose_orientation", "get_settings", "describe_setting", "propose_settings")))
        self.assertEqual(by_name["get_plate"]["inputSchema"]["required"], ["plate_id"])
        self.assertEqual(by_name["get_model_geometry"]["inputSchema"]["required"], ["model_id"])
        self.assertEqual(by_name["get_printer_capabilities"]["inputSchema"]["properties"], {})
        self.assertTrue(set(by_name) <= set(spooler_mcp.TOOL_NAMES))

    def test_bad_arguments_are_errors_the_model_can_read_and_the_server_keeps_answering(self):
        self.project()
        session = McpSession(self.dir, 1)
        try:
            session.start()
            replies = [session.call("get_plate"), session.call("get_plate", {"plate_id": "1"}), session.call("get_plate", {"plate_id": True}),
                       session.call("get_plate", {"plate_id": 1, "project_id": 2}), session.call("get_plate", {"plate_id": 99}),
                       session.call("get_model_geometry", {"model_id": 99}), session.call("get_slice_summary", {"plate_id": 99}),
                       session.call("get_project", {"x": 1})]
            again = session.request("ping")
        finally:
            session.close()
        for reply in replies:
            self.assertTrue(reply["result"]["isError"], reply)
        self.assertIn("plate_id", replies[0]["result"]["content"][0]["text"])
        self.assertIn("not found", replies[4]["result"]["content"][0]["text"])
        self.assertEqual(again["result"], {})

    def test_get_plate_reports_objects_pinned_overrides_and_what_it_cannot_know_without_geometry(self):
        self.project()
        got = self.result("get_plate", {"plate_id": 1})
        self.assertEqual(got["revision"], 7)
        self.assertEqual(got["plate"]["name_quoted"], 'Plate "one"')
        self.assertEqual(got["plate"]["overrides"], {"layer_height": "0.12"})
        first, second = got["objects"]
        self.assertEqual((first["id"], first["position_mm"], first["rotation_deg"], first["scale"]), (1, [10.12, 20.0], [0.0, 90.0, 0.0], [1.0, 1.0, 2.0]))
        self.assertIs(first["pinned"], True)
        self.assertIs(second["pinned"], False)
        self.assertEqual(first["overrides"], {"sparse_infill_density": "40%"})
        self.assertIsNone(first["needs_supports"], "unknown (no mesh) is not reported as no")
        self.assertEqual(got["slice"], {"state": "none", "outdated": False})
        self.assertEqual(self.result("get_plate", {"plate_id": 2})["objects"], [])

    def test_get_printer_capabilities_reads_the_locked_machine_preset_only(self):
        self.project()
        got = self.result("get_printer_capabilities")
        self.assertEqual(got["revision"], 7)
        self.assertEqual(got["bed_mm"], {"x_min": 0.0, "y_min": 0.0, "x_max": 256.0, "y_max": 256.0, "width": 256.0, "depth": 256.0})
        self.assertEqual(got["height_mm"], 250.0)
        self.assertEqual(got["exclusion_area_mm"], {"x_min": 246.0, "y_min": 0.0, "x_max": 256.0, "y_max": 20.0})
        self.assertEqual(got["nozzle_mm"], 0.4)
        self.assertNotIn("Centauri", json.dumps(got), "no preset or printer names")

    def test_get_printer_capabilities_without_a_locked_profile_says_so(self):
        self.project()
        self.add_project(2, "Unlocked", 1)
        reply, _ = self.ask("get_printer_capabilities", project=2)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("No profile", reply["result"]["content"][0]["text"])

    def analysis(self, plate=1):
        folder = self.dir / "projects" / "1" / "plates" / str(plate)
        folder.mkdir(parents=True)
        (folder / "analysis.json").write_text(json.dumps({
            "slicer": "OrcaSlicer 2.3", "sha256": "f" * 64, "config": {"output_dir": "C:/Users/me/out"},
            "estimates": {"time_s": 3601, "filament_g": 21.5}, "layers": [{"z": 0.2}, {"z": 0.4}],
            "feature_stats": {"Outer wall": {"extruded_mm": 100.0, "time_s": 900}}, "bounds": {"model": {"min": [1, 2, 0], "max": [30, 40, 9.8]}},
            "occupancy": {"bits": "AAAA"}, "commands": {"G1": 5000}, "stats": {"extruded_mm": 5.0},
            "temperatures": {"nozzle": {"max": 250, "first": 250, "values": [250]}, "bed": None},
            "findings": [{"line": 12, "command": "G28", "reason": "homes the axes after printing has started", "level": "warn"}]}), encoding="utf-8")

    def sliced_project(self, current):
        self.project()
        with database.transaction() as conn:
            row = conn.execute("SELECT data FROM projects WHERE id = 1").fetchone()
            record = {"data": json.loads(row["data"]), "models": [], "slices": {}}
            raw = project_read.fingerprint(record, 1) if current else "stale"
            conn.execute("UPDATE projects SET slices = ? WHERE id = 1", (json.dumps({"1": {
                "state": "sliced", "fingerprint": raw, "estimate_s": 3601, "estimate_g": 21.5, "layers": 2, "sent_job": 5}}),))
        self.analysis()

    def test_get_slice_summary_gives_numbers_and_labels_never_gcode_or_paths(self):
        self.sliced_project(current=True)
        got = self.result("get_slice_summary", {"plate_id": 1})
        self.assertEqual((got["revision"], got["state"], got["outdated"]), (7, "sliced", False))
        self.assertEqual(got["estimates"], {"time_s": 3601, "filament_g": 21.5})
        self.assertEqual(got["layer_count"], 2)
        self.assertEqual(got["features"], [{"feature": "Outer wall", "extruded_mm": 100.0, "time_s": 900}])
        self.assertEqual(got["findings"], [{"level": "warn", "command": "G28", "reason": "homes the axes after printing has started"}])
        for hidden in ("occupancy", "commands", "sha256", "config", "output_dir", "sent_job"):
            self.assertNotIn(hidden, json.dumps(got))

    def test_an_outdated_slice_says_outdated_and_what_changed_without_numbers(self):
        self.sliced_project(current=False)
        got = self.result("get_slice_summary", {"plate_id": 1})
        self.assertIs(got["outdated"], True)
        self.assertIn("changed", got["changed"])
        for stale in ("estimates", "estimate_s", "features", "findings"):
            self.assertNotIn(stale, got)
        plate = self.result("get_plate", {"plate_id": 1})
        self.assertEqual(plate["slice_findings"], [], "an Outdated slice's findings are not current")

    def test_an_unsliced_plate_is_state_none(self):
        self.project()
        self.assertEqual(self.result("get_slice_summary", {"plate_id": 2}), {"revision": 7, "plate_id": 2, "state": "none", "outdated": False})

    def test_long_lists_are_summarised_under_the_cap(self):
        self.project()
        objects = [{"id": i, "model": 11, "plate": 1, "name": "n" * 100, "position": [1, 1], "rotation": [0, 0, 0], "scale": [1, 1, 1],
                    "overrides": {f"key_{k}": "v" * 80 for k in range(40)}} for i in range(1, 60)]
        with database.transaction() as conn:
            row = conn.execute("SELECT data FROM projects WHERE id = 1").fetchone()
            data = json.loads(row["data"])
            data["objects"] = objects
            conn.execute("UPDATE projects SET data = ? WHERE id = 1", (json.dumps(data),))
        got = self.result("get_plate", {"plate_id": 1})
        self.assertEqual(got["truncated"]["objects"], 59)
        self.assertEqual(got["revision"], 7)


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ReadToolsThroughTheAssistantTests(unittest.TestCase):
    """A real project (real meshes, fake slicer) asked about through the project API with the fake claude. The helpers are borrowed
    from AskTests one by one (not inherited, so its own tests do not run again here)."""
    tearDown = ask_module.AskTests.tearDown
    call = ask_module.AskTests.call
    make_project = ask_module.AskTests.make_project
    scenario = ask_module.AskTests.scenario
    ask = ask_module.AskTests.ask
    poll = ask_module.AskTests.poll
    finished = ask_module.AskTests.finished

    def setUp(self):
        ask_module.AskTests.setUp(self)
        record = projects.get_project(self.project["id"])
        data = record["data"]
        data["profile"] = {**data["profile"], "process": dict(SUPPORTS_ON)}
        status, saved = self.call("PATCH", f"/api/projects/{record['id']}", {"data": data, "revision": record["revision"]})
        self.assertEqual(status, 200, saved)
        self.project = saved

    def tools_seen(self, calls):
        self.scenario({"calls": calls, "answer": "done"})
        view = self.finished("What does this project look like?")
        self.assertEqual(view["state"], "answer", view)
        results = read_record(self.cli_dir)["tool_results"]
        self.assertEqual([r["is_error"] for r in results], [False] * len(calls), results)
        for r in results:
            assert_clean(self, r["text"])
            self.assertLessEqual(len(r["text"].encode("utf-8")), 20_000)
            self.assertEqual(json.loads(r["text"])["revision"], self.project["revision"])
        return view, [json.loads(r["text"]) for r in results]

    def test_every_read_tool_is_allowed_by_the_runner_and_answers_for_a_real_project(self):
        models = projects.get_project(self.project["id"])["models"]
        mid = models[0]["id"]
        view, got = self.tools_seen([{"tool": "get_plate", "arguments": {"plate_id": 1}}, {"tool": "get_model_geometry", "arguments": {"model_id": mid}},
                                     {"tool": "get_printer_capabilities"}, {"tool": "get_slice_summary", "arguments": {"plate_id": 1}}])
        self.assertEqual(len(view["steps"]), 4)
        plate, geometry, printer, slice_ = got
        self.assertEqual(len(plate["objects"]), 2)
        for obj in plate["objects"]:
            self.assertEqual(len(obj["position_mm"]), 2)
            self.assertIn("pinned", obj)
            self.assertIsInstance(obj["needs_supports"], bool)
        self.assertEqual(plate["time_estimate"]["approximate"], True)
        self.assertTrue(any(ask_module.INJECTED in o["name_quoted"] for o in plate["objects"]), "names travel in *_quoted fields")
        self.assertEqual(geometry["geometry"]["size_mm"], [20.0, 20.0, 20.0])
        self.assertEqual(geometry["geometry"]["as_placed"]["footprint_mm"], [20.0, 20.0])
        self.assertEqual({c["mode"] for c in geometry["orientation_candidates"]}, {"least-support", "strength"})
        self.assertTrue(all("support" in c["suggested_score"] for c in geometry["orientation_candidates"] if c["available"]))
        self.assertNotIn("verts", json.dumps(geometry))
        self.assertEqual((printer["height_mm"], printer["nozzle_mm"], printer["bed_mm"]["width"]), (256.0, 0.4, 256.0))
        self.assertEqual(printer["exclusion_area_mm"]["x_min"], 246.0)
        self.assertEqual(slice_["state"], "none")

    def test_a_sliced_plate_reads_back_and_goes_outdated_when_the_project_changes(self):
        record = projects.get_project(self.project["id"])
        self.assertEqual(record["slices"], {})
        plate_dir = Path(self.dir) / "projects" / str(self.project["id"]) / "plates" / "1"
        plate_dir.mkdir(parents=True, exist_ok=True)
        (plate_dir / "analysis.json").write_text(json.dumps({"estimates": {"time_s": 900}, "layers": [{}], "findings": []}), encoding="utf-8")
        data = record["data"]
        entry = {"state": "sliced", "inputs_version": project_read.INPUTS_VERSION,
                 "fingerprint": project_read.print_fingerprint(record, 1), "estimate_s": 900, "estimate_g": 3.0, "layers": 1}
        with database.transaction() as conn:
            conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps({"1": entry}), self.project["id"]))
        revision = projects.get_project(self.project["id"])["revision"]
        self.project = {**self.project, "revision": revision}
        _, got = self.tools_seen([{"tool": "get_slice_summary", "arguments": {"plate_id": 1}}])
        if got[0]["outdated"]:                      # the fingerprint cannot be resolved with this stub profile: still the Outdated shape
            self.assertIn("changed", got[0]["changed"])
        else:
            self.assertEqual(got[0]["estimates"], {"time_s": 900})
        data["objects"][0]["position"] = [data["objects"][0]["position"][0] + 5, data["objects"][0]["position"][1]]
        status, saved = self.call("PATCH", f"/api/projects/{self.project['id']}", {"data": data, "revision": revision})
        self.assertEqual(status, 200, saved)
        self.project = saved
        _, got = self.tools_seen([{"tool": "get_slice_summary", "arguments": {"plate_id": 1}}])
        self.assertIs(got[0]["outdated"], True)
        self.assertIn("changed", got[0]["changed"])
        self.assertNotIn("estimates", got[0])

    def test_a_bad_tool_argument_is_an_error_result_and_the_run_still_answers(self):
        self.scenario({"calls": [{"tool": "get_plate", "arguments": {"plate_id": "one"}}], "answer": "I could not read that plate."})
        view = self.finished()
        self.assertEqual(view["state"], "answer")
        results = read_record(self.cli_dir)["tool_results"]
        self.assertTrue(results[0]["is_error"])
        self.assertIn("plate_id", results[0]["text"])

    def test_the_init_allow_list_names_every_tool_the_server_registers(self):
        self.assertEqual({f"mcp__spooler__{n}" for n in READ_TOOLS} <= set(__import__("assistant_runner").ALLOWED_TOOLS), True)
        view = self.finished()
        self.assertEqual(view["state"], "answer", "the system/init check passes with all eleven tools allowed")


if __name__ == "__main__":
    unittest.main()
