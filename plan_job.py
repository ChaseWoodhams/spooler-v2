"""
The plate planner as a background job with a stored Proposal (auto-plating ticket 18; ADR 0002, Runtime).

`start` reads the Project, then in a worker thread runs the planner's search (`planner`), slices the top plans
(`confirmation`) and stores a Proposal: the best plan plus up to two "also considered", each with its figures, the resulting
Project data and the plates that would be emptied. Nothing in the Project changes until `accept`, which applies one stored plan
with `projects.save_data` (the existing revision guard): one revision, so one Undo reverses it, and a Proposal made before the
Project last changed is refused ("changed elsewhere"). Replanned plates keep their ids, so their slices turn Outdated by
fingerprint; plates a plan empties lose theirs.

  progress    one run at a time (others wait). The job's `events` record phase (reading, searching, confirming, done), percent
              and a message; each is also sent to browsers over the websocket as {"type": "plan_job", "job": …}.
  stored      `<projects dir>/.plans/<token>.json`, swept after a day. It outlives a restart; the job registry does not.

Public API: `start(project_id)`, `get_job(project_id, job_id)`, `get_proposal(project_id, token)`, `accept(project_id, token,
plan)`, `dismiss(project_id, token)`.
"""

import asyncio
import copy
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import confirmation
import datapaths
import jobs
import planner

SEARCH_SECONDS = planner.SEARCH_SECONDS
MAX_STEPS = planner.MAX_STEPS
CONFIRM_BUDGET_S = confirmation.BUDGET_S
TTL_S = 24 * 3600
EVENT_LIMIT = 300
FOLDER = ".plans"
BRIM = {"brim_type": "outer_only", "brim_width": "5"}   # given to Objects that are tippy in every orientation
SUPERSEDED = "The project was changed elsewhere — reload it"
VERSION = 1

# percent of the whole run each phase covers
READING, SEARCHING, CONFIRMING = (0, 8), (8, 60), (60, 96)

_registry_lock = threading.Lock()
_jobs: dict = {}
_ids = iter(range(1, 1 << 62))
_run_lock = threading.Lock()   # one run at a time: each uses every core for its search and its slices


# --- the registry ----------------------------------------------------------------------------------------------------------

class _Job:
    def __init__(self, project_id: int, record: dict, nozzle: float, printers: dict):
        with _registry_lock:
            self.id = next(_ids)
        self.project_id, self.record, self.nozzle, self.printers = project_id, record, nozzle, printers
        self.revision = record["revision"]
        self.state = "queued"
        self.events, self.error, self.token = [], None, None
        self.cancelled = False
        self.thread = None
        self.lock = threading.Lock()

    def view(self, full: bool = True) -> dict:
        with self.lock:
            events = list(self.events)
            last = events[-1] if events else {"phase": "queued", "percent": 0, "message": ""}
            out = {"id": self.id, "project_id": self.project_id, "revision": self.revision, "state": self.state,
                   "progress": {k: last[k] for k in ("phase", "percent", "message")}, "error": self.error,
                   "proposal_token": self.token}
            if full:
                out["events"] = events
            token = self.token
        if full and token and self.state == "done":
            out["proposal"] = get_proposal(self.project_id, token)
        elif full:
            out["proposal"] = None
        return out

    def emit(self, phase: str, percent: float, message: str = "") -> None:
        with self.lock:
            percent = max(0, min(100, int(percent)))
            if self.events and self.events[-1]["phase"] == phase and self.events[-1]["percent"] >= percent \
                    and self.events[-1]["message"] == message:
                return
            if self.events and phase != "done":
                percent = max(percent, self.events[-1]["percent"])
            self.events.append({"phase": phase, "percent": percent, "message": message, "at": time.time()})
            del self.events[:-EVENT_LIMIT]
        _broadcast(self)


def _broadcast(job: _Job) -> None:
    import state
    loop = state.loop
    if loop is None or not loop.is_running() or not state.browser_clients:
        return
    asyncio.run_coroutine_threadsafe(state.broadcast_to_browsers({"type": "plan_job", "job": job.view(full=False)}), loop)


def reset() -> None:
    """Forget every job (tests)."""
    with _registry_lock:
        _jobs.clear()


def wait_idle(timeout: float = 60) -> bool:
    """Wait for the running jobs to stop (tests); True when none is left."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with _registry_lock:
            live = [j for j in _jobs.values() if j.thread is not None and j.thread.is_alive()]
        if not live:
            return True
        time.sleep(0.05)
    return False


# --- the Project and the printers ------------------------------------------------------------------------------------------

def plan_nozzle_of(record: dict) -> float:
    """The Project's plan nozzle: its profile bundle's nozzle."""
    import slicer
    bundle = ((record.get("data") or {}).get("profile") or {}).get("bundle_id")
    if not bundle:
        raise jobs.JobError("Choose a profile for the project before planning", 409)
    try:
        value = slicer.bundle_settings(bundle)["machine"].get("nozzle_diameter")
        return float(value[0] if isinstance(value, list) else value)
    except (KeyError, TypeError, ValueError, IndexError, jobs.JobError):
        raise jobs.JobError("The project's profile does not say what nozzle it is for", 409)


def printer_nozzles() -> dict:
    """{printer id: installed nozzle in mm, None when not set} for every printer Spooler knows."""
    import preflight
    import state
    return {pid: preflight.printer_hardware(pid).get("nozzle_diameter") for pid in sorted(state.printers, key=str)}


def _printer_names() -> dict:
    import state
    return {pid: (getattr(p, "name", None) or str(pid)) for pid, p in state.printers.items()}


# --- starting --------------------------------------------------------------------------------------------------------------

def start(project_id: int) -> dict:
    """Start planning the Project and return the job (`id`, `state`, `progress`…). Changes nothing."""
    import projects
    record = projects.get_project(project_id)
    job = _Job(project_id, record, plan_nozzle_of(record), printer_nozzles())
    with _registry_lock:
        for other in _jobs.values():
            if other.project_id == project_id and other.state in ("queued", "running"):
                other.cancelled = True          # its Proposal would be superseded by this run's
        _jobs[job.id] = job
    job.emit("reading", 0, "Waiting for another plan to finish" if _run_lock.locked() else "Reading the project")
    job.thread = threading.Thread(target=_work, args=(job,), name=f"plan-job-{job.id}", daemon=True)
    job.thread.start()
    return job.view()


def get_job(project_id: int, job_id) -> dict:
    with _registry_lock:
        job = _jobs.get(int(job_id)) if str(job_id).isdigit() else None
    if job is None or job.project_id != project_id:
        raise jobs.JobError("That plan is not running any more — plan again", 404)
    return job.view()


def _work(job: _Job) -> None:
    with _run_lock:
        if job.cancelled:
            job.state = "cancelled"
            job.emit("done", 100, "Replaced by a newer plan")
            return
        job.state = "running"
        try:
            proposal = _run(job)
            if job.cancelled:
                job.state = "cancelled"
                job.emit("done", 100, "Replaced by a newer plan")
                return
            job.token = proposal["token"]
            job.state = "done"
            job.emit("done", 100, "Plan ready")
        except Exception as e:   # the worker must say what stopped it
            job.error = str(e) or type(e).__name__
            job.state = "failed"
            print(f"[Plan] Job {job.id} of project {job.project_id} failed: {job.error}")
            job.emit("done", 100, "Planning failed")


# --- one run ---------------------------------------------------------------------------------------------------------------

def _run(job: _Job) -> dict:
    import calibration
    record, nozzle, printers = job.record, job.nozzle, job.printers
    job.emit("reading", READING[0], "Reading the project")
    models = calibration.fit_models()
    problem = planner.problem_from_project(job.project_id, record, plan_nozzle=nozzle, printers=printers, models=models)
    job.emit("reading", READING[1], f"{len(problem.items) + len(problem.oversize)} objects to plan")
    matching = [pid for pid, installed in printers.items() if planner._nozzle_matches(installed, nozzle)]
    plans = []
    if (problem.items or problem.oversize) and matching:
        plans = _search(job, problem)
    confirmed = []
    if plans:
        job.emit("confirming", CONFIRMING[0], "Slicing the best plans")

        def progress(done, total):
            job.emit("confirming", CONFIRMING[0] + (CONFIRMING[1] - CONFIRMING[0]) * done / max(total, 1),
                     f"Sliced {done} of {total} plates")
        confirmed = confirmation.confirm_plans(job.project_id, record, problem, plans, budget_s=CONFIRM_BUDGET_S,
                                               progress=progress)
    job.emit("confirming", CONFIRMING[1], "Writing the proposal")
    proposal = _proposal(job, problem, models, confirmed)
    _store(proposal)
    return proposal


def _search(job: _Job, problem: planner.Problem) -> list:
    started = time.monotonic()
    job.emit("searching", SEARCHING[0], "Looking for a better layout")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plan-search")
    try:
        future = pool.submit(planner.plan_problem, problem, budget_s=SEARCH_SECONDS, max_steps=MAX_STEPS)
        while not wait([future], timeout=0.5)[0]:
            share = min((time.monotonic() - started) / max(SEARCH_SECONDS, 0.1), 1.0)
            job.emit("searching", SEARCHING[0] + (SEARCHING[1] - SEARCHING[0]) * share, "Looking for a better layout")
        return future.result()
    finally:
        pool.shutdown(wait=False)


# --- the Project as it stands ----------------------------------------------------------------------------------------------

def _names(data: dict) -> dict:
    import problem_kinds
    return problem_kinds.object_names(data)


def _kept_and_open(project_id: int, record: dict) -> tuple:
    """([{"id", "name", "reason"}] plates a plan leaves alone, [ids] of the plates it may replan)."""
    import database
    import placement_read
    from project_read import FAILED, READY, read_ledger
    data = record["data"]
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    failed = {m["id"] for m in record["models"] if m["state"] == FAILED}
    importing = {o["plate"] for o in data["objects"] if o["model"] not in ready and o["model"] not in failed}
    with database.transaction() as conn:
        ledger = read_ledger(conn, project_id)
    kept, replannable = [], []
    for plate in data["plates"]:
        reason = (placement_read.KEPT_REASONS.get(ledger.status(plate["id"]))
                  or ("A print of it is on record" if plate["id"] in ledger.recorded else None)
                  or ("Its models are still importing" if plate["id"] in importing else None))
        if reason:
            kept.append({"id": plate["id"], "name": plate["name"], "reason": reason})
        else:
            replannable.append(plate["id"])
    return kept, replannable


class _Reading:
    """What a run needs of the Project besides the planner's Problem: which plates are kept, how every Object stands now,
    and why an Object is tippy."""

    def __init__(self, project_id: int, record: dict, nozzle: float, models):
        import placement_read
        import profile_read
        from project_read import READY
        self.project_id, self.record, self.nozzle, self.models = project_id, record, nozzle, models
        self.ready = {m["id"] for m in record["models"] if m["state"] == READY}
        self.kept, self.replannable = _kept_and_open(project_id, record)
        self.angle = placement_read._overhang_angle(record, {})
        limits = profile_read._machine_limits_for(record)
        self.limits = {"height": limits["height"], "bed_x": limits["bed"][2] - limits["bed"][0],
                       "bed_y": limits["bed"][3] - limits["bed"][1]}
        self.settings_hash = profile_read.settings_hash(record["data"]["profile"]["bundle_id"])
        self.grams = models.support_g_per_cm3(self.settings_hash, nozzle)
        self.names = _names(record["data"])
        self._previews, self._current, self._reasons = {}, {}, {}

    def _mesh(self, model: int):
        import placement_read
        if model not in self._previews:
            self._previews[model] = placement_read.load_mesh(self.project_id, model, preview=True)
        return self._previews[model]

    def standing(self, obj: dict) -> dict:
        """How the Object stands now: {"steady", "support_volume" (cm3, 0 when it needs none)}."""
        import orientation
        key = (obj["model"], tuple(obj["scale"]), tuple(obj["rotation"]))
        if key not in self._current:
            verts, tris = self._mesh(obj["model"])
            got = orientation.current(verts, tris, obj["scale"], obj["rotation"], angle=self.angle, nozzle=self.nozzle,
                                      limits=self.limits)
            self._current[key] = {"steady": got["steady"],
                                  "support_volume": got["support_volume"] / 1000 if got["needs_support"] else 0.0}
        return self._current[key]

    def tippy_reason(self, obj: dict) -> str:
        import orientation
        key = (obj["model"], tuple(obj["scale"]), tuple(obj["rotation"]))
        if key not in self._reasons:
            verts, tris = self._mesh(obj["model"])
            got = orientation.rank(verts, tris, obj["scale"], obj["rotation"], angle=self.angle, nozzle=self.nozzle,
                                   limits=self.limits)
            self._reasons[key] = got["reason"] or "It is tippy in every orientation"
        return self._reasons[key]


def _eligible(printers: dict, nozzle: float) -> list:
    """Indexes (into the sorted printer ids) of the printers a plate of this nozzle may go to; every printer when none does."""
    ids = sorted(printers, key=str)
    mine = [i for i, pid in enumerate(ids) if planner._nozzle_matches(printers[pid], nozzle)]
    return mine or list(range(len(ids)))


def _before(reading: _Reading, printers: dict) -> dict:
    """The replannable plates as they are now, on the same footing as a plan: their time (the slicer's while a slice is current,
    else the model's), their support grams by the same ray-cast measure, how many Objects stand tippy, and when the printers
    would finish them, balanced across the printers with the plan nozzle."""
    import placement_read
    import time_estimate
    record = reading.record
    data = record["data"]
    plates = {p["id"]: p for p in data["plates"]}
    rows = []
    for plate_id in reading.replannable:
        objects = [o for o in data["objects"] if o["plate"] == plate_id and o["model"] in reading.ready]
        if not objects:
            continue
        loose = [o for o in objects if not o.get("pinned")]
        volume = sum(reading.standing(o)["support_volume"] for o in loose)
        entry = (record.get("slices") or {}).get(str(plate_id)) or {}
        measured = entry.get("state") == "sliced" and not entry.get("outdated") and bool(entry.get("estimate_s"))
        features = None if measured else placement_read.plate_features(reading.project_id, record, plate_id)
        if measured:
            seconds = int(entry["estimate_s"])
        elif features:
            seconds = time_estimate.predict_seconds(features["layers"], features["volume_cm3"], features["first_layer_cm2"],
                                                    models=reading.models, support_volume_cm3=volume,
                                                    settings_hash=reading.settings_hash, nozzle=reading.nozzle)
        else:
            seconds = 0
        rows.append({"plate": plate_id, "name": plates[plate_id]["name"], "objects": sorted(o["id"] for o in objects),
                     "seconds": int(seconds), "support_g": round(volume * reading.grams, 1), "estimated": not measured,
                     "tippy": sum(1 for o in loose if not reading.standing(o)["steady"])})
    ids = sorted(printers, key=str)
    finish = {pid: 0 for pid in ids}
    span = 0
    if rows and ids:
        eligible = _eligible(printers, reading.nozzle)
        got = planner.assign_plates([r["seconds"] for r in rows], [tuple(eligible)] * len(rows))
        for row, printer in zip(rows, got.printers):
            row["printer"] = ids[printer]
            finish[ids[printer]] += row["seconds"]
        span = got.makespan
    return {"plates": len(rows), "support_g": round(sum(r["support_g"] for r in rows), 1), "tippy": sum(r["tippy"] for r in rows),
            "makespan_s": int(span), "finish": finish, "machine_s": sum(r["seconds"] for r in rows),
            "partly_estimated": any(r["estimated"] for r in rows), "plate_rows": rows}


# --- a Proposal ------------------------------------------------------------------------------------------------------------

def _proposal(job: _Job, problem: planner.Problem, models, confirmed) -> dict:
    record, nozzle, printers = job.record, job.nozzle, job.printers
    reading = _Reading(job.project_id, record, nozzle, models)
    plans = [_apply(job, problem, reading, index, cplan) for index, cplan in enumerate(confirmed.plans if confirmed else [])]
    before = _before(reading, printers)
    for plan in plans:
        plan["change"] = {"makespan_s": plan["makespan_s"] - before["makespan_s"],
                          "support_g": round(plan["support_g"] - before["support_g"], 1),
                          "plates": plan["plates"] - before["plates"], "tippy": plan["tippy"] - before["tippy"]}
    names = _printer_names()
    statuses = []
    for pid in sorted(printers, key=str):
        installed = printers[pid]
        status = "unset" if installed is None else "ok" if planner._nozzle_matches(installed, nozzle) else "mismatch"
        label = names.get(pid, str(pid))
        message = {"ok": None,
                   "unset": f"Set the nozzle installed in {label} in its printer settings; until then it gets no plates",
                   "mismatch": (f"{label} has a {installed:g} mm nozzle installed, not the {nozzle:g} mm this project "
                                "is planned for, so it gets no plates") if installed is not None else None}[status]
        statuses.append({"id": pid, "name": label, "nozzle": installed, "status": status, "message": message,
                         "receives_plates": any(r["printer"] == pid for p in plans[:1] for r in p["plate_rows"])})
    unplanned = [{"object": key, "name": reading.names.get(key, f"Object {key}"), "reason": reason}
                 for key, reason in problem.unplanned]
    return {"version": VERSION, "token": uuid.uuid4().hex, "project_id": job.project_id,
            "project_created_at": record["created_at"], "base_revision": record["revision"], "created_at": time.time(),
            "job_id": job.id, "plan_nozzle": nozzle, "printers": statuses, "before": before, "plans": plans,
            "unplanned": unplanned, "kept": reading.kept, "stats": dict(confirmed.stats) if confirmed else {},
            "summary": _summary(plans, unplanned)}


def _summary(plans: list, unplanned: list) -> str:
    if not plans:
        return "Nothing to plan" + (f": {len(unplanned)} object{'s' if len(unplanned) != 1 else ''} left unplanned" if unplanned else "")
    best = plans[0]
    return (f"{best['plates']} plate{'s' if best['plates'] != 1 else ''}, done in {best['makespan_s'] // 60} min"
            + (", partly estimated" if best["partly_estimated"] else ""))


def _apply(job: _Job, problem: planner.Problem, reading: _Reading, index: int, cplan) -> dict:
    """One confirmed plan as the Project data it would leave (kept apart from the figures), with the figures."""
    import placement_read
    import projects
    import state
    record = job.record
    data = copy.deepcopy(record["data"])
    objects = {o["id"]: o for o in data["objects"]}
    was = {o["id"]: list(o["rotation"]) for o in data["objects"]}
    held = {i.key: i for i in problem.items + tuple(problem.oversize)}
    ordered = sorted(cplan.plates, key=lambda p: -p.seconds)    # longest first, ties in the plan's order
    layout, tippy = [], []
    for cp in ordered:
        items = []
        for placed in cp.plate.items:
            objects[placed.key]["rotation"] = list(placed.rotation)
            items.append({"key": placed.key, "center": placed.center, "angle": placed.angle})
            if held[placed.key].candidates[placed.candidate].tippy:
                tippy.append(placed.key)
        layout.append({"plate": cp.plate.anchor, "group": cp.plate.group, "items": items})
    used = placement_read._place_layout(data, reading.replannable, layout, lambda group: json.loads(group)["overrides"]) if layout else []
    for key in tippy:   # tippy whatever the orientation: a brim holds it down
        objects[key]["overrides"] = {**BRIM, **(objects[key].get("overrides") or {})}
    plates = {p["id"]: p for p in data["plates"]}
    for cp, plate_id in zip(ordered, used):
        if cp.plate.supports:
            plates[plate_id]["overrides"] = {**(plates[plate_id].get("overrides") or {}), **projects.SUPPORT_OVERRIDES}
        if cp.printer in state.printers:
            plates[plate_id]["printer_id"] = cp.printer
    holding = {o["plate"] for o in data["objects"]}
    emptied = [p for p in reading.replannable if p not in holding] if used else []
    if len(data["plates"]) - len(emptied) < 1:
        emptied = emptied[1:]
    data["plates"] = [p for p in data["plates"] if p["id"] not in emptied]
    plates = {p["id"]: p for p in data["plates"]}
    existing = {p["id"] for p in record["data"]["plates"]}
    _order_and_name(data, record, plates, [p for p in used if p in plates], {k["id"] for k in reading.kept})
    rows = []
    for cp, plate_id in zip(ordered, used):
        members = sorted((objects[i.key] for i in cp.plate.items), key=lambda o: o["id"])
        rows.append({"plate": plate_id, "name": plates[plate_id]["name"], "new": plate_id not in existing,
                     "printer": cp.printer, "seconds": int(cp.seconds), "support_g": round(cp.support_g, 1),
                     "time_measured": cp.time_measured, "support_measured": cp.support_measured,
                     "partly_estimated": cp.partly_estimated, "error": cp.error, "supports": bool(cp.plate.supports),
                     "kind": cp.kind,
                     "objects": [{"id": o["id"], "name": reading.names.get(o["id"], f"Object {o['id']}"),
                                  "turned": placement_read._turned(was[o["id"]], o["rotation"]), "tippy": o["id"] in tippy}
                                 for o in members]})
    finish = {pid: {"seconds": 0, "plates": []} for pid in sorted(job.printers, key=str)}
    for row in rows:
        slot = finish.setdefault(row["printer"], {"seconds": 0, "plates": []})
        slot["seconds"] += row["seconds"]
        slot["plates"].append(row["plate"])
    tippy_rows = [{"object": key, "name": reading.names.get(key, f"Object {key}"), "reason": reading.tippy_reason(objects[key]),
                   "brim": True, "plate": plates[objects[key]["plate"]]["name"]} for key in sorted(tippy)]
    return {"index": index, "recommended": index == 0, "cost": round(cplan.cost, 2), "terms": cplan.terms,
            "makespan_s": int(cplan.makespan_s), "support_g": round(sum(r["support_g"] for r in rows), 1), "plates": len(rows),
            "tippy": len(tippy), "machine_s": sum(r["seconds"] for r in rows), "partly_estimated": bool(cplan.partly_estimated),
            "partly_estimated_plates": [r["name"] for r in rows if r["partly_estimated"]], "printers": finish,
            "plate_rows": rows, "tippy_objects": tippy_rows, "emptied": list(emptied), "data": data}


def _order_and_name(data: dict, record: dict, plates: dict, ordered: list, kept_ids: set) -> None:
    """Put the plates in plan order (kept plates keep their place) and renumber the default names ("Plate 3") to follow it,
    leaving the numbers kept plates hold free of clashes; a name the user gave stays."""
    import re
    default = re.compile(r"^Plate (\d+)$")
    kept_at = {p["id"]: i for i, p in enumerate(record["data"]["plates"]) if p["id"] in kept_ids and p["id"] in plates}
    order, queue, later = [], list(ordered), sorted(kept_at, key=kept_at.get)
    for position in range(len(kept_at) + len(ordered)):
        holder = next((k for k in later if kept_at[k] == position), None)
        if holder is not None:
            order.append(holder)
            later.remove(holder)
        elif queue:
            order.append(queue.pop(0))
    order += later
    seen = set(order)
    order += [p["id"] for p in data["plates"] if p["id"] not in seen]    # plates the plan did not touch
    taken = {int(m.group(1)) for p in data["plates"] if p["id"] in kept_ids and (m := default.match(p["name"] or ""))}
    free = (n for n in range(1, 10000) if n not in taken)
    for plate_id in ordered:
        if not plates[plate_id]["name"] or default.match(plates[plate_id]["name"]):
            plates[plate_id]["name"] = f"Plate {next(free)}"
    data["plates"] = [plates[plate_id] for plate_id in order]


# --- storing ---------------------------------------------------------------------------------------------------------------

def _folder() -> Path:
    return datapaths.projects_dir() / FOLDER


def _store(proposal: dict) -> None:
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in folder.glob("*.json"):   # a day-old Proposal is long superseded
        try:
            if now - old.stat().st_mtime > TTL_S:
                old.unlink()
        except OSError:
            pass
    temp = folder / f"{proposal['token']}.tmp"
    temp.write_text(json.dumps(proposal), encoding="utf-8")
    os.replace(temp, folder / f"{proposal['token']}.json")


def _load(project_id: int, token) -> dict:
    if not isinstance(token, str) or len(token) != 32 or not all(c in "0123456789abcdef" for c in token):
        raise jobs.JobError("Send the proposal to apply")
    try:
        stored = json.loads((_folder() / f"{token}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise jobs.JobError("That proposal is gone — plan again", 404)
    if stored.get("project_id") != project_id:
        raise jobs.JobError("That proposal is gone — plan again", 404)
    return stored


def _view(stored: dict, record: dict) -> dict:
    """The Proposal as the page reads it: no Project data, and whether the Project has moved on since."""
    superseded = record["created_at"] != stored["project_created_at"] or record["revision"] != stored["base_revision"]
    plans = [{k: v for k, v in plan.items() if k != "data"} for plan in stored["plans"]]
    return {**{k: v for k, v in stored.items() if k not in ("plans", "project_created_at")}, "plans": plans, "superseded": superseded}


def get_proposal(project_id: int, token) -> dict:
    import projects
    return _view(_load(project_id, token), projects.get_project(project_id))


# --- accept and dismiss ----------------------------------------------------------------------------------------------------

def accept(project_id: int, token, plan=0) -> dict:
    """Apply one stored plan (0 the recommended one, 1 and 2 the "also considered") as one change. Returns the Project (one
    revision later) plus "plan", the plan applied. Refused with 409 when the Project changed since the Proposal was made."""
    import projects
    stored = _load(project_id, token)
    try:
        index = int(plan)
        chosen = stored["plans"][index] if index >= 0 else None
    except (TypeError, ValueError, IndexError):
        chosen = None
    if chosen is None:
        raise jobs.JobError("Choose which of the proposal's plans to apply")
    record = projects.get_project(project_id)
    if record["created_at"] != stored["project_created_at"] or record["revision"] != stored["base_revision"]:
        raise jobs.JobError(SUPERSEDED, 409)
    kept_now = {k["id"] for k in _kept_and_open(project_id, record)[0]}
    if kept_now != {k["id"] for k in stored["kept"]}:   # queued or printed since: not the plan's to move
        raise jobs.JobError("A plate was queued or printed since this plan was made — plan again", 409)
    projects.save_data(project_id, record, chosen["data"], chosen["emptied"])
    (_folder() / f"{token}.json").unlink(missing_ok=True)
    return {**projects.get_project(project_id), "plan": {k: v for k, v in chosen.items() if k != "data"}}


def dismiss(project_id: int, token) -> dict:
    """Throw a Proposal away. Changes nothing in the Project."""
    _load(project_id, token)
    (_folder() / f"{token}.json").unlink(missing_ok=True)
    return {"ok": True}
