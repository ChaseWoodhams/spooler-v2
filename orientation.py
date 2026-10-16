"""Print orientation suggestions, scored without slicing (research/orientation.md).

A small set of candidate "up" directions is built for a mesh: its largest face-normal groups, the faces of its convex
hull, the axes and their 45 degree diagonals. Each is scored from the triangles alone: overhang area weighted by height
above the bed and by how far past the support angle it leans (a support volume proxy), bed contact for a stability
rule, and build height. The cheapest stable candidate wins. Nothing is sliced, voxelised or simulated.

Rotations are Spooler's object rotations: degrees about X, then Y, then Z (projects.rotation_matrix). The turn about Z
is kept, as Lay on face does; arranging chooses where the part sits.
"""
import math

import numpy as np

LEAST_SUPPORT, STRENGTH, LOOKS = "least-support", "strength", "looks"
MODES = (LEAST_SUPPORT, STRENGTH, LOOKS)
DEFAULT_OVERHANG_ANGLE = 30.0      # degrees from horizontal at or below which a face needs support (the profile's support_threshold_angle)
FIRST_LAYER_MM = 0.3               # faces this close to the bed are on it, not overhanging
FLAT_COS = 0.985                   # a face this close to straight down lies flat on the bed
FAVOURED_COS = math.cos(math.radians(44))
FAVOURED_WEIGHT = 4.0              # a favoured face's overhang counts this many times (Orca's 1 + 3)
SAME_DIRECTION_COS = math.cos(math.radians(5))
NORMAL_GROUPS, HULL_GROUPS = 10, 14
HEIGHT_TERM = {LEAST_SUPPORT: 0.0005, LOOKS: 0.0005, STRENGTH: 0.05}   # build height x surface: a tie-break for support, the aim for strength
SUPPORT_WEIGHT = {LEAST_SUPPORT: 1.0, LOOKS: 1.0, STRENGTH: 0.25}
STABLE_SHARE = {LEAST_SUPPORT: 0.4, LOOKS: 0.4, STRENGTH: 0.4}     # of the best bed contact on offer
KEEP_TOLERANCE = 0.02              # the current orientation stays when within this share (or 1 mm³) of the best


class OrientError(ValueError):
    pass


def _unit(v) -> np.ndarray:
    v = np.asarray(v, float)
    length = float(np.linalg.norm(v))
    if v.shape != (3,) or not math.isfinite(length) or length == 0:
        raise OrientError("A direction needs three numbers that aren't all zero")
    return v / length


def _rz(degrees: float) -> np.ndarray:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def rotation_matrix(degrees) -> np.ndarray:
    rx, ry, rz = (math.radians(float(d)) for d in degrees)
    Rx = np.array([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]])
    Ry = np.array([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]])
    return _rz(math.degrees(rz)) @ Ry @ Rx


def _to_z(up: np.ndarray) -> np.ndarray:
    """Rotation taking direction `up` onto +Z."""
    axis = np.cross(up, [0.0, 0.0, 1.0])
    s, c = float(np.linalg.norm(axis)), float(up[2])
    if s < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + k + k @ k * ((1 - c) / s ** 2)


def _euler(R: np.ndarray) -> list:
    """Degrees about X, Y, Z for R = Rz @ Ry @ Rx, each 0 to 360."""
    sy = max(-1.0, min(1.0, -float(R[2, 0])))
    ry = math.asin(sy)
    if abs(sy) > 0.999999:   # looking straight along an axis: only rx - rz (or rx + rz) is defined, so turn about X alone
        rx, rz = math.atan2(-R[1, 2], R[1, 1]), 0.0
    else:
        rx, rz = math.atan2(R[2, 1], R[2, 2]), math.atan2(R[1, 0], R[0, 0])
    return [round(math.degrees(v) % 360, 3) % 360 for v in (rx, ry, rz)]


class Mesh:
    """Triangle data for one object's mesh at its scale, ready to score against any up direction."""

    def __init__(self, verts, tris, scale):
        verts = np.asarray(verts, float) * np.asarray(scale, float)
        tri = verts[np.asarray(tris)]
        cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        doubled = np.linalg.norm(cross, axis=1)
        keep = doubled > 1e-12
        if not keep.any():
            raise OrientError("The model has no triangles to orient")
        self.verts = verts
        self.tris = np.asarray(tris)
        self.normals = cross[keep] / doubled[keep, None]
        self.areas = doubled[keep] / 2
        self.centres = tri[keep].mean(axis=1)
        self.surface = float(self.areas.sum())
        self._hull = None
        self._com = None

    def centre_of_mass(self) -> np.ndarray:
        """Centroid of the enclosed volume (uniform density); the mean of the vertices when the mesh isn't closed."""
        if self._com is None:
            tri = self.verts[self.tris]
            vol = np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])) / 6.0
            total = float(vol.sum())
            if abs(total) > 1e-9 * max(1.0, self.surface ** 1.5):
                self._com = (vol[:, None] * tri.sum(axis=1) / 4.0).sum(axis=0) / total
            else:
                self._com = self.verts.mean(axis=0)
        return self._com

    def hull(self):
        """The convex hull's points and its facets as (unit normal, area)."""
        if self._hull is None:
            from scipy.spatial import ConvexHull
            try:
                hull = ConvexHull(self.verts)
            except Exception:   # flat or degenerate: no hull faces, the mesh's own normals still give candidates
                self._hull = (self.verts, [])
                return self._hull
            tri = hull.points[hull.simplices]
            cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
            doubled = np.linalg.norm(cross, axis=1)
            ok = doubled > 1e-12
            self._hull = (hull.points[hull.vertices], (cross[ok] / doubled[ok, None], doubled[ok] / 2))
        return self._hull

    def candidates(self, current_up) -> list:
        """Up directions worth scoring: the current one first, then the others without near repeats."""
        found = [_unit(current_up)]
        groups = []
        _, hull_faces = self.hull()
        for normals, areas, count in ((self.normals, self.areas, NORMAL_GROUPS), *(
                [(hull_faces[0], hull_faces[1], HULL_GROUPS)] if hull_faces else [])):
            keys = np.round(normals * 12).astype(int)
            unique, inverse = np.unique(keys, axis=0, return_inverse=True)
            inverse = inverse.reshape(-1)
            area = np.bincount(inverse, areas)
            total = np.zeros((len(unique), 3))
            np.add.at(total, inverse, normals * areas[:, None])
            for index in np.argsort(-area)[:count]:
                if np.linalg.norm(total[index]) > 1e-9:
                    groups.append(-total[index] / np.linalg.norm(total[index]))   # that face goes on the bed, so up is its opposite
        axes = [s * np.eye(3)[i] for i in range(3) for s in (1.0, -1.0)]
        diagonals = [_unit(v) for v in ((a, b, 0) for a in (1, -1) for b in (1, -1))]
        diagonals = [np.roll(d, k) * 1.0 for d in diagonals for k in range(3)]
        for up in [*groups, *axes, *diagonals]:
            if all(float(up @ f) < SAME_DIRECTION_COS for f in found):
                found.append(up)
        return found

    def measure(self, up, angle: float, favoured=None) -> dict:
        """What this orientation costs: support proxy (mm³), bed contact (mm²) and build height (mm)."""
        up = _unit(up)
        z = self.verts @ up
        low = float(z.min())
        height = float(z.max()) - low
        zc = self.centres @ up - low
        down = self.normals @ up
        limit = math.cos(math.radians(min(89.0, max(1.0, angle))))
        over = (-down > limit) & (zc >= FIRST_LAYER_MM)
        lean = np.clip((-down[over] - limit) / (1 - limit), 0.0, 1.0)
        weight = 0.25 + 0.75 * lean                                # a barely overhanging face needs far less than a ceiling
        if favoured is not None:
            weight = weight * np.where(self.normals[over] @ favoured > FAVOURED_COS, FAVOURED_WEIGHT, 1.0)
        support = float((self.areas[over] * zc[over] * weight).sum())
        contact = float(self.areas[(down < -FLAT_COS) & (zc < FIRST_LAYER_MM)].sum())
        return {"support": support, "contact": contact, "height": height,
                "overhang_area": float(self.areas[over].sum())}



def _fits(points, up, limits) -> bool:
    """Whether the part standing on `up` is under the printer's height and fits the bed at some turn about Z."""
    z = points @ up
    if float(z.max() - z.min()) > limits["height"] + 1e-6:
        return False
    R = _to_z(up)
    flat = (points @ R.T)[:, :2]
    bed = sorted((limits["bed_x"], limits["bed_y"]))
    for turn in range(0, 90, 5):
        t = math.radians(turn)
        x = flat[:, 0] * math.cos(t) + flat[:, 1] * math.sin(t)
        y = -flat[:, 0] * math.sin(t) + flat[:, 1] * math.cos(t)
        size = sorted((float(np.ptp(x)), float(np.ptp(y))))
        if size[0] <= bed[0] + 1e-6 and size[1] <= bed[1] + 1e-6:
            return True
    return False


def suggest(verts, tris, scale, rotation, *, mode: str = LEAST_SUPPORT, angle: float = DEFAULT_OVERHANG_ANGLE,
            favoured=None, limits: dict | None = None) -> dict:
    """The orientation to print this mesh in, as {"rotation": [x, y, z], "changed", "current": metrics, "suggested": metrics}.

    `favoured` is a direction in the model's own space (the side that should look best); looks mode needs it.
    `limits` ({"height", "bed_x", "bed_y"}) drops orientations that don't fit the printer."""
    if mode not in MODES:
        raise OrientError(f"Orientation mode must be one of {', '.join(MODES)}")
    if mode == LOOKS:
        if favoured is None:
            raise OrientError("Choose the side that should look best")
        favoured = _unit(favoured)
    else:
        favoured = None
    mesh = Mesh(verts, tris, scale)
    points = mesh.hull()[0]
    current_R = rotation_matrix(rotation)
    options = []
    for index, up in enumerate(mesh.candidates(current_R[2])):
        if index and limits and not _fits(points, up, limits):
            continue
        options.append((up, mesh.measure(up, angle, favoured)))
    best_contact = max(m["contact"] for _, m in options)
    stable = [o for o in options if o[1]["contact"] >= STABLE_SHARE[mode] * best_contact] or options

    def cost(m):
        return SUPPORT_WEIGHT[mode] * m["support"] + HEIGHT_TERM[mode] * m["height"] * mesh.surface

    now = options[0]
    best = min(stable, key=lambda o: cost(o[1]))
    if any(o is now for o in stable) and cost(now[1]) <= cost(best[1]) + max(1.0, KEEP_TOLERANCE * cost(best[1])):
        best = now       # near ties keep the part as it is, like Orca
    turn = float(rotation[2])
    R = _rz(turn) @ _to_z(best[0])
    return {"rotation": list(rotation) if best is now else _euler(R), "changed": best is not now,
            "current": now[1], "suggested": best[1]}


# --- ranked steady candidates for the planner (auto-plating ticket 14) -------------------------------------------------

CONTACT_TOLERANCE_MM = 0.3         # vertices this close to the bed make up the footprint
MIN_CONTACT_PER_NOZZLE_SQ = 50.0   # minimum first-layer contact in mm2 is this x nozzle diameter squared (8 at 0.4, 24.5 at 0.7) ...
MIN_CONTACT_SHARE = 0.5            # ... but never more than this share of the best contact any candidate offers (tiny parts)
TALL_RATIO = 5.0                   # steady needs height <= this x sqrt(footprint area)
COM_MARGIN_SHARE = 0.1             # the centre of mass sits at least this x sqrt(footprint area) (max 1 mm) inside its edge
RERANK_TOP = 8                     # candidates ray-cast after the proxy screen
MAX_RAYS = 20000
CUT_FACE_BONUS = 0.05              # a cut face's cost is cut by this share: a tie-break, nothing more
NEEDS_SUPPORT_MM3 = 50.0           # below this support volume a candidate prints without support


def _ray_support_volume(mesh: "Mesh", up: np.ndarray, angle: float):
    """Support volume (mm3) with `up` up: from each overhang face, the column straight down to the first thing it hits
    (the part or the bed). None when the embree backend isn't installed."""
    try:
        import trimesh
        from trimesh.ray.ray_pyembree import RayMeshIntersector
    except ImportError:
        return None
    points = mesh.verts @ _to_z(up).T
    points[:, 2] -= points[:, 2].min()
    solid = trimesh.Trimesh(points, mesh.tris, process=False)
    normals_z = solid.face_normals[:, 2]
    area, centre_z = solid.area_faces, solid.triangles_center[:, 2]
    limit = math.cos(math.radians(min(89.0, max(1.0, angle))))
    over = np.where((-normals_z > limit) & (centre_z >= FIRST_LAYER_MM) & (area > 1e-12))[0]
    if len(over) == 0:
        return 0.0
    projected = area[over] * np.abs(normals_z[over])
    if len(over) > MAX_RAYS:   # an area-weighted sample stands in for all of them
        pick = np.random.default_rng(0).choice(len(over), MAX_RAYS, replace=False, p=area[over] / area[over].sum())
        projected = np.full(MAX_RAYS, float(projected.sum()) / MAX_RAYS)
        over = over[pick]
    origins = solid.triangles_center[over] - [0, 0, 1e-3]
    hit_z = np.zeros(len(over))
    found, rays, _ = RayMeshIntersector(solid).intersects_location(origins, np.tile([0, 0, -1.0], (len(over), 1)),
                                                                   multiple_hits=False)
    hit_z[rays] = found[:, 2]
    return float((projected * np.maximum(centre_z[over] - hit_z, 0.0)).sum())


def _footprint(mesh: "Mesh", up: np.ndarray):
    """(contact area mm2, height mm, how far the centre of mass sits inside the footprint's edge in mm) standing on `up`."""
    R = _to_z(up)
    points = mesh.verts @ R.T
    low = float(points[:, 2].min())
    height = float(points[:, 2].max()) - low
    base = points[points[:, 2] <= low + CONTACT_TOLERANCE_MM, :2]
    if len(base) < 3:
        return 0.0, height, -1.0
    from scipy.spatial import ConvexHull
    try:
        hull = ConvexHull(base)
    except Exception:   # a line or a point: nothing to stand on
        return 0.0, height, -1.0
    centre = (R @ mesh.centre_of_mass())[:2]
    inside = float(-(hull.equations[:, :2] @ centre + hull.equations[:, 2]).max())   # > 0 inside: distance to the nearest edge
    return float(hull.volume), height, inside


def rank(verts, tris, scale, rotation, *, angle: float = DEFAULT_OVERHANG_ANGLE, nozzle: float = 0.4,
         limits: dict | None = None, cut_normals=()) -> dict:
    """Steady orientations to print this mesh in, best first, for the planner.

    Returns {"candidates": [...], "unavoidably_tippy": bool, "reason": str | None}. Each candidate is
    {"rotation": [x, y, z], "support_volume": mm3, "support_source": "raycast" | "proxy", "needs_support": bool,
     "contact": mm2, "height": mm, "cut_face": bool}. The first candidate is the one to use.

    Candidates are the current orientation, the hull, normal-group and axis ones (`Mesh.candidates`) and each cut face
    (`cut_normals`: outward normals of cut faces in the model's own space, which go on the bed; a small bonus breaks
    ties in their favour). A steady one has a first-layer contact of at least `MIN_CONTACT_PER_NOZZLE_SQ` x nozzle^2,
    its centre of mass inside that footprint, and is not taller than `TALL_RATIO` x the footprint's size. The steady
    ones that fit `limits` are screened with the overhang proxy, and the best `RERANK_TOP` are re-ranked by an embree
    ray-cast support volume (the proxy's own number when embree isn't installed). With nothing steady that fits an
    empty bed the part is unavoidably tippy: the best that fits (failing that the best at all) is still returned,
    with the reason."""
    mesh = Mesh(verts, tris, scale)
    points = mesh.hull()[0]
    current_R = rotation_matrix(rotation)
    ups = mesh.candidates(current_R[2])
    cut = [False] * len(ups)
    for normal in cut_normals:
        up = -_unit(normal)
        for i, known in enumerate(ups):
            if float(up @ known) >= SAME_DIRECTION_COS:
                cut[i] = True
                break
        else:
            ups.append(up)
            cut.append(True)
    options = []
    for index, up in enumerate(ups):
        contact, height, inside = _footprint(mesh, up)
        options.append({"up": up, "index": index, "cut": cut[index], "contact": contact, "height": height, "inside": inside,
                        "fits": not limits or _fits(points, up, limits), "metrics": mesh.measure(up, angle)})
    best_contact = max(o["contact"] for o in options)
    need = min(MIN_CONTACT_PER_NOZZLE_SQ * nozzle ** 2, MIN_CONTACT_SHARE * best_contact)
    for o in options:
        size = math.sqrt(o["contact"])
        o["steady"] = (o["contact"] > 0 and o["contact"] >= need and o["height"] <= TALL_RATIO * size
                       and o["inside"] >= min(1.0, COM_MARGIN_SHARE * size))

    def bonus(o):
        return 1 - CUT_FACE_BONUS if o["cut"] else 1.0

    def height_term(o):
        return HEIGHT_TERM[LEAST_SUPPORT] * o["height"] * mesh.surface

    pool = [o for o in options if o["steady"] and o["fits"]]
    tippy, reason = not pool, None
    if tippy:
        fitting = [o for o in options if o["fits"]]
        size = " x ".join(f"{limits[k]:g}" for k in ("bed_x", "bed_y", "height")) if limits else "any"
        if fitting and any(o["steady"] for o in options):
            pool = sorted(fitting, key=lambda o: -o["contact"])[:3]
            reason = f"Too big to print steadily on a {size} mm bed: only its tilted orientations fit"
        elif fitting:
            pool = sorted(fitting, key=lambda o: -o["contact"])[:3]
            reason = ("No steady orientation: it has no footprint wide and flat enough to stand on without tipping "
                      "(first-layer contact, centre of mass, height)")
        else:
            pool = sorted(options, key=lambda o: -o["contact"])[:3]
            reason = f"Doesn't fit a {size} mm bed in any orientation"
    pool.sort(key=lambda o: (o["metrics"]["support"] + height_term(o)) * bonus(o))
    top = pool[:RERANK_TOP]
    for o in top:
        volume = _ray_support_volume(mesh, o["up"], angle)
        o["volume"], o["source"] = (o["metrics"]["support"], "proxy") if volume is None else (volume, "raycast")
    top.sort(key=lambda o: (o["volume"] + height_term(o)) * bonus(o))
    turn = float(rotation[2])
    candidates = [{"rotation": list(rotation) if o["index"] == 0 else _euler(_rz(turn) @ _to_z(o["up"])),
                   "support_volume": o["volume"], "support_source": o["source"],
                   "needs_support": o["volume"] > NEEDS_SUPPORT_MM3, "contact": o["contact"],
                   "height": o["height"], "cut_face": o["cut"]} for o in top]
    return {"candidates": candidates, "unavoidably_tippy": tippy, "reason": reason}
