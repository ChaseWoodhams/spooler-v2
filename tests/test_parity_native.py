"""The PETG/ASA parity matrix against the pinned Orca (see tools/PARITY.md).

Opt-in: set SPOOLER_ORCA (or ORCA_SLICER_EXE) to the bundled 2.4.2 executable; otherwise every case is reported as skipped,
which means NOT RUN, never passed. Each case uses a disposable database and data directory, a fresh executable datadir for the
direct runs, and generated cubes: no live Project, saved profile or SEAT fixture is touched.

For every scenario: export the Project (one faithful 3MF, or the per-Plate package when one file cannot be faithful), slice each
file with a direct Orca command that is given nothing but the file, slice the same Plate in Spooler, and compare them under the
spec's bounds (tests/parity.py). Then re-import each file through the normal reviewed flow, slice the reopened Project and compare
again. Every comparison also checks `expected`, written below by hand from the settings the scenario asked for, so a bug shared by
both paths still fails. On failure the expected/observed config and both G-code files are kept; set SPOOLER_PARITY_DIAGNOSTICS to
choose where, otherwise they go under the system temp directory."""
import asyncio
import copy
import io
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import database
import jobs
import projects
import slicer
import splits
from tests import parity
from tests.test_slicer import cube_stl

ORCA = os.environ.get("SPOOLER_ORCA") or os.environ.get("ORCA_SLICER_EXE")
PACKAGE = "plate-3mf-package"
PRINTER, PROCESS = "Elegoo Centauri Carbon 0.4 nozzle", "0.20mm Standard @Elegoo CC 0.4 nozzle"
PRESET = {"PETG": "Elegoo PETG @ECC", "ASA": "Elegoo ASA @ECC"}
# Only settings the stock presets state themselves: a setting they do not state is not carried by a re-import (see the known-gap tests).
BASE_PROCESS = {"wall_loops": "1", "wall_generator": "classic", "sparse_infill_density": "10%"}
# Written from the cubes themselves, not from Spooler: a 20 mm cube at 0.2 mm layers is 100 layers.
COMMON = {"layer_height": "0.2", "nozzle_diameter": "0.4", "curr_bed_type": "Textured PEI Plate", "_layers": 100}
SLOTS = {1: ([70.0, 90.0], [100.0, 90.0]), 2: ([160.0, 90.0], [190.0, 90.0])}   # two Objects per Plate, as the re-import needs


def diagnostics_root() -> Path:
    chosen = os.environ.get("SPOOLER_PARITY_DIAGNOSTICS")
    return Path(chosen) if chosen else Path(tempfile.gettempdir()) / "spooler-parity-diagnostics" / time.strftime("%Y%m%d-%H%M%S")


@unittest.skipUnless(ORCA and Path(ORCA).is_file(), "Set SPOOLER_ORCA to the bundled Orca 2.4.2 (NOT RUN without it)")
class ParityMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.artifacts = 0
        self._tmp = tempfile.TemporaryDirectory(prefix="spooler-parity-")
        self.root = Path(self._tmp.name)
        self.diagnostics = diagnostics_root()
        database.configure(self.root / "test.db")
        jobs.init(self.root)
        projects.configure(self.root)
        splits.configure(self.root)
        slicer.configure(self.root, orca_path=Path(ORCA))
        slicer.init()
        projects.init()

    async def asyncTearDown(self):
        for task in list(jobs._start_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)
        projects.configure()
        splits.configure()
        slicer.configure()
        jobs._data_dir = jobs.DATA_DIR
        database.configure(database.DB_FILE)
        self._tmp.cleanup()

    async def settle(self):
        while await projects.process_next():
            pass

    async def build(self, material, plates, edit):
        """A disposable Project of 20 mm cubes, two Objects per Plate; `edit(data)` applies the scenario's settings."""
        bundle = slicer.create_bundle(PRINTER, PROCESS, PRESET[material])
        project = projects.create_project(f"Disposable {material} parity")
        for index in range(2 * plates):
            mesh = cube_stl(20)
            projects.import_model(project["id"], io.BytesIO(mesh), len(mesh), f"cube{index}.stl")
        await self.settle()
        data = copy.deepcopy(projects.get_project(project["id"])["data"])
        data["profile"] = {**data["profile"], "bundle_id": bundle["id"], "process": dict(BASE_PROCESS)}
        for number in range(2, plates + 1):
            data["plates"].append({"id": number, "name": f"Plate {number}", "overrides": {}})
        for index, obj in enumerate(data["objects"][:2 * plates]):
            obj["plate"], obj["position"] = index // 2 + 1, SLOTS[index // 2 + 1][index % 2]
        edit(data)
        return projects.update_project(project["id"], {"revision": projects.get_project(project["id"])["revision"], "data": data})

    def direct(self, path):
        output = self.root / f"direct-{path.stem}"
        output.mkdir()
        code, log = slicer.runner([ORCA, "--datadir", str(self.root / "direct-datadir"), "--slice", "0", "--arrange", "0",
                                   "--outputdir", str(output), str(path)], 300)
        self.assertEqual(code, 0, log[-3000:])
        return (output / "plate_1.gcode").read_text(encoding="utf-8", errors="replace")

    async def spooler(self, project_id, plate_id):
        projects.request_slice(project_id, plate_id)
        await self.settle()
        state = projects.get_project(project_id)["slices"][str(plate_id)]
        self.assertEqual(state["state"], "sliced", state.get("error"))
        return projects.plate_file(project_id, plate_id, "gcode").read_text(encoding="utf-8", errors="replace")

    def files(self, project, faithful):
        """The exported artifact as {plate id: path of a self-contained 3MF}, with the plan that produced it."""
        plan = projects.export_plan(project["id"])
        self.assertEqual(plan["faithful"], faithful, plan["differences"])
        representation = "single-3mf" if faithful else PACKAGE
        artifact = projects.export_project(project["id"], plan["revision"], plan["fingerprint"], representation)
        self.artifacts += 1
        folder = self.root / f"artifact-{self.artifacts}"
        folder.mkdir()
        if faithful:
            path = folder / "project.3mf"
            path.write_bytes(artifact["body"])
            return {plan["plates"][0]["id"]: path}
        with zipfile.ZipFile(io.BytesIO(artifact["body"])) as archive:
            archive.extractall(folder)
        return {item["plate_id"]: folder / item["filename"] for item in plan["package"]["files"]}

    async def scenario(self, name, material, plates, edit, expected, *, faithful):
        """`expected`: {plate id: independent expectations}. Expectations common to every scenario are added here."""
        project = await self.build(material, plates, edit)
        files = self.files(project, faithful)
        for plate, path in files.items():
            want = {**COMMON, **expected[plate]}
            reference = self.direct(path)
            produced = await self.spooler(project["id"], plate)
            parity.assert_parity(f"{name} plate {plate} direct Orca vs Spooler", reference, produced, want, self.diagnostics)
            data = path.read_bytes()
            review = projects.prepare_import(io.BytesIO(data), len(data), path.name)
            self.assertFalse(review["blocked"], review["report"])
            # Nothing but blocked printer scripts (never imported, by policy) may be left out on the way back in: any other difference
            # is a fidelity gap, not something to acknowledge and move past.
            left_out = [(d["code"], d.get("key") or d.get("keys")) for d in review["report"]["differences"] if d["code"] != "blocked-setting"]
            self.assertEqual(left_out, [], f"{name} plate {plate}: the re-import would not carry everything")
            done = projects.confirm_import(review["token"], {**review["binding"], "acknowledged": bool(review["required"])})
            await self.settle()
            reopened = projects.get_project(done["id"])
            reimported = await self.spooler(done["id"], reopened["data"]["plates"][0]["id"])
            parity.assert_parity(f"{name} plate {plate} direct Orca vs Spooler after re-import", reference, reimported, want, self.diagnostics)
        return project

    # ── Baselines ────────────────────────────────────────────────────────────────

    async def test_petg_baseline_cubes(self):
        await self.scenario("petg baseline", "PETG", 1, lambda d: None,
                            {1: {"filament_type": "PETG", "wall_loops": "1", "sparse_infill_density": "10%", "_moves": {"Outer wall": 50}}}, faithful=True)

    async def test_asa_baseline_cubes(self):
        await self.scenario("asa baseline", "ASA", 1, lambda d: None,
                            {1: {"filament_type": "ASA", "wall_loops": "1", "sparse_infill_density": "10%", "_moves": {"Outer wall": 50}}}, faithful=True)

    # ── Layered overrides ───────────────────────────────────────────────────────

    async def test_project_plate_and_object_overrides_layer_in_that_order(self):
        """The Project says 1 wall, the Plate changes the infill, one Object asks for 4 walls. One wall prints no Inner wall at
        all, so Inner wall deposition can only come from the Object override reaching Orca: dropping it in both paths still fails."""
        def edit(data):
            data["plates"][0]["overrides"] = {"sparse_infill_density": "25%"}
            data["objects"][0]["overrides"] = {"wall_loops": "4"}
        await self.scenario("layered overrides", "PETG", 1, edit,
                            {1: {"filament_type": "PETG", "wall_loops": "1", "sparse_infill_density": "25%",
                                 "_moves": {"Inner wall": 50, "Outer wall": 50}}}, faithful=True)

    # ── Nullable retract/lift/wipe, bed, temperature, cooling and flow ────────────────

    async def nullable(self, material):
        """A nil retraction length and wipe fall back to the printer's values; a set z-hop reaches Orca. Orca omits nil overrides
        from its footer, so what is asserted is the resolved printer value (0.8) next to the explicit lift (0.4)."""
        def edit(data):
            data["profile"]["filament"] = {"filament_retraction_length": [None], "filament_z_hop": ["0.4"], "filament_wipe": ["nil"]}
        await self.scenario(f"{material} nullable", material, 1, edit,
                            {1: {"filament_type": material, "filament_z_hop": "0.4", "z_hop": "0.4", "retraction_length": "0.8"}}, faithful=True)

    async def test_nullable_retract_lift_wipe_overrides_for_petg(self):
        await self.nullable("PETG")

    async def test_nullable_retract_lift_wipe_overrides_for_asa(self):
        await self.nullable("ASA")

    async def test_temperature_cooling_flow_and_bed_changes(self):
        def edit(data):
            data["profile"]["filament"] = {"nozzle_temperature": ["245"], "nozzle_temperature_initial_layer": ["250"],
                                           "filament_flow_ratio": ["0.97"], "fan_min_speed": ["35"], "textured_plate_temp": ["75"]}
        await self.scenario("temperature cooling flow bed", "PETG", 1, edit,
                            {1: {"filament_type": "PETG", "nozzle_temperature": "245", "nozzle_temperature_initial_layer": "250",
                                 "filament_flow_ratio": "0.97", "fan_min_speed": "35", "textured_plate_temp": "75"}}, faithful=True)

    async def test_the_other_bed_side_is_what_orca_prints_for(self):
        def edit(data):
            data["bed"] = "smooth"
        await self.scenario("high temp plate", "PETG", 1, edit,
                            {1: {"filament_type": "PETG", "curr_bed_type": "High Temp Plate"}}, faithful=True)

    # ── Geometry ────────────────────────────────────────────────────────────────

    async def test_a_rotated_and_scaled_multi_object_plate(self):
        """A 45 degree turn and a scaled Object. Rounding the Slice file's bed coordinates to 4 decimals once drifted Spooler's time
        estimate 0.15% from direct Orca's (1364 s against 1362 s); the Slice file now keeps 9 significant figures."""
        def edit(data):
            data["objects"][0]["rotation"] = [0.0, 0.0, 45.0]
            data["objects"][1]["scale"] = [0.8, 0.8, 0.8]
        await self.scenario("transformed objects", "PETG", 1, edit,
                            {1: {"filament_type": "PETG", "wall_loops": "1", "_moves": {"Outer wall": 50}}}, faithful=True)

    async def test_a_setting_the_stock_preset_does_not_state_survives_a_round_trip(self):
        """brim_type is not stated by the stock presets. The re-import compares it with Orca's own resolved value for those presets,
        so no_brim is carried and the reopened Project slices without a brim, as direct Orca does."""
        def edit(data):
            data["profile"]["process"] = {**data["profile"]["process"], "brim_type": "no_brim"}
        await self.scenario("unstated setting", "PETG", 1, edit, {1: {"filament_type": "PETG", "brim_type": "no_brim"}}, faithful=True)

    # ── Plates that one file cannot hold ─────────────────────────────────────────

    async def test_a_two_material_project_exports_one_file_per_plate(self):
        def edit(data):
            second = {str(o["model"]): {"preset": PRESET["ASA"], "colour": "grey"} for o in data["objects"][2:4]}
            data["filaments"] = second
        await self.scenario("two material", "PETG", 2, edit,
                            {1: {"filament_type": "PETG", "_moves": {"Outer wall": 50}}, 2: {"filament_type": "ASA", "_moves": {"Outer wall": 50}}},
                            faithful=False)

    async def test_plates_differing_in_a_non_object_process_setting_export_one_file_per_plate(self):
        def edit(data):
            data["plates"][1]["overrides"] = {"wall_loops": "3", "sparse_infill_density": "25%"}
        await self.scenario("non-object plate difference", "PETG", 2, edit,
                            {1: {"filament_type": "PETG", "wall_loops": "1", "sparse_infill_density": "10%"},
                             2: {"filament_type": "PETG", "wall_loops": "3", "sparse_infill_density": "25%", "_moves": {"Inner wall": 100}}},
                            faithful=False)


if __name__ == "__main__":
    unittest.main()
