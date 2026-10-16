"""
Nesting objects onto build plates by outline.

This places each object's outline (a convex hull or the true silhouette from above) turned to many angles. Every object has a clearance `b`, the reach of its supports beyond its
outline (research/brim-support-measurements.md): the nester keeps two objects at least max(b_i, b_j) apart, an object
EDGE_MARGIN_MM from the bed edge and b from the purge area. Pinned objects are fixed outlines on a plate that others pack around. An object that fits nowhere on an empty plate gets a plate of its own, marked oversize.

Placement tries a few hundred bottom-left candidate positions per object and angle, then slides the best one left and
down until it touches. The best spot keeps the pile's longest side shortest (then its area), so parts gather in a squarish
cluster rather than a row across the bed, so concave objects can tuck into each other's gaps.
"""

import math

import numpy as np
import shapely
from shapely.geometry import MultiPoint, Polygon, box

from packing import GAP_MM, MARGIN_MM, SUPPORT_GAP_MM
from problem_kinds import OFF_THE_BED, OVER_THE_PURGE_AREA, OVERLAPS, TOO_CLOSE

CLEARANCE_MM = GAP_MM        # a plain object's clearance; the pair rule is max, so two plain objects keep today's 6 mm
SUPPORT_CLEARANCE_MM = SUPPORT_GAP_MM   # an object that needs tree supports in its orientation
EDGE_MARGIN_MM = MARGIN_MM   # brim and supports are clipped at the bed edge, so no more than the packer's margin
ANGLES = tuple(range(0, 360, 15))
FINE_ANGLES = tuple(range(0, 360, 5))   # tried, slower, only when the coarse turns leave a group on more plates than it needs
RETRY_UP_TO = 40                         # objects; with more, the first order and turns are kept as they are (each try takes seconds)
_ROUND = 8                  # segments per quarter circle when growing an outline by its clearance
_REACH = 1.012 / math.cos(math.pi / (4 * _ROUND))   # grow a little more so the polygon encloses the true circle; GEOS also simplifies a buffer by up to 1% of its distance
_NUDGE = 1e-4               # clears an obstacle's edge so touching candidates aren't taken as overlapping
_SLIDES = (16.0, 8.0, 4.0, 2.0, 1.0, 0.5, 0.25, 0.1)


def hull_outline(points) -> list:
    """Convex hull of (x, y) points as a list of corners."""
    return list(MultiPoint(np.asarray(points, float)[:, :2]).convex_hull.exterior.coords)[:-1]


def silhouette_outline(verts, tris, tolerance: float = 0.2) -> list:
    """Outline of a mesh seen from above, holes ignored, never inside the true outline. Pieces that don't touch are
    joined by their convex hull. `verts` are (x, y, z) rows, `tris` index triples."""
    xy = np.asarray(verts, float)[:, :2][np.asarray(tris)]                      # (triangles, 3, 2)
    a, b = xy[:, 1] - xy[:, 0], xy[:, 2] - xy[:, 0]
    xy = xy[np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) > 1e-9]   # drop triangles seen edge-on
    shape = shapely.union_all(shapely.polygons(np.concatenate([xy, xy[:, :1]], axis=1))).buffer(0.01)
    if shape.geom_type != "Polygon":
        shape = shape.convex_hull
    shape = Polygon(shape.exterior).simplify(tolerance).buffer(tolerance, join_style="mitre", mitre_limit=3)
    return list(shape.exterior.coords)[:-1]


def _grow(poly, distance: float):
    return poly.buffer(distance * _REACH, quad_segs=_ROUND) if distance > 0 else poly


class _Plate:
    """One plate: the polygons on it, and for each clearance the region a new object's outline must stay out of."""

    def __init__(self, bed, purge):
        self.bed = bed
        self.purge = box(*purge) if purge else None
        self.shapes = []        # (polygon, clearance) of everything placed or fixed
        self.keys = []          # keys of the movable objects placed here
        self.fixed = []         # keys of the pinned objects on it
        self.items = []
        self.reach = {}         # clearance -> (prepared union of grown obstacles, [their bounds])

    def add(self, poly, clearance: float) -> None:
        self.shapes.append((poly, clearance))
        for b, (union, bounds) in list(self.reach.items()):
            grown = _grow(poly, max(clearance, b))
            union = shapely.union_all([union, grown])
            shapely.prepare(union)
            self.reach[b] = (union, bounds + [grown.bounds])

    def keep_out(self, b: float):
        if b not in self.reach:
            grown = [_grow(self.purge, b)] if self.purge is not None else []
            grown += [_grow(poly, max(c, b)) for poly, c in self.shapes]
            union = shapely.union_all(grown)
            shapely.prepare(union)
            self.reach[b] = (union, [g.bounds for g in grown])
        return self.reach[b]

    def pile(self):
        """Corner of the box around everything on the plate, from the bed's lower left."""
        x, y = self.bed[0], self.bed[1]
        for poly, _ in self.shapes:
            x, y = max(x, poly.bounds[2]), max(y, poly.bounds[3])
        return x, y

    def place(self, shape, b: float):
        """Best spot for an object, as (x, y, angle) of its bounding box corner, or None if it fits nowhere."""
        union, bounds = self.keep_out(b)
        bx0, by0, bx1, by1 = self.bed
        px, py = self.pile()
        best = None
        for angle, (coords, w, d) in shape:
            m = EDGE_MARGIN_MM
            lo_x, hi_x, lo_y, hi_y = bx0 + m, bx1 - m - w, by0 + m, by1 - m - d
            if hi_x < lo_x - 1e-9 or hi_y < lo_y - 1e-9:
                continue
            xs = {lo_x, hi_x} | {v for o in bounds for v in (o[2] + _NUDGE, o[0] - w - _NUDGE)}
            ys = {lo_y, hi_y} | {v for o in bounds for v in (o[3] + _NUDGE, o[1] - d - _NUDGE)}
            grid = np.array([(x, y) for x in xs if lo_x - 1e-9 <= x <= hi_x + 1e-9
                             for y in ys if lo_y - 1e-9 <= y <= hi_y + 1e-9])
            if not len(grid):
                continue
            free = grid[~shapely.intersects(union, shapely.polygons(coords[None] + grid[:, None, :]))]
            if not len(free):
                continue
            pw, pd = np.maximum(px, free[:, 0] + w) - bx0, np.maximum(py, free[:, 1] + d) - by0
            x, y = free[np.lexsort((free[:, 0] + free[:, 1], pw * pd, np.maximum(pw, pd)))[0]]   # longest side of the pile first: a squarer pile, not a row
            x, y = self._slide(union, coords, x, y, lo_x, lo_y)
            pw, pd = max(px, x + w) - bx0, max(py, y + d) - by0
            score = (max(pw, pd), pw * pd, x + y)
            if best is None or score < best[0]:
                best = score, (float(x), float(y), angle)
        return None if best is None else best[1]

    def centre(self) -> None:
        """Move the finished pile, as one piece, to the middle of the bed. The packer works from the corner, so without this
        every plate hugs the front left. Plates with pinned objects stay as they are, and the pile stops short of the purge
        area (it moves back toward the corner by halves until it is clear)."""
        if self.fixed or not self.items:
            return
        x0, y0, x1, y1 = shapely.union_all([poly for poly, _ in self.shapes]).bounds
        bx0, by0, bx1, by1 = self.bed
        dx, dy = (bx0 + bx1 - x0 - x1) / 2, (by0 + by1 - y0 - y1) / 2
        for fraction in (1.0, 0.5, 0.25, 0.0):
            move = (dx * fraction, dy * fraction)
            if all(self.purge is None or not shapely.intersects(_grow(self.purge, c), shapely.transform(poly, lambda c_, m=move: c_ + m))
                   for poly, c in self.shapes):
                break
        else:
            return
        if not any(move):
            return
        self.shapes = [(shapely.transform(poly, lambda c_, m=move: c_ + m), c) for poly, c in self.shapes]
        for item in self.items:
            item["center"] = (item["center"][0] + move[0], item["center"][1] + move[1])
        self.reach = {}

    @staticmethod
    def _slide(union, coords, x, y, lo_x, lo_y):
        """Move left and down in shrinking steps while the object stays clear."""
        for _ in range(2):
            for axis in (0, 1):
                for step in _SLIDES:
                    while True:
                        nx, ny = (x - step, y) if axis == 0 else (x, y - step)
                        if nx < lo_x - 1e-9 or ny < lo_y - 1e-9 or shapely.intersects(union, shapely.polygons(coords + (nx, ny))):
                            break
                        x, y = nx, ny
        return x, y


def _edge_angles(outline, count: int = 3) -> list:
    """Angles that lay each of the hull's longest edges along the X axis and the Y axis."""
    hull = np.asarray(hull_outline(outline))
    edges = np.roll(hull, -1, axis=0) - hull
    longest = np.argsort(-np.hypot(edges[:, 0], edges[:, 1]))[:count]
    return [a % 360 for i in longest for a in (-math.degrees(math.atan2(edges[i, 1], edges[i, 0])),
                                               90 - math.degrees(math.atan2(edges[i, 1], edges[i, 0])))]


def _rotations(outline, angles) -> list:
    """[(angle, (closed corners from the bounding box's lower left, width, depth))] for the outline at each angle,
    and at the angles that lay its longest hull edges along the axes."""
    pts = np.asarray(outline, float)
    out = []
    extra = [a for a in _edge_angles(outline) if all(abs((a - b + 180) % 360 - 180) > 0.5 for b in angles)]
    for angle in (*angles, *dict.fromkeys(round(a, 3) for a in extra)):
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        rot = pts @ np.array([[c, s], [-s, c]])
        rot -= rot.min(axis=0)
        w, d = rot.max(axis=0)
        out.append((angle, (np.vstack([rot, rot[:1]]), float(w), float(d))))
    return out


TOLERANCE_MM = 0.01   # positions are stored to 0.001 mm and angles to 0.0001 degrees; the viewport allows the same 0.01 mm


def placement_problems(shapes: list, bed, exclude=None, tolerance: float = TOLERANCE_MM) -> list:
    """The one definition of a valid layout, for the nester's result and for anything else that must say whether objects
    are placed well. shapes: [{"key", "plate", "polygon": the object's outline from above in bed mm, "clearance": mm}];
    bed and exclude: (x0, y0, x1, y1). Returns one {"key", "plate", "problem", "other"} per fault, in shape order:
    "off the bed" (any part of the outline past the bed edge), "over the purge area" (the outline enters the exclusion
    area), "overlaps" (two outlines on one plate touch or cross) or "too close" (they are apart, but closer than the
    larger of their clearances, which is what the nester keeps); both are reported for both objects, `other` being the
    key of the one it is too near. Objects on different plates never overlap (see `problem_kinds`). Empty when the layout is valid. The nester also keeps EDGE_MARGIN_MM from the edge and its clearance from
    the purge area, but a layout is not invalid for being closer to them than that."""
    x0, y0, x1, y1 = bed
    purge = box(*exclude) if exclude else None
    polys = [Polygon(s["polygon"]) for s in shapes]
    problems = []
    for n, (s, poly) in enumerate(zip(shapes, polys)):
        lx, ly, hx, hy = poly.bounds
        if lx < x0 - tolerance or ly < y0 - tolerance or hx > x1 + tolerance or hy > y1 + tolerance:
            problems.append({"key": s["key"], "plate": s["plate"], "problem": OFF_THE_BED, "other": None})
        if purge is not None and poly.intersection(purge).area > tolerance * tolerance:
            problems.append({"key": s["key"], "plate": s["plate"], "problem": OVER_THE_PURGE_AREA, "other": None})
        for m, (t, other) in enumerate(zip(shapes, polys)):
            if m != n and t["plate"] == s["plate"]:
                need = max(float(s["clearance"]), float(t["clearance"]))
                gap = poly.distance(other)
                if gap < need - tolerance:
                    problems.append({"key": s["key"], "plate": s["plate"], "problem": OVERLAPS if gap <= tolerance else TOO_CLOSE,
                                     "other": t["key"]})
    return problems


def nest(items: list, bed, exclude=None, fixed=(), angles=ANGLES) -> list:
    """items: [{"key", "outline": [(x, y), …] in mm, "clearance": mm (default CLEARANCE_MM; SUPPORT_CLEARANCE_MM
    for an object that needs tree supports), "group"}]; bed and
    exclude: (x0, y0, x1, y1) in bed millimetres. fixed: pinned objects as [{"key", "group", "slot", "polygon":
    corners in bed mm, "clearance"}]; `slot` counts plates of the group from 0, and every slot up to the highest
    gets a plate. Returns plates in group order (groups in order of first appearance) as
    [{"group", "fixed": [keys], "items": [{"key", "center": (x, y), "turned": False, "angle": degrees, "oversize"}]}]
    where `center` is the middle of the turned outline's bounding box and `angle` is how far it was turned
    counter-clockwise about Z. An object that can't fit even on an empty plate gets a plate of its own, centred,
    marked oversize. Objects go in largest first, and when that leaves a group on more plates than it must have, other
    orders are tried too (see ORDERS) and the layout with the fewest plates wins: the first order is kept on a tie, so a
    layout that already fits is not disturbed."""
    groups = set(i["group"] for i in items) | set(f["group"] for f in fixed)
    room = (bed[2] - bed[0] - 2 * EDGE_MARGIN_MM) * (bed[3] - bed[1] - 2 * EDGE_MARGIN_MM)
    if exclude:
        room -= (exclude[2] - exclude[0]) * (exclude[3] - exclude[1])
    least = sum(max(max([f["slot"] + 1 for f in fixed if f["group"] == group], default=1),
                    math.ceil(sum(_area(i["outline"]) for i in items if i["group"] == group) / room))   # the outlines alone, no clearance
                for group in groups)

    def used(plates):   # an oversize object's plate is no fault of the order
        return sum(1 for p in plates if not any(i["oversize"] for i in p["items"]))
    tries = [(angles, order) for order in ORDERS] + ([(FINE_ANGLES, order) for order in ORDERS] if angles == ANGLES else [])
    best = None
    for turns, order in tries[:1] if len(items) > RETRY_UP_TO else tries:
        plates = _nest_once(items, bed, exclude, fixed, turns, order)
        if best is None or used(plates) < used(best):
            best = plates
        if used(best) <= least:
            break
    return best


def _area(outline) -> float:
    return Polygon(outline).area


def _longest(outline) -> float:
    x0, y0, x1, y1 = Polygon(outline).bounds
    return max(x1 - x0, y1 - y0)


def _boxed(outline) -> float:
    x0, y0, x1, y1 = Polygon(outline).bounds
    return (x1 - x0) * (y1 - y0)


# The orders objects are tried in: each is a key that sorts the biggest first. Placement is greedy, so which comes first
# decides what space is left, and no one order is best for every set.
ORDERS = (_area, _longest, _boxed)


def _nest_once(items: list, bed, exclude, fixed, angles, order) -> list:
    groups = list(dict.fromkeys([i["group"] for i in items] + [f["group"] for f in fixed]))
    plates = []   # [{"group", "plate": _Plate | None}]
    for group in groups:
        slots = max([f["slot"] + 1 for f in fixed if f["group"] == group], default=0)
        for slot in range(slots):
            plate = _Plate(bed, exclude)
            for f in (f for f in fixed if f["group"] == group and f["slot"] == slot):
                plate.add(Polygon(f["polygon"]), f.get("clearance", CLEARANCE_MM))
                plate.fixed.append(f["key"])
            plates.append({"group": group, "plate": plate})

    shapes = {}   # outline -> its rotations, so copies share them
    for item in sorted(items, key=lambda i: (groups.index(i["group"]), -order(i["outline"]), -_area(i["outline"]))):
        b = float(item.get("clearance", CLEARANCE_MM))
        outline = tuple(map(tuple, item["outline"]))
        if outline not in shapes:
            shapes[outline] = _rotations(outline, angles)
        spot = None
        for entry in (p for p in plates if p["group"] == item["group"] and p["plate"] is not None):
            spot = entry["plate"].place(shapes[outline], b)
            if spot:
                break
        if spot is None:
            entry = {"group": item["group"], "plate": _Plate(bed, exclude)}
            plates.append(entry)
            spot = entry["plate"].place(shapes[outline], b)
            if spot is None:
                entry["plate"] = None   # nothing else goes on an oversize object's plate
                entry["oversize"] = {"key": item["key"], "center": ((bed[0] + bed[2]) / 2, (bed[1] + bed[3]) / 2),
                                     "turned": False, "angle": 0.0, "oversize": True}
                continue
        plate = entry["plate"]
        x, y, angle = spot
        coords, w, d = dict(shapes[outline])[angle]
        plate.add(shapely.polygons(coords + (x, y)), b)
        plate.items.append({"key": item["key"], "center": (x + w / 2, y + d / 2), "turned": False, "angle": float(angle),
                            "oversize": False})
    for entry in plates:
        if entry["plate"] is not None:
            entry["plate"].centre()
    plates.sort(key=lambda p: groups.index(p["group"]))
    return [{"group": p["group"], "fixed": p["plate"].fixed if p["plate"] else [],
             "items": [p["oversize"]] if p["plate"] is None else p["plate"].items} for p in plates]
