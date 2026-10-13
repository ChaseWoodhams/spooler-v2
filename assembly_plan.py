"""
How a finished split goes together: lettered Sections, numbered steps, and "cannot be assembled" reports.

Public pieces (later tickets build on these)
  SplitModel(result, cuts, preview=None)   what the planner knows about a split; `.fingerprint` identifies its cuts and parts
  build_plan(model, fallback=None)         PlanResult: `.plan` (Plan) or `.failure` (Failure); `.to_dict()` is the API shape.
                                           The fallback planner is used when the cut tree can't be rebuilt; `fallback=False` turns it off
  fallback_plan(model, reason)             the tree-less planner (disassembly over the cut-normal directions); Plan.fallback_reason says why it ran
  validate_step(model, unit, placed, direction)
                                           the single gate for a step: None when allowed, else a typed Failure
  find_direction(model, unit, placed)      the direction a unit must slide in along, or the Failure that stops it
  Plan.fingerprint / Plan.code             what the plan contains (Sections, steps, directions, rods); code = first six hex characters
  Failure(kind, parts, point, message, numbers, fixes)
                                           kinds: rod_direction, rod_side, blocked, disconnected, interlock, anomaly

The plan comes from the cut tree: the cuts are replayed backwards, merging the groups that each cut's joints connect.
At every node the children are joined one at a time along the cut normal, away from the plane, so the rods are parallel
and nothing crosses the plane. A node built from several parts is a Section built apart; the heavier child of a node is
the spine, so it continues its parent's Section. Sections are lettered in the order they finish (side Sections first,
Main = A last), which is also their page order.

When the tree can't be rebuilt (no cut history, several input bodies, a joint with several cuts) the fallback takes the
split apart instead: along each signed cut normal (or rod axis) it removes the smallest group that can leave, meaning no
rod across the group's boundary is off-axis or on the wrong side and nothing sits in its way, and plans the group itself
the same way. Each group of several parts removed together is built apart as a Section. A joint with several cuts whose
rods follow none of them is an anomaly. When no order exists the report names the cheapest set of joints in the way.
"""

import hashlib
import json
import math
import struct
from dataclasses import dataclass, field

import numpy as np

try:
    import manifold3d as m3d
except ImportError:  # sweeps then use bounding boxes only
    m3d = None

ROD_DIRECTION, ROD_SIDE, BLOCKED = "rod_direction", "rod_side", "blocked"
DISCONNECTED, INTERLOCK, ANOMALY = "disconnected", "interlock", "anomaly"

ROD_TOLERANCE_DEG = 1.0
MAX_PARTS_PER_STEP = 3
TOUCH_MM = 0.01          # boxes that overlap by less than this only touch
SWEEP_WIDTH_MM = 0.0004  # cross-section of the segment a part is swept with
SWEEP_HIT_MM3 = 2.0      # a sweep must cut this much volume out of a placed part to count as a hit
MESH_MAGIC = b"SPV1"


# ── Reports ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Failure:
    """Why a step or a whole split cannot be done: the joint (or parts) to blame in plain words, and ranked fixes."""
    kind: str
    parts: tuple
    point: tuple | None
    message: str
    numbers: dict = field(default_factory=dict)
    fixes: tuple = ()

    def to_dict(self) -> dict:
        return {"kind": self.kind, "parts": list(self.parts), "point": list(self.point) if self.point else None,
                "message": self.message, "numbers": self.numbers, "fixes": list(self.fixes)}


@dataclass(frozen=True)
class Step:
    id: str
    kind: str              # "part" (one to three parts) or "section" (a finished Section joins)
    parts: tuple
    receiver: tuple
    direction: tuple
    rods: int
    connectors: dict
    glue_only: bool
    section: str | None    # the letter of the Section joined, for kind "section"

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "parts": list(self.parts), "receiver": list(self.receiver),
                "direction": list(self.direction), "rods": self.rods, "connectors": self.connectors,
                "glue_only": self.glue_only, "section": self.section}


@dataclass(frozen=True)
class Section:
    letter: str
    main: bool
    parts: tuple
    start_part: int
    steps: tuple
    disconnected: bool
    note: str

    def to_dict(self) -> dict:
        return {"letter": self.letter, "main": self.main, "parts": list(self.parts), "start_part": self.start_part,
                "steps": [s.to_dict() for s in self.steps], "disconnected": self.disconnected, "note": self.note}


@dataclass(frozen=True)
class Plan:
    sections: tuple       # in page order: side Sections first, Main (A) last
    fingerprint: str
    fallback_reason: str = ""   # why the cut tree was not used (the fallback planner made this plan), else ""

    @property
    def code(self) -> str:
        return self.fingerprint[:6]

    @property
    def steps(self) -> list:
        return [step for section in self.sections for step in section.steps]

    def summary(self) -> dict:
        return {"parts": len({p for s in self.sections for p in s.parts}),
                "sections": len(self.sections), "steps": len(self.steps), "rods": sum(s.rods for s in self.steps)}

    def to_dict(self) -> dict:
        return {"sections": [s.to_dict() for s in self.sections], "summary": self.summary(),
                "fingerprint": self.fingerprint, "code": self.code, "fallback_reason": self.fallback_reason}


@dataclass(frozen=True)
class PlanResult:
    plan: Plan | None = None
    failure: Failure | None = None

    @property
    def ok(self) -> bool:
        return self.plan is not None

    def to_dict(self) -> dict:
        if self.plan:
            return {"ok": True, "cannot_be_assembled": False, **self.plan.to_dict()}
        return {"ok": False, "cannot_be_assembled": True, "headline": "Cannot be assembled as cut",
                "failure": self.failure.to_dict()}


class _Stop(Exception):
    def __init__(self, failure: Failure):
        super().__init__(failure.message)
        self.failure = failure


# ── What the planner knows about a split ───────────────────────────────────────

def read_preview(path) -> list:
    """The per-part preview meshes (assembled pose) as [(vertices, triangles)], or [] when unreadable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if data[:4] != MESH_MAGIC:
            return []
        count, = struct.unpack_from("<I", data, 4)
        at, meshes = 8, []
        for _ in range(count):
            nv, nt = struct.unpack_from("<II", data, at)
            at += 8
            verts = np.frombuffer(data, "<f4", nv * 3, at).reshape(-1, 3)
            at += nv * 12
            tris = np.frombuffer(data, "<u4", nt * 3, at).reshape(-1, 3)
            at += nt * 12
            meshes.append((verts, tris))
        return meshes
    except (OSError, struct.error, ValueError):
        return []


def _unit_vector(v) -> np.ndarray:
    v = np.asarray(v, float)
    length = float(np.linalg.norm(v))
    return v / length if length else v


def axis_words(v) -> str:
    v = _unit_vector(v)
    for i, name in enumerate("XYZ"):
        if abs(v[i]) > 0.9999:
            return f"the {'-' if v[i] < 0 else ''}{name} axis"
    return "the direction (" + ", ".join(f"{x:.2f}" for x in v) + ")"


def _pair(a, b) -> tuple:
    return (a, b) if a < b else (b, a)


def _parts_words(parts) -> str:
    parts = sorted(parts)
    if len(parts) == 1:
        return f"part {parts[0]}"
    return f"parts {', '.join(map(str, parts[:-1]))} and {parts[-1]}"


class SplitModel:
    """A split result and its cuts, indexed for planning. `preview` (per-part meshes in the assembled pose) makes the
    sweep test exact; without it the test uses each part's bounding box."""

    def __init__(self, result: dict, cuts: list, preview: list | None = None):
        self.cuts = list(cuts or [])
        self.cut_by_id = {c["id"]: c for c in self.cuts}
        parts = sorted(result.get("parts") or [], key=lambda p: p["number"])
        self.numbers = [p["number"] for p in parts]
        self.volume = {p["number"]: float(p["volume_cm3"]) for p in parts}
        self.box = {p["number"]: np.asarray(p["bbox"], float).reshape(2, 3) for p in parts}
        self.joints = {_pair(*j["parts"]): j for j in result.get("joints") or []}
        self.pins = {}
        for pin in result.get("pins") or []:
            self.pins.setdefault(_pair(*pin["parts"]), []).append(pin)
        self._infer_joints()
        self.partners = {n: set() for n in self.numbers}
        for a, b in self.joints:
            self.partners.setdefault(a, set()).add(b)
            self.partners.setdefault(b, set()).add(a)
        boxes = list(self.box.values())
        extent = (np.max([b[1] for b in boxes], axis=0) - np.min([b[0] for b in boxes], axis=0)) if boxes else np.zeros(3)
        self.reach = float(np.linalg.norm(extent)) * 2 + 10
        self._meshes = preview if (m3d is not None and preview and len(preview) == len(self.numbers)) else None
        self._manifolds, self._sweeps, self._hit_cache = {}, {}, {}

    def _infer_joints(self) -> None:
        """The engine pairs a joint's parts from one probe point, so a cut face shared by several part pairs can list
        rods for a pair that has no joint. Such a pair is a real contact: it is added, owned by the cut whose plane
        holds its rods. A pair on no cut plane stays unlisted and is reported as a data problem."""
        for pair, pins in sorted(self.pins.items()):
            if pair in self.joints or pair[0] not in self.box or pair[1] not in self.box:
                continue
            p, axis = np.asarray(pins[0]["p"], float), _unit_vector(pins[0]["axis"])
            for cut in self.cuts:
                n = _unit_vector(cut["normal"])
                if abs(float(np.dot(n, axis))) >= math.cos(math.radians(ROD_TOLERANCE_DEG)) and abs(float(np.dot(n, p)) - float(cut["offset"])) <= 1.0:
                    self.joints[pair] = {"parts": list(pair), "area_mm2": 0, "rods": len(pins), "connectors": {}, "cuts": [cut["id"]],
                                         "keyed": True, "inferred": True}
                    break

    @property
    def fingerprint(self) -> str:
        """The cut list plus each part's rounded size and volume: whether two splits are the same cut."""
        cuts = [[c["id"], c.get("parent"), [round(float(x), 4) for x in c["normal"]], round(float(c["offset"]), 3)] for c in self.cuts]
        parts = [[n, [round(float(x), 1) for x in sorted(self.box[n][1] - self.box[n][0])], round(self.volume[n], 1)] for n in self.numbers]
        return hashlib.sha256(json.dumps([cuts, parts], sort_keys=True).encode()).hexdigest()

    # geometry
    def centre(self, part: int) -> np.ndarray:
        return self.box[part].mean(axis=0)

    def joint_point(self, a: int, b: int) -> tuple:
        pins = self.pins.get(_pair(a, b))
        if pins:
            point = np.mean([p["p"] for p in pins], axis=0)
        else:
            lo = np.maximum(self.box[a][0], self.box[b][0])
            hi = np.minimum(self.box[a][1], self.box[b][1])
            point = (lo + hi) / 2 if (hi >= lo - TOUCH_MM).all() else (self.centre(a) + self.centre(b)) / 2
        return tuple(round(float(x), 2) for x in point)

    def _manifold(self, part: int):
        if part not in self._manifolds:
            verts, tris = self._meshes[self.numbers.index(part)]
            self._manifolds[part] = m3d.Manifold(m3d.Mesh(vert_properties=np.array(verts, np.float32, order="C"), tri_verts=np.array(tris, np.uint32, order="C")))
        return self._manifolds[part]

    def _sweep(self, part: int, w: np.ndarray):
        key = (part, tuple(np.round(w, 4)))
        if key not in self._sweeps:
            helper = np.array([1.0, 0, 0]) if abs(w[0]) < 0.9 else np.array([0, 1.0, 0])
            e1 = _unit_vector(np.cross(w, helper))
            e2 = np.cross(w, e1)
            t = SWEEP_WIDTH_MM
            points = [s * w * self.reach + a * t * e1 + b * t * e2 for s in (0, 1) for a in (-1, 1) for b in (-1, 1)]
            self._sweeps[key] = self._manifold(part).minkowski_sum(m3d.Manifold.hull_points(np.asarray(points)))
        return self._sweeps[key]

    def sweep_hits(self, part: int, w, other: int) -> bool:
        """Does `part`, slid away along `w` to far off, run into `other`? Touching faces are not hits."""
        w = _unit_vector(w)
        lo, hi = self.box[part]
        ends = np.array([lo, hi, lo + w * self.reach, hi + w * self.reach])
        swept_lo, swept_hi = ends.min(axis=0), ends.max(axis=0)
        overlap = np.minimum(swept_hi, self.box[other][1]) - np.maximum(swept_lo, self.box[other][0])
        if (overlap <= TOUCH_MM).any():
            return False
        if self._meshes is None:
            return True
        key = (part, other, tuple(np.round(w, 4) + 0.0))
        if key not in self._hit_cache:
            self._hit_cache[key] = (self._sweep(part, w) ^ self._manifold(other)).volume() > SWEEP_HIT_MM3
        return self._hit_cache[key]


# ── The step validator ─────────────────────────────────────────────────────────

def _joints_between(model: SplitModel, unit, placed) -> list:
    """The joints (sorted) between a part of `unit` and a part of `placed`, as (unit part, placed part)."""
    return sorted((a, b) for a in unit for b in model.partners.get(a, ()) if b in placed)


def _blame(model: SplitModel, pairs) -> tuple:
    """The joint to blame among `pairs`: fewest rods, then smallest area, then the higher part numbers."""
    def cost(pair):
        joint = model.joints.get(_pair(*pair), {})
        return (len(model.pins.get(_pair(*pair), [])), joint.get("area_mm2", 0), -max(pair), -min(pair))
    return min(pairs, key=cost)


def _joint_facts(model: SplitModel, a: int, b: int) -> dict:
    joint = model.joints.get(_pair(a, b), {})
    return {"rods": len(model.pins.get(_pair(a, b), [])), "area_mm2": joint.get("area_mm2", 0)}


def validate_step(model: SplitModel, unit, placed, direction, theta_deg: float = ROD_TOLERANCE_DEG) -> Failure | None:
    """None when `unit` may be placed along `direction` onto `placed`, else why not. Checks, in this order: the unit
    shares a joint with what is placed; every rod across the join is parallel to the direction (within theta); the
    direction points the unit towards the cut plane of each rodded joint; the unit's sweep away hits nothing placed."""
    unit, placed = sorted(unit), sorted(placed)
    u = _unit_vector(direction)
    between = _joints_between(model, unit, set(placed))
    if not between:
        return Failure(DISCONNECTED, (), None,
                       f"{_parts_words(unit).capitalize()} touches none of the parts already placed ({_parts_words(placed)}), so there is nothing to join it to.",
                       {"unit": unit, "placed": placed},
                       ("Check that the two meet at a cut: a contact may have been lost, or the model has separate bodies.",
                        "Put it in its own Section and join it by hand."))
    cos_limit = math.cos(math.radians(theta_deg))
    off = [(a, b) for a, b in between if any(abs(np.dot(_unit_vector(p["axis"]), u)) < cos_limit for p in model.pins.get(_pair(a, b), []))]
    if off:
        a, b = _blame(model, off)
        worst = min(model.pins[_pair(a, b)], key=lambda p: abs(np.dot(_unit_vector(p["axis"]), u)))
        angle = math.degrees(math.acos(min(1.0, abs(float(np.dot(_unit_vector(worst["axis"]), u))))))
        facts = _joint_facts(model, a, b)
        who = _parts_words(unit)
        return Failure(ROD_DIRECTION, _pair(a, b), model.joint_point(a, b),
                       f"Part {min(a, b)} and part {max(a, b)} are joined by {facts['rods']} rods along {axis_words(worst['axis'])}, "
                       f"but {who} has to slide in along {axis_words(u)} to meet its other joints, so those rods are {angle:.0f} degrees off.",
                       {"angle_deg": round(angle, 1), "rods": facts["rods"], "area_mm2": facts["area_mm2"], "direction": [round(float(x), 3) for x in u]},
                       (f"Join {who} to its neighbours on the other axis first as a separate Section, then slide that Section on.",
                        f"Make the joint of part {min(a, b)} and part {max(a, b)} glue only: that drops {facts['rods']} rods and {facts['area_mm2']} mm2 of keyed bond, and the part will smear glue as it slides across.",
                        "Change the cuts so these joints meet along one axis."))
    wrong = []
    for a, b in between:
        for pin in model.pins.get(_pair(a, b), []):
            axis = _unit_vector(pin["axis"])
            side = 1.0 if np.dot(model.centre(a) - np.asarray(pin["p"], float), axis) >= 0 else -1.0
            if np.dot(-side * axis, u) < 0:   # the unit must move towards the plane
                wrong.append((a, b))
                break
    if wrong:
        a, b = _blame(model, wrong)
        facts = _joint_facts(model, a, b)
        return Failure(ROD_SIDE, _pair(a, b), model.joint_point(a, b),
                       f"Part {min(a, b)} and part {max(a, b)} are joined by {facts['rods']} rods, which only go in if {_parts_words(unit)} "
                       f"moves towards the joint, but this step moves it away along {axis_words(u)}"
                       + (f" (another joint needs the opposite side)." if len(wrong) < len(between) else "."),
                       {"rods": facts["rods"], "area_mm2": facts["area_mm2"], "direction": [round(float(x), 3) for x in u]},
                       (f"Place {_parts_words(unit)} before one of its two neighbours so it only meets one side.",
                        "Cut the part so each piece meets the others from one side."))
    w = -u
    for a in unit:
        for q in placed:
            if model.sweep_hits(a, w, q):
                return Failure(BLOCKED, _pair(a, q), model.joint_point(a, q) if q in model.partners.get(a, ()) else tuple(round(float(x), 2) for x in model.centre(q)),
                               f"Part {a} can't slide in along {axis_words(u)}: part {q} is in its way.",
                               {"direction": [round(float(x), 3) for x in u], "blocker": q},
                               (f"Place part {q} after part {a}.",
                                f"If part {q} can't be placed later either, the two lock each other: move a cut or add one between them."))
    return None


def find_direction(model: SplitModel, unit, placed) -> tuple:
    """(direction, None) for the direction `unit` must slide in along to join `placed`, or (None, Failure).
    The rodded joints decide it (towards each cut plane); a unit joined only by glue uses its cut's normal."""
    unit, placed = sorted(unit), set(placed)
    between = _joints_between(model, unit, placed)
    if not between:
        return None, validate_step(model, unit, placed, [0, 0, 1])
    wanted = []
    for a, b in between:
        pins = model.pins.get(_pair(a, b), [])
        for pin in pins[:1]:
            axis = _unit_vector(pin["axis"])
            side = 1.0 if np.dot(model.centre(a) - np.asarray(pin["p"], float), axis) >= 0 else -1.0
            wanted.append(-side * axis)
    if not wanted:
        a, b = between[0]
        cut = model.cut_by_id.get((model.joints[_pair(a, b)].get("cuts") or [None])[0])
        if cut is None:
            return None, validate_step(model, unit, placed, [0, 0, 1])
        n = _unit_vector(cut["normal"])
        side = 1.0 if np.dot(model.centre(a), n) - float(cut["offset"]) >= 0 else -1.0
        wanted.append(-side * n)
    direction = wanted[0]
    failure = validate_step(model, unit, placed, direction)
    return (None, failure) if failure else (tuple(round(float(x), 6) + 0.0 for x in direction), None)


# ── The cut tree ───────────────────────────────────────────────────────────────

class _Node:
    def __init__(self, model, parts, cut=None, kids=()):
        self.parts, self.cut, self.kids = frozenset(parts), cut, list(kids)
        self.volume = sum(model.volume[p] for p in self.parts)
        self.first = min(self.parts)

    def heavy_key(self) -> tuple:
        return (-round(self.volume, 1), -len(self.parts), self.first)


def _anomaly(parts, message, fixes, model, numbers=None) -> Failure:
    point = model.joint_point(*parts) if len(parts) == 2 else None
    return Failure(ANOMALY, tuple(sorted(parts)), point, message, numbers or {}, tuple(fixes))


def _check_data(model: SplitModel) -> Failure | None:
    """Data problems that stop the plan: a pin whose parts are not a joint pair, or whose axis is not along its cut."""
    for pair, pins in sorted(model.pins.items()):
        if pair not in model.joints:
            return _anomaly(pair, f"A rod is recorded between part {pair[0]} and part {pair[1]}, but the split lists no joint between them.",
                            ("Split the model again so the joints and rods are worked out together.",), model)
        cuts = model.joints[pair].get("cuts") or []
        cut = model.cut_by_id.get(cuts[0]) if len(cuts) == 1 else None
        if cut is None:
            continue
        n = _unit_vector(cut["normal"])
        for pin in pins:
            if abs(float(np.dot(_unit_vector(pin["axis"]), n))) < math.cos(math.radians(ROD_TOLERANCE_DEG)):
                return _anomaly(pair, f"The rods between part {pair[0]} and part {pair[1]} do not run along the cut that made the joint, "
                                      f"so the order can't be worked out safely.",
                                ("Split the model again.", "If it happens again, the cuts may be tilted too far for rods to follow."), model)
    return None


def _tree_problem(model: SplitModel) -> str | None:
    if len(model.numbers) > 1 and not model.joints:
        return None   # nothing touches: every part is its own Section
    if not model.cuts:
        return "This split has no cut history to work out the order from."
    for pair, joint in sorted(model.joints.items()):
        cuts = joint.get("cuts") or []
        if len(cuts) != 1:
            return f"The joint of part {pair[0]} and part {pair[1]} was made by {len(cuts)} cuts, so the order can't be read from the cut tree."
        if cuts[0] not in model.cut_by_id:
            return f"The joint of part {pair[0]} and part {pair[1]} refers to cut {cuts[0]}, which is not in the cut list."
    if len([r for r in _rebuild_tree(model) if len(r.parts) > 1]) > 1:
        return "This split was made from several input bodies, each cut on its own, so there is no single cut tree to read the order from."
    return None


def _rebuild_tree(model: SplitModel) -> list:
    """Replay the cuts backwards, merging the groups that each cut's joints connect. Returns the roots."""
    units = [_Node(model, [n]) for n in model.numbers]
    for cut in reversed(model.cuts):
        edges = [pair for pair, j in model.joints.items() if j["cuts"][0] == cut["id"]]
        where = {p: i for i, u in enumerate(units) for p in u.parts}
        parent = list(range(len(units)))

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x
        for a, b in edges:
            ra, rb = find(where[a]), find(where[b])
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        groups = {}
        for i, u in enumerate(units):
            groups.setdefault(find(i), []).append(u)
        merged = []
        for _, members in sorted(groups.items()):
            if len(members) == 1:
                merged.append(members[0])
            else:
                members.sort(key=lambda m: m.first)
                merged.append(_Node(model, set().union(*[m.parts for m in members]), cut, members))
        units = merged
    return sorted(units, key=lambda u: u.first)


# ── Building the plan ──────────────────────────────────────────────────────────

class _Draft:
    def __init__(self, main=False, disconnected=False):
        self.main, self.disconnected = main, disconnected
        self.parts, self.start, self.steps = set(), None, []
        self.letter = None


class _Planner:
    def __init__(self, model: SplitModel):
        self.model = model
        self.finished = []   # drafts in the order they were completed

    def _side(self, unit: _Node, cut: dict) -> float:
        n = _unit_vector(cut["normal"])
        lo, hi = math.inf, -math.inf
        for p in unit.parts:
            c, half = self.model.centre(p), (self.model.box[p][1] - self.model.box[p][0]) / 2
            reach = float(np.dot(np.abs(n), half))
            lo, hi = min(lo, float(np.dot(c, n)) - reach), max(hi, float(np.dot(c, n)) + reach)
        return 1.0 if (lo + hi) / 2 >= float(cut["offset"]) else -1.0

    def _direction(self, node: _Node, child: _Node) -> np.ndarray:
        return -self._side(child, node.cut) * _unit_vector(node.cut["normal"])

    def _connected(self, units: list) -> bool:
        if len(units) <= 1:
            return True
        parts = {i: u.parts for i, u in enumerate(units)}
        seen, todo = {0}, [0]
        while todo:
            i = todo.pop()
            touching = {q for p in parts[i] for q in self.model.partners.get(p, ())}
            for j, other in parts.items():
                if j not in seen and touching & other:
                    seen.add(j)
                    todo.append(j)
        return len(seen) == len(units)

    def _order(self, node: _Node) -> tuple:
        """(base, children in the order they are added). The base is the heaviest child; the rest are taken off lightest
        first among those that can slide away without hitting a same-side sibling, and added back in reverse."""
        model, cut = self.model, node.cut
        kids = node.kids
        base = min(kids, key=_Node.heavy_key)
        side = {id(k): self._side(k, cut) for k in kids}
        remaining, removed, trouble = [k for k in kids if k is not base], [], None
        while remaining:
            candidates, trouble = [], None
            for g in sorted(remaining, key=lambda k: k.first):
                others = [k for k in kids if k is not g and k not in removed]
                if not self._connected(others):
                    continue
                w = side[id(g)] * _unit_vector(cut["normal"])
                blocker = next(((pg, ph) for h in others if side[id(h)] == side[id(g)]
                                for pg in sorted(g.parts) for ph in sorted(h.parts) if model.sweep_hits(pg, w, ph)), None)
                if blocker:
                    trouble = trouble or blocker
                else:
                    candidates.append(g)
            if not candidates:
                a, b = trouble if trouble else (min(remaining, key=lambda k: k.first).first, base.first)
                raise _Stop(Failure(
                    INTERLOCK, _pair(a, b), tuple(round(float(x), 2) for x in (model.centre(a) + model.centre(b)) / 2),
                    f"Part {min(a, b)} and part {max(a, b)} are on the same side of the cut at {cut['offset']:g} mm and are in each other's way: "
                    f"however the parts around them are ordered, none of them can be slid into place last.",
                    {"cut": cut["id"], "offset_mm": cut["offset"]},
                    (f"Move the cut at {cut['offset']:g} mm so the two parts no longer overlap along it, then split again.",
                     f"Add a cut that separates part {min(a, b)} from part {max(a, b)}.",
                     f"Make the joint of part {max(a, b)} to the rest glue only, so it can slide in another way.")))

            def lightest(k):
                area = sum(j.get("area_mm2", 0) for pair, j in model.joints.items()
                           if (pair[0] in k.parts) != (pair[1] in k.parts) and not (set(pair) <= k.parts))
                return (round(k.volume, 1), -area, k.first)
            g = min(candidates, key=lightest)
            removed.append(g)
            remaining.remove(g)
        return base, list(reversed(removed))

    def _make_step(self, draft: _Draft, parts, receiver, u, kind, joined: _Draft | None) -> None:
        model = self.model
        pins = [p for a in parts for b in receiver for p in model.pins.get(_pair(a, b), [])]
        connectors = {}
        for pin in pins:
            connectors[pin["connector"]] = connectors.get(pin["connector"], 0) + 1
        draft.steps.append(dict(kind=kind, parts=tuple(sorted(parts)), receiver=tuple(sorted(receiver)),
                                direction=tuple(round(float(x), 6) + 0.0 for x in u), rods=len(pins),
                                connectors=dict(sorted(connectors.items())), glue_only=not pins, joined=joined))

    def _check(self, parts, placed, u) -> None:
        failure = validate_step(self.model, parts, placed, u)
        if failure:
            raise _Stop(failure)

    def emit(self, node: _Node, draft: _Draft) -> None:
        draft.parts |= node.parts
        if not node.kids:
            draft.start = min(node.parts)
            return
        base, order = self._order(node)
        self.emit(base, draft)
        receiver = set(base.parts)
        group, start, group_u = [], None, None

        def flush():
            nonlocal group, receiver
            if group:
                self._make_step(draft, group, start, group_u, "part", None)
                receiver |= set(group)
                group = []
        for child in order:
            u = self._direction(node, child)
            if child.kids:
                flush()
                sub = _Draft()
                self.emit(child, sub)
                self.finished.append(sub)
                self._check(child.parts, receiver, u)
                self._make_step(draft, child.parts, receiver, u, "section", sub)
                draft.parts |= child.parts
                receiver |= child.parts
                continue
            x = child.first
            if group and len(group) < MAX_PARTS_PER_STEP and np.allclose(u, group_u) \
                    and not any(g in self.model.partners.get(x, ()) for g in group) \
                    and validate_step(self.model, [x], receiver | set(group), u) is None:
                group.append(x)
                continue
            flush()
            self._check([x], receiver, u)
            group, start, group_u = [x], frozenset(receiver), u
        flush()

    def run(self, roots: list) -> list:
        main_node = min(roots, key=_Node.heavy_key)
        main = _Draft(main=True)
        self.emit(main_node, main)
        for root in sorted((r for r in roots if r is not main_node), key=lambda r: r.first):
            side = _Draft(disconnected=True)
            self.emit(root, side)
            self.finished.append(side)
        self.finished.append(main)
        return self.finished


def _letters():
    yield "A"
    n = 0
    while True:
        n += 1
        label, k = "", n
        while k:
            k, r = divmod(k - 1, 25)
            label = chr(ord("B") + r) + label
        yield label


def _finish(drafts: list) -> tuple:
    """Letters (Main = A, the rest in finishing order), step ids per Section, and the fingerprint."""
    side = [d for d in drafts if not d.main]
    letters = _letters()
    next(letters)
    for d in side:
        d.letter = next(letters)
    main = next(d for d in drafts if d.main)
    main.letter = "A"
    sections = []
    for d in side + [main]:
        steps = []
        for i, s in enumerate(d.steps, start=1):
            steps.append(Step(id=f"{d.letter}{i}", kind=s["kind"], parts=s["parts"], receiver=s["receiver"], direction=s["direction"],
                              rods=s["rods"], connectors=s["connectors"], glue_only=s["glue_only"],
                              section=s["joined"].letter if s["joined"] else None))
        sections.append(Section(letter=d.letter, main=d.main, parts=tuple(sorted(d.parts)), start_part=d.start, steps=tuple(steps),
                                disconnected=d.disconnected,
                                note="No rods: this part does not touch the others at a cut." if d.disconnected and len(d.parts) == 1 else ""))
    content = [[s.letter, s.main, list(s.parts), [[st.kind, list(st.parts), list(st.receiver), list(st.direction), st.rods,
                                                    sorted(st.connectors.items())] for st in s.steps]] for s in sections]
    return tuple(sections), hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def build_plan(model: SplitModel, fallback=None) -> PlanResult:
    """The plan for a split, or the typed report of why it cannot be assembled as cut. When the cut tree can't be read
    (no cut history, several input bodies, a joint with several cuts) `fallback(model, reason)` is asked for a PlanResult
    instead: `fallback_plan` by default, or nothing at all with `fallback=False` (the tree planner alone)."""
    if fallback is None:
        fallback = fallback_plan
    if not model.numbers:
        return PlanResult(failure=Failure(ANOMALY, (), None, "The split has no parts.", {}, ("Split the model first.",)))
    failure = _check_data(model)
    if failure:
        return PlanResult(failure=failure)
    problem = _tree_problem(model)
    if problem:
        if fallback:
            return fallback(model, problem)
        return PlanResult(failure=Failure(ANOMALY, (), None, problem, {}, ("Split the model again so the cut history is saved.",)))
    try:
        sections, fingerprint = _finish(_Planner(model).run(_rebuild_tree(model)))
    except _Stop as stop:
        return PlanResult(failure=stop.failure)
    return PlanResult(plan=Plan(sections, fingerprint))


# ── The fallback planner: no usable cut tree ───────────────────────────────────

FALLBACK_TRIES = 3         # candidate groups tried per state before giving up on it
FALLBACK_BUDGET = 400      # states explored before the search stops and the split is reported as stuck


def _compatible(model: SplitModel, a: int, b: int, u: np.ndarray) -> bool:
    """Can part `a` meet part `b` moving along `u`: every rod parallel to u, and `a` moving towards the joint?"""
    cos_limit = math.cos(math.radians(ROD_TOLERANCE_DEG))
    for pin in model.pins.get(_pair(a, b), []):
        axis = _unit_vector(pin["axis"])
        if abs(float(np.dot(axis, u))) < cos_limit:
            return False
        side = 1.0 if np.dot(model.centre(a) - np.asarray(pin["p"], float), axis) >= 0 else -1.0
        if np.dot(-side * axis, u) < 0:
            return False
    return True


def _fallback_anomaly(model: SplitModel) -> Failure | None:
    """A joint made by several cuts has to have its rods along one of them."""
    cos_limit = math.cos(math.radians(ROD_TOLERANCE_DEG))
    for pair, joint in sorted(model.joints.items()):
        cuts = [model.cut_by_id[c] for c in joint.get("cuts") or [] if c in model.cut_by_id]
        if len(cuts) < 2:
            continue
        normals = [_unit_vector(c["normal"]) for c in cuts]
        for pin in model.pins.get(pair, []):
            if all(abs(float(np.dot(_unit_vector(pin["axis"]), n))) < cos_limit for n in normals):
                return _anomaly(pair, f"The joint of part {pair[0]} and part {pair[1]} was made by {len(cuts)} cuts, and its rods run along none of them, "
                                      f"so the order can't be worked out safely.",
                                ("Split the model again.", "If it happens again, the cuts may be tilted too far for rods to follow."), model,
                                {"cuts": [c["id"] for c in cuts]})
    return None


class _Fallback(_Planner):
    """Assembly by disassembly over the signed cut-normal directions (plus any rod axis no cut explains). Parts come
    off along one direction at a time. A group leaves together when something in it can't go without the rest (a rod
    on another axis, or a part in its way); each such group is built apart as a Section and joined whole."""

    def __init__(self, model: SplitModel):
        super().__init__(model)
        self.directions = self._directions()
        self._hits, self._solved, self.tries, self.dead_end = {}, {}, 0, None

    def _directions(self) -> list:
        axes = [_unit_vector(c["normal"]) for c in self.model.cuts] + [_unit_vector(p["axis"]) for pins in self.model.pins.values() for p in pins]
        cos_limit = math.cos(math.radians(ROD_TOLERANCE_DEG))
        keep = []
        for axis in axes:
            if not any(abs(float(np.dot(axis, k))) >= cos_limit for k in keep):
                keep.append(axis)
        if not keep:
            keep = [np.eye(3)[i] for i in range(3)]
        out = [s * k for k in keep for s in (1.0, -1.0)]
        return sorted(out, key=lambda w: tuple(round(float(x), 6) + 0.0 for x in w))

    def _hit(self, p: int, w: np.ndarray, q: int) -> bool:
        key = (p, q, tuple(np.round(w, 4) + 0.0))
        if key not in self._hits:
            self._hits[key] = self.model.sweep_hits(p, w, q)
        return self._hits[key]

    def _connected_parts(self, parts) -> bool:
        parts = set(parts)
        if len(parts) <= 1:
            return True
        seen, todo = set(), [min(parts)]
        while todo:
            p = todo.pop()
            if p not in seen:
                seen.add(p)
                todo.extend(q for q in self.model.partners.get(p, ()) if q in parts and q not in seen)
        return seen == parts

    def _closure(self, seeds, w: np.ndarray, state: frozenset, sweeps: bool = True) -> frozenset:
        """The parts that must leave with `seeds` along `w`: those a member is rodded to on a way it can't be pulled out
        of and, with `sweeps`, those in a member's way. The cheap rod pass can be run alone to rank groups."""
        u = -w
        group, todo = set(seeds), list(seeds)
        while todo:
            p = todo.pop()
            for q in sorted(state - group):
                if (q in self.model.partners.get(p, ()) and not _compatible(self.model, p, q, u)) or (sweeps and self._hit(p, w, q)):
                    group.add(q)
                    todo.append(q)
        return frozenset(group)

    def _candidates(self, state: frozenset) -> list:
        """The (group, assembly direction) pairs that can leave `state`, smallest and lightest group first. Groups are
        ranked by their rods alone first, and the sweeps (the costly part) only run on the front of that ranking."""
        ranked = {}
        for w in self.directions:
            for seed in sorted(state):
                group = self._closure([seed], w, state, sweeps=False)
                if len(group) < len(state):
                    ranked.setdefault((group, tuple(-w)), (group, -w, w))
        order = sorted(ranked.values(), key=lambda g: (len(g[0]), min(g[0]), tuple(g[1])))
        found, best = {}, []
        for rod_group, u, w in order:
            if len(best) >= FALLBACK_TRIES and len(rod_group) > best[FALLBACK_TRIES - 1]:
                break
            group = self._closure(rod_group, w, state)
            rest = state - group
            if not rest or (group, tuple(u)) in found or not self._connected_parts(rest) or not self._connected_parts(group):
                continue
            if validate_step(self.model, group, rest, u) is None:
                found[(group, tuple(u))] = (group, u)
                best = sorted(len(g) for g, _ in found.values())

        def key(item):
            group, u = item
            return (len(group), round(sum(self.model.volume[p] for p in group), 1), round(float(u[2]), 3),
                    tuple(round(float(x), 3) + 0.0 for x in u), min(group))
        return sorted(found.values(), key=key)

    def solve(self, state: frozenset):
        """The removals (group, assembly direction) that take `state` apart down to one part, in removal order, or None.
        A group of several parts must itself come apart, and what is left must; a few candidates are tried per state."""
        if len(state) <= 1:
            return []
        if state in self._solved:
            return self._solved[state]
        self.tries += 1
        result = None
        if self.tries <= FALLBACK_BUDGET:
            candidates = self._candidates(state)
            if not candidates and (self.dead_end is None or len(state) < len(self.dead_end)):
                self.dead_end = state
            for group, u in candidates[:FALLBACK_TRIES]:
                inside, outside = self.solve(group), self.solve(state - group)
                if inside is not None and outside is not None:
                    result = [(group, u)] + outside
                    break
        self._solved[state] = result
        return result

    def stuck(self, state: frozenset) -> Failure:
        """No order exists: the cheapest set of joints (by rods lost) that keeps some part from leaving, naming the weakest."""
        model, best = self.model, None
        state = self.dead_end if self.dead_end is not None else state
        for w in self.directions:
            u = -w
            for p in sorted(state):
                joints, blockers = [], []
                for q in sorted(state - {p}):
                    if q in model.partners.get(p, ()) and not _compatible(model, p, q, u):
                        joints.append((p, q))
                    elif self._hit(p, w, q):
                        blockers.append(q)
                if not joints and not blockers:
                    continue
                cost = sum(len(model.pins.get(_pair(*j), [])) + 0.001 * model.joints[_pair(*j)].get("area_mm2", 0) for j in joints) + 0.5 * len(blockers)
                key = (round(cost, 3), p, tuple(np.round(u, 3) + 0.0))
                if best is None or key < best[0]:
                    best = (key, p, u, joints, blockers)
        if best is None:
            a, b = sorted(state)[:2]
            return Failure(DISCONNECTED, (a, b), model.joint_point(a, b), f"No order works for this split: nothing about part {a} and part {b} lets either come off.",
                           {"conflict_set": []}, ("Split the model again.",))
        _, p, u, joints, blockers = best
        conflict = [list(_pair(*j)) for j in joints] + [list(_pair(p, q)) for q in blockers]
        if joints:
            a, b = _blame(model, joints)
            facts = _joint_facts(model, a, b)
            return Failure(ROD_DIRECTION, _pair(a, b), model.joint_point(a, b),
                           f"No order works for this split: part {p} could only go along {axis_words(u)} if the rods of the joint of part {min(a, b)} "
                           f"and part {max(a, b)} were not there, and every other way round is stuck in the same way.",
                           {"conflict_set": conflict, "rods": facts["rods"], "area_mm2": facts["area_mm2"], "direction": [round(float(x), 3) for x in u]},
                           (f"Make the joint of part {min(a, b)} and part {max(a, b)} glue only: that drops {facts['rods']} rods and {facts['area_mm2']} mm2 of keyed bond.",
                            "Change the cuts so these joints meet along one axis, then split again.",
                            "Add a cut that separates the parts that lock each other."))
        q = blockers[0]
        return Failure(INTERLOCK, _pair(p, q), tuple(round(float(x), 2) for x in (model.centre(p) + model.centre(q)) / 2),
                       f"No order works for this split: part {min(p, q)} and part {max(p, q)} lock each other in place.",
                       {"conflict_set": conflict, "direction": [round(float(x), 3) for x in u]},
                       (f"Move a cut so part {min(p, q)} and part {max(p, q)} no longer overlap, then split again.",
                        f"Add a cut that separates part {min(p, q)} from part {max(p, q)}.",
                        f"Make the joint of part {max(p, q)} to the rest glue only, so it can slide in another way."))

    def build(self, parts: frozenset, draft: _Draft) -> None:
        """Fill `draft` with the base part and the steps that add everything else, in assembly order."""
        removals = self.solve(parts)
        base = min(parts - frozenset().union(*[g for g, _ in removals]))
        draft.parts |= {base}
        draft.start = base
        receiver = {base}
        group, start, group_u = [], None, None

        def flush():
            nonlocal group, receiver
            if group:
                self._make_step(draft, group, start, group_u, "part", None)
                receiver |= set(group)
                draft.parts |= set(group)
                group = []
        for part_group, u in reversed(removals):
            if len(part_group) > 1:
                flush()
                sub = _Draft()
                self.build(part_group, sub)
                self.finished.append(sub)
                self._check(part_group, receiver, u)
                self._make_step(draft, part_group, receiver, u, "section", sub)
                draft.parts |= part_group
                receiver |= part_group
                continue
            x = min(part_group)
            if group and len(group) < MAX_PARTS_PER_STEP and np.allclose(u, group_u) \
                    and not any(g in self.model.partners.get(x, ()) for g in group) \
                    and validate_step(self.model, [x], receiver | set(group), u) is None:
                group.append(x)
                continue
            flush()
            self._check([x], receiver, u)
            group, start, group_u = [x], frozenset(receiver), u
        flush()

    def components(self) -> list:
        left, out = set(self.model.numbers), []
        while left:
            comp, todo = set(), [min(left)]
            while todo:
                p = todo.pop()
                if p not in comp:
                    comp.add(p)
                    todo.extend(self.model.partners.get(p, ()))
            out.append(frozenset(comp))
            left -= comp
        return out

    def run(self) -> list:
        """The drafts in finishing order: nested Sections, then separate bodies, then Main (the heaviest body)."""
        nodes = [_Node(self.model, c) for c in self.components()]
        main_node = min(nodes, key=_Node.heavy_key)
        side, main = [], None
        for node in sorted(nodes, key=lambda n: (n is main_node, n.first)):
            if self.solve(node.parts) is None:
                raise _Stop(self.stuck(node.parts))
            draft = _Draft(main=node is main_node, disconnected=node is not main_node)
            self.build(node.parts, draft)
            if draft.main:
                main = draft
            else:
                side.append(draft)
        return self.finished + side + [main]


def fallback_plan(model: SplitModel, reason: str = "") -> PlanResult:
    """The plan for a split whose cut tree can't be read: assembly by disassembly over the cut-normal directions.
    Data anomalies stop it with a typed report; when no order exists the split is flagged "cannot be assembled as cut"."""
    failure = _fallback_anomaly(model)
    if failure:
        return PlanResult(failure=failure)
    try:
        sections, fingerprint = _finish(_Fallback(model).run())
    except _Stop as stop:
        return PlanResult(failure=stop.failure)
    return PlanResult(plan=Plan(sections, fingerprint, reason))
