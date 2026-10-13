"""
Model splitting engine: cuts models that are too big for the printer into parts
with alignment holes for steel rods or filament pins.

Pure geometry (Manifold, trimesh, numpy) with no Spooler imports. Spooler runs
each stage as `python split_engine.py <stage> <work dir>` in a subprocess, so a
large model never blocks the server or keeps its memory.

Work directory
  job.json        settings written by Spooler: source, scale, up, printer, planner, connectors
  model.npz       the solid in millimetres, Z up, bounding box corner at the origin
  work.npz        a lighter copy used for planning and checking cuts
  preview.bin     decimated mesh for the browser (see write_mesh_bin)
  analysis.json   size, repairs, volume …
  cuts.json       {"mode", "cuts": [...]} — written by `plan`, edited by the user
  check.json      the pieces the current cuts make and whether each fits
  parts/          part_NN.stl, oriented for printing
  parts_preview.bin, result.json
  progress.json   {"stage", "percent", "message"} while a stage runs
  error.json      {"stage", "error"} when a stage fails

Cuts
  {"id", "parent": [cut id, side] | null, "normal": [x, y, z], "offset", "scope": [6 floats] | null}
  A cut splits the pieces that lie on `side` (+1 / -1) of its parent cut — every
  piece when parent is null — and overlap `scope`, along the plane normal·x = offset.
  Grid cuts are global; smart cuts form a tree, each scoped to the piece it split.

Lessons from validating this approach (see README "Splitting large models"):
  * cutting happens before slicing — OrcaSlicer's CLI cannot cut;
  * parts must stay within ~240 mm on the CC1 because of the purge-zone corner;
  * nudging cut planes avoids slicing through thin features;
  * holes are placed only after every cut is known, then each hole is checked to
    be shared by exactly two parts;
  * shells need per-joint pin sizes: 6 mm dowels do not fit an 8 mm wall.
"""

import json
import math
import os
import struct
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

import manifold3d as m3d
import numpy as np
import trimesh
from scipy.ndimage import binary_erosion, distance_transform_edt

M = m3d.Manifold
OK = m3d.Error.NoError

PREVIEW_TRIANGLES = 250_000
WORK_TRIANGLES    = 400_000
SIMPLIFY_ABOVE    = 400_000      # full models above this are simplified at 0.01 mm (sculpt detail)
MIN_BODY_MM3      = 1.0
MIN_SLAB_MM       = 30.0         # never leave a slab thinner than this along a cut normal
NUDGE_FRACTIONS   = np.linspace(0.2, 0.8, 13)
TOP_CANDIDATES    = 3
MAX_CUTS          = 200
OVERHANG_SIN      = math.sin(math.radians(45))
UP_AXES = {
    "z": np.eye(3),
    "y": np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float),
    "x": np.array([[0, 0, -1], [0, 1, 0], [1, 0, 0]], float),
}
DEFAULT_PRINTER    = {"bed_x": 240.0, "bed_y": 240.0, "height": 250.0}
DEFAULT_CONNECTORS = {"kind": "rod", "diameter": 2.0, "fallback_diameter": 1.0, "clearance": 0.15, "depth": 6.5,
                      "spacing": 100.0, "max_per_joint": 8}
MESH_MAGIC = b"SPV1"


class EngineError(Exception):
    """A problem with the model or the settings, shown to the user as is."""


# ── Small geometry helpers ─────────────────────────────────────────────────────

def unit(v) -> np.ndarray:
    v = np.asarray(v, float)
    length = float(np.linalg.norm(v))
    if v.shape != (3,) or not math.isfinite(length) or length == 0:
        raise EngineError("A cut has an invalid direction")
    return v / length


def to_z(v) -> np.ndarray:
    """Rotation matrix taking direction v onto +Z."""
    v = unit(v)
    axis = np.cross(v, [0.0, 0.0, 1.0])
    s, c = float(np.linalg.norm(axis)), float(v[2])
    if s < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + k + k @ k * ((1 - c) / s ** 2)


def rotate(man, R):
    return man.transform(np.hstack([np.asarray(R, float), np.zeros((3, 1))]))


def boxes_overlap(a, b, margin: float = 0.0) -> bool:
    return bool(np.all(a[:3] <= b[3:] + margin) and np.all(b[:3] <= a[3:] + margin))


def bbox_projection(bb, n) -> tuple:
    corners = np.array([[x, y, z] for x in (bb[0], bb[3]) for y in (bb[1], bb[4]) for z in (bb[2], bb[5])])
    proj = corners @ n
    return float(proj.min()), float(proj.max())


def _self_check() -> None:
    R = to_z([0.3, 0.5, -0.8])
    bb = rotate(M.cube([0.01] * 3, True).translate([12.0, -7.0, 31.0]), R).bounding_box()
    assert np.allclose(R @ [12.0, -7.0, 31.0], [(bb[i] + bb[i + 3]) / 2 for i in range(3)], atol=1e-3), "transform convention"
    positive, negative = M.cube([1.0, 1.0, 1.0]).split_by_plane([1.0, 0.0, 0.0], 0.25)
    assert positive.bounding_box()[0] > 0.2 and negative.bounding_box()[3] < 0.3, "split_by_plane order"


_self_check()


def manifold_from_arrays(verts, tris):
    mesh = m3d.Mesh(vert_properties=np.ascontiguousarray(verts, np.float32),
                    tri_verts=np.ascontiguousarray(tris, np.uint32))
    mesh.merge()
    return M(mesh)


def arrays(man) -> tuple:
    mesh = man.to_mesh()
    return np.asarray(mesh.vert_properties, np.float32)[:, :3], np.asarray(mesh.tri_verts, np.uint32)


def hull_points(man) -> np.ndarray:
    return np.asarray(man.hull().to_mesh().vert_properties, float)[:, :3]


def obb_extents(points):
    try:
        return trimesh.bounds.oriented_bounds(points)[1]
    except Exception:
        return None


def simplified(man, target: int, start: float = 0.0) -> tuple:
    """Simplify until at most `target` triangles; returns (manifold, tolerance used in mm).
    Every attempt starts from `man`, so the error never stacks past the tolerance reported, and the gentle
    step lands close to the target instead of far under it."""
    tolerance, current = start, man
    while current.num_tri() > target and tolerance < 8.0:
        tolerance = 0.02 if tolerance == 0 else round(tolerance * 1.4, 4)
        current = man.simplify(tolerance)
    return current, tolerance


def printer_limits(job: dict) -> dict:
    limits = {**DEFAULT_PRINTER, **(job.get("printer") or {})}
    for key in DEFAULT_PRINTER:
        value = float(limits[key])
        if not 50 <= value <= 2000:
            raise EngineError(f"Printer {key.replace('_', ' ')} must be between 50 and 2000 mm")
        limits[key] = value
    return limits


def parts_needed(points, limits: dict) -> int:
    """How many parts a piece needs at least, judged by its axis-aligned and oriented boxes."""
    if len(points) < 4:
        return 1
    lim = sorted([limits["bed_x"], limits["bed_y"], limits["height"]])
    best = None
    for ext in (np.ptp(points, axis=0), obb_extents(points)):
        if ext is None:
            continue
        e = sorted(float(x) for x in ext)
        count = math.prod(math.ceil(e[i] / lim[i] - 1e-9) for i in range(3))
        best = count if best is None else min(best, count)
    return max(1, best or 1)


def connector_specs(job: dict) -> tuple:
    c = {**DEFAULT_CONNECTORS, **(job.get("connectors") or {})}
    kind = c.get("kind") or "rod"
    if kind not in ("rod", "pin", "none"):
        raise EngineError("Connector kind must be rod, pin or none")
    spacing = float(c.get("spacing") or DEFAULT_CONNECTORS["spacing"])
    per_joint = int(c.get("max_per_joint") or DEFAULT_CONNECTORS["max_per_joint"])
    if kind == "none":
        return [], spacing, per_joint
    clearance, depth = float(c["clearance"]), float(c["depth"])
    if not 0 <= clearance <= 0.6:
        raise EngineError("Hole clearance must be between 0 and 0.6 mm")
    if not 2 <= depth <= 30:
        raise EngineError("Hole depth must be between 2 and 30 mm")
    specs = []
    for index, diameter in enumerate((c.get("diameter"), c.get("fallback_diameter"))):
        if not diameter:
            continue
        diameter = float(diameter)
        if not 0.8 <= diameter <= 12:
            raise EngineError("Rod and pin diameters must be between 0.8 and 12 mm")
        specs.append({
            "label":     f"{diameter:g} mm {'rod' if kind == 'rod' else 'pin'}",
            "diameter":  diameter,
            "clearance": clearance,
            "wall":      max(1.2, 0.8 * diameter),
            "depth":     depth if index == 0 else min(depth, max(4.0, 3.0 * diameter)),
            "segments":  32 if diameter >= 2 else 20,
        })
    return specs, spacing, per_joint


def keepout(spec: dict) -> float:
    return spec["diameter"] / 2 + spec["clearance"] + spec["wall"]


# ── Files ──────────────────────────────────────────────────────────────────────

def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _replace(tmp: Path, path: Path, attempts: int = 200) -> None:
    """os.replace, retried: Windows refuses to replace a file that Spooler is reading or serving at that moment."""
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def write_json(path: Path, data) -> None:
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    _replace(tmp, path)


def save_model(path: Path, man) -> None:
    verts, tris = arrays(man)
    with open(path, "wb") as fh:
        np.savez(fh, verts=verts, tris=tris)


def load_model(path: Path):
    if not Path(path).is_file():
        raise EngineError("The model hasn't been analysed yet")
    with np.load(path) as data:
        man = manifold_from_arrays(data["verts"], data["tris"])
    if man.status() != OK:
        raise EngineError(f"The stored model is damaged ({man.status().name}) — upload it again")
    return man


def write_mesh_bin(path: Path, meshes: list) -> None:
    """'SPV1', uint32 mesh count, then per mesh: uint32 vertices, uint32 triangles, float32 xyz…, uint32 indices…
    Every block is a multiple of 4 bytes, so the browser can view it with typed arrays directly."""
    tmp = Path(f"{path}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(MESH_MAGIC + struct.pack("<I", len(meshes)))
        for verts, tris in meshes:
            fh.write(struct.pack("<II", len(verts), len(tris)))
            fh.write(np.ascontiguousarray(verts, "<f4").tobytes())
            fh.write(np.ascontiguousarray(tris, "<u4").tobytes())
    _replace(tmp, path)


class Stage:
    def __init__(self, work: Path, name: str):
        self.work, self.name, self.started = work, name, time.monotonic()

    def progress(self, percent: float, message: str) -> None:
        tmp, path = self.work / "progress.json.tmp", self.work / "progress.json"
        try:  # best effort: a progress update that collides with Spooler reading the file is simply skipped
            tmp.write_text(json.dumps({"stage": self.name, "percent": round(max(0.0, min(100.0, percent)), 1),
                                       "message": message, "elapsed_s": round(time.monotonic() - self.started, 1)}),
                           encoding="utf-8")
            _replace(tmp, path, attempts=5)
        except OSError:
            pass


# ── Repair ─────────────────────────────────────────────────────────────────────

def fan_fill(tm):
    """Close open boundary loops with a triangle fan. Returns (vertices, faces, loop sizes), or None when loops touch."""
    faces = np.asarray(tm.faces)
    directed = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]).tolist()
    have = set(map(tuple, directed))
    following = defaultdict(list)
    for a, b in directed:
        if (b, a) not in have:
            following[b].append(a)  # walk each boundary backwards so the fill gets the opposite winding
    if not following or any(len(v) > 1 for v in following.values()):
        return None
    verts, out, loops = list(np.asarray(tm.vertices)), list(faces), []
    while any(following.values()):
        start = next(v for v, nxt in following.items() if nxt)
        loop, v = [start], following[start].pop()
        while v != start:
            loop.append(v)
            if not following[v]:
                return None
            v = following[v].pop()
        centre = len(verts)
        verts.append(np.asarray(tm.vertices)[loop].mean(axis=0))
        out += [[centre, loop[i], loop[(i + 1) % len(loop)]] for i in range(len(loop))]
        loops.append(len(loop))
    return np.array(verts), np.array(out), loops


def build_solid(tm) -> tuple:
    """Turn a triangle mesh into a valid solid: merge, repair, close holes, union overlapping shells."""
    steps = []
    model = manifold_from_arrays(tm.vertices, tm.faces)
    if model.status() != OK:
        tm = tm.copy()
        tm.merge_vertices()
        try:
            trimesh.repair.fix_winding(tm)
            trimesh.repair.fill_holes(tm)
            model = manifold_from_arrays(tm.vertices, tm.faces)
            if model.status() == OK:
                steps.append("Fixed flipped faces and small holes")
        except ImportError:
            pass
    if model.status() != OK:
        filled = fan_fill(tm)
        if filled is not None:
            model = manifold_from_arrays(filled[0], filled[1])
            if model.status() == OK:
                steps.append(f"Closed {len(filled[2])} open boundar{'y' if len(filled[2]) == 1 else 'ies'}")
    if model.status() != OK:
        raise EngineError(f"The model isn't a closed solid and couldn't be repaired automatically ({model.status().name}). "
                          "Repair it in your modelling program and upload it again.")
    bodies = model.decompose()
    if len(bodies) > 1:
        union = M.batch_boolean(bodies, m3d.OpType.Add)
        if union.volume() < model.volume() * (1 - 1e-6):
            steps.append(f"Merged overlapping shells ({len(bodies)} bodies)")
        model = union
    if model.volume() <= 0:
        raise EngineError("The model has no volume")
    return model, steps, len(bodies)


# ── Applying cuts ──────────────────────────────────────────────────────────────

def validate_cuts(cuts) -> list:
    if not isinstance(cuts, list):
        raise EngineError("Cuts must be a list")
    if len(cuts) > MAX_CUTS:
        raise EngineError(f"At most {MAX_CUTS} cuts are supported")
    seen, out = set(), []
    for raw in cuts:
        try:
            cut_id = int(raw["id"])
            normal = unit(raw["normal"])
            offset = float(raw["offset"])
            parent = raw.get("parent")
            scope = raw.get("scope")
            if parent is not None:
                parent = [int(parent[0]), int(parent[1])]
                if parent[0] not in seen or parent[1] not in (1, -1):
                    raise EngineError(f"Cut {cut_id} refers to a cut that comes after it")
            if scope is not None:
                scope = [float(x) for x in scope]
                if len(scope) != 6:
                    raise ValueError
        except (KeyError, TypeError, ValueError, IndexError):
            raise EngineError("A cut is missing its id, direction or position")
        if cut_id in seen or not math.isfinite(offset):
            raise EngineError(f"Cut {cut_id} is invalid")
        seen.add(cut_id)
        out.append({"id": cut_id, "parent": parent, "normal": [round(float(x), 6) for x in normal],
                    "offset": round(offset, 3), "scope": scope})
    return out


def whole(model) -> list:
    return [{"man": model, "member": frozenset(), "last": None, "normals": []}]


def apply_one_cut(pieces: list, cut: dict) -> tuple:
    n, d = unit(cut["normal"]), float(cut["offset"])
    parent = tuple(cut["parent"]) if cut.get("parent") else None
    scope = np.asarray(cut["scope"], float) if cut.get("scope") else None
    out, events = [], []
    for piece in pieces:
        bb = np.asarray(piece["man"].bounding_box(), float)
        if (parent is not None and parent not in piece["member"]) or (scope is not None and not boxes_overlap(bb, scope)):
            out.append(piece)
            continue
        lo, hi = bbox_projection(bb, n)
        produced, sides = [], set()
        if lo + 1e-6 < d < hi - 1e-6:
            for side, half in zip((1, -1), piece["man"].split_by_plane(n.tolist(), d)):
                for body in half.decompose():
                    if body.volume() >= MIN_BODY_MM3:
                        sides.add(side)
                        produced.append({"man": body, "member": piece["member"] | {(cut["id"], side)},
                                         "last": (cut["id"], side), "normals": piece["normals"] + [n.tolist()]})
        if len(sides) < 2:  # the plane misses the solid: keep the piece, remember which side it is on
            side = 1 if (lo + hi) / 2 >= d else -1
            out.append({**piece, "member": piece["member"] | {(cut["id"], side)}})
            continue
        events.append({"cut": cut["id"], "piece": piece["man"], "bb": bb, "n": n, "d": d})
        out += produced
    return out, events


def apply_cuts(model, cuts: list, stage: Stage | None = None, start: float = 0, span: float = 0) -> tuple:
    pieces, events = whole(model), []
    for index, cut in enumerate(cuts):
        if stage:
            stage.progress(start + span * index / max(len(cuts), 1), f"Cutting ({index + 1} of {len(cuts)})")
        pieces, new = apply_one_cut(pieces, cut)
        events += new
    return pieces, events


def describe_pieces(pieces: list, limits: dict) -> list:
    out = []
    for piece in pieces:
        pts = hull_points(piece["man"])
        ext = obb_extents(pts)
        out.append({
            "bbox":       [round(float(x), 2) for x in piece["man"].bounding_box()],
            "volume_cm3": round(piece["man"].volume() / 1000, 2),
            "size":       sorted(round(float(x), 1) for x in (ext if ext is not None else np.ptp(pts, axis=0))),
            "fits":       parts_needed(pts, limits) == 1,
        })
    return out


# ── Planning ───────────────────────────────────────────────────────────────────

def plan_grid(model, limits: dict, balanced: bool) -> list:
    """PrintSplitWeb-style grid: world-axis cuts every bed length, or evenly spaced when balanced."""
    bb = model.bounding_box()
    per_axis = [limits["bed_x"], limits["bed_y"], limits["height"]]
    cuts = []
    for axis in range(3):
        lo, hi = bb[axis], bb[axis + 3]
        size, limit = hi - lo, per_axis[axis]
        count = math.ceil(size / limit - 1e-9)
        for i in range(1, count):
            offset = lo + size * i / count if balanced else lo + limit * i
            normal = [0.0, 0.0, 0.0]
            normal[axis] = 1.0
            cuts.append({"id": len(cuts) + 1, "parent": None, "normal": normal, "offset": round(offset, 3), "scope": None})
    return cuts


def _cut_candidates(piece, limits: dict) -> tuple:
    pts = hull_points(piece)
    axes = list(np.eye(3))
    try:
        axes += list(trimesh.bounds.oriented_bounds(pts)[0][:3, :3])
    except Exception:
        pass
    dirs = []
    for n in axes:
        n = unit(n)
        if all(abs(n @ m) < 0.985 for m in dirs):
            dirs.append(n)
    reach = min(limits["bed_x"], limits["bed_y"], limits["height"])
    out = []
    for n in dirs:
        proj = pts @ n
        lo, hi = float(proj.min()), float(proj.max())
        if hi - lo <= reach:
            continue
        offsets = {lo + f * (hi - lo) for f in NUDGE_FRACTIONS} | {lo + reach - 8, hi - reach + 8}
        out += [(n, float(d)) for d in sorted(offsets) if d - lo >= MIN_SLAB_MM and hi - d >= MIN_SLAB_MM]
    return pts, out


def _cheap_score(pts, rotated, R, n, d, limits: dict, room: float) -> dict:
    section = rotated.slice(d)
    islands = section.decompose()
    no_room = sum(isl.offset(-room, m3d.JoinType.Round).is_empty() for isl in islands)
    polys = [np.asarray(p) for p in section.to_polygons()]
    ring = np.concatenate(polys) if polys else np.zeros((0, 2))
    if len(ring) > 3000:
        ring = ring[:: len(ring) // 3000 + 1]
    world = np.column_stack([ring, np.full(len(ring), d)]) @ R
    proj = pts @ n
    estimate = (parts_needed(np.vstack([pts[proj <= d], world]), limits)
                + parts_needed(np.vstack([pts[proj >= d], world]), limits))
    balance = round(max(d - float(proj.min()), float(proj.max()) - d) / 20)  # prefer even slabs over a sliver plus a tall piece
    return {"n": n, "d": d, "estimate": estimate, "no_room": no_room, "islands": len(islands), "balance": balance}


def _exact_score(piece, candidate: dict, limits: dict) -> tuple:
    bodies = [b for half in piece.split_by_plane(candidate["n"].tolist(), candidate["d"])
              for b in half.decompose() if b.volume() >= MIN_BODY_MM3]
    hulls = [hull_points(b) for b in bodies]
    need = sum(parts_needed(h, limits) for h in hulls)
    tiny = sum(b.volume() < 2000.0 for b in bodies)
    widest = max((float(np.ptp(h, axis=0).max()) for h in hulls), default=0.0)
    return need, tiny, candidate["no_room"], candidate["islands"], round(widest / 20)


def choose_cut(piece, limits: dict, room: float):
    pts, candidates = _cut_candidates(piece, limits)
    rotations, scored = {}, []
    for n, d in candidates:
        key = tuple(np.round(n, 6))
        if key not in rotations:
            R = to_z(n)
            rotations[key] = (R, rotate(piece, R))
        R, rotated = rotations[key]
        scored.append(_cheap_score(pts, rotated, R, n, d, limits, room))
    if not scored:
        return None
    scored.sort(key=lambda s: (s["estimate"], s["no_room"], s["islands"]))
    return min(scored[:TOP_CANDIDATES], key=lambda s: _exact_score(piece, s, limits))


def plan_smart(model, limits: dict, room: float, base_cuts: list, stage: Stage) -> tuple:
    """Recursive cuts: repeatedly split the biggest piece that doesn't fit, trying world and oriented-box axes."""
    cuts = list(base_cuts)
    pieces, _ = apply_cuts(model, cuts)
    next_id = max((c["id"] for c in cuts), default=0) + 1
    while len(cuts) < MAX_CUTS:
        oversize = [p for p in pieces if parts_needed(hull_points(p["man"]), limits) > 1]
        if not oversize:
            return cuts, pieces
        target = max(oversize, key=lambda p: p["man"].volume())
        stage.progress(5 + 90 * len(cuts) / (len(cuts) + 2 * len(oversize)),
                       f"Choosing cut {len(cuts) + 1} — {len(oversize)} piece{'s' if len(oversize) != 1 else ''} still too big")
        best = choose_cut(target["man"], limits, room)
        if best is None:
            raise EngineError("A piece couldn't be cut small enough — check the printer size settings")
        bb = np.asarray(target["man"].bounding_box(), float)
        cut = {"id": next_id, "parent": list(target["last"]) if target["last"] else None,
               "normal": [round(float(x), 6) for x in best["n"]], "offset": round(best["d"], 3),
               "scope": [round(float(x), 2) for x in np.concatenate([bb[:3] - 1.0, bb[3:] + 1.0])]}
        next_id += 1
        cuts.append(cut)
        pieces, _ = apply_one_cut(pieces, cut)
    raise EngineError(f"The model needs more than {MAX_CUTS} cuts")


# ── Alignment holes ────────────────────────────────────────────────────────────

def inside_mask(polys: list, pts: np.ndarray) -> np.ndarray:
    x, y = pts[:, 0], pts[:, 1]
    hit = np.zeros(len(pts), bool)
    for poly in polys:
        ax, ay = poly[:, 0], poly[:, 1]
        bx, by = np.roll(ax, 1), np.roll(ay, 1)
        for x1, y1, x2, y2 in zip(ax, ay, bx, by):
            if y1 == y2:
                continue
            hit ^= ((y1 > y) != (y2 > y)) & (x < (x2 - x1) * (y - y1) / (y2 - y1) + x1)
    return hit


def clear_of_other_cuts(world: np.ndarray, n, depth: float, room: float, others: list) -> np.ndarray:
    """True for hole centres whose hole (±depth along n) stays `room` away from every other cut."""
    ok = np.ones(len(world), bool)
    for m, e, bb in others:
        inside = np.all((world >= bb[:3] - room - depth) & (world <= bb[3:] + room + depth), axis=1)
        s1 = (world - n * depth) @ m - e
        s2 = (world + n * depth) @ m - e
        near = (s1 * s2 <= 0) | (np.minimum(np.abs(s1), np.abs(s2)) < room)
        ok &= ~(inside & near)
    return ok


MIN_RADIUS_GAP = 3.0   # a pattern with one hole this much nearer or farther from the centre than the rest fits only one way round
EDGE_MARGIN    = 3.0   # holes keep this much extra wall beyond the minimum when the island has room


def pick_spread(cand: np.ndarray, want: int, gap: float) -> list:
    """Indices into `cand`: the point farthest from the middle, then repeatedly the point farthest from those chosen."""
    picked = [int(np.argmax(np.linalg.norm(cand - cand.mean(axis=0), axis=1)))]
    nearest = np.linalg.norm(cand - cand[picked[0]], axis=1)
    while len(picked) < want:
        k = int(np.argmax(nearest))
        if nearest[k] < gap:
            break
        picked.append(k)
        nearest = np.minimum(nearest, np.linalg.norm(cand - cand[k], axis=1))
    return picked


def is_keyed(cand: np.ndarray, picked: list, centre: np.ndarray) -> bool:
    """True when the holes cannot match themselves after turning the part in the plane of the joint.

    Only turns matter: a part cannot be flipped over its own joint face. If one hole's distance from the middle
    differs from every other hole's, no turn maps the pattern onto itself."""
    if len(picked) < 2:
        return False
    radii = np.linalg.norm(cand[picked] - centre, axis=1)
    return any(all(abs(r - o) >= MIN_RADIUS_GAP for k, o in enumerate(radii) if k != i) for i, r in enumerate(radii))


def key_pattern(cand: np.ndarray, picked: list, centre: np.ndarray, gap: float, allowed: np.ndarray | None = None) -> list:
    """Move one hole so the pattern fits only one way round, if the island has a spot that allows it."""
    if len(picked) < 2 or is_keyed(cand, picked, centre):
        return picked
    own = np.linalg.norm(cand - centre, axis=1)
    for i in reversed(range(len(picked))):
        rest = [p for k, p in enumerate(picked) if k != i]
        clear = np.min(np.linalg.norm(cand[:, None, :] - cand[rest][None, :, :], axis=2), axis=1)
        ok = (clear >= gap) & (np.ones(len(cand), bool) if allowed is None else allowed) & np.all(np.abs(own[:, None] - own[rest][None, :]) >= MIN_RADIUS_GAP, axis=1)
        if ok.any():
            best = int(np.flatnonzero(ok)[int(np.argmax(clear[ok]))])
            return rest[:i] + [best] + rest[i:]
    return picked


def pick_connectors(cand: np.ndarray, clearance: np.ndarray, want: int, gap: float) -> tuple:
    """Choose hole positions: off thin edges where the island allows it, spread out, and keyed."""
    picked = pick_spread(cand, want, gap)
    allowed = clearance >= min(EDGE_MARGIN, 0.5 * float(clearance.max()))
    inner = np.flatnonzero(allowed)
    if 0 < len(inner) < len(cand):
        safer = [int(inner[k]) for k in pick_spread(cand[inner], want, gap)]
        if len(safer) >= len(picked):
            picked = safer
        else:
            allowed = None
    else:
        allowed = None
    picked = key_pattern(cand, picked, cand.mean(axis=0), gap, allowed)
    return picked, is_keyed(cand, picked, cand.mean(axis=0))


def event_cutter(normal: np.ndarray, pins: list):
    """The cylinders that cut one joint's holes into both faces, as one solid in model space."""
    if not pins:
        return None
    d = float(np.asarray(pins[0]["p"]) @ normal)
    cylinders = [M.cylinder(2 * pin["depth"], pin["hole_diameter"] / 2, pin["hole_diameter"] / 2, pin["segments"])
                 .translate([pin["uv"][0], pin["uv"][1], d - pin["depth"]]) for pin in pins]
    return rotate(M.batch_boolean(cylinders, m3d.OpType.Add), to_z(normal).T)


def place_pins(event: dict, others: list, specs: list, spacing: float, per_joint: int) -> tuple:
    n, d = event["n"], event["d"]
    R = to_z(n)
    rotated = rotate(event["piece"], R)
    pins, islands, guards = [], [], {}
    for island in rotated.slice(d).decompose():
        polys = [np.asarray(p) for p in island.to_polygons()]
        perimeter = sum(float(np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1).sum()) for p in polys)
        edge = island.offset(-0.5, m3d.JoinType.Round)
        probe_uv = np.asarray(edge.to_polygons()[0])[0] if not edge.is_empty() else polys[0][0]
        entry = {"cut": event["cut"], "area_mm2": round(island.area()), "length_mm": round(perimeter / 2),
                 "connector": None, "pins": 0, "keyed": False, "probe": (np.array([probe_uv[0], probe_uv[1], d]) @ R).tolist()}
        islands.append(entry)
        want = int(min(per_joint, max(2, round(perimeter / 2 / spacing))))
        best = None  # the first connector size that gives two holes, otherwise the one that gives most
        for index, spec in enumerate(specs):
            inset = island.offset(-keepout(spec), m3d.JoinType.Round)
            if inset.is_empty():
                continue
            x0, y0, x1, y1 = inset.bounds()
            step = max(1.0, min(4.0, math.sqrt(max((x1 - x0) * (y1 - y0), 1.0) / 20000)))
            gx, gy = np.meshgrid(np.arange(x0, x1 + 1e-6, step), np.arange(y0, y1 + 1e-6, step))
            pts = np.column_stack([gx.ravel(), gy.ravel()])
            inside = inside_mask([np.asarray(p) for p in inset.to_polygons()], pts)
            # distance from each point to the edge of the usable area, so holes can stay off thin edges
            clearance = (distance_transform_edt(np.pad(inside.reshape(gx.shape), 1)) * step)[1:-1, 1:-1].ravel()[inside]
            pts = pts[inside]
            depth = spec["depth"]
            for dz in (-depth - 0.5, -depth / 2, depth / 2, depth + 0.5):  # the hole must stay inside the wall
                if not len(pts):
                    break
                key = (index, dz)
                if key not in guards:
                    wall = spec["diameter"] / 2 + spec["clearance"] + spec["wall"] / 2
                    guards[key] = [np.asarray(p) for p in rotated.slice(d + dz).offset(-wall, m3d.JoinType.Round).to_polygons()]
                keep = inside_mask(guards[key], pts) if guards[key] else np.zeros(len(pts), bool)
                pts, clearance = pts[keep], clearance[keep]
            if len(pts):
                world = np.column_stack([pts, np.full(len(pts), d)]) @ R
                keep = clear_of_other_cuts(world, n, depth, keepout(spec), others)
                pts, clearance = pts[keep], clearance[keep]
            if not len(pts):
                continue
            picked, keyed = pick_connectors(pts, clearance, want, 2 * keepout(spec))
            if best is None or len(picked) > len(best[2]):
                best = (spec, pts, picked, keyed)
            if len(picked) >= min(2, want):
                break
        if best is None:
            continue
        chosen, cand, picked, keyed = best
        entry.update(connector=chosen["label"], pins=len(picked), keyed=keyed)
        radius = chosen["diameter"] / 2 + chosen["clearance"]
        for k in picked:
            u, v = (float(x) for x in cand[k])
            pins.append({"p": [round(float(x), 3) for x in np.array([u, v, d]) @ R], "axis": [round(float(x), 6) for x in n],
                         "connector": chosen["label"], "diameter": chosen["diameter"], "hole_diameter": round(2 * radius, 3),
                         "depth": chosen["depth"], "island": len(islands) - 1, "uv": [u, v], "segments": chosen["segments"]})
    return pins, islands


def containing_part(parts: list, point, exclude: set = frozenset()):
    point = np.asarray(point, float)
    cube = M.cube([0.2, 0.2, 0.2], True).translate(point.tolist())
    for index, part in enumerate(parts):
        if index in exclude or not boxes_overlap(part["bb"], np.concatenate([point, point]), margin=0.2):
            continue
        if (part["man"] ^ cube).volume() > 1e-4:
            return index
    return None


# ── Part numbers ───────────────────────────────────────────────────────────────

LABEL_HEIGHTS   = (7.0, 6.5, 6.0, 5.5)   # text height in mm, tried largest first
LABEL_STROKE    = 1.0                    # stroke and underline width (at least two 0.4 mm perimeters)
LABEL_DEPTH     = 0.6                    # three layers of 0.2 mm
LABEL_WALL      = 0.8                    # solid kept between the pocket and any hole or edge
LABEL_GAP       = 0.8                    # between the digits and the underline
LABEL_CELL      = 0.6                    # digit width as a fraction of the text height
LABEL_SPACING   = 0.25                   # space between digits as a fraction of the text height
LABEL_TRIES     = 25                     # positions tried on one face before moving on
# segments of a seven-segment digit: a top, b top right, c bottom right, d bottom, e bottom left, f top left, g middle
DIGIT_SEGMENTS = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd",
                  "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg"}


def label_shape(text: str, height: float):
    """The label as a 2D shape centred on its middle, reading along +x with +y up: bold digits and an underline.
    Returns (shape, width, box height)."""
    sw, cell = LABEL_STROKE, LABEL_CELL * height
    pitch = cell + LABEL_SPACING * height
    width = len(text) * cell + (len(text) - 1) * LABEL_SPACING * height
    box = height + LABEL_GAP + sw
    w, h = cell - sw, height - sw     # the stroke centre lines
    ends = {"a": ((0, h), (w, h)), "b": ((w, h), (w, h / 2)), "c": ((w, h / 2), (w, 0)), "d": ((0, 0), (w, 0)),
            "e": ((0, h / 2), (0, 0)), "f": ((0, h), (0, h / 2)), "g": ((0, h / 2), (w, h / 2))}
    dot = m3d.CrossSection.circle(sw / 2, 16)
    pieces = [m3d.CrossSection.square([width, sw])]
    for i, char in enumerate(text):
        ox, oy = i * pitch + sw / 2, sw + LABEL_GAP + sw / 2
        for seg in DIGIT_SEGMENTS[char]:
            (x0, y0), (x1, y1) = ends[seg]
            pieces.append(m3d.CrossSection.batch_hull([dot.translate([ox + x0, oy + y0]), dot.translate([ox + x1, oy + y1])]))
    shape = m3d.CrossSection.batch_boolean(pieces, m3d.OpType.Add)
    return shape.translate([-width / 2, -box / 2]), width, box


def label_faces(part, number: int, R, shift, islands: list, events: list) -> list:
    """Every face of an oriented part (print frame, bed at z = 0) a number could go on, best first.
    Each is a flat piece of the part's surface: a cut face, which is hidden once glued, or the bed face."""
    normal_of = {e["cut"]: e["n"] for e in events}
    groups = {}  # (cut, outward normal) → [(probe in print frame, neighbour)]
    for island in islands:
        if len(island.get("parts", [])) != 2 or number not in island["parts"] or island["cut"] not in normal_of:
            continue
        n = normal_of[island["cut"]]
        first = number == island["parts"][0]   # the first part is on the +normal side of the cut
        n_o = unit(R @ (-n if first else n))
        probe = R @ np.asarray(island["probe"], float) - shift
        groups.setdefault((island["cut"], tuple(np.round(n_o, 4))), []).append((probe, island["parts"][1 if first else 0]))
    bed = np.array([0.0, 0.0, -1.0])
    wanted = [("bed", None, bed, [p for (c, n), v in groups.items() if float(np.dot(n, bed)) > 0.9999 for p in v])]
    wanted += [("cut", cut, np.array(n), v) for (cut, n), v in sorted(groups.items()) if float(np.dot(n, bed)) <= 0.9999]
    faces = []
    for kind, cut, n_o, probes in wanted:
        Rf = to_z(n_o)
        rot = rotate(part, Rf)
        top = float(rot.bounding_box()[5])  # the part lies on one side of its plane, so the face is the highest thing in this frame
        for index, comp in enumerate(rot.slice(top - 0.05).decompose()):
            if comp.area() < 1.0:
                continue
            polys = [np.asarray(p) for p in comp.to_polygons()]
            neighbours = [nb for probe, nb in probes if inside_mask(polys, (Rf @ probe)[None, :2])[0]]
            if kind == "cut" and not neighbours:
                continue
            clear = comp.offset(-LABEL_WALL, m3d.JoinType.Round)
            if clear.is_empty():
                continue
            tier = 2 if kind == "bed" else (1 if n_o[2] > -OVERHANG_SIN else 3)
            faces.append({"kind": kind, "cut": cut, "neighbour": min(neighbours) if neighbours else None, "tier": tier, "n": n_o,
                          "Rf": Rf, "rot": rot, "top": top, "clear": clear, "room": round(clear.area()), "index": index})
    return sorted(faces, key=lambda f: (f["tier"], -f["room"], f["neighbour"] or 10 ** 9, f["cut"] or 0, f["index"]))


def reading_angle(face: dict) -> float:
    """Angle (radians, in the face's frame seen from outside) of the label's reading direction:
    along the longest clear direction of the face, upright where the face is upright."""
    pts = np.vstack([np.asarray(p) for p in face["clear"].to_polygons()])
    axis = np.array([1.0, 0.0])
    try:
        transform, extents = trimesh.bounds.oriented_bounds_2D(pts)
        axis = np.asarray(transform)[:2, :2][int(np.argmax(extents))]
    except Exception:
        pass
    theta = math.atan2(float(axis[1]), float(axis[0]))
    u = face["Rf"].T @ [math.cos(theta), math.sin(theta), 0.0]
    v = face["Rf"].T @ [-math.sin(theta), math.cos(theta), 0.0]
    for value in (v[2], u[2], u[0], u[1]):
        if abs(value) > 1e-6:
            return theta if value > 0 else theta + math.pi
    return theta


def label_positions(face: dict, theta: float, width: float, box: float) -> list:
    """Centres, in the face's own frame, where a label turned by theta fits inside the clear area, middle of the face first."""
    c, s = math.cos(theta), math.sin(theta)
    to_label = np.array([[c, -s], [s, c]])          # face frame → label frame (rows are points)
    polys = [np.asarray(p) @ to_label for p in face["clear"].to_polygons()]
    lo, hi = np.min([p.min(axis=0) for p in polys], axis=0), np.max([p.max(axis=0) for p in polys], axis=0)
    step = max(0.25, math.sqrt(float(np.prod(hi - lo)) / 40000))
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + 1e-9, step), np.arange(lo[1], hi[1] + 1e-9, step))
    mask = inside_mask(polys, np.column_stack([gx.ravel(), gy.ravel()])).reshape(gx.shape)
    nx, ny = math.ceil(width / 2 / step), math.ceil(box / 2 / step)
    fit = binary_erosion(mask, np.ones((2 * ny + 1, 2 * nx + 1), bool), border_value=0)
    if not fit.any():
        return []
    centre = np.array([gx[mask].mean(), gy[mask].mean()])
    spots = np.column_stack([gx[fit], gy[fit]])
    order = np.lexsort((spots[:, 1], spots[:, 0], np.round(np.linalg.norm(spots - centre, axis=1), 3)))
    return [tuple(float(x) for x in spots[i] @ to_label.T) for i in order]


def solid_around(face: dict, shape) -> bool:
    """True when the label placed as `shape` (face frame) opens onto the face and keeps LABEL_WALL of solid
    around and under it: no hole, no hole sleeve, no edge, checked on the real part."""
    rot, top = face["rot"], face["top"]
    below = M.extrude(shape.offset(LABEL_WALL, m3d.JoinType.Round), LABEL_DEPTH + LABEL_WALL - 0.02
                      ).translate([0, 0, top - LABEL_DEPTH - LABEL_WALL])
    above = M.extrude(shape, 0.48).translate([0, 0, top + 0.02])
    return bool((rot ^ below).volume() >= below.volume() - 0.02 and (rot ^ above).volume() <= 0.02)


def label_part(part, number: int, R, shift, islands: list, events: list) -> tuple:
    """Deboss `number` into an oriented part. Returns (part, label record), or (part, None) when nothing fits."""
    faces = label_faces(part, number, R, shift, islands, events)
    angles = {}
    for height in LABEL_HEIGHTS:
        shape, width, box = label_shape(str(number), height)
        for face in faces:
            theta = angles.setdefault(id(face), reading_angle(face))
            tried = []
            for spot in label_positions(face, theta, width, box):
                if len(tried) >= LABEL_TRIES:
                    break
                if any(math.hypot(spot[0] - t[0], spot[1] - t[1]) < 1.5 for t in tried):
                    continue
                tried.append(spot)
                placed = shape.rotate(math.degrees(theta)).translate(list(spot))
                if not solid_around(face, placed):
                    continue
                pocket = rotate(M.extrude(placed, LABEL_DEPTH + 0.1).translate([0, 0, face["top"] - LABEL_DEPTH]), face["Rf"].T)
                cut = part - pocket
                if cut.status() != OK or part.volume() - cut.volume() <= 0:
                    continue
                up = face["Rf"].T @ [-math.sin(theta), math.cos(theta), 0.0]
                where = face["Rf"].T @ [spot[0], spot[1], face["top"]]
                label = {"text": str(number), "face": face["kind"], "cut": face["cut"], "neighbour": face["neighbour"],
                         "tier": face["tier"], "height_mm": height, "depth_mm": LABEL_DEPTH,
                         "position": [round(float(x), 3) for x in R.T @ (where + shift)],      # centre of the label, model frame
                         "position_print": [round(float(x), 3) for x in where],                 # same point in the printed part's frame
                         "normal": [round(float(x), 6) for x in R.T @ face["n"]],               # out of the part, model frame
                         "up": [round(float(x), 6) for x in R.T @ up]}                          # which way is up when reading, model frame
                return cut, label
    return part, None


# ── Orientation ────────────────────────────────────────────────────────────────

def choose_orientation(man, cut_normals: list, limits: dict):
    """Pick a print orientation that fits, keeps overhangs low and stands on a big flat face."""
    verts, tris = arrays(man)
    tri = verts[tris].astype(float)
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    doubled = np.linalg.norm(cross, axis=1)
    keep = doubled > 1e-12
    normals, areas, centres = cross[keep] / doubled[keep, None], doubled[keep] / 2, tri[keep].mean(axis=1)
    surface = float(areas.sum())
    hull = hull_points(man)
    dirs = [np.asarray(c, float) for c in cut_normals] + list(np.eye(3))
    try:
        dirs += list(trimesh.bounds.oriented_bounds(hull)[0][:3, :3])
    except Exception:
        pass
    bed = sorted([limits["bed_x"], limits["bed_y"]])
    options, tried = [], []
    for v in dirs:
        v = unit(v)
        for sign in (1.0, -1.0):
            up = sign * v
            if any(float(up @ t) > 0.999 for t in tried):
                continue
            tried.append(up)
            R = to_z(up)
            hp = hull @ R.T
            height = float(np.ptp(hp[:, 2]))
            if height > limits["height"]:
                continue
            T2, ext2 = trimesh.bounds.oriented_bounds_2D(hp[:, :2])
            footprint = sorted(float(x) for x in ext2)
            if footprint[0] > bed[0] or footprint[1] > bed[1]:
                continue
            nz = normals @ R[2]
            z = centres @ R[2] - float(hp[:, 2].min())
            contact = float(areas[(nz < -0.985) & (z < 0.3)].sum())
            overhang = float(areas[(nz < -OVERHANG_SIN) & (z >= 0.3)].sum())
            rot2 = np.asarray(T2)[:2, :2]
            if np.linalg.det(rot2) > 0:
                R = np.block([[rot2, np.zeros((2, 1))], [np.zeros((1, 2)), np.ones((1, 1))]]) @ R
            options.append({"R": R, "contact": contact, "overhang": overhang, "height": height, "footprint": footprint})
    if not options:
        return None, surface
    best_contact = max(o["contact"] for o in options)
    stable = [o for o in options if o["contact"] >= max(300.0, 0.4 * best_contact)] or options
    return min(stable, key=lambda o: (round(o["overhang"] / 500), -o["contact"])), surface


# ── Stages ─────────────────────────────────────────────────────────────────────

def analyze(work: Path) -> None:
    job = read_json(work / "job.json")
    stage = Stage(work, "analyze")
    scale, up = float(job.get("scale") or 1.0), job.get("up") or "z"
    if up not in UP_AXES:
        raise EngineError("Up axis must be x, y or z")
    if not 1e-4 <= scale <= 1e5:
        raise EngineError("Scale must be a positive number")
    stage.progress(2, "Reading the model")
    try:
        loaded = trimesh.load(work / job["source"], force="mesh", process=False)
    except Exception as e:
        raise EngineError(f"The model can't be read: {e}")
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise EngineError("The file contains no triangles")
    raw_size = [float(x) for x in np.ptp(loaded.vertices, axis=0)]
    verts = (np.asarray(loaded.vertices, float) * scale) @ UP_AXES[up].T
    tm = trimesh.Trimesh(vertices=verts - verts.min(axis=0), faces=np.asarray(loaded.faces), process=False)
    stage.progress(15, "Checking the mesh is a closed solid")
    model, repairs, bodies = build_solid(tm)
    source_triangles = len(tm.faces)
    if model.num_tri() > SIMPLIFY_ABOVE:
        stage.progress(40, f"Simplifying {model.num_tri():,} triangles (0.01 mm tolerance)")
        model = model.simplify(0.01)
    bb = model.bounding_box()
    model = model.translate([-bb[0], -bb[1], -bb[2]])
    save_model(work / "model.npz", model)
    stage.progress(60, "Preparing a lighter copy for planning")
    work_model, work_tolerance = simplified(model, WORK_TRIANGLES)
    save_model(work / "work.npz", work_model)
    stage.progress(80, "Preparing the 3D preview")
    preview, preview_tolerance = ((work_model, work_tolerance) if work_model.num_tri() <= PREVIEW_TRIANGLES
                                  else simplified(model, PREVIEW_TRIANGLES, work_tolerance))
    write_mesh_bin(work / "preview.bin", [arrays(preview)])
    limits = printer_limits(job)
    bb = model.bounding_box()
    hull = hull_points(model)
    analysis = {
        "raw_size":           [round(x, 4) for x in raw_size],
        "size":               [round(bb[i + 3] - bb[i], 1) for i in range(3)],
        "scale":              scale,
        "up":                 up,
        "source_triangles":   source_triangles,
        "triangles":          model.num_tri(),
        "work_triangles":     work_model.num_tri(),
        "work_tolerance_mm":  work_tolerance,
        "preview_triangles":  preview.num_tri(),
        "preview_tolerance_mm": preview_tolerance,
        "volume_cm3":         round(model.volume() / 1000, 1),
        "surface_cm2":        round(model.surface_area() / 100),
        "mean_wall_mm":       round(2 * model.volume() / model.surface_area(), 2),
        "genus":              model.genus(),
        "bodies":             bodies,
        "repairs":            repairs,
        "watertight_source":  not repairs,
        "fits_whole":         parts_needed(hull, limits) == 1,
        "estimated_parts":    parts_needed(hull, limits),
    }
    write_json(work / "analysis.json", analysis)
    stage.progress(100, "Analysed")


def plan(work: Path) -> None:
    job = read_json(work / "job.json")
    stage = Stage(work, "plan")
    limits = printer_limits(job)
    specs, _, _ = connector_specs(job)
    planner = job.get("planner") or {}
    mode = planner.get("mode") or "smart"
    stage.progress(2, "Loading the model")
    model = load_model(work / "work.npz")
    if mode == "grid":
        cuts = plan_grid(model, limits, bool(planner.get("balanced", True)))
        stage.progress(50, "Checking the grid")
        pieces, _ = apply_cuts(model, cuts)
    elif mode == "smart":
        base = validate_cuts(read_json(work / "cuts.json")["cuts"]) if planner.get("keep_cuts") and (work / "cuts.json").is_file() else []
        room = keepout(specs[0]) if specs else 2.75
        cuts, pieces = plan_smart(model, limits, room, base, stage)
    else:
        raise EngineError("Planner must be smart or grid")
    write_json(work / "cuts.json", {"mode": mode, "cuts": cuts})
    write_json(work / "check.json", {"cuts": cuts, "pieces": describe_pieces(pieces, limits)})
    stage.progress(100, f"{len(pieces)} parts from {len(cuts)} cuts")


def check(work: Path) -> None:
    job = read_json(work / "job.json")
    stage = Stage(work, "check")
    limits = printer_limits(job)
    cuts = validate_cuts(read_json(work / "cuts.json")["cuts"])
    model = load_model(work / "work.npz")
    pieces, _ = apply_cuts(model, cuts, stage, 5, 80)
    write_json(work / "check.json", {"cuts": cuts, "pieces": describe_pieces(pieces, limits)})
    stage.progress(100, f"{len(pieces)} parts")


def split(work: Path) -> None:
    job = read_json(work / "job.json")
    stage = Stage(work, "split")
    limits = printer_limits(job)
    specs, spacing, per_joint = connector_specs(job)
    cuts = validate_cuts(read_json(work / "cuts.json")["cuts"])
    started = time.monotonic()
    stage.progress(2, "Loading the model")
    model = load_model(work / "model.npz")
    pieces, events = apply_cuts(model, cuts, stage, 3, 22)
    oversize = sum(parts_needed(hull_points(p["man"]), limits) > 1 for p in pieces)
    if oversize:
        raise EngineError(f"{oversize} part{'s are' if oversize != 1 else ' is'} still too big for the printer — use Auto-fix or move the cuts")

    # holes, placed once every cut is known so no cut can go through a hole
    pins, islands = [], []
    for j, event in enumerate(events):
        stage.progress(25 + 25 * j / max(len(events), 1), f"Placing alignment holes ({j + 1} of {len(events)} joints)")
        others = [(o["n"], o["d"], o["bb"]) for k, o in enumerate(events)
                  if k != j and not (abs(o["n"] @ event["n"]) > 0.999 and abs(o["d"] * np.sign(o["n"] @ event["n"]) - event["d"]) < 0.5)]
        new_pins, new_islands = place_pins(event, others, specs, spacing, per_joint)
        for pin in new_pins:
            pin["island"] += len(islands)
            pin["event"] = j
        pins += new_pins
        islands += new_islands
        event["piece"] = None

    def cut_holes() -> list:
        """The parts with every current hole cut in."""
        cutters = []
        for j, event in enumerate(events):
            cutter = event_cutter(event["n"], [pin for pin in pins if pin["event"] == j])
            if cutter is not None:
                cutters.append((np.asarray(cutter.bounding_box(), float), cutter))
        built = []
        for piece in pieces:
            man = piece["man"]
            bb = np.asarray(man.bounding_box(), float)
            near = [c for cbb, c in cutters if boxes_overlap(bb, cbb)]
            cutter = (near[0] if len(near) == 1 else M.batch_boolean(near, m3d.OpType.Add)) if near else None
            if cutter is not None:
                man = man - cutter
            # body and cutter are kept so the preview can be simplified before the holes go in (see parts_preview.bin)
            built.append({"man": man, "body": piece["man"], "cutter": cutter,
                          "bb": np.asarray(man.bounding_box(), float), "normals": piece["normals"]})
        built.sort(key=lambda p: (round((p["bb"][2] + p["bb"][5]) / 100), round((p["bb"][1] + p["bb"][4]) / 100), (p["bb"][0] + p["bb"][3]) / 2))
        return built

    def unshared_holes(built: list) -> list:
        """Indexes of holes that are not cut into exactly two parts, recording which parts each one touches."""
        bad = []
        for index, pin in enumerate(pins):
            n = np.asarray(pin["axis"])
            u, v = pin["uv"]
            d = float(np.asarray(pin["p"]) @ n)
            radius = pin["hole_diameter"] / 2
            sleeve = (M.cylinder(2 * pin["depth"], radius + 0.4, radius + 0.4, pin["segments"])
                      - M.cylinder(2 * pin["depth"] + 2, radius, radius, pin["segments"]).translate([0, 0, -1]))
            sleeve = rotate(sleeve.translate([u, v, d - pin["depth"]]), to_z(n).T)
            sbb = np.asarray(sleeve.bounding_box(), float)
            touching = [k for k, part in enumerate(built) if boxes_overlap(part["bb"], sbb) and (part["man"] ^ sleeve).volume() > 0.05]
            pin["parts"] = [k + 1 for k in touching]
            if len(touching) != 2:
                bad.append(index)
        return bad

    stage.progress(52, "Cutting the holes")
    parts = cut_holes()
    stage.progress(55, "Checking every hole is shared by two parts")
    bad_holes = unshared_holes(parts)
    holes_removed = 0
    for _ in range(3):  # a hole that is not shared by two parts is dropped, then the check runs again on what is left
        if not bad_holes:
            break
        drop = set(bad_holes)
        holes_removed += len(drop)
        for index in drop:
            island = islands[pins[index]["island"]]
            island["pins"] -= 1
            if not island["pins"]:
                island["connector"] = None
        pins = [pin for index, pin in enumerate(pins) if index not in drop]
        stage.progress(58, f"Removing {len(drop)} hole{'s' if len(drop) != 1 else ''} that don't line up")
        parts = cut_holes()
        bad_holes = unshared_holes(parts)
    # a removal can leave a joint with a single hole, which is no longer keyed
    for island in islands:
        if island["pins"] < 2:
            island["keyed"] = False

    stage.progress(62, "Working out which parts join")
    joints = {}
    for island in islands:
        probe, n = np.asarray(island["probe"]), None
        event_normal = next(e["n"] for e in events if e["cut"] == island["cut"])
        a = containing_part(parts, probe + 0.4 * event_normal)
        b = containing_part(parts, probe - 0.4 * event_normal, exclude={a} if a is not None else set())
        island["parts"] = [k + 1 for k in (a, b) if k is not None]
        if a is None or b is None:
            continue
        key = tuple(sorted((a + 1, b + 1)))
        joint = joints.setdefault(key, {"parts": list(key), "area_mm2": 0, "rods": 0, "connectors": defaultdict(int), "cuts": set(), "keyed": True})
        joint["area_mm2"] += island["area_mm2"]
        joint["rods"] += island["pins"]
        joint["keyed"] = joint["keyed"] and (island["keyed"] or not island["pins"])
        joint["cuts"].add(island["cut"])
        if island["connector"]:
            joint["connectors"][island["connector"]] += island["pins"]
    joint_list = [{**j, "connectors": dict(j["connectors"]), "cuts": sorted(j["cuts"])}
                  for j in sorted(joints.values(), key=lambda j: j["parts"])]

    out_dir = work / "parts"
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("part_*.stl"):
        old.unlink()
    labelling = (job.get("labels") or {}).get("enabled", True)
    result_parts, previews = [], []
    total_triangles = sum(part["body"].num_tri() for part in parts) or 1
    for number, part in enumerate(parts, start=1):
        stage.progress(65 + 30 * (number - 1) / len(parts), f"Orienting and saving part {number} of {len(parts)}")
        placed, surface = choose_orientation(part["man"], part["normals"], limits)
        if placed is None:
            raise EngineError(f"Part {number} has no orientation that fits the printer")
        oriented = rotate(part["man"], placed["R"])
        obb = oriented.bounding_box()
        oriented = oriented.translate([-obb[0], -obb[1], -obb[2]])
        label = None
        if labelling:  # after the orientation is chosen, before the part is saved; nothing here changes which parts touch
            try:
                oriented, label = label_part(oriented, number, np.asarray(placed["R"], float), np.array(obb[:3], float), islands, events)
            except Exception:  # a number is never worth failing the split for
                label = None
        verts, tris = arrays(oriented)
        name = f"part_{number:02d}.stl"
        trimesh.Trimesh(vertices=verts, faces=tris, process=False).export(out_dir / name)
        # simplify the body, then cut the exact holes into it: simplifying afterwards turns 32-sided holes into 8-sided ones
        budget = max(20_000, round(PREVIEW_TRIANGLES * part["body"].num_tri() / total_triangles))
        body = simplified(part["body"], budget)[0]
        previews.append(arrays(body if part["cutter"] is None else body - part["cutter"]))
        neighbours = [{"part": next(p for p in j["parts"] if p != number), "rods": j["rods"], "area_mm2": j["area_mm2"]}
                      for j in joint_list if number in j["parts"]]
        result_parts.append({
            "number":          number,
            "file":            name,
            "volume_cm3":      round(part["man"].volume() / 1000, 1),
            "bbox":            [round(float(x), 2) for x in part["bb"]],
            "print_size":      [round(placed["footprint"][0], 1), round(placed["footprint"][1], 1), round(placed["height"], 1)],
            "bed_contact_mm2": round(placed["contact"]),
            "overhang_mm2":    round(placed["overhang"]),
            "overhang_pct":    round(100 * placed["overhang"] / surface, 1) if surface else 0,
            "needs_supports":  placed["overhang"] > max(500.0, 0.02 * surface),
            "tall_on_small_base": placed["height"] > 150 and placed["contact"] < 1500,
            "rotation":        np.round(placed["R"], 6).tolist(),
            "holes":           sum(number in pin["parts"] for pin in pins),
            "neighbours":      neighbours,
            "label":           label,
        })

    assembly = []
    if result_parts:
        placed_parts = {1}
        assembly.append({"part": 1, "joins": [], "rods": 0})
        while len(placed_parts) < len(result_parts):
            options = [(j["area_mm2"], j) for j in joint_list if len(set(j["parts"]) & placed_parts) == 1]
            if not options:  # a separate cluster (e.g. an island that only touches through glue)
                nxt = min(set(range(1, len(result_parts) + 1)) - placed_parts)
                assembly.append({"part": nxt, "joins": [], "rods": 0})
                placed_parts.add(nxt)
                continue
            _, joint = max(options, key=lambda t: t[0])
            nxt = next(p for p in joint["parts"] if p not in placed_parts)
            links = [j for j in joint_list if nxt in j["parts"] and len(set(j["parts"]) & placed_parts) == 1]
            assembly.append({"part": nxt, "joins": sorted({p for j in links for p in j["parts"] if p != nxt}),
                             "rods": sum(j["rods"] for j in links)})
            placed_parts.add(nxt)

    stage.progress(96, "Saving the preview")
    write_mesh_bin(work / "parts_preview.bin", previews)
    rods = defaultdict(int)
    for pin in pins:
        rods[pin["connector"]] += 1
    result = {
        "parts":    result_parts,
        "joints":   joint_list,
        "cuts":     cuts,   # the cuts these parts were made with: the assembly plan replays them
        "assembly": assembly,
        "unlabelled": [p["number"] for p in result_parts if not p["label"]],
        "pins":     [{k: pin[k] for k in ("p", "axis", "connector", "diameter", "hole_diameter", "depth", "parts")} for pin in pins],
        "summary": {
            "parts":                  len(result_parts),
            "labels":                 labelling,
            "cuts":                   len(cuts),
            "rods":                   dict(rods),
            "rod_lengths_mm":         {spec["label"]: round(2 * spec["depth"] - 1, 1) for spec in specs},
            "joints":                 len(joint_list),
            "joints_without_rods":    sum(1 for j in joint_list if not j["rods"]),
            "holes_not_shared_by_two": len(bad_holes),
            "holes_removed":          holes_removed,
            "joints_single_rod":      sum(1 for j in joint_list if j["rods"] == 1),
            "joints_unkeyed":         sum(1 for j in joint_list if j["rods"] and not j["keyed"]),
            "parts_needing_supports": sum(p["needs_supports"] for p in result_parts),
            "volume_cm3":             round(sum(p["volume_cm3"] for p in result_parts), 1),
            "seconds":                round(time.monotonic() - started, 1),
        },
    }
    write_json(work / "result.json", result)
    stage.progress(100, f"Split into {len(result_parts)} parts")


STAGES = {"analyze": analyze, "plan": plan, "check": check, "split": split}


def main(argv: list) -> int:
    if len(argv) != 3 or argv[1] not in STAGES:
        print(f"usage: {Path(argv[0]).name} {{{'|'.join(STAGES)}}} <work dir>", file=sys.stderr)
        return 64
    work = Path(argv[2])
    (work / "error.json").unlink(missing_ok=True)
    try:
        STAGES[argv[1]](work)
    except EngineError as e:
        write_json(work / "error.json", {"stage": argv[1], "error": str(e)})
        return 2
    except MemoryError:
        write_json(work / "error.json", {"stage": argv[1], "error": "Ran out of memory — try a model with fewer triangles"})
        return 3
    except Exception as e:  # anything unexpected is recorded for the user and the logs
        write_json(work / "error.json", {"stage": argv[1], "error": f"Unexpected error: {type(e).__name__}: {e}",
                                         "traceback": traceback.format_exc()})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
