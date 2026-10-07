"""
Nesting parts onto build plates by outline.

Where packing.py places footprint rectangles, this places each part's outline (a convex hull or the true
silhouette from above) turned to many angles. Every part has a clearance `b`, the reach of its supports beyond its
outline (research/brim-support-measurements.md): the nester keeps two parts at least max(b_i, b_j) apart, a part
EDGE_MARGIN_MM from the bed edge and b from the purge area. Pinned objects are fixed outlines on a plate that others pack around. Plates come out in the shape
packing.pack returns, so a part that fits nowhere on an empty plate gets a plate of its own, marked oversize.

Placement tries a few hundred bottom-left candidate positions per part and angle, then slides the best one left and
down until it touches, so concave parts can tuck into each other's gaps.
"""

import math

import numpy as np
import shapely
from shapely.geometry import MultiPoint, Polygon, box

from packing import GAP_MM, MARGIN_MM, SUPPORT_GAP_MM

CLEARANCE_MM = GAP_MM        # a plain part's clearance; the pair rule is max, so two plain parts keep today's 6 mm
SUPPORT_CLEARANCE_MM = SUPPORT_GAP_MM   # a part that needs tree supports in its orientation
EDGE_MARGIN_MM = MARGIN_MM   # brim and supports are clipped at the bed edge, so no more than the packer's margin
ANGLES = tuple(range(0, 360, 15))
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
    """One plate: the polygons on it, and for each clearance the region a new part's outline must stay out of."""

    def __init__(self, bed, purge):
        self.bed = bed
        self.purge = box(*purge) if purge else None
        self.shapes = []        # (polygon, clearance) of everything placed or fixed
        self.keys = []          # keys of the movable parts placed here
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
        """Best spot for a part, as (x, y, angle) of its bounding box corner, or None if it fits nowhere."""
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
            cost = (np.maximum(px, free[:, 0] + w) - bx0) * (np.maximum(py, free[:, 1] + d) - by0)
            x, y = free[np.lexsort((free[:, 0] + free[:, 1], cost))[0]]
            x, y = self._slide(union, coords, x, y, lo_x, lo_y)
            score = ((max(px, x + w) - bx0) * (max(py, y + d) - by0), x + y)
            if best is None or score < best[0]:
                best = score, (float(x), float(y), angle)
        return None if best is None else best[1]

    @staticmethod
    def _slide(union, coords, x, y, lo_x, lo_y):
        """Move left and down in shrinking steps while the part stays clear."""
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


def nest(items: list, bed, exclude=None, fixed=(), angles=ANGLES) -> list:
    """items: [{"key", "outline": [(x, y), …] in mm, "clearance": mm (default CLEARANCE_MM; SUPPORT_CLEARANCE_MM
    for a part that needs tree supports), "group"}]; bed and
    exclude: (x0, y0, x1, y1) in bed millimetres. fixed: pinned objects as [{"key", "group", "slot", "polygon":
    corners in bed mm, "clearance"}]; `slot` counts plates of the group from 0, and every slot up to the highest
    gets a plate. Returns plates in group order (groups in order of first appearance) as
    [{"group", "fixed": [keys], "items": [{"key", "center": (x, y), "turned": False, "angle": degrees, "oversize"}]}]
    where `center` is the middle of the turned outline's bounding box and `angle` is how far it was turned
    counter-clockwise about Z. A part that can't fit even on an empty plate gets a plate of its own, centred,
    marked oversize."""
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
    for item in sorted(items, key=lambda i: (groups.index(i["group"]), -Polygon(i["outline"]).area)):
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
                entry["plate"] = None   # nothing else goes on an oversize part's plate
                entry["oversize"] = {"key": item["key"], "center": ((bed[0] + bed[2]) / 2, (bed[1] + bed[3]) / 2),
                                     "turned": False, "angle": 0.0, "oversize": True}
                continue
        plate = entry["plate"]
        x, y, angle = spot
        coords, w, d = dict(shapes[outline])[angle]
        plate.add(shapely.polygons(coords + (x, y)), b)
        plate.items.append({"key": item["key"], "center": (x + w / 2, y + d / 2), "turned": False, "angle": float(angle),
                            "oversize": False})
    plates.sort(key=lambda p: groups.index(p["group"]))
    return [{"group": p["group"], "fixed": p["plate"].fixed if p["plate"] else [],
             "items": [p["oversize"]] if p["plate"] is None else p["plate"].items} for p in plates]
