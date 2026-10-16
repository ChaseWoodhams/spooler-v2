"""
Confirmation slices of the top plans (auto-plating, staged planner step 3; ADR 0002).

The search ranks plans on proxies. Here every plate of the top plans is sliced in OrcaSlicer at full profile, in a pool of
workers (each one an OrcaSlicer process), cached by plate content so a plate that two plans share is sliced once. The plans
are then re-ranked by the same cost (`planner.cost`) with the measured print time and support grams, and the plates are
handed to the printers again with the measured times.

  budget      slices start only while `budget_s` (about 25 s) lasts, the first plan's plates first; a slice in flight is given
              what is left of the budget as its timeout. What was not measured stays on its proxy figures and is marked
              partly estimated, and so is a plate whose slice failed. A run therefore always completes.
  reduced     a plate that needs supports and would alone take most of the budget gets a reduced-shell slice (1 wall, no
              infill, no shells): it gives the support grams but no time, so its time stays the proxy.
  did not slice a plate whose slice failed in OrcaSlicer itself (not for lack of budget), while others sliced, is replanned: its
              Objects go onto plates of their own, which are sliced in a short second round (`REPAIR_BUDGET_S`). A plate that
              still fails is marked `did_not_slice`, and a plan holding one ranks after every plan that slices: Accept must
              not apply a plate known to fail.
  calibration every slice that finishes adds `slice_log` and `support_log` rows (`time_estimate.record_slice`), `kind` being
              `full` or `reduced`.

Nothing here depends on how the plans were found: it takes a `planner.Problem` and `planner.Plan`s.

Public API: `confirm_plans(project_id, record, problem, plans, ...) -> Confirmation`, with `ConfirmedPlan` / `ConfirmedPlate`
(rank order in `Confirmation.plans`), `PlateMeasure`, `PlateJob`, and `ProjectSlicer`, the real slicer (any callable
`(PlateJob, timeout_s) -> PlateMeasure` can stand in, which is how tests run without OrcaSlicer).
"""

import copy
import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

import planner

BUDGET_S = 25.0                  # default time for all confirmation slices
MAX_WORKERS = 4                  # 4-8 parallel slices gave about 2.5x throughput (research 02); more only made each one slower (ticket 22)
SLICE_S_PER_PRINT_S = 1 / 1000   # rough slicing time per second of print: a 6-7 h supported plate took about 25 s (measured, small_mix)
REPAIR_BUDGET_S = 12.0           # extra time to slice the plates a failed plate was split into
HEAVY_SHARE = 0.8                # a plate expected to take more than this share of the budget alone is "heavy"
REDUCED_PROCESS = {"wall_loops": "1", "sparse_infill_density": "0%", "top_shell_layers": "0", "bottom_shell_layers": "0"}
SLOT_BASE = 100                  # OrcaSlicer data directories slicer-data-100.. so the project worker's are left alone


# --- results -------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PlateMeasure:
    """What a slice of one plate showed. `seconds` is None for a reduced slice (it gives support only)."""
    seconds: int | None
    support_g: float


@dataclass(frozen=True)
class PlateJob:
    """One slice to make. `objects` are the Objects of the plate as the 3MF takes them (Pinned ones included), `keys` the
    ids of the planned ones, `proxy_volumes` the proxy support volume (cm3) per Object name, for the calibration log."""
    key: str                       # hash of the plate's content and kind: the cache key
    kind: str                      # "full" or "reduced"
    group: str
    supports: bool
    keys: tuple
    objects: tuple
    overrides: dict
    proxy_volumes: dict
    printer: object
    proxy_seconds: int = 0


@dataclass(frozen=True)
class ConfirmedPlate:
    plate: planner.PlanPlate       # as planned (proxy figures)
    printer: object                # as assigned with the measured times
    seconds: int                   # measured when `time_measured`, else the proxy
    support_g: float
    time_measured: bool
    support_measured: bool
    kind: str | None = None        # "full" / "reduced" for what was sliced, None when nothing was
    error: str | None = None
    did_not_slice: bool = False    # OrcaSlicer itself failed on this plate (not the budget): applying it would not print

    @property
    def partly_estimated(self) -> bool:
        return not (self.time_measured and self.support_measured)


@dataclass(frozen=True)
class ConfirmedPlan:
    plan: planner.Plan
    plates: tuple                  # of ConfirmedPlate, in the plan's order
    cost: float
    terms: dict
    makespan_s: float

    @property
    def partly_estimated(self) -> bool:
        return any(p.partly_estimated for p in self.plates)

    @property
    def did_not_slice(self) -> bool:
        return any(p.did_not_slice for p in self.plates)


@dataclass
class Confirmation:
    plans: list                    # of ConfirmedPlan, cheapest first
    stats: dict = field(default_factory=dict)


# --- building the slices ---------------------------------------------------------------------------------------------------

def _group_overrides(group: str) -> dict:
    """The process overrides in a planner group name (a JSON of overrides, filament and nozzle); none for any other name."""
    try:
        got = json.loads(group)
    except (TypeError, ValueError):
        return {}
    return dict(got.get("overrides") or {}) if isinstance(got, dict) else {}


def _supports_overrides() -> dict:
    import projects
    return dict(projects.SUPPORT_OVERRIDES)


def _build_job(record: dict, problem: planner.Problem, plate: planner.PlanPlate, kind: str) -> PlateJob:
    """The slice of one planned plate: its Objects turned and placed as planned, with the Pinned Objects of its plate."""
    objects = {o["id"]: o for o in record["data"]["objects"]}
    items = {i.key: i for i in problem.items + tuple(problem.oversize)}
    built, volumes, keys = [], {}, []
    for placed in sorted(plate.items, key=lambda p: str(p.key)):
        source = objects[placed.key]
        rotation = list(placed.rotation)
        rotation[2] = round((rotation[2] + placed.angle) % 360, 4)
        name = f"obj{source['id']}"   # plain: G-code names are this plus _id_N_copy_M
        built.append({**copy.deepcopy(source), "name": name, "rotation": rotation,
                      "position": [round(placed.center[0], 3), round(placed.center[1], 3)]})
        cand = items[placed.key].candidates[placed.candidate]
        if cand.needs_support:
            volumes[name] = cand.support_volume_cm3
        keys.append(placed.key)
    if plate.anchor is not None:
        pins = {p.key for p in problem.pinned if p.plate == plate.anchor}
        built += [{**copy.deepcopy(o), "name": f"obj{o['id']}"}
                  for o in record["data"]["objects"] if o["id"] in pins]
    overrides = _group_overrides(plate.group)
    if plate.supports:
        overrides.update(_supports_overrides())
    if kind == "reduced":
        overrides.update(REDUCED_PROCESS)
    content = [kind, plate.group, bool(plate.supports), overrides,
               sorted(([o["model"], [round(float(x), 4) for x in o["scale"]], [round(float(x), 3) for x in o["rotation"]],
                        [round(float(x), 3) for x in o["position"]], o.get("overrides") or {}] for o in built), key=json.dumps)]
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
    return PlateJob(digest, kind, plate.group, bool(plate.supports), tuple(sorted(keys, key=str)), tuple(built), overrides,
                    volumes, plate.printer, plate.seconds)


def _is_heavy(plate: planner.PlanPlate, budget_s: float) -> bool:
    return plate.seconds * SLICE_S_PER_PRINT_S > HEAVY_SHARE * budget_s


def _fits(plate: planner.PlanPlate) -> bool:
    return not any(p.oversize for p in plate.items)


# --- the pool -----------------------------------------------------------------------------------------------------------------

def _is_budget_error(error: BaseException) -> bool:
    """True for a slice that ran out of time, not one OrcaSlicer failed."""
    text = (str(error) or "").lower()
    return isinstance(error, TimeoutError) or "budget" in text or "took longer" in text


def confirm_plans(project_id: int, record: dict, problem: planner.Problem, plans: list, *, budget_s: float = BUDGET_S,
                  workers: int | None = None, slice_plate=None, progress=None) -> Confirmation:
    """Slice every plate of `plans` (the search's best, cheapest first) and re-rank them with the measured figures.

    `slice_plate(job, timeout_s) -> PlateMeasure` makes one slice and raises when it fails; the default is `ProjectSlicer`.
    `progress(done, total)` is called as slices finish. Returns within about `budget_s` plus a moment (plus
    `REPAIR_BUDGET_S` when a plate failed and was split), whatever happens."""
    started = time.monotonic()
    deadline = [started + budget_s]
    slice_plate = slice_plate or ProjectSlicer(project_id, record)
    workers = max(1, workers or min(MAX_WORKERS, os.cpu_count() or 1))

    # the slices, in the order they are worth making: plan by plan, each plan's plates shortest first
    wanted, order = {}, []
    rows = []          # per plan, [plate, job key or None] in the plan's order
    reused = 0

    def register(plate) -> str | None:
        nonlocal reused
        if not _fits(plate):
            return None
        kind = "reduced" if plate.supports and _is_heavy(plate, budget_s) else "full"
        job = _build_job(record, problem, plate, kind)
        if job.key in wanted:
            reused += 1
        else:
            wanted[job.key] = job
            order.append(job.key)
        return job.key

    for plan in plans:
        keyed = {}
        for index, plate in sorted(enumerate(plan.plates), key=lambda t: (t[1].seconds, t[0])):   # shortest first: most plates measured
            keyed[index] = register(plate)
        rows.append([[plate, keyed[index]] for index, plate in enumerate(plan.plates)])

    results, errors, walls, hard = {}, {}, {}, set()
    done_count = 0

    def work(job):
        began = time.monotonic()
        left = deadline[0] - began
        if left <= 0:
            raise TimeoutError("the confirmation budget ran out")
        try:
            return slice_plate(job, left)
        finally:
            walls[job.key] = (time.monotonic() - began, job.proxy_seconds)

    def worth_starting(job) -> bool:
        """Slices start freely until one has finished; after that a slice starts only if, at the wall time per print second
        the finished ones took (under the same parallel load), it would end before the deadline."""
        spent = sum(w for w, _ in walls.values())
        printed = sum(p for _, p in walls.values())
        if not walls or printed <= 0:
            return True
        return job.proxy_seconds * spent / printed <= deadline[0] - time.monotonic()

    def run_round(keys):
        """Slice these jobs in the pool until they are done or the deadline passes."""
        nonlocal done_count
        pending, running = deque(keys), {}
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="confirm")
        try:
            while pending or running:
                while pending and len(running) < workers and time.monotonic() < deadline[0]:
                    key = pending.popleft()
                    if not worth_starting(wanted[key]):
                        continue   # stays unstarted, on its proxy figures
                    running[executor.submit(work, wanted[key])] = key
                if not running:
                    break
                finished, _ = wait(list(running), timeout=max(deadline[0] - time.monotonic(), 0), return_when=FIRST_COMPLETED)
                if not finished:
                    break   # the budget is spent with slices still running: they stay estimated
                for future in finished:
                    key = running.pop(future)
                    try:
                        results[key] = future.result()
                    except Exception as e:   # a failed slice leaves its plate on the proxy figures
                        errors[key] = str(e) or type(e).__name__
                        if not _is_budget_error(e):
                            hard.add(key)
                    done_count += 1
                    if progress:
                        progress(done_count, len(wanted))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        for future, key in running.items():
            errors.setdefault(key, "the confirmation budget ran out")

    run_round(list(order))

    # a plate OrcaSlicer failed on is replanned as one plate per Object. If none of those slices either, the slicer itself is
    # at fault (or the Objects can't be sliced at all): the plate stays as it was, marked
    swaps, again = [], []
    for p, plan_rows in enumerate(rows):
        made = []
        for pair in plan_rows:
            solos = _solo_plates(problem, pair[0]) if pair[1] in hard else None
            if solos:
                new = [[solo, register(solo)] for solo in solos]
                swaps.append((p, pair, new))
                made += new
                again += [k for _, k in new if k is not None]
            else:
                made.append(pair)
        plan_rows[:] = made
    again = [k for k in dict.fromkeys(again) if k not in results and k not in errors]
    if again:
        deadline[0] = time.monotonic() + REPAIR_BUDGET_S
        run_round(again)
    replanned = 0
    for p, old, new in swaps:
        if all(key in hard for _, key in new):
            first = next(n for n, pair in enumerate(rows[p]) if pair is new[0])
            rows[p][first:first + len(new)] = [old]
        else:
            replanned += 1
    unstarted = [k for k in wanted if k not in results and k not in errors]

    confirmed = []
    for p, plan in enumerate(plans):
        new_plates = tuple(plate for plate, _ in rows[p])
        if new_plates != plan.plates:
            plan = _with_plates(problem, plan, new_plates)
        confirmed.append(_rescore(problem, plan, [_confirmed(plate, key, wanted, results, errors, hard) for plate, key in rows[p]]))
    ranked = {id(c): n for n, c in enumerate(confirmed)}
    confirmed.sort(key=lambda c: (c.did_not_slice, c.cost, ranked[id(c)]))   # a plan with a plate that does not slice ranks after every plan that does
    stats = {"slices": len(results), "failed": len(errors), "reused": reused, "unstarted": len(unstarted),
             "reduced": sum(1 for k in results if wanted[k].kind == "reduced"), "replanned": replanned,
             "did_not_slice": sum(p.did_not_slice for c in confirmed for p in c.plates),
             "budget_exhausted": bool(unstarted) or any("budget" in e for e in errors.values()),
             "seconds": round(time.monotonic() - started, 1)}
    return Confirmation(confirmed, stats)


def _solo_plates(problem: planner.Problem, plate: planner.PlanPlate):
    """One plate per Object of a plate that did not slice, each laid out alone, or None when it can't be split (a single
    Object, a plate holding Pinned Objects, or no layout)."""
    if len(plate.items) < 2 or plate.anchor is not None or any(p.oversize for p in plate.items):
        return None
    run = planner._Run(problem)
    out = []
    for placed in sorted(plate.items, key=lambda p: str(p.key)):
        members = ((placed.key, placed.candidate),)
        layout = run.fit(None, members)
        if not layout:
            return None
        seconds, grams, _, supports = run.metrics((None, plate.group, members, False))
        spot = layout[0]
        out.append(planner.PlanPlate(None, plate.group, (planner.Placed(placed.key, placed.candidate, placed.rotation,
                                     tuple(spot["center"]), float(spot["angle"])),), seconds, grams, plate.printer, supports))
    return out


def _with_plates(problem: planner.Problem, plan: planner.Plan, plates: tuple) -> planner.Plan:
    """The plan with these plates, its count and neighbour term following; the rest of its terms are rescored by the caller."""
    where = {i.key: n for n, plate in enumerate(plates) for i in plate.items}
    apart = sum(1 for a, b in problem.neighbours if a in where and b in where and where[a] != where[b])
    terms = {**plan.terms, "plates": len(plates), "split_pairs_apart": apart}
    return planner.Plan(plates, plan.cost, terms, plan.makespan_s, plan.unplanned, plan.exact, plan.stats)


def _confirmed(plate, key, wanted, results, errors, hard=frozenset()) -> ConfirmedPlate:
    """The plate with its slice's figures, or the proxy figures when it has none."""
    if key is None:
        return ConfirmedPlate(plate, plate.printer, plate.seconds, plate.support_g, False, False, None, "does not fit a plate")
    measure = results.get(key)
    if measure is None:
        return ConfirmedPlate(plate, plate.printer, plate.seconds, plate.support_g, False, False, None,
                              errors.get(key, "not sliced: the confirmation budget ran out"), key in hard)
    timed = measure.seconds is not None
    return ConfirmedPlate(plate, plate.printer, int(measure.seconds) if timed else plate.seconds, float(measure.support_g),
                          timed, True, wanted[key].kind)


def _rescore(problem: planner.Problem, plan: planner.Plan, plates: list) -> ConfirmedPlan:
    """The plan's cost with these plates' figures, printers assigned again with their times."""
    ids = sorted(problem.printers, key=str)
    eligible = []
    for p in plates:
        nozzle = problem.groups[p.plate.group].nozzle if p.plate.group in problem.groups else None
        mine = tuple(i for i, pid in enumerate(ids) if problem.printers[pid] is not None and nozzle is not None
                     and abs(problem.printers[pid] - nozzle) < planner.NOZZLE_TOLERANCE)
        eligible.append(mine or (ids.index(p.plate.printer),))
    assignment = planner.assign_plates([p.seconds for p in plates], eligible)
    plates = [ConfirmedPlate(**{**p.__dict__, "printer": ids[i]}) for p, i in zip(plates, assignment.printers)]
    terms = {**plan.terms, "makespan_min": assignment.makespan / 60, "support_g": sum(p.support_g for p in plates),
             "machine_min": sum(p.seconds for p in plates) / 60, "plates": len(plates)}
    return ConfirmedPlan(plan, tuple(plates), planner.cost(**terms), terms, assignment.makespan)


# --- the real slicer ----------------------------------------------------------------------------------------------------------

def _generator(path) -> str | None:
    """'OrcaSlicer 2.4.2' from the G-code's `generated by` header line."""
    import re
    with open(path, encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(40), fh):
            match = re.search(r"generated by\s+(\S+(?:\s+\d[\w.]*)?)", line)
            if match:
                return match.group(1)
    return None


class ProjectSlicer:
    """Slices a planned plate with OrcaSlicer, on the Project's profile, without touching the Project: the plate is built
    as a one-plate copy of the Project's record in a scratch folder. Thread-safe; each concurrent slice has its own
    OrcaSlicer data directory."""

    def __init__(self, project_id: int, record: dict):
        self.project_id, self.record = project_id, record
        self.slots = queue.Queue()
        for slot in range(SLOT_BASE, SLOT_BASE + MAX_WORKERS * 2):
            self.slots.put(slot)

    def scratch_record(self, job: PlateJob) -> dict:
        data = copy.deepcopy(self.record["data"])
        data["plates"] = [{"id": 1, "name": "Plate 1", "overrides": dict(job.overrides)}]
        data["objects"] = [{**o, "id": n, "plate": 1} for n, o in enumerate(job.objects, start=1)]
        return {**self.record, "data": data}

    def __call__(self, job: PlateJob, timeout_s: float) -> PlateMeasure:
        slot = self.slots.get()
        try:
            return self._slice(job, timeout_s, slot)
        finally:
            self.slots.put(slot)

    def _slice(self, job: PlateJob, timeout_s: float, slot: int) -> PlateMeasure:
        import gcode_meta
        import placement_read
        import projects
        import slicer
        import support_measure
        import time_estimate
        record = self.scratch_record(job)
        plate_id = 1
        settings = projects.effective_settings(self.project_id, plate_id, record=record)
        process, filament = settings["process"], settings["filament"]
        work = tempfile.mkdtemp(prefix="plan-confirm-", dir=str(projects._data_dir))
        try:
            from pathlib import Path
            work = Path(work)
            (work / "profile").mkdir()
            (work / "output").mkdir()
            for kind, values in (("machine", settings["machine"]), ("process", process), ("filament", filament)):
                (work / "profile" / f"{kind}.json").write_text(json.dumps(values, indent=1, sort_keys=True), encoding="utf-8")
            projects.write_plate_3mf(work / "plate.3mf", self.project_id, record["data"]["objects"], projects.machine_limits(settings["machine"]))
            orca = slicer.require_orca()
            args = [str(orca), "--datadir", str(projects._data_dir / f"slicer-data-{slot}"), "--slice", "0", "--arrange", "0",
                    "--load-settings", f"{work / 'profile' / 'machine.json'};{work / 'profile' / 'process.json'}",
                    "--load-filaments", str(work / "profile" / "filament.json"), "--outputdir", str(work / "output"), str(work / "plate.3mf")]
            code, text = slicer.runner(args, max(timeout_s, 1.0))
            code = code - (1 << 32) if code >= (1 << 31) else code
            if code != 0:
                raise slicer.SliceError(slicer.explain_exit(code), code)
            produced = sorted((work / "output").glob("plate_*.gcode"))
            if not produced:
                raise slicer.SliceError("OrcaSlicer finished without producing G-code", code)
            gcode = produced[0]   # Orca's own output: the log is of what Orca made, before any post-processing
            measured = support_measure.measure(gcode)
            estimates = gcode_meta.read_estimates(gcode)   # the header's figures: the full toolpath analysis is slow Python
            analysis = {"slicer": _generator(gcode), "estimates": estimates,   # and would serialise the workers on the GIL
                        "feature_stats": {"Support": [measured["support_mm"], 0.0]} if measured["support_mm"] > 0 else {}}
            bundle_id = self.record["data"]["profile"].get("bundle_id")
            nozzle = projects._logged_nozzle(analysis, bundle_id)
            by_object = {n: v["support_g"] for n, v in (measured.get("objects") or {}).items()}
            if job.kind == "full" and not estimates.get("estimate_s"):
                raise slicer.SliceError("OrcaSlicer gave no print time", 0)
            time_estimate.record_slice(
                self.project_id, 0, placement_read.plate_features(self.project_id, record, plate_id), analysis, bundle_id=bundle_id,
                settings_hash=slicer.settings_hash(bundle_id) if bundle_id else None, nozzle=nozzle, kind=job.kind,
                gcode_path=gcode, support_volume_cm3=sum(job.proxy_volumes.values()) if job.proxy_volumes else None,
                object_support_volumes=job.proxy_volumes)
            return PlateMeasure(int(estimates["estimate_s"]) if job.kind == "full" else None, float(measured["support_g"]))
        finally:
            shutil.rmtree(work, ignore_errors=True)
