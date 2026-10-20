"""Line-art drawings for the Assembly guide: two frames per step, and small pictures of finished Sections.

Everything here is pure Python on numpy (no GPU, no Blender). A mesh is turned into feature edges (boundary, crease above
35 degrees, per-view silhouette), each edge is tested for visibility against a depth buffer (centre-pixel test, bias about
0.35% of the view diagonal), short gaps in a silhouette are bridged and tiny disconnected bits dropped. The surviving
lines stay vectors and are drawn straight onto the PDF in points.

The API, in the order a guide uses it (assembly_pdf.py does this per step)

    scene = Scene.from_split(result, preview)        parts' meshes in the assembled pose, decimated to 0.25 mm, plus pins
    camera = choose_camera(scene, step, previous)    the camera for a step: first fit, reuse, scored fallback (the camera
                                                     quality section below); choose_view() also says if a close-up is needed
    art = prepare_step(scene, step, camera)          all the heavy work: line runs for both frames. No PDF involved
                                                     (close_up=True adds the close-up frame; an upward join is drawn turned
                                                     over, with a cue in frame 2)
    frame1, frame2 = layout_frames(art, width)       the two frame rectangles for a step drawn `width` points wide
    draw_step(canvas, art, left, top, width)         draws both frames, returns the height used (callers put the step
                                                     number above and the caption below)
    picture = section_picture(scene, parts); draw_picture(canvas, picture, x, y, w, h)
                                                     a small drawing of finished parts, for Section headers

`step` is a step dictionary of the plan (assembly_plan.Step.to_dict()). Its `direction` is the way the moving unit slides
into place; the receiving face looks the opposite way. The up axis is z, as the split engine leaves every model.

Extension points for later tickets: `draw_frame_one` / `draw_frame_two` are the per-frame compositions (add the zoom circle,
the hole map or a margin list there, using `layout_frames` for the room), `StepArt` carries what they need (view, runs,
pins, contact face), and `choose_camera` is the camera selection.
"""

from dataclasses import dataclass, field

import numpy as np
import trimesh
from reportlab.lib.units import mm
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from trimesh.grouping import group_rows

try:
    import manifold3d as m3d
except ImportError:  # meshes are then drawn undecimated
    m3d = None

import assembly_rules as rules

MM = mm
UP = np.array([0.0, 0.0, 1.0])
MIN_FONT = 9


# ── Geometry ───────────────────────────────────────────────────────────────────

def decimated(vertices, triangles, tolerance: "float | None" = None) -> trimesh.Trimesh:
    """The part mesh simplified to `tolerance` mm (it is left as it is when that fails or empties it)."""
    vertices, triangles = np.asarray(vertices, float), np.asarray(triangles)
    tolerance = rules.DECIMATE_MM if tolerance is None else tolerance
    if m3d is not None:
        try:
            solid = m3d.Manifold(m3d.Mesh(vert_properties=np.array(vertices, np.float32, order="C"),
                                          tri_verts=np.array(triangles, np.uint32, order="C")))
            simple = solid.simplify(tolerance).to_mesh()
            if len(simple.tri_verts):
                vertices, triangles = np.asarray(simple.vert_properties)[:, :3].astype(float), np.asarray(simple.tri_verts)
        except Exception:
            pass
    return trimesh.Trimesh(vertices, triangles, process=True)


def _pair(a: int, b: int) -> tuple:
    return (a, b) if a < b else (b, a)


class Scene:
    """What the drawings need to know about a split: each part's mesh in the assembled pose, the rods, which parts touch."""

    def __init__(self, meshes: dict, pins: list, touching: set):
        self.meshes, self.pins, self.touching = meshes, pins, touching
        self._creases = {}

    @classmethod
    def from_split(cls, result: dict, preview: list, tolerance: "float | None" = None) -> "Scene":
        """`result` is the split result, `preview` the per-part (vertices, triangles) of parts_preview.bin."""
        numbers = sorted(p["number"] for p in result.get("parts") or [])
        if not numbers or len(preview) != len(numbers):
            raise ValueError("The part previews are missing")
        meshes = {n: decimated(v, t, tolerance) for n, (v, t) in zip(numbers, preview)}
        pins = list(result.get("pins") or [])
        touching = {_pair(*j["parts"]) for j in result.get("joints") or []} | {_pair(*p["parts"]) for p in pins}
        return cls(meshes, pins, touching)

    def pins_between(self, group_a, group_b) -> list:
        a, b = set(group_a), set(group_b)
        return [p for p in self.pins if (p["parts"][0] in a and p["parts"][1] in b) or (p["parts"][1] in a and p["parts"][0] in b)]

    def touches(self, part: int, group) -> bool:
        return any(_pair(part, other) in self.touching for other in group)

    def creases(self, part: int) -> np.ndarray:
        """Edges that do not depend on the view: open boundaries and creases (n, 2, 3)."""
        if part not in self._creases:
            mesh = self.meshes[part]
            keep = mesh.face_adjacency_angles > np.radians(rules.CREASE_DEG)
            edges = mesh.face_adjacency_edges[keep]
            boundary = group_rows(mesh.edges_sorted, require_count=1)
            if len(boundary):
                edges = np.vstack([edges, mesh.edges_sorted[boundary]])
            self._creases[part] = mesh.vertices[edges]
        return self._creases[part]

    def feature_edges(self, part: int, toward_camera: np.ndarray) -> np.ndarray:
        mesh = self.meshes[part]
        pairs, normals = mesh.face_adjacency, mesh.face_normals
        flips = np.sign(normals[pairs[:, 0]] @ toward_camera) != np.sign(normals[pairs[:, 1]] @ toward_camera)
        silhouette = mesh.vertices[mesh.face_adjacency_edges[flips]]
        return np.concatenate([self.creases(part), silhouette]) if len(silhouette) else self.creases(part)

    def corners(self, part: int, offset=0.0) -> np.ndarray:
        box = self.meshes[part].bounds
        return np.array([[x, y, z] for x in box[:, 0] for y in box[:, 1] for z in box[:, 2]]) + offset


def unit(v) -> np.ndarray:
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


# ── Camera ─────────────────────────────────────────────────────────────────────

def camera_candidates() -> list:
    """The 8 diagonal views (4 sides x 2 elevations), in the fixed order the first-fit rule walks them. Each is the
    direction from the model to the camera."""
    return [unit([sx, sy, rules.CAMERA_ELEVATION * sz]) for sz in (1, -1) for sx in (1, -1) for sy in (1, -1)]


def _first_fit(u: np.ndarray, previous) -> np.ndarray:
    """Keep the previous camera while it still sees the receiving face (dot with the face normal above 0.3), else the first
    of the 8 diagonal views that sees it (above 0.05), preferring from above, then the angle closest to 55 degrees to the
    insertion axis."""
    if previous is not None and float(np.dot(previous, -u)) > rules.REUSE_FACING:
        return np.asarray(previous, float)
    best, best_key = None, None
    for camera in camera_candidates():
        key = (float(np.dot(camera, -u)) > rules.SEES_FACING, bool(camera[2] > 0), -abs(abs(float(np.dot(camera, u))) - np.cos(np.radians(rules.FIRST_FIT_DEGREES))))
        if best is None or key > best_key:
            best, best_key = camera, key
    return best


def choose_camera(scene, step: dict, previous) -> np.ndarray:
    """The camera for a step, in the step's pose (see `pose_for`: an upward join is drawn turned, so the camera is above
    the turned model). Without a `scene` this is the first-fit rule alone; with one the quality rule below applies."""
    return choose_view(scene, step, previous).camera


# ── The depth-buffer renderer ──────────────────────────────────────────────────

def view_matrix(camera) -> np.ndarray:
    back = unit(camera)
    up = UP if abs(float(back @ UP)) < 0.95 else np.array([1.0, 0, 0])
    right = unit(np.cross(up, back))
    return np.stack([right, np.cross(back, right), back])


def raster_depth(points, faces, width: int, height: int, depth) -> None:
    """Draw triangles (points: n x 3 in pixels with depth) into `depth`, keeping the nearest. Small triangles are done
    in vectorised batches by size, big ones one at a time."""
    A, B, C = points[faces[:, 0]], points[faces[:, 1]], points[faces[:, 2]]
    lo = np.floor(np.minimum(np.minimum(A, B), C)[:, :2]).astype(int)
    hi = np.ceil(np.maximum(np.maximum(A, B), C)[:, :2]).astype(int)
    lo[:, 0], hi[:, 0] = lo[:, 0].clip(0, width - 1), hi[:, 0].clip(0, width - 1)
    lo[:, 1], hi[:, 1] = lo[:, 1].clip(0, height - 1), hi[:, 1].clip(0, height - 1)
    area = (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])
    ok = np.abs(area) > 1e-9
    size = np.maximum(hi[:, 0] - lo[:, 0] + 1, hi[:, 1] - lo[:, 1] + 1)
    for low, k in ((0, 3), (3, 6), (6, 12), (12, 24), (24, 48)):
        chosen = np.where(ok & (size > low) & (size <= k))[0]
        grid = np.arange(k)
        for batch in (np.array_split(chosen, max(1, len(chosen) // 8000)) if len(chosen) else []):
            gx = lo[batch, 0, None, None] + grid[None, :, None] + 0 * grid[None, None, :]
            gy = lo[batch, 1, None, None] + 0 * grid[None, :, None] + grid[None, None, :]
            px, py = gx + .5, gy + .5
            a, b, c, ar = A[batch, None, None], B[batch, None, None], C[batch, None, None], area[batch, None, None]
            w0 = ((b[..., 0] - px) * (c[..., 1] - py) - (b[..., 1] - py) * (c[..., 0] - px)) / ar
            w1 = ((c[..., 0] - px) * (a[..., 1] - py) - (c[..., 1] - py) * (a[..., 0] - px)) / ar
            w2 = 1 - w0 - w1
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6) & (gx <= hi[batch, 0, None, None]) & (gy <= hi[batch, 1, None, None])
            z = w0 * a[..., 2] + w1 * b[..., 2] + w2 * c[..., 2]
            np.maximum.at(depth, (gy[inside], gx[inside]), z[inside].astype(np.float32))
    for i in np.where(ok & (size > 48))[0]:
        xs, ys = np.arange(lo[i, 0], hi[i, 0] + 1), np.arange(lo[i, 1], hi[i, 1] + 1)
        px, py = np.meshgrid(xs + .5, ys + .5)
        a, b, c = A[i], B[i], C[i]
        w0 = ((b[0] - px) * (c[1] - py) - (b[1] - py) * (c[0] - px)) / area[i]
        w1 = ((c[0] - px) * (a[1] - py) - (c[1] - py) * (a[0] - px)) / area[i]
        w2 = 1 - w0 - w1
        z = np.where((w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6), w0 * a[2] + w1 * b[2] + w2 * c[2], -np.inf)
        window = depth[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]
        np.maximum(window, z, out=window)


def drop_small_bits(runs: list, min_len: float) -> list:
    """Drop groups of connected runs whose total length is under `min_len` pixels."""
    if not runs:
        return runs
    pts = np.array([[a, b] for a, b in runs])
    rounded = np.round(pts.reshape(-1, 2)).astype(int)
    _, inverse = np.unique(rounded, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1, 2)
    n = inverse.max() + 1
    graph = coo_matrix((np.ones(len(runs)), (inverse[:, 0], inverse[:, 1])), shape=(n, n))
    _, label = connected_components(graph, directed=False)
    length = np.linalg.norm(pts[:, 1] - pts[:, 0], axis=1)
    total = np.bincount(label[inverse[:, 0]], weights=length, minlength=label.max() + 1)
    return [run for run, i in zip(runs, inverse[:, 0]) if total[label[i]] >= min_len]


class View:
    """An orthographic view fitted to some world points, `width` pixels wide (the height follows the content)."""

    def __init__(self, camera, fit_points, width: "int | None" = None, margin: float = 0.06, aspect=(0.8, 1.5)):
        self.camera = unit(camera)
        width = rules.RENDER_PX if width is None else width
        self.R = view_matrix(camera)
        screen = np.asarray(fit_points) @ self.R.T
        self.lo, self.hi = screen.min(axis=0), screen.max(axis=0)
        sx, sy = max(self.hi[0] - self.lo[0], 1e-6), max(self.hi[1] - self.lo[1], 1e-6)
        ratio = float(np.clip(sx / sy, *aspect))
        self.W, self.H = width, int(round(width / ratio))
        self.scale = (1 - 2 * margin) * min(self.W / sx, self.H / sy)      # pixels per mm
        self.centre = (self.hi + self.lo) / 2
        self.bias = max(1.5, rules.BIAS * np.linalg.norm(self.hi - self.lo) * self.scale)

    def project(self, points) -> np.ndarray:
        """World points to (x px, y px down, depth px, larger is nearer the camera)."""
        screen = np.atleast_2d(points) @ self.R.T
        return np.c_[(screen[:, 0] - self.centre[0]) * self.scale + self.W / 2,
                     self.H / 2 - (screen[:, 1] - self.centre[1]) * self.scale,
                     (screen[:, 2] - self.lo[2]) * self.scale]

    def render(self, scene: Scene, items: list, bridge_px: "float | None" = None, min_bit_px: "float | None" = None) -> dict:
        """items: [(key, part, offset)]. Returns {key: [(p0, p1), ...]}: the visible line runs in pixels."""
        depth = np.full((self.H, self.W), -np.inf, np.float32)
        for _key, part, offset in items:
            mesh = scene.meshes[part]
            raster_depth(self.project(mesh.vertices + offset), mesh.faces, self.W, self.H, depth)
        bridge_px = rules.BRIDGE_PX if bridge_px is None else bridge_px
        min_bit_px = rules.MIN_BIT_PX if min_bit_px is None else min_bit_px
        bridge = int(round(bridge_px / rules.SAMPLE_PX))
        out = {}
        for key, part, offset in items:
            edges = self.project(scene.feature_edges(part, self.camera).reshape(-1, 3) + offset).reshape(-1, 2, 3)
            length = np.linalg.norm(edges[:, 1, :2] - edges[:, 0, :2], axis=1)
            count = np.maximum(2, np.ceil(length / rules.SAMPLE_PX).astype(int) + 1)
            owner = np.repeat(np.arange(len(edges)), count)
            t = np.concatenate([np.linspace(0, 1, n) for n in count]) if len(count) else np.zeros(0)
            samples = edges[owner, 0] * (1 - t[:, None]) + edges[owner, 1] * t[:, None]
            xi, yi = np.floor(samples[:, 0]).astype(int), np.floor(samples[:, 1]).astype(int)
            inside = (xi >= 0) & (xi < self.W) & (yi >= 0) & (yi < self.H)
            seen = inside & (samples[:, 2] >= depth[yi.clip(0, self.H - 1), xi.clip(0, self.W - 1)] - self.bias)
            change = seen.astype(np.int64) + 2 * owner
            starts = np.where(np.r_[True, change[1:] != change[:-1]])[0]
            ends = np.r_[starts[1:], len(samples)]
            spans, last = [], None
            for a, b in zip(starts, ends):
                if not seen[a]:
                    continue
                if last is not None and owner[a] == last[0] and a - last[2] <= bridge:
                    last[2] = b                      # a short hidden gap inside one edge: bridge it
                else:
                    last = [owner[a], a, b]
                    spans.append(last)
            runs = [(samples[a, :2], samples[b - 1, :2]) for _, a, b in spans if b - a >= 2]
            out[key] = drop_small_bits(runs, min_bit_px)
        return out


class Box:
    """Maps the pixels of a View into a rectangle of the page (points, y up), keeping the aspect."""

    def __init__(self, rect, view: View):
        x, y, w, h = rect
        self.view = view
        self.k = min(w / view.W, h / view.H)
        self.ox, self.oy = x + (w - view.W * self.k) / 2, y + (h - view.H * self.k) / 2
        self.pt_per_mm = self.k * view.scale

    def px(self, points) -> np.ndarray:
        points = np.atleast_2d(points)
        return np.c_[self.ox + points[:, 0] * self.k, self.oy + (self.view.H - points[:, 1]) * self.k]

    def world(self, points) -> np.ndarray:
        return self.px(self.view.project(np.atleast_2d(points))[:, :2])


# ── A step ─────────────────────────────────────────────────────────────────────

@dataclass
class StepArt:
    """Everything one step's two frames need, worked out once (nothing here depends on the page)."""
    step: dict
    view: View
    direction: np.ndarray           # unit vector the moving unit slides along
    receiver: tuple
    mover: tuple
    touching: tuple                 # receiver parts that touch the moving unit
    offset: np.ndarray              # how far the moving unit is exploded for frame 2
    pins: list                      # the rods of this step
    runs_one: dict                  # frame 1: {("r", part): runs}
    runs_two: dict                  # frame 2: {("r" | "t" | "m", part): runs}
    face: "ContactFace | None" = None
    label: str = ""                 # what the moving bubble says when the unit is a Section
    extras: dict = field(default_factory=dict)    # room for later tickets' per-step material (zoom, hole map...)


@dataclass
class ContactFace:
    """The real join face: where the moving unit's leading face meets the receiver, in plane coordinates (mm)."""
    plane: float                    # offset of the plane along the direction
    e1: np.ndarray
    e2: np.ndarray
    receiver: np.ndarray            # (n, 3, 2) triangles of the receiver's face
    mover: np.ndarray               # (m, 3, 2) triangles of the moving unit's face
    centre: np.ndarray              # world point to hang the glue drop on


def _plane_basis(u: np.ndarray) -> tuple:
    helper = np.array([1.0, 0, 0]) if abs(u[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = unit(np.cross(u, helper))
    return e1, np.cross(u, e1)


def _flat_faces(scene: Scene, parts, u: np.ndarray, sign: float) -> list:
    """(offset along u, area, 3 x 3 vertices) of every triangle whose normal is (almost) sign * u."""
    out = []
    for part in parts:
        mesh = scene.meshes[part]
        flat = mesh.face_normals @ u * sign > 0.98
        if flat.any():
            tri = mesh.triangles[flat]
            out.append((tri.mean(axis=1) @ u, mesh.area_faces[flat], tri))
    return out


def contact_face(scene: Scene, mover, touching, u: np.ndarray) -> "ContactFace | None":
    """Find the plane where the moving unit's leading face lies on the receiver: the plane with the most area of both."""
    lead = _flat_faces(scene, mover, u, +1.0)
    back = _flat_faces(scene, touching, u, -1.0)
    if not lead or not back:
        return None
    lead_at, lead_area = np.concatenate([o for o, _, _ in lead]), np.concatenate([a for _, a, _ in lead])
    back_at, back_area = np.concatenate([o for o, _, _ in back]), np.concatenate([a for _, a, _ in back])
    best, best_score = None, 0.0
    for plane in np.unique(np.round(lead_at / rules.FACE_TOLERANCE_MM) * rules.FACE_TOLERANCE_MM):
        score = min(lead_area[abs(lead_at - plane) <= rules.FACE_TOLERANCE_MM].sum(), back_area[abs(back_at - plane) <= rules.FACE_TOLERANCE_MM].sum())
        if score > best_score + 1e-6:
            best, best_score = plane, score
    if best is None:
        return None
    e1, e2 = _plane_basis(u)
    flatten = lambda tris: np.stack([tris @ e1, tris @ e2], axis=-1)
    pick = lambda groups, at: np.concatenate([tri[abs(o - at) <= rules.FACE_TOLERANCE_MM] for o, _, tri in groups])
    receiver_tris, mover_tris = pick(back, best), pick(lead, best)
    middle = receiver_tris.mean(axis=(0, 1)) if len(receiver_tris) else mover_tris.mean(axis=(0, 1))
    return ContactFace(float(best), e1, e2, flatten(receiver_tris), flatten(mover_tris), middle)


def _points_in_triangles(points: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Which of `points` (n, 2) lie in any of the triangles (m, 3, 2)."""
    inside = np.zeros(len(points), bool)
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
    ok = abs(d) > 1e-12
    a, b, c, d = a[ok], b[ok], c[ok], d[ok]
    for start in range(0, len(points), 1500):
        p = points[start:start + 1500, None, :]
        w0 = ((b[:, 1] - c[:, 1]) * (p[..., 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (p[..., 1] - c[:, 1])) / d
        w1 = ((c[:, 1] - a[:, 1]) * (p[..., 0] - c[:, 0]) + (a[:, 0] - c[:, 0]) * (p[..., 1] - c[:, 1])) / d
        inside[start:start + 1500] = ((w0 >= 0) & (w1 >= 0) & (w0 + w1 <= 1)).any(axis=1)
    return inside


def _prepare_posed_step(scene: Scene, step: dict, camera) -> StepArt:
    """Work out both frames of a step already in its pose: the line runs seen from `camera`, shared by the two frames."""
    u = unit(step["direction"])
    receiver, mover = tuple(step["receiver"]), tuple(step["parts"])
    touching = tuple(p for p in receiver if scene.touches(p, mover))
    pins = scene.pins_between(mover, receiver)
    mover_points = np.vstack([scene.corners(p) for p in mover])
    extent = float((mover_points @ u).max() - (mover_points @ u).min())
    offset = -u * (0.3 * extent + 22.0)          # the moving unit waits this far back along its way in
    items = [(("r", p), p, np.zeros(3)) for p in receiver]
    moving = [(("m", p), p, offset) for p in mover]
    fit = np.vstack([scene.corners(p, o) for _, p, o in items + moving])
    view = View(camera, fit)
    runs_one = view.render(scene, items)
    keyed = [(("t" if p in touching else "r", p), p, o) for (_, p, o) in items]
    runs_two = view.render(scene, keyed + moving)
    return StepArt(step, view, u, receiver, mover, touching, offset, pins, runs_one, runs_two,
                   face=contact_face(scene, mover, touching, u), label=step.get("section") or "")


# ── Camera quality: pose, scored fallback, close-up ────────────────────────────

def rotation_between(a, b) -> np.ndarray:
    """The smallest rotation turning direction `a` onto direction `b`."""
    a, b = unit(a), unit(b)
    c = float(a @ b)
    if c > 0.9999:
        return np.eye(3)
    if c < -0.9999:
        return np.diag([1.0, -1.0, -1.0])      # half a turn about x
    v = np.cross(a, b)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k / (1 + c)


def pose_for(step: dict) -> "np.ndarray | None":
    """The rotation that turns the model over for a step, or None to draw it in the finished pose. A join that goes upward
    (no view from above sees its receiving face) is drawn with the model turned so the join goes down."""
    u = unit(step["direction"])
    if any(float(camera @ -u) > rules.SEES_FACING for camera in camera_candidates() if camera[2] > 0):
        return None
    return rotation_between(u, -UP)


def posed(scene: "Scene | None", step: dict) -> tuple:
    """(scene, step, turn) as the step is drawn: the receiver, the moving unit and their rods rotated by `turn`. In the
    finished pose `turn` is None and nothing is copied (the scene may be None then or when only the step is wanted)."""
    turn = pose_for(step)
    if turn is None:
        return scene, step, None
    turned_step = dict(step, direction=tuple(turn @ unit(step["direction"])))
    if scene is None:
        return None, turned_step, turn
    parts = set(step["receiver"]) | set(step["parts"])
    meshes = {p: trimesh.Trimesh(scene.meshes[p].vertices @ turn.T, scene.meshes[p].faces, process=False) for p in parts}
    pins = [dict(pin, p=(turn @ np.array(pin["p"], float)).tolist(), axis=(turn @ np.array(pin["axis"], float)).tolist())
            for pin in scene.pins if pin["parts"][0] in parts and pin["parts"][1] in parts]
    return Scene(meshes, pins, scene.touching), turned_step, turn


def _face_samples(face: "ContactFace | None") -> np.ndarray:
    """World points spread over the real join face (where both faces overlap), at most rules.FACE_SAMPLES of them."""
    if face is None or not len(face.receiver):
        return np.zeros((0, 3))
    both = face.receiver.reshape(-1, 2)
    lo, hi = both.min(axis=0), both.max(axis=0)
    area = max(float(np.prod(hi - lo)), 1e-6)
    spacing = float(np.sqrt(area / (rules.FACE_SAMPLES * 4)))
    gx, gy = np.meshgrid(np.arange(lo[0] + spacing / 2, hi[0], spacing), np.arange(lo[1] + spacing / 2, hi[1], spacing))
    dots = np.c_[gx.ravel(), gy.ravel()]
    keep = _points_in_triangles(dots, face.receiver)
    if len(face.mover):
        keep &= _points_in_triangles(dots, face.mover)
    dots = dots[keep]
    if len(dots) > rules.FACE_SAMPLES:
        dots = dots[np.linspace(0, len(dots) - 1, rules.FACE_SAMPLES).astype(int)]
    u = np.cross(face.e1, face.e2)
    return face.plane * u + dots[:, :1] * face.e1 + dots[:, 1:] * face.e2


class _StepProbe:
    """What it takes to ask "how much of this join does a camera show": the receiver and the exploded moving unit as depth
    items, sample points over the join face and the tips of the rods. `scene` and `step` are already in the step's pose."""

    def __init__(self, scene: Scene, step: dict):
        u = unit(step["direction"])
        receiver, mover = tuple(step["receiver"]), tuple(step["parts"])
        touching = tuple(p for p in receiver if scene.touches(p, mover))
        mover_points = np.vstack([scene.corners(p) for p in mover])
        extent = float((mover_points @ u).max() - (mover_points @ u).min())
        offset = -u * (0.3 * extent + 22.0)
        self.scene, self.u = scene, u
        self.items = [(p, np.zeros(3)) for p in receiver] + [(p, offset) for p in mover]
        self.fit = np.vstack([scene.corners(p, o) for p, o in self.items])
        self.face = _face_samples(contact_face(scene, mover, touching, u))
        self.tips = np.array([np.array(pin["p"], float) - u * max(pin["depth"] - 0.5, 1.0) for pin in scene.pins_between(mover, receiver)])

    def quality(self, camera) -> dict:
        view = View(camera, self.fit, width=rules.QUALITY_PX)
        depth = np.full((view.H, view.W), -np.inf, np.float32)
        for part, offset in self.items:
            mesh = self.scene.meshes[part]
            raster_depth(view.project(mesh.vertices + offset), mesh.faces, view.W, view.H, depth)

        def seen(points) -> float:
            if not len(points):
                return 1.0
            q = view.project(points)
            xi, yi = np.floor(q[:, 0]).astype(int), np.floor(q[:, 1]).astype(int)
            inside = (xi >= 0) & (xi < view.W) & (yi >= 0) & (yi < view.H)
            return float(np.mean(inside & (q[:, 2] >= depth[yi.clip(0, view.H - 1), xi.clip(0, view.W - 1)] - view.bias)))

        face, rods = seen(self.face), seen(self.tips)
        facing = max(float(np.dot(camera, -self.u)), 0.0)
        above = bool(camera[2] > 0)
        w_face, w_rods, w_facing = rules.SCORE_WEIGHTS
        score = w_face * face + w_rods * rods + w_facing * min(facing / rules.SCORE_FACING_FULL, 1.0) + (rules.SCORE_ABOVE_BONUS if above else 0.0)
        return {"score": score, "face": face, "rods": rods, "facing": facing, "above": above}


def scoring_candidates(u: np.ndarray) -> list:
    """The 10 cameras of the fallback: the 8 diagonal views, then 2 obliques looking at the face (-u raised half a unit
    and shifted half a unit to either side). `u` is the insertion direction in the step's pose."""
    ref = UP if abs(float(u @ UP)) < 0.9 else np.array([1.0, 0, 0])
    side = unit(np.cross(ref, u))
    return camera_candidates() + [unit(-u + 0.5 * ref + 0.5 * s * side) for s in (1, -1)]


@dataclass
class CameraChoice:
    camera: np.ndarray           # in the pose of the step
    quality: "dict | None"       # face / rods / score of the camera, None when it was not measured (no scene)
    scored: bool = False         # the first-fit camera was not good enough and 10 candidates were scored
    close_up: bool = False       # even the best candidate scores under rules.CLOSE_UP_BELOW: add a close-up frame


def choose_view(scene, step: dict, previous) -> CameraChoice:
    """The camera rule. First fit (or the previous camera while it still sees the face); keep it when it shows at least 85%
    of the join face and of the rod tips; else score 10 candidates and take the best (ties to the lower index); when even
    that scores under 70% the step also gets a close-up. Deterministic. Without a `scene` it stops at the first fit."""
    pose_scene, pose_step, _turn = posed(scene, step)
    u = unit(pose_step["direction"])
    first = _first_fit(u, previous)
    if scene is None:
        return CameraChoice(first, None)
    probe = _StepProbe(pose_scene, pose_step)
    quality = probe.quality(first)
    if quality["face"] >= rules.CLEAR_SHARE and quality["rods"] >= rules.CLEAR_SHARE:
        return CameraChoice(first, quality)
    candidates = scoring_candidates(u)
    results = [probe.quality(camera) for camera in candidates]
    best = int(np.argmax([r["score"] for r in results]))      # the first of equal scores
    return CameraChoice(candidates[best], results[best], scored=True, close_up=results[best]["score"] < rules.CLOSE_UP_BELOW)


@dataclass
class CloseUp:
    view: View
    runs: dict                   # {("r", part): runs} of the receiver
    zoom: float


def _close_up(scene: Scene, art: StepArt, camera) -> CloseUp:
    """The join region about 3x larger than frame 1 shows it: the receiver, the stippled face and the rods."""
    if art.face is not None:
        centre = art.face.centre
    elif art.pins:
        centre = np.mean([p["p"] for p in art.pins], axis=0)
    else:
        centre = np.mean(np.vstack([scene.corners(p) for p in art.receiver]), axis=0)
    main = art.view
    half = main.W / main.scale / rules.CLOSE_UP_ZOOM / 2          # mm
    corners = np.array([centre + sx * half * main.R[0] + sy * half * main.R[1] for sx in (-1, 1) for sy in (-1, 1)])
    view = View(camera, corners, width=rules.CLOSE_UP_PX, margin=0.0, aspect=(1.0, 1.0))
    return CloseUp(view, view.render(scene, [(("r", p), p, np.zeros(3)) for p in art.receiver]), rules.CLOSE_UP_ZOOM)


def prepare_step(scene: Scene, step: dict, camera, close_up: bool = False) -> StepArt:
    """Both frames of a step from `camera` (as `choose_camera` gives it), in the step's pose: turned over when the join goes
    upward (`art.extras["turn"]` is then the rotation, and frame 2 carries the "turn it over" cue). `close_up` adds the
    close-up frame (`art.extras["close_up"]`) that `choose_view` asks for when no camera shows the join well."""
    pose_scene, pose_step, turn = posed(scene, step)
    art = _prepare_posed_step(pose_scene, pose_step, camera)
    art.step = step
    if turn is not None:
        art.extras["turn"] = turn
    if close_up:
        art.extras["close_up"] = _close_up(pose_scene, art, camera)
    return art


# ── Drawing ────────────────────────────────────────────────────────────────────

FRAME_GAP = 4 * MM
FRAME_MIN_H, FRAME_MAX_H = 50 * MM, 62 * MM     # shorter than 74 mm so a step with its extras still leaves two to a page


def layout_frames(art: StepArt, width: float) -> tuple:
    """The two frame rectangles (x, y, w, h) for a step `width` points wide whose top-left is the origin: x from 0, y from
    the bottom of the frames at 0. `draw_step` shifts them to the page."""
    frame_w = (width - FRAME_GAP) / 2
    frame_h = min(max(frame_w * art.view.H / art.view.W, FRAME_MIN_H), FRAME_MAX_H)
    return (0.0, 0.0, frame_w, frame_h), (frame_w + FRAME_GAP, 0.0, frame_w, frame_h)


def step_height(art: StepArt, width: float) -> float:
    """Height of the frames of a step, including the close-up frame under frame 1 when it has one (that step then takes a
    whole page)."""
    return layout_frames(art, width)[0][3] + close_up_height(art, width)


def stroke_runs(canvas, runs, box: Box, grey: float, weight: float) -> None:
    if not runs:
        return
    canvas.setStrokeGray(grey)
    canvas.setLineWidth(weight)
    canvas.setLineCap(1)
    path = canvas.beginPath()
    for a, b in runs:
        start, end = box.px(np.array([a, b]))
        path.moveTo(*start)
        path.lineTo(*end)
    canvas.drawPath(path, stroke=1, fill=0)


def _bounds_pt(box: Box, runs):
    if not runs:
        return None
    pts = np.vstack([box.px(np.array([a, b])) for a, b in runs])
    return pts.min(axis=0), pts.max(axis=0)


def _rods(art: StepArt, box: Box) -> list:
    """Rod segments in world space: standing out of the receiving face, long enough to see (the rod page has the true size)."""
    out = []
    for pin in art.pins:
        p = np.array(pin["p"], float)
        length = max(pin["depth"] - 0.5, rules.MIN_ROD_PT / box.pt_per_mm)
        out.append((p - art.direction * length, p + art.direction * rules.ROD_TIP_MM, pin["diameter"]))
    return out


def _draw_rods(canvas, box: Box, rods: list) -> None:
    for a, b, diameter in rods:
        start, end = box.world(np.array([a, b]))
        width = max(diameter * box.pt_per_mm, rules.ROD_MIN_WIDTH_PT)
        canvas.setLineCap(0)
        canvas.setStrokeGray(0)
        canvas.setLineWidth(width + rules.ROD_OUTLINE_PT)
        canvas.line(*start, *end)
        canvas.setStrokeGray(1)
        canvas.setLineWidth(max(width - rules.ROD_CORE_TRIM_PT, rules.ROD_CORE_MIN_PT))
        canvas.line(*start, *end)


def _stipple_face(canvas, box: Box, face: ContactFace, spacing_pt: "float | None" = None) -> None:
    """Dots over the real contact area (where the receiver's face and the moving unit's face overlap)."""
    if face is None or not len(face.receiver):
        return
    step = (rules.STIPPLE_SPACING_PT if spacing_pt is None else spacing_pt) / box.pt_per_mm
    both = face.receiver.reshape(-1, 2)
    lo, hi = both.min(axis=0), both.max(axis=0)
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0], step), np.arange(lo[1], hi[1], step * 0.87))
    gx = gx + (np.arange(gx.shape[0]) % 2)[:, None] * step / 2
    dots = np.c_[gx.ravel(), gy.ravel()]
    if len(dots) > rules.STIPPLE_MAX_DOTS:
        return
    keep = _points_in_triangles(dots, face.receiver)
    if len(face.mover):
        keep &= _points_in_triangles(dots, face.mover)
    if not keep.any():
        return
    u = np.cross(face.e1, face.e2)
    world = face.plane * u + dots[keep, :1] * face.e1 + dots[keep, 1:] * face.e2
    canvas.setFillGray(rules.STIPPLE_GREY)
    for x, y in box.world(world):
        canvas.circle(x, y, rules.STIPPLE_DOT_PT, stroke=0, fill=1)


def glue_drop(canvas, xy, size: "float | None" = None) -> None:
    x, y = xy
    size = rules.GLUE_DROP_PT if size is None else size
    path = canvas.beginPath()
    path.moveTo(x, y + size)
    path.curveTo(x + size * 0.2, y + size * 0.5, x + size * 0.7, y + size * 0.1, x + size * 0.7, y - size * 0.35)
    path.curveTo(x + size * 0.7, y - size * 0.85, x - size * 0.7, y - size * 0.85, x - size * 0.7, y - size * 0.35)
    path.curveTo(x - size * 0.7, y + size * 0.1, x - size * 0.2, y + size * 0.5, x, y + size)
    path.close()
    canvas.setFillGray(0)
    canvas.drawPath(path, fill=1, stroke=0)
    canvas.setFillGray(1)
    canvas.circle(x - size * 0.25, y - size * 0.3, size * 0.15, stroke=0, fill=1)


def _arrow(canvas, start, end, weight: "float | None" = None, head: "float | None" = None) -> None:
    start, end = np.array(start, float), np.array(end, float)
    weight = rules.ARROW_WEIGHT if weight is None else weight
    head = rules.ARROW_HEAD_PT if head is None else head
    d = end - start
    length = np.linalg.norm(d)
    if length < 4:
        return
    d /= length
    side = np.array([-d[1], d[0]])
    canvas.setStrokeGray(0)
    canvas.setFillGray(0)
    canvas.setLineWidth(weight)
    canvas.setLineCap(0)
    canvas.line(*start, *(end - d * head * 0.8))
    path = canvas.beginPath()
    path.moveTo(*end)
    path.lineTo(*(end - d * head + side * head * 0.5))
    path.lineTo(*(end - d * head - side * head * 0.5))
    path.close()
    canvas.drawPath(path, fill=1, stroke=0)


def _bubble(canvas, xy, text: str, bold: bool = True, radius: "float | None" = None) -> None:
    default_radius, ring = rules.BUBBLE_BOLD if bold else rules.BUBBLE_LIGHT
    radius = default_radius if radius is None else radius
    canvas.setStrokeGray(0 if bold else rules.TOUCHING[0])
    canvas.setFillGray(1)
    canvas.setLineWidth(ring)
    canvas.circle(xy[0], xy[1], radius, stroke=1, fill=1)
    canvas.setFillGray(0 if bold else rules.BUBBLE_LIGHT_TEXT)
    canvas.setFont("Helvetica-Bold", MIN_FONT)
    canvas.drawCentredString(xy[0], xy[1] - 3.2, str(text))


def _place_bubble(canvas, box: Box, frame, runs, text, corner: str, bold: bool, slot: int = 0) -> None:
    """A bubble outside the geometry, in a corner of the part's own bounds, on a short leader to its nearest line."""
    bounds = _bounds_pt(box, runs)
    if bounds is None:
        return
    lo, hi = bounds
    r = (rules.BUBBLE_BOLD if bold else rules.BUBBLE_LIGHT)[0]
    x = lo[0] - r - 2 if "l" in corner else hi[0] + r + 2
    y = hi[1] + r + 2 + slot * (2 * r + 3) if "t" in corner else lo[1] - r - 2 - slot * (2 * r + 3)
    x = float(np.clip(x, frame[0] + r + 1, frame[0] + frame[2] - r - 1))
    y = float(np.clip(y, frame[1] + r + 1, frame[1] + frame[3] - r - 1))
    ends = np.vstack([box.px(np.array([a, b])) for a, b in runs[::max(1, len(runs) // 400)]])
    nearest = ends[np.argmin(np.linalg.norm(ends - [x, y], axis=1))]
    canvas.setStrokeGray(0 if bold else rules.TOUCHING[0])
    canvas.setLineWidth(rules.LEADER_WEIGHT)
    canvas.line(x, y, *nearest)
    _bubble(canvas, (x, y), text, bold, r)


def _frame_badge(canvas, frame, number: int) -> None:
    x, y = frame[0] + 9, frame[1] + frame[3] - 9
    canvas.setFillGray(0)
    canvas.circle(x, y, rules.BADGE_RADIUS, stroke=0, fill=1)
    canvas.setFillGray(1)
    canvas.setFont("Helvetica-Bold", MIN_FONT)
    canvas.drawCentredString(x, y - 3.2, str(number))


def _ink_points(box: Box, runs_by_key: dict) -> np.ndarray:
    pts = [box.px(np.array([a, b])) for runs in runs_by_key.values() for a, b in runs[::3]]
    return np.vstack(pts) if pts else np.zeros((0, 2))


def draw_frame_one(canvas, art: StepArt, frame) -> None:
    """Frame 1: the receiving unit in normal weight, the join face stippled for glue, the rods bold in the face, a glue drop."""
    box = Box(frame, art.view)
    for runs in art.runs_one.values():
        stroke_runs(canvas, runs, box, *rules.RECEIVER)
    _stipple_face(canvas, box, art.face)
    rods = _rods(art, box)
    _draw_rods(canvas, box, rods)
    anchor_world = np.mean([p["p"] for p in art.pins], axis=0) if art.pins else (art.face.centre if art.face else None)
    if anchor_world is not None:
        anchor = box.world(anchor_world)[0]
        ink = _ink_points(box, art.runs_one)
        spots = [anchor + rules.GLUE_DROP_GAP_PT * np.array([np.cos(a), np.sin(a)]) for a in np.radians((45, 135, 315, 225, 90, 0, 180, 270))]
        inside = [s for s in spots if frame[0] + 10 < s[0] < frame[0] + frame[2] - 10 and frame[1] + 10 < s[1] < frame[1] + frame[3] - 10] or spots[:1]
        drop = min(inside, key=lambda s: int((np.linalg.norm(ink - s, axis=1) < 12).sum()) if len(ink) else 0)
        canvas.setStrokeGray(0)
        canvas.setLineWidth(rules.LEADER_WEIGHT)
        canvas.line(*(drop + (anchor - drop) * 0.12), *anchor)
        glue_drop(canvas, drop)
    _frame_badge(canvas, frame, 1)


def frame_two_marks(art: StepArt, frame) -> tuple:
    """The arrow of frame 2 as ((x0, y0), (x1, y1)) and the moving unit's bounds as (lo, hi), in frame coordinates."""
    box = Box(frame, art.view)
    mover_runs = [r for key, runs in art.runs_two.items() if key[0] == "m" for r in runs]
    bounds = _bounds_pt(box, mover_runs)
    if bounds is None:
        return ((frame[0], frame[1]), (frame[0] + rules.MIN_ARROW_PT, frame[1])), (np.zeros(2), np.zeros(2))
    lo, hi = bounds
    centre = (lo + hi) / 2
    way = box.world(np.zeros(3) + art.direction)[0] - box.world(np.zeros(3))[0]
    heading = way / (np.linalg.norm(way) or 1.0)                  # the way the part slides, on the page
    side = np.array([-heading[1], heading[0]])
    length = max(rules.MIN_ARROW_PT, 0.9 * float(np.linalg.norm(box.world(art.offset)[0] - box.world(np.zeros(3))[0])))
    inner = np.array([frame[0] + 10, frame[1] + 10]), np.array([frame[0] + frame[2] - 10, frame[1] + frame[3] - 10])
    half = lambda d: 0.5 * (abs(d[0]) * (hi[0] - lo[0]) + abs(d[1]) * (hi[1] - lo[1]))
    best = None
    for s in (1, -1):                                              # beside the part, on whichever side has room
        start = centre + s * side * (half(side) + 12) - heading * length * 0.35
        end = start + heading * length
        room = min(np.min(np.minimum(start, end) - inner[0]), np.min(inner[1] - np.maximum(start, end)))
        if best is None or room > best[0]:
            best = (room, start, end)
    _, start, end = best
    return (tuple(start), tuple(end)), (lo, hi)


def draw_frame_two(canvas, art: StepArt, frame) -> None:
    """Frame 2: the same view with the moving unit exploded and bold, placed parts grey, a dashed path, the arrow."""
    box = Box(frame, art.view)
    for (role, _part), runs in art.runs_two.items():
        if role != "m":
            stroke_runs(canvas, runs, box, *(rules.TOUCHING if role == "t" else rules.PLACED))
    for (role, _part), runs in art.runs_two.items():
        if role == "m":
            stroke_runs(canvas, runs, box, 1.0, rules.BOLD + rules.HALO)
            stroke_runs(canvas, runs, box, 0.0, rules.BOLD)
    _draw_rods(canvas, box, _rods(art, box))
    (start, end), (lo, hi) = frame_two_marks(art, frame)
    mover_runs = {key: runs for key, runs in art.runs_two.items() if key[0] == "m"}
    travel = box.world(np.zeros(3))[0] - box.world(art.offset)[0]
    centre = (lo + hi) / 2
    canvas.setStrokeGray(rules.PATH_LINE[0])
    canvas.setLineWidth(rules.PATH_LINE[1])
    canvas.setDash(*rules.PATH_DASH)
    canvas.line(*centre, *(centre + travel))
    canvas.setDash()
    _arrow(canvas, start, end)
    if art.step["kind"] == "section":      # a whole Section is one bubble: its letter
        _place_bubble(canvas, box, frame, [r for runs in mover_runs.values() for r in runs], art.label, "tl", True)
    else:
        for slot, part in enumerate(art.mover):
            _place_bubble(canvas, box, frame, mover_runs.get(("m", part), []), str(part), "tl", True, slot)
    for slot, part in enumerate(art.touching):
        _place_bubble(canvas, box, frame, art.runs_two.get(("t", part), []), str(part), "tr", False, slot)
    if "turn" in art.extras:
        draw_turn_cue(canvas, frame)
    _frame_badge(canvas, frame, 2)


def draw_step(canvas, art: StepArt, left: float, top: float, width: float) -> float:
    """Draw a step's two frames with their top edge at `top`; returns the height used."""
    first, second = layout_frames(art, width)
    height = first[3]
    for frame, draw in ((first, draw_frame_one), (second, draw_frame_two)):
        placed = (left + frame[0], top - height, frame[2], frame[3])
        canvas.saveState()
        path = canvas.beginPath()
        path.rect(*placed)
        canvas.clipPath(path, stroke=0, fill=0)
        draw(canvas, art, placed)
        canvas.restoreState()
        canvas.setStrokeGray(rules.FRAME_BORDER[0])
        canvas.setLineWidth(rules.FRAME_BORDER[1])
        canvas.rect(*placed)
    if "close_up" in art.extras:
        height += draw_close_up(canvas, art, left, top - height, width)
    return height


# ── Close-up frame and the "turn it over" cue ──────────────────────────────────

CLOSE_UP_MAX_H = 100 * MM


def close_up_height(art: StepArt, width: float) -> float:
    """Room under frame 1 for the close-up frame (a gap and a frame as wide as frame 1); 0 when the step has none."""
    if "close_up" not in art.extras:
        return 0.0
    return FRAME_GAP + min((width - FRAME_GAP) / 2, CLOSE_UP_MAX_H)


def draw_close_up(canvas, art: StepArt, left: float, top: float, width: float) -> float:
    """Frame 3: the join region at about 3x, under frame 1 (the receiver, the stippled face, the rods). Returns the height used."""
    close = art.extras["close_up"]
    side = (width - FRAME_GAP) / 2
    h = min(side, CLOSE_UP_MAX_H)
    frame = (left, top - FRAME_GAP - h, side, h)
    canvas.saveState()
    path = canvas.beginPath()
    path.rect(*frame)
    canvas.clipPath(path, stroke=0, fill=0)
    box = Box(frame, close.view)
    for runs in close.runs.values():
        stroke_runs(canvas, runs, box, *rules.RECEIVER)
    _stipple_face(canvas, box, art.face)
    _draw_rods(canvas, box, _rods(art, box))
    canvas.setFillGray(0)
    canvas.setFont("Helvetica-Bold", MIN_FONT)
    canvas.drawRightString(frame[0] + frame[2] - 4, frame[1] + 4, f"Close-up, about {close.zoom:g}x")
    _frame_badge(canvas, frame, 3)
    canvas.restoreState()
    canvas.setStrokeGray(rules.FRAME_BORDER[0])
    canvas.setLineWidth(rules.FRAME_BORDER[1])
    canvas.rect(*frame)
    return FRAME_GAP + h


def draw_turn_cue(canvas, frame) -> None:
    """A curved arrow about 12 mm across with "Turn it over" beside it, in the top right corner of the frame."""
    r = rules.CUE_MM * MM / 2
    text = "Turn it over"
    text_w = canvas.stringWidth(text, "Helvetica-Bold", MIN_FONT)
    cx, cy = frame[0] + frame[2] - 6 - r, frame[1] + frame[3] - 6 - r
    canvas.setFillGray(1)
    canvas.rect(cx - r - 6 - text_w - 3, cy - r - 3, text_w + 2 * r + 12, 2 * r + 6, stroke=0, fill=1)
    angles = np.radians(np.linspace(30, 300, 28))
    pts = np.c_[cx + r * 0.8 * np.cos(angles), cy + r * 0.8 * np.sin(angles)]
    canvas.setStrokeGray(0)
    canvas.setLineWidth(rules.CUE_WEIGHT)
    canvas.setLineCap(1)
    path = canvas.beginPath()
    path.moveTo(*pts[0])
    for pt in pts[1:]:
        path.lineTo(*pt)
    canvas.drawPath(path, stroke=1, fill=0)
    tangent = pts[-1] - pts[-2]
    _arrow(canvas, pts[-1] - tangent / np.linalg.norm(tangent) * 6, pts[-1] + tangent / np.linalg.norm(tangent) * 1.5, weight=rules.CUE_WEIGHT, head=7)
    canvas.setFillGray(0)
    canvas.setFont("Helvetica-Bold", MIN_FONT)
    canvas.drawRightString(cx - r - 4, cy - 3, text)


# ── Pictures of finished parts (Section headers) ───────────────────────────────

@dataclass
class Picture:
    view: View
    runs: dict


def section_picture(scene: Scene, parts, camera=None, width: int = 600) -> Picture:
    """The finished `parts` seen from the first diagonal view, for a Section header."""
    camera = camera_candidates()[0] if camera is None else camera
    items = [(p, p, np.zeros(3)) for p in sorted(parts)]
    view = View(camera, np.vstack([scene.corners(p) for p in sorted(parts)]), width=width, aspect=(0.7, 1.4))
    return Picture(view, view.render(scene, items, min_bit_px=rules.PICTURE_MIN_BIT_PX))


def draw_picture(canvas, picture: Picture, x: float, y: float, w: float, h: float) -> None:
    box = Box((x, y, w, h), picture.view)
    for runs in picture.runs.values():
        stroke_runs(canvas, runs, box, *rules.PICTURE)
