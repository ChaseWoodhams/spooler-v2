"""
The plate planner's search (auto-plating, staged planner step 2; ADR 0002). Proxies only: no slicer runs here.

Given the Objects to plan (each with a short ranked list of steady orientations from `orientation.rank`), Pinned Objects
as fixed obstacles, and each printer's installed nozzle, it returns ranked complete plans: which Objects share a plate, in
which orientation and where, and which printer prints each plate.

  seed        greedy first-fit, grouped by compatibility key (Filament choice, process overrides, plan nozzle, needs-support),
              sorted by height then footprint
  search      seeded local search for `budget_s` seconds: move, swap, switch to a near-tied orientation, empty the lightest
              plate. Copies (Objects of one Model) are never swapped with each other. `nesting.nest` is the fit check, memoised
              per plate content.
  assignment  every plan evaluated has its plates given to printers whose installed nozzle matches the plate's, by exact
              enumeration (`assign_plates`), LPT above EXACT_MAX_PLATES plates
  cost        `cost()`, in equivalent finish minutes, lower is better

The seed of the random search derives from the problem's content, so the same Project gives the same plans whenever the run
ends on its step cap (`max_steps`) rather than its time cap.

Public API: `plan_project` / `greedy_seed_project` (from a Project), `plan_problem` / `seed_plan` (from a `Problem`), `cost`,
`assign_plates`, and the data classes `Candidate`, `Item`, `Pinned`, `Group`, `Problem`, `Plan`, `PlanPlate`, `Placed`.
"""

import hashlib
import json
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field

import nesting

# --- cost weights, in equivalent finish minutes (spec: Cost) -------------------------------------------------------------

SUPPORT_G_WEIGHT = 6.0           # per gram of support
PLATE_WEIGHT = 30.0              # per plate
MACHINE_MIN_WEIGHT = 0.1         # per printer-minute, all plates together
TIPPY_WEIGHT = 120.0             # per Object that is tippy in every orientation
SPLIT_APART_WEIGHT = 0.5         # per pair of neighbouring Split Parts on different plates

SEARCH_SECONDS = 30.0            # default time budget of the local search
MAX_STEPS = 20000                # default step cap (the run is reproducible when it ends here rather than on time)
EXACT_MAX_PLATES = 20            # above this many plates, printers are assigned by LPT instead of enumeration
EXACT_MAX_NODES = 400000         # a safety stop for the enumeration
TOP_PLANS = 3                    # plans returned
NEAR_TIE_COST = 10.0             # an orientation within this much cost of the best one's support is a near tie
MAX_NEAR = 4                     # near-tied orientations kept per Object (the best included)
NEW_PLATE_SHARE = 0.15           # of the move steps that go to a new plate
NOZZLE_TOLERANCE = 1e-6
MAX_FAILED_SETS = 400            # sets that did not fit, remembered per plate to rule out their supersets
SUPPORT_KEYS = ("enable_support", "support_type")   # plate overrides that supports switch on; not part of a compatibility key


def cost(*, makespan_min: float, support_g: float, plates: int, machine_min: float, unavoidable_tippy: int,
         split_pairs_apart: int) -> float:
    """`makespan + 6*support_g + 30*plates + 0.1*machine_min + 120*unavoidable_tippy + 0.5*split_neighbour_pairs_apart`."""
    return (makespan_min + SUPPORT_G_WEIGHT * support_g + PLATE_WEIGHT * plates + MACHINE_MIN_WEIGHT * machine_min
            + TIPPY_WEIGHT * unavoidable_tippy + SPLIT_APART_WEIGHT * split_pairs_apart)


# --- plates to printers ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Assignment:
    makespan: float        # seconds until the last printer is done, all idle at the start
    printers: tuple        # per plate, the index of the printer it goes to
    exact: bool            # False when LPT (or the safety stop) decided it


def assign_plates(times, eligible, exact_max: int = EXACT_MAX_PLATES) -> Assignment:
    """Give each plate to one of its eligible printers so the last one finishes as early as possible. `times[i]` is plate i's
    print time, `eligible[i]` the indexes of the printers it may go to (never empty). Exact (branch and bound over the
    choices, which is enumeration with pruning) up to `exact_max` plates, LPT above."""
    n = len(times)
    if n == 0:
        return Assignment(0.0, (), True)
    if any(not e for e in eligible):
        raise ValueError("a plate has no printer it can go to")
    count = 1 + max(p for e in eligible for p in e)
    order = sorted(range(n), key=lambda i: (-times[i], i))
    loads, choice = [0.0] * count, [0] * n
    for i in order:   # LPT: the longest plate next onto the eligible printer that is free first
        p = min(eligible[i], key=lambda q: (loads[q], q))
        loads[p] += times[i]
        choice[i] = p
    best, best_choice = max(loads), tuple(choice)
    if n > exact_max:
        return Assignment(best, best_choice, False)
    usable = len({p for e in eligible for p in e})
    floor = max(max(times), sum(times) / usable)
    same = len({tuple(e) for e in eligible}) == 1
    nodes, exact = [0], [True]
    state = {"best": best, "choice": best_choice}
    loads, choice = [0.0] * count, [0] * n

    def walk(k: int):
        if state["best"] <= floor + 1e-9:
            return
        nodes[0] += 1
        if nodes[0] > EXACT_MAX_NODES:
            exact[0] = False
            return
        if k == n:
            state["best"], state["choice"] = max(loads), tuple(choice)
            return
        i, tried = order[k], set()
        for p in eligible[i]:
            if same and loads[p] in tried:   # identical printers at the same load: the same branch
                continue
            tried.add(loads[p])
            if loads[p] + times[i] >= state["best"] - 1e-9:
                continue
            loads[p] += times[i]
            choice[i] = p
            walk(k + 1)
            loads[p] -= times[i]
            if nodes[0] > EXACT_MAX_NODES:
                return

    walk(0)
    return Assignment(state["best"], state["choice"], exact[0])


# --- the problem ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One orientation of an Object, ready for packing."""
    rotation: tuple
    outline: tuple                 # footprint corners (x, y) in mm around the origin
    clearance: float               # mm the nester keeps from its neighbours
    height_mm: float
    volume_cm3: float              # plastic deposited
    footprint_cm2: float           # first-layer area, as the time estimate counts it
    support_g: float = 0.0         # proxy grams of support (0 when it needs none)
    support_volume_cm3: float = 0.0
    needs_support: bool = False
    tippy: bool = False            # unavoidably: no steady orientation fits an empty bed


@dataclass(frozen=True)
class Item:
    """An Object to plan. Objects of one Model (Copies) share `model`; `group` is the hard compatibility key (Filament
    choice, process overrides, plan nozzle): a plate never mixes groups. `candidates[0]` is the preferred orientation and
    `near` the indexes the search may switch between."""
    key: object
    model: object
    group: str
    candidates: tuple
    near: tuple = (0,)


@dataclass(frozen=True)
class Pinned:
    """A Pinned Object: it stays on its plate, where it is. `polygon` is its outline in bed millimetres."""
    key: object
    plate: object
    group: str
    polygon: tuple
    clearance: float
    height_mm: float = 0.0
    volume_cm3: float = 0.0
    footprint_cm2: float = 0.0


@dataclass(frozen=True)
class Group:
    nozzle: float | None
    layer_height: float
    seconds: object                # (layers, volume_cm3, first_layer_cm2, support_volume_cm3) -> plate seconds


@dataclass
class Problem:
    items: tuple                   # Items to search over
    groups: dict                   # group -> Group
    printers: dict                 # printer id -> installed nozzle in mm (None: matches nothing)
    bed: tuple                     # (x0, y0, x1, y1)
    exclude: tuple | None = None
    pinned: tuple = ()
    anchors: dict = field(default_factory=dict)       # plate id -> group, for each plate holding Pinned Objects
    oversize: tuple = ()           # Items that fit no plate in any orientation: each gets a plate of its own
    unplanned: tuple = ()          # (key, reason): left out, e.g. no printer has their nozzle
    neighbours: tuple = ()         # (key, key): neighbouring Parts of a Split
    seed: int = 0


@dataclass(frozen=True)
class Placed:
    key: object
    candidate: int                 # index into the Item's candidates
    rotation: tuple
    center: tuple                  # bounding-box centre of the turned outline, bed mm
    angle: float                   # degrees about Z the outline was turned by the nester
    oversize: bool = False


@dataclass(frozen=True)
class PlanPlate:
    anchor: object                 # existing plate id when Pinned Objects hold it, else None
    group: str
    items: tuple                   # of Placed
    seconds: int
    support_g: float
    printer: object                # printer id
    supports: bool                 # some Object on it needs supports


@dataclass(frozen=True)
class Plan:
    plates: tuple                  # of PlanPlate, longest first
    cost: float
    terms: dict                    # the cost's named terms
    makespan_s: float
    unplanned: tuple = ()
    exact: bool = True
    stats: dict = field(default_factory=dict, compare=False)   # steps taken, fits computed, seconds: for the benchmark

    def as_dict(self) -> dict:
        return {"cost": round(self.cost, 3), "terms": self.terms, "makespan_s": self.makespan_s, "exact": self.exact,
                "unplanned": [list(u) for u in self.unplanned],
                "plates": [{"anchor": p.anchor, "group": p.group, "seconds": p.seconds, "support_g": round(p.support_g, 2),
                            "printer": p.printer, "supports": p.supports,
                            "items": [{"key": i.key, "candidate": i.candidate, "rotation": list(i.rotation),
                                       "center": list(i.center), "angle": i.angle, "oversize": i.oversize} for i in p.items]}
                           for p in self.plates]}


def _shoelace(outline) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(outline, list(outline[1:]) + [outline[0]]))) / 2


# --- one run ---------------------------------------------------------------------------------------------------------------

class _Run:
    """The state of one planning run: memo tables for fits and evaluations, shared by the seed and the search."""

    def __init__(self, problem: Problem):
        self.p = problem
        self.items = {i.key: i for i in problem.items}
        self.oversize = {i.key: i for i in problem.oversize}
        self.pinned_on = defaultdict(list)
        for pin in problem.pinned:
            self.pinned_on[pin.plate].append(pin)
        self.printer_ids = sorted(problem.printers, key=str)
        self.eligible = {}
        for name, group in problem.groups.items():
            self.eligible[name] = tuple(i for i, pid in enumerate(self.printer_ids)
                                        if problem.printers[pid] is not None and group.nozzle is not None
                                        and abs(problem.printers[pid] - group.nozzle) < NOZZLE_TOLERANCE)
        self.partners = defaultdict(set)
        for a, b in problem.neighbours:
            self.partners[a].add(b)
            self.partners[b].add(a)
        self.room = ((problem.bed[2] - problem.bed[0] - 2 * nesting.EDGE_MARGIN_MM)
                     * (problem.bed[3] - problem.bed[1] - 2 * nesting.EDGE_MARGIN_MM))
        if problem.exclude:
            self.room -= (problem.exclude[2] - problem.exclude[0]) * (problem.exclude[3] - problem.exclude[1])
        self.fit_memo, self.metric_memo, self.span_memo, self.eval_memo = {}, {}, {}, {}
        self.pool = {}
        self.failed = {}   # anchor -> sets of (key, candidate) that did not fit one plate
        self.steps = self.nests = self.inserts = 0
        blob = json.dumps([problem.seed, [(i.key, str(i.model), i.group, [[round(r, 3) for r in c.rotation] for c in i.candidates])
                                          for i in problem.items], sorted(map(str, problem.printers.items()))], default=str)
        self.rng_seed = int(hashlib.sha256(blob.encode()).hexdigest()[:16], 16)

    # fit

    def fit(self, anchor, members) -> list | None:
        """The nester's layout of these (key, candidate) members on the plate holding `anchor`'s Pinned Objects, or None when
        they don't all fit on one plate. Three shortcuts keep the search cheap: a set that did not fit rules out its
        supersets, a set one Object bigger than a laid-out one is tried by placing just that Object among the others
        (`inserts`), and only then is the whole plate nested (`nests`)."""
        key = (anchor, members)
        if key not in self.fit_memo:
            self.fit_memo[key] = self._fit_cheap(anchor, members)
        return self.fit_memo[key]

    def _fit_cheap(self, anchor, members):
        wanted = frozenset(members)
        if any(known <= wanted for known in self.failed.get(anchor, ())):
            return None
        if len(members) > 1:
            for gone in members:
                base = self.fit_memo.get((anchor, tuple(m for m in members if m != gone)))
                if base:
                    placed = self._insert(anchor, base, gone, members)
                    if placed is not None:
                        self.inserts += 1
                        return placed
                    break
        self.nests += 1
        placed = self._fit(anchor, members)
        if placed is None:
            known = self.failed.setdefault(anchor, [])
            if len(known) < MAX_FAILED_SETS:
                known.append(wanted)
        return placed

    def _gaps(self, members) -> dict:
        """Gap each member keeps from its neighbours. A plate with supports on is arranged with the support gap throughout
        (`projects` chooses the gap by the plate's supports), so one Object that needs supports widens every gap on it."""
        cands = {k: self.items[k].candidates[c] for k, c in members}
        wide = max((c.clearance for c in cands.values() if c.needs_support), default=0.0)
        return {k: max(c.clearance, wide) for k, c in cands.items()}

    def _insert(self, anchor, base, gone, members):
        """Layout of `members` made by placing the one Object `gone` among the laid-out others, or None if it won't go."""
        import numpy as np
        gaps = self._gaps(members)
        if gaps != {**self._gaps([m for m in members if m[0] != gone[0]]), gone[0]: gaps[gone[0]]}:
            return None   # the new Object widens the gap the others were laid out with
        fixed = [{"key": pin.key, "group": 0, "slot": 0, "polygon": list(pin.polygon), "clearance": pin.clearance}
                 for pin in self.pinned_on.get(anchor, [])]
        for laid in base:
            cand = self.items[laid["key"]].candidates[dict(members)[laid["key"]]]
            a = math.radians(laid["angle"])
            c, s = math.cos(a), math.sin(a)
            pts = np.asarray(cand.outline, float) @ np.array([[c, s], [-s, c]])
            middle = (pts.max(axis=0) + pts.min(axis=0)) / 2
            fixed.append({"key": laid["key"], "group": 0, "slot": 0, "clearance": gaps[laid["key"]],
                          "polygon": [(float(x), float(y)) for x, y in pts - middle + np.asarray(laid["center"], float)]})
        cand = self.items[gone[0]].candidates[gone[1]]
        placed = nesting.nest([{"key": gone[0], "outline": list(cand.outline), "clearance": gaps[gone[0]], "group": 0}],
                              self.p.bed, self.p.exclude, fixed)
        if len(placed) != 1 or len(placed[0]["items"]) != 1 or placed[0]["items"][0]["oversize"]:
            return None
        return list(base) + placed[0]["items"]

    def _fit(self, anchor, members):
        if not members:
            return []
        cands = [(k, self.items[k].candidates[c]) for k, c in members]
        if sum(_shoelace(c.outline) for _, c in cands) > self.room:
            return None
        fixed = [{"key": pin.key, "group": 0, "slot": 0, "polygon": list(pin.polygon), "clearance": pin.clearance}
                 for pin in self.pinned_on.get(anchor, [])]
        gaps = self._gaps(members)
        placed = nesting.nest([{"key": k, "outline": list(c.outline), "clearance": gaps[k], "group": 0} for k, c in cands],
                              self.p.bed, self.p.exclude, fixed)
        if len(placed) != 1 or any(i["oversize"] for i in placed[0]["items"]) or len(placed[0]["items"]) != len(members):
            return None
        return placed[0]["items"]

    # metrics

    def metrics(self, plate) -> tuple:
        """(seconds, support_g, tippy count, supports needed) of one plate."""
        if plate not in self.metric_memo:
            anchor, group, members, _ = plate
            held = [self.items.get(k) or self.oversize[k] for k, _ in members]
            cands = [i.candidates[c] for i, (_, c) in zip(held, members)]
            pins = self.pinned_on.get(anchor, [])
            height = max([c.height_mm for c in cands] + [p.height_mm for p in pins], default=0.0)
            info = self.p.groups[group]
            layers = math.ceil(round(height / info.layer_height, 6)) if height > 0 else 0
            seconds = info.seconds(layers, sum(c.volume_cm3 for c in cands) + sum(p.volume_cm3 for p in pins),
                                   sum(c.footprint_cm2 for c in cands) + sum(p.footprint_cm2 for p in pins),
                                   sum(c.support_volume_cm3 for c in cands))
            self.metric_memo[plate] = (int(seconds), sum(c.support_g for c in cands), sum(c.tippy for c in cands),
                                       any(c.needs_support for c in cands))
        return self.metric_memo[plate]

    def makespan(self, times, eligible) -> float:
        key = tuple(sorted(zip(times, eligible)))
        if key not in self.span_memo:
            self.span_memo[key] = assign_plates([t for t, _ in key], [e for _, e in key]).makespan
        return self.span_memo[key]

    def evaluate(self, state):
        """(cost, terms, exact) of a state, or None when a plate of it doesn't fit."""
        if state in self.eval_memo:
            return self.eval_memo[state]
        result, plates_of = [], {}
        times, eligible, support, tippy = [], [], 0.0, 0
        for index, plate in enumerate(state):
            if not plate[3] and self.fit(plate[0], plate[2]) is None:
                self.eval_memo[state] = None
                return None
            seconds, grams, tips, _ = self.metrics(plate)
            times.append(seconds)
            eligible.append(self.eligible[plate[1]])
            support += grams
            tippy += tips
            for k, _ in plate[2]:
                plates_of[k] = index
        apart = sum(1 for a, partners in self.partners.items() for b in partners
                    if str(a) < str(b) and a in plates_of and b in plates_of and plates_of[a] != plates_of[b])
        span = self.makespan(times, eligible)
        terms = {"makespan_min": span / 60, "support_g": support, "plates": len(state), "machine_min": sum(times) / 60,
                 "unavoidable_tippy": tippy, "split_pairs_apart": apart}
        result = (cost(**terms), terms)
        self.eval_memo[state] = result
        return result

    # states: a tuple of plates (anchor, group, ((key, candidate), ...) sorted by key, frozen)

    @staticmethod
    def canon(plates) -> tuple:
        built = [(a, g, tuple(sorted(m.items())), f) for a, g, m, f in plates if m or a is not None]
        return tuple(sorted(built, key=lambda p: (p[0] is None, str(p[0]), p[3], p[2])))

    def seed_state(self) -> tuple:
        """Greedy first-fit: items sorted by compatibility key then height then footprint, each onto the first plate of its
        group with room (Pinned Objects' plates first), else a plate of its own."""
        plates = [[anchor, group, None, {}] for anchor, group in sorted(self.p.anchors.items(), key=lambda a: str(a[0]))]
        for plate in plates:
            plate[2] = None   # anchored plates take any tag of their group
        order = sorted(self.p.items, key=lambda i: (i.group, i.candidates[0].needs_support, -i.candidates[0].height_mm,
                                                    -i.candidates[0].footprint_cm2, str(i.key)))
        for item in order:
            tag = item.candidates[0].needs_support
            for plate in plates:
                if plate[1] != item.group or (plate[2] is not None and plate[2] != tag):
                    continue
                members = {**plate[3], item.key: 0}
                if self.fit(plate[0], tuple(sorted(members.items()))) is not None:
                    plate[3] = members
                    break
            else:
                plates.append([None, item.group, tag, {item.key: 0}])
        frozen = [[None, i.group, None, {i.key: 0}, True] for i in self.p.oversize]
        return self.canon([(a, g, m, False) for a, g, _, m in plates] + [(a, g, m, True) for a, g, _, m, _ in frozen])

    # moves

    def neighbour(self, state, rng):
        plates = [[a, g, dict(m), f] for a, g, m, f in state]
        live = [p for p in plates if not p[3]]
        kind = rng.choices(("move", "swap", "switch", "empty"), weights=(4, 3, 2, 1))[0]
        if kind == "move":
            sources = [p for p in live if p[2]]
            if not sources:
                return None
            source = rng.choice(sources)
            key = rng.choice(sorted(source[2]))
            targets = [p for p in live if p is not source and p[1] == source[1]]
            if rng.random() < NEW_PLATE_SHARE or not targets:
                if source[0] is None and len(source[2]) == 1:
                    return None
                target = [None, source[1], {}, False]
                plates.append(target)
            else:
                target = rng.choice(targets)
            target[2][key] = source[2].pop(key)
            return self.canon(plates)
        if kind == "swap":
            pairs = [(a, b) for n, a in enumerate(live) for b in live[n + 1:] if a[1] == b[1] and a[2] and b[2]]
            if not pairs:
                return None
            a, b = rng.choice(pairs)
            ka, kb = rng.choice(sorted(a[2])), rng.choice(sorted(b[2]))
            if self.items[ka].model == self.items[kb].model:   # Copies are never swapped with each other
                return None
            a[2][kb], b[2][ka] = b[2].pop(kb), a[2].pop(ka)
            return self.canon(plates)
        if kind == "switch":
            options = [(p, k) for p in live for k in p[2] if len(self.items[k].near) > 1]
            if not options:
                return None
            plate, key = rng.choice(options)
            other = [c for c in self.items[key].near if c != plate[2][key]]
            plate[2][key] = rng.choice(other)
            return self.canon(plates)
        movable = [p for p in live if p[0] is None and p[2]]
        if len(live) < 2 or not movable:
            return None
        lightest = min(movable, key=lambda p: (self.metrics((p[0], p[1], tuple(sorted(p[2].items())), False))[0], str(sorted(p[2]))))
        rest = [p for p in live if p is not lightest]
        for key in sorted(lightest[2], key=lambda k: -self.items[k].candidates[lightest[2][k]].footprint_cm2):
            for target in sorted((p for p in rest if p[1] == lightest[1]),
                                 key=lambda p: self.metrics((p[0], p[1], tuple(sorted(p[2].items())), False))[0]):
                members = {**target[2], key: lightest[2][key]}
                if self.fit(target[0], tuple(sorted(members.items()))) is not None:
                    target[2] = members
                    break
            else:
                return None
        lightest[2] = {}
        return self.canon(plates)

    # the search

    def consider(self, state, evaluated):
        if evaluated is not None:
            self.pool[state] = evaluated[0]
            if len(self.pool) > 200:
                keep = sorted(self.pool.items(), key=lambda s: (s[1], repr(s[0])))[:60]
                self.pool = dict(keep)

    def search(self, start, deadline: float, max_steps: int):
        rng = random.Random(self.rng_seed)
        current, here = start, self.evaluate(start)
        self.consider(current, here)
        for _ in range(max_steps):
            self.steps += 1
            if time.monotonic() >= deadline:
                break
            candidate = self.neighbour(current, rng)
            if candidate is None or candidate == current:
                continue
            found = self.evaluate(candidate)
            if found is None:
                continue
            self.consider(candidate, found)
            if here is None or found[0] <= here[0] + 1e-9:
                current, here = candidate, found

    # the answer

    def plan_of(self, state) -> Plan:
        cost_value, terms = self.evaluate(state)
        times = [self.metrics(p)[0] for p in state]
        eligible = [self.eligible[p[1]] for p in state]
        assignment = assign_plates(times, eligible)
        plates = []
        for plate, printer in zip(state, assignment.printers):
            anchor, group, members, frozen = plate
            if frozen:
                key, idx = members[0]
                cx, cy = (self.p.bed[0] + self.p.bed[2]) / 2, (self.p.bed[1] + self.p.bed[3]) / 2
                placed = (Placed(key, idx, self.oversize[key].candidates[idx].rotation, (cx, cy), 0.0, True),)
            else:
                layout = {i["key"]: i for i in self.fit(anchor, members)}
                placed = tuple(Placed(k, c, self.items[k].candidates[c].rotation, tuple(layout[k]["center"]),
                                      float(layout[k]["angle"])) for k, c in members)
            seconds, grams, _, supports = self.metrics(plate)
            plates.append(PlanPlate(anchor, group, placed, seconds, grams, self.printer_ids[printer], supports))
        plates.sort(key=lambda p: (-p.seconds, str(p.anchor), str([i.key for i in p.items])))
        return Plan(tuple(plates), cost_value, terms, assignment.makespan, self.p.unplanned, assignment.exact)


def seed_plan(problem: Problem) -> Plan:
    """The greedy first-fit plan alone, with no search."""
    run = _Run(problem)
    return run.plan_of(run.seed_state())


def plan_problem(problem: Problem, *, budget_s: float = SEARCH_SECONDS, max_steps: int = MAX_STEPS, top: int = TOP_PLANS) -> list:
    """The best `top` distinct plans, cheapest first (the seed is always among the candidates, so there is always one)."""
    started = time.monotonic()
    run = _Run(problem)
    seed = run.seed_state()   # the budget covers the seed too
    run.search(seed, started + budget_s, max_steps)
    ranked = sorted(run.pool.items(), key=lambda s: (s[1], repr(s[0])))[:top]
    plans = [run.plan_of(state) for state, _ in ranked]
    stats = {"steps": run.steps, "nests": run.nests, "inserts": run.inserts, "seconds": round(time.monotonic() - started, 1)}
    return [Plan(p.plates, p.cost, p.terms, p.makespan_s, p.unplanned, p.exact, stats) for p in plans]


# --- from a Project --------------------------------------------------------------------------------------------------------

def _nozzle_matches(a, b) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) < NOZZLE_TOLERANCE


def _measure(verts, tris, obj: dict, rotation, settings: dict):
    """(outline around its centre, height, plastic deposited in cm3, first-layer cm2) of the Object standing in `rotation`."""
    import numpy as np
    import placement_read
    import time_estimate
    placed = placement_read.place(verts, {**obj, "rotation": list(rotation), "position": [0.0, 0.0]})
    size = placed.max(axis=0) - placed.min(axis=0)
    corners = np.asarray(verts, float)[np.asarray(tris)]
    volume = abs(float(np.einsum("ij,ij->i", corners[:, 0], np.cross(corners[:, 1], corners[:, 2])).sum())) / 6000
    surface = float(np.linalg.norm(np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1).sum()) / 200
    factor = abs(float(obj["scale"][0]) * float(obj["scale"][1]) * float(obj["scale"][2]))
    deposited = time_estimate.deposited_cm3(volume * factor, surface * factor ** (2 / 3), float(size[2]), settings)
    centre = (placed[:, :2].max(axis=0) + placed[:, :2].min(axis=0)) / 2
    outline = tuple((float(x - centre[0]), float(y - centre[1])) for x, y in nesting.hull_outline(placed[:, :2]))
    return outline, float(size[2]), deposited, float(size[0] * size[1]) / 100


def _fits_alone(candidate: Candidate, limits: dict) -> bool:
    if candidate.height_mm > limits["height"] + 0.01:
        return False
    placed = nesting.nest([{"key": 0, "outline": list(candidate.outline), "clearance": candidate.clearance, "group": 0}],
                          limits["bed"], limits["exclude"])
    return bool(placed) and not any(i["oversize"] for p in placed for i in p["items"])


def _split_neighbours(keys, data: dict, record: dict) -> tuple:
    """Pairs of Objects that are neighbouring Parts of a Split (the i-th Copy of one with the i-th Copy of the other)."""
    import splits
    wanted = set(keys)
    by_part = defaultdict(list)
    models = {m["id"]: m for m in record["models"]}
    for obj in sorted(data["objects"], key=lambda o: o["id"]):
        split = (models.get(obj["model"]) or {}).get("split")
        if split and obj["id"] in wanted:
            by_part[(split["id"], split["part"])].append(obj["id"])
    pairs = set()
    for split_id in {s for s, _ in by_part}:
        try:
            parts = (splits.get_split(split_id, detail=True).get("result") or {}).get("parts", [])
        except Exception:   # the split is gone: its Parts have no known neighbours
            continue
        for part in parts:
            for near in part.get("neighbours", []):
                a, b = by_part.get((split_id, part["number"]), []), by_part.get((split_id, near["part"]), [])
                pairs.update((x, y) if str(x) < str(y) else (y, x) for x, y in zip(a, b))
    return tuple(sorted(pairs, key=str))


def problem_from_project(project_id: int, record: dict, *, plan_nozzle: float, printers: dict, models=None, ledger=None,
                         seed: int = 0) -> Problem:
    """Read the Project into a Problem. Planned: every Object that is not Pinned, on a plate that is not queued, printing,
    printed, recorded or still importing, whose model is ready. Pinned Objects on those plates are fixed obstacles. `printers`
    maps printer id to its installed nozzle in mm (None: unknown, matches nothing); `plan_nozzle` is the nozzle planned for;
    `models` is `calibration.fit_models()` (fitted now when not given). Objects of one Model are Copies."""
    import calibration
    import database
    import orientation
    import placement_read
    import profile_read
    import time_estimate
    from project_read import FAILED, READY, _choice_key, pack_for, read_ledger
    if models is None:
        models = calibration.fit_models()
    data = record["data"]
    ready = {m["id"] for m in record["models"] if m["state"] == READY}
    failed = {m["id"] for m in record["models"] if m["state"] == FAILED}
    importing = {o["plate"] for o in data["objects"] if o["model"] not in ready and o["model"] not in failed}
    if ledger is None:
        with database.transaction() as conn:
            ledger = read_ledger(conn, project_id)
    plates = {p["id"]: p for p in data["plates"]}
    open_plates = {pid for pid in plates if not (placement_read.KEPT_REASONS.get(ledger.status(pid)) or pid in ledger.recorded
                                                  or pid in importing)}
    limits = profile_read._machine_limits_for(record)
    bed_limits = {"height": limits["height"], "bed_x": limits["bed"][2] - limits["bed"][0],
                  "bed_y": limits["bed"][3] - limits["bed"][1]}
    angle = placement_read._overhang_angle(record, {})
    shash = profile_read.settings_hash(data["profile"]["bundle_id"])
    choices = data.get("filaments") or {}
    groups, group_settings = {}, {}

    def group_for(obj) -> str:
        overrides = {k: v for k, v in (plates[obj["plate"]].get("overrides") or {}).items() if k not in SUPPORT_KEYS}
        name = json.dumps({"overrides": overrides, "filament": _choice_key(choices.get(str(obj["model"]))), "nozzle": plan_nozzle},
                          sort_keys=True, default=str)
        if name not in groups:
            settings = group_settings[name] = time_estimate.settings_from(placement_read._plate_process(record, {"overrides": overrides}))
            groups[name] = Group(float(plan_nozzle), settings["layer_height"],
                                 lambda layers, volume, first, support:
                                 time_estimate.predict_seconds(layers, volume, first, models=models, support_volume_cm3=support,
                                                               settings_hash=shash, nozzle=plan_nozzle))
        return name

    grams = models.support_g_per_cm3(shash, plan_nozzle)
    matching = [pid for pid, nozzle in printers.items() if _nozzle_matches(nozzle, plan_nozzle)]
    shapes = placement_read._Shapes(project_id, record, data)
    meshes, previews, ranked = {}, {}, {}
    items, oversize, unplanned, pinned, anchors = [], [], [], [], {}
    for obj in sorted((o for o in data["objects"] if o["plate"] in open_plates and o["model"] in ready), key=lambda o: o["id"]):
        group = group_for(obj)
        if obj["model"] not in meshes:
            meshes[obj["model"]] = placement_read.load_mesh(project_id, obj["model"])
            previews[obj["model"]] = placement_read.load_mesh(project_id, obj["model"], preview=True)
        verts, tris = meshes[obj["model"]]
        if obj.get("pinned"):
            anchors.setdefault(obj["plate"], group)
            _, height, volume, first = _measure(verts, tris, obj, obj["rotation"], group_settings[group])
            pinned.append(Pinned(obj["id"], obj["plate"], group, tuple(map(tuple, shapes.polygon(obj))),
                                 shapes.outline_and_clearance(obj)[1], height, volume, first))
            continue
        if not matching:
            unplanned.append((obj["id"], f"No printer has a {plan_nozzle:g} mm nozzle installed"))
            continue
        key = (obj["model"], tuple(obj["scale"]), tuple(obj["rotation"]))
        if key not in ranked:
            ranked[key] = orientation.rank(*previews[obj["model"]], obj["scale"], obj["rotation"], angle=angle,
                                           nozzle=float(plan_nozzle), limits=bed_limits)
        result = ranked[key]
        pack = pack_for(data, obj["plate"])
        cands = []
        for c in result["candidates"]:
            outline, height, volume, first = _measure(verts, tris, obj, c["rotation"], group_settings[group])
            needs = bool(c["needs_support"])
            cands.append(Candidate(tuple(c["rotation"]), outline, pack["support_gap" if needs else "gap"], height, volume, first,
                                   c["support_volume"] / 1000 * grams if needs else 0.0,
                                   c["support_volume"] / 1000 if needs else 0.0, needs, bool(result["unavoidably_tippy"])))
        if not cands:
            unplanned.append((obj["id"], "No orientation to print it in"))
            continue
        fitting = [c for c in cands if _fits_alone(c, limits)]
        if not fitting:
            oversize.append(Item(obj["id"], obj["model"], group, (cands[0],)))
            continue
        near = tuple(i for i, c in enumerate(fitting)
                     if i == 0 or (c.support_g - fitting[0].support_g) * SUPPORT_G_WEIGHT <= NEAR_TIE_COST)[:MAX_NEAR]
        items.append(Item(obj["id"], obj["model"], group, tuple(fitting), near))
    neighbours = _split_neighbours([i.key for i in items + oversize], data, record)
    return Problem(tuple(items), groups, dict(printers), tuple(limits["bed"]), limits["exclude"], tuple(pinned), anchors,
                   tuple(oversize), tuple(unplanned), neighbours, seed)


def plan_project(project_id: int, record: dict, *, plan_nozzle: float, printers: dict, models=None, ledger=None,
                 budget_s: float = SEARCH_SECONDS, max_steps: int = MAX_STEPS, seed: int = 0) -> list:
    """The best plans for a Project, cheapest first: `plan_problem` on `problem_from_project`."""
    built = problem_from_project(project_id, record, plan_nozzle=plan_nozzle, printers=printers, models=models, ledger=ledger, seed=seed)
    return plan_problem(built, budget_s=budget_s, max_steps=max_steps)


def greedy_seed_project(project_id: int, record: dict, *, plan_nozzle: float, printers: dict, models=None, ledger=None) -> Plan:
    """Only the greedy first-fit plan, for the entry points that need a layout at once."""
    return seed_plan(problem_from_project(project_id, record, plan_nozzle=plan_nozzle, printers=printers, models=models, ledger=ledger))
