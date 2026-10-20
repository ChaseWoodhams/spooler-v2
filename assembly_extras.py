"""The aids around each step's two frames in the Assembly guide.

    attach(art, scene, section_parts)             tell a prepared step which scene and Section it belongs to
    draw_step_extras(canvas, art, left, top, w)   zoom circle on frame 1, drawn over the frames whose top edge is at `top`
    draw_strip(canvas, art, left, top, w)         the strip under the caption: "you need" list, hole map, thumbnail
    zoom_circle(art, frame)                       ((x, y), radius, magnification) in page points, or None
    strip_height(art)                             height of the strip in points (it grows for a hole map)

This module only adds to what assembly_render.py draws; it reads a StepArt and draws onto the same canvas.
"""

import numpy as np
from reportlab.pdfbase.pdfmetrics import stringWidth

import assembly_render as render
import assembly_rules as rules
from assembly_bom import cut_length, mm_text

MM = render.MM
FONT, BOLD = "Helvetica", "Helvetica-Bold"
SIZE = render.MIN_FONT                # nothing smaller is printed

STRIP_MIN_H = 21 * MM                 # a step without a hole map keeps two steps to a page
STRIP_MAP_H = 34 * MM                 # a step with a hole map gets a taller strip
STRIP_GAP = 2 * MM
HOLE_MAP_MIN_RODS = 5
NEED_W, MAP_X, MAP_W, THUMB_W = 76 * MM, 78 * MM, 30 * MM, 50 * MM


def attach(art, scene, section_parts) -> None:
    art.extras["scene"] = scene
    art.extras["section_parts"] = tuple(section_parts)


# ── Zoom circle ────────────────────────────────────────────────────────────────

def zoom_radius() -> float:
    """Radius of the zoom circle in points (rule ZOOM_MM is its width in mm)."""
    return rules.ZOOM_MM * MM / 2


def segment_distance(point, a, b) -> float:
    p, a, b = np.asarray(point, float), np.asarray(a, float), np.asarray(b, float)
    ab = b - a
    t = float(np.clip(np.dot(p - a, ab) / (np.dot(ab, ab) or 1.0), 0, 1))
    return float(np.linalg.norm(p - (a + ab * t)))


def rod_segments(art, frame) -> list:
    """The rods of frame 1 as (start, end, drawn width) in page points."""
    box = render.Box(frame, art.view)
    out = []
    for a, b, diameter in render._rods(art, box):
        start, end = box.world(np.array([a, b]))
        out.append((start, end, render.rod_width(diameter, box.pt_per_mm) + rules.ROD_OUTLINE_PT))
    return out


class Lens:
    """Maps world points into the zoom circle the way `Box` maps them into the frame: the frame's page point of `focus`
    lands on the circle's centre and everything is `k` times larger. Rods and the stipple take it where they take a Box."""

    def __init__(self, box, centre, focus, k):
        self.box, self.centre, self.focus, self.k = box, np.asarray(centre, float), np.asarray(focus, float), k
        self.pt_per_mm = box.pt_per_mm * k

    def world(self, points) -> np.ndarray:
        return self.centre + self.k * (self.box.world(points) - self.focus)


def _spec(art):
    return art.extras.get("zoom") or render._zoom_spec(art)


def _round_half(value: float, up: bool = False) -> float:
    return float((np.ceil if up else np.floor)(value * 2) / 2)


def _rod_share(art, box, ppm: float, r: float, offsets) -> float:
    """How much of the circle's width the rods standing in it would cover side by side: the union of their widths across
    the way they point on the page, over the circle's diameter. `offsets` is each rod's offset from the circle's centre in
    the circle, in points. Rods seen end on cover nothing across."""
    way = box.world(art.direction)[0] - box.world(np.zeros(3))[0]
    if np.linalg.norm(way) < 1e-6:
        return 0.0
    across = np.array([-way[1], way[0]]) / np.linalg.norm(way)
    spans = []
    for pin, offset in zip(art.pins, offsets):
        if np.linalg.norm(offset) <= r:
            centre, half = float(offset @ across), render.rod_width(float(pin["diameter"]), ppm) / 2
            spans.append((max(centre - half, -r), min(centre + half, r)))
    covered, end = 0.0, -np.inf
    for lo, hi in sorted(spans):
        if hi > lo:
            covered += hi - max(lo, end)
            end = hi
    return covered / (2 * r)


def zoom_factor(art, frame) -> float:
    """The magnification of the zoom circle. Holes must be clear of each other (ZOOM_HOLE_GAP_PT apart in the circle) without
    zooming further than that, the holes and a margin of surface must fit in the circle, and the rods must not cover so
    much of the circle that it reads as a barcode of bars: then the factor is lowered."""
    box = render.Box(frame, art.view)
    holes = box.world(np.array([p for p, _ in _spec(art).holes]))
    focus = holes.mean(axis=0)
    ppm, r = box.pt_per_mm, zoom_radius()
    radii = np.array([d / 2 * ppm for _, d in _spec(art).holes])
    reach = float((np.linalg.norm(holes - focus, axis=1) + radii).max()) + _spec(art).margin_mm * ppm
    k = _round_half(r / max(reach, 1e-6))
    if len(holes) > 1:
        gaps = np.linalg.norm(holes[:, None] - holes[None], axis=2)
        gaps = gaps[gaps > 1e-6]
        if len(gaps):
            k = min(k, _round_half(rules.ZOOM_HOLE_GAP_PT / float(gaps.min()), up=True))
    k = float(np.clip(k, *rules.ZOOM_RANGE))
    while k > rules.ZOOM_FLOOR and _rod_share(art, box, ppm * k, r, (holes - focus) * k) > rules.ZOOM_MAX_ROD_SHARE:
        k = max(k - 0.5, rules.ZOOM_FLOOR)
    return k


def zoom_circle(art, frame):
    """Where the zoom circle goes in frame 1: the position with the least drawing under it among those that keep clear of
    every rod. Returns ((x, y), radius, magnification) or None (no rods, or no room)."""
    rods = rod_segments(art, frame)
    if not rods:
        return None
    magnification = zoom_factor(art, frame)
    box = render.Box(frame, art.view)
    ink = render._ink_points(box, art.runs_one)
    badge = np.array([frame[0] + 9, frame[1] + frame[3] - 9])
    r = zoom_radius()
    xs = np.linspace(frame[0] + r + 3, frame[0] + frame[2] - r - 3, 14)
    ys = np.linspace(frame[1] + r + 3 + rules.ZOOM_LABEL_PT, frame[1] + frame[3] - r - 3, 8)
    corners = np.array([[frame[0], frame[1]], [frame[0] + frame[2], frame[1]], [frame[0], frame[1] + frame[3]],
                        [frame[0] + frame[2], frame[1] + frame[3]]])
    best, best_score = None, None
    if xs[0] > xs[-1] or ys[0] > ys[-1]:
        return None
    for x in xs:
        for y in ys:
            centre = np.array([x, y])
            if any(segment_distance(centre, s, e) < r + w / 2 + rules.ZOOM_CLEAR_PT for s, e, w in rods):
                continue
            covered = int((np.linalg.norm(ink - centre, axis=1) < r).sum()) if len(ink) else 0
            score = covered + (200 if np.linalg.norm(badge - centre) < r + 10 else 0) \
                + 0.01 * float(np.linalg.norm(corners - centre, axis=1).min())
            if best_score is None or score < best_score:
                best, best_score = centre, score
    if best is None:
        return None
    return (float(best[0]), float(best[1])), r, magnification


def _lens(art, frame):
    zoom = zoom_circle(art, frame)
    if zoom is None:
        return None
    (cx, cy), r, k = zoom
    box = render.Box(frame, art.view)
    focus = box.world(np.array([p for p, _ in _spec(art).holes])).mean(axis=0)
    return Lens(box, (cx, cy), focus, k), r


def _hole_polygon(art, centre, diameter) -> np.ndarray:
    face = _spec(art).face
    e1, e2 = (face.e1, face.e2) if face is not None else render._plane_basis(art.direction)
    angles = np.linspace(0, 2 * np.pi, rules.ZOOM_HOLE_SIDES, endpoint=False)
    return centre + diameter / 2 * (np.cos(angles)[:, None] * e1 + np.sin(angles)[:, None] * e2)


def zoom_facts(art, frame) -> "dict | None":
    """What the zoom circle shows, read off the geometry: the `factor`, how many `holes_shown` are inside the circle, the
    smallest gap between holes in the circle (`hole_gap_pt`), whether the join face is in it, and the `rod_share`."""
    found = _lens(art, frame)
    if found is None:
        return None
    lens, r = found
    spec = _spec(art)
    holes = lens.world(np.array([p for p, _ in spec.holes]))
    inside = np.linalg.norm(holes - lens.centre, axis=1) <= r
    gap = float("inf")
    if len(holes) > 1:
        gaps = np.linalg.norm(holes[:, None] - holes[None], axis=2)
        gaps = gaps[gaps > 1e-6]
        gap = float(gaps.min()) if len(gaps) else gap
    face_shown = False
    if spec.face is not None:
        face = spec.face
        face_shown = bool(inside.any())
        edges = render.face_outline(face)
        u = np.cross(face.e1, face.e2)
        for a, b in edges:
            pa, pb = (lens.world(face.plane * u + x * face.e1 + y * face.e2)[0] for x, y in (a, b))
            face_shown = face_shown or segment_distance(lens.centre, pa, pb) < r
    return {"factor": lens.k, "holes_shown": int(inside.sum()), "hole_gap_pt": gap, "face_shown": face_shown,
            "rod_share": _rod_share(art, lens.box, lens.pt_per_mm, r, holes - lens.centre)}


def draw_zoom(canvas, art, frame) -> "dict | None":
    """The zoom circle: a close-up of the join face with its outline and stipple, the holes in it and the rods standing in
    them, over a margin of the receiver's own lines. Returns the zoom facts plus `holes_drawn` and `face_edges_drawn`."""
    found = _lens(art, frame)
    if found is None:
        return None
    lens, r = found
    facts = zoom_facts(art, frame)
    spec = _spec(art)
    (cx, cy), k, focus, box = lens.centre, lens.k, lens.focus, lens.box
    toward = focus - lens.centre
    gap = float(np.linalg.norm(toward))
    if gap > r + 4:                                       # a thin leader from the circle to the rods it magnifies
        edge = lens.centre + toward / gap * r
        canvas.setStrokeGray(rules.ZOOM_LEADER[0])
        canvas.setLineWidth(rules.ZOOM_LEADER[1])
        canvas.line(*edge, *(focus - toward / gap * 2))
    canvas.saveState()
    clip = canvas.beginPath()
    clip.circle(cx, cy, r)
    canvas.clipPath(clip, stroke=0, fill=0)
    canvas.setFillGray(1)
    canvas.circle(cx, cy, r, stroke=0, fill=1)
    canvas.saveState()
    canvas.translate(cx, cy)
    canvas.scale(k, k)
    canvas.translate(-focus[0], -focus[1])
    reach = r / k + 4
    near = []
    for runs in art.runs_one.values():
        for a, b in runs:
            pts = box.px(np.array([a, b]))
            if (pts.min(axis=0) <= focus + reach).all() and (pts.max(axis=0) >= focus - reach).all():
                near.append((a, b))
    render.stroke_runs(canvas, near, box, rules.RECEIVER[0], rules.RECEIVER[1] * rules.ZOOM_LINE_SCALE / k)
    canvas.restoreState()
    render._stipple_face(canvas, lens, spec.face)
    edges_drawn = 0
    if spec.face is not None:
        face = spec.face
        u = np.cross(face.e1, face.e2)
        canvas.setStrokeGray(rules.RECEIVER[0])
        canvas.setLineWidth(rules.RECEIVER[1] * rules.ZOOM_LINE_SCALE)
        for a, b in render.face_outline(face):
            pa, pb = (lens.world(face.plane * u + x * face.e1 + y * face.e2)[0] for x, y in (a, b))
            canvas.line(*pa, *pb)
            edges_drawn += segment_distance(lens.centre, pa, pb) < r
    holes_drawn = 0
    canvas.setStrokeGray(0)
    canvas.setLineWidth(rules.LEADER_WEIGHT)
    for point, diameter in spec.holes:
        outline = lens.world(_hole_polygon(art, point, diameter))
        path = canvas.beginPath()
        path.moveTo(*outline[0])
        for x, y in outline[1:]:
            path.lineTo(x, y)
        path.close()
        canvas.setFillGray(1)
        canvas.drawPath(path, stroke=1, fill=1)
        holes_drawn += np.linalg.norm(outline.mean(axis=0) - lens.centre) <= r
    render._draw_rods(canvas, lens, render._rods(art, lens))
    canvas.restoreState()
    canvas.setStrokeGray(0)
    canvas.setLineWidth(rules.ZOOM_RING_PT)
    canvas.circle(cx, cy, r, stroke=1, fill=0)
    label = f"Zoom {k:g}x"
    width = stringWidth(label, BOLD, SIZE)
    canvas.setFillGray(1)
    canvas.rect(cx - width / 2 - 2, cy - r - rules.ZOOM_LABEL_PT + 1, width + 4, SIZE + 2, stroke=0, fill=1)
    canvas.setFillGray(0)
    canvas.setFont(BOLD, SIZE)
    canvas.drawCentredString(cx, cy - r - rules.ZOOM_LABEL_PT + 3, label)
    return {**facts, "holes_drawn": int(holes_drawn), "face_edges_drawn": int(edges_drawn)}


def draw_step_extras(canvas, art, left: float, top: float, width: float) -> None:
    """The extras that sit on the frames, for frames whose top edge is at `top` (as `render.draw_step` takes them)."""
    first = render.layout_frames(art, width)[0]
    draw_zoom(canvas, art, (left + first[0], top - first[3], first[2], first[3]))


# ── The strip under the caption ────────────────────────────────────────────────

def _rods_needed(art) -> list:
    """[(label, count, diameter, cut length)] for the rods of the step, biggest first."""
    found = {}
    for pin in art.pins:
        entry = found.setdefault(pin["connector"], [0, float(pin["diameter"]), cut_length(float(pin["depth"]))])
        entry[0] += 1
    return sorted(((label, *entry) for label, entry in found.items()), key=lambda e: -e[2])


def _moving_label(art) -> str:
    if art.step["kind"] == "section":
        return f"Section {art.step['section']}"
    return ("part " if len(art.mover) == 1 else "parts ") + ", ".join(str(p) for p in art.mover)


def strip_height(art) -> float:
    rows = max(1, min(len(_rods_needed(art)), 3))
    need = 17 + 12 * rows + 11 + 6
    return max(STRIP_MAP_H if hole_map_wanted(art) else STRIP_MIN_H, need)


def draw_you_need(canvas, art, x: float, top: float) -> None:
    canvas.setFillGray(0)
    canvas.setFont(BOLD, SIZE)
    canvas.drawString(x, top - SIZE - 1, "You need")
    y = top
    bubble_text = art.label if art.step["kind"] == "section" else str(art.mover[0])
    render._bubble(canvas, (x + 52, y - 8.5), bubble_text, True, 8.5)
    canvas.setFillGray(0)
    canvas.setFont(FONT, SIZE)
    canvas.drawString(x + 65, y - 11.5, _moving_label(art))
    y -= 17
    rods = _rods_needed(art)
    for label, count, diameter, length in rods[:3]:
        canvas.setFillGray(0)
        canvas.setFont(BOLD, SIZE)
        canvas.drawString(x, y - 9, f"{count} x {label}")
        icon_x = x + 25 * MM
        room = NEED_W - 25 * MM - 11 * MM
        drawn = min(length * MM, room)                       # true size, unless it would not fit: then it is shown broken
        canvas.setLineWidth(0.5)
        canvas.setStrokeGray(0)
        canvas.setFillGray(0.85)
        canvas.rect(icon_x, y - 5.5 - diameter * MM / 2, drawn, diameter * MM, stroke=1, fill=1)
        if drawn < length * MM:
            canvas.setStrokeGray(1)
            canvas.setLineWidth(1.5)
            canvas.line(icon_x + drawn / 2 - 1, y - 8, icon_x + drawn / 2 + 1, y - 3)
        canvas.setFillGray(0)
        canvas.setFont(FONT, SIZE)
        canvas.drawString(icon_x + drawn + 3, y - 9, f"{mm_text(length)} mm")
        y -= 12
    if not rods:
        canvas.setFont(FONT, SIZE)
        canvas.drawString(x, y - 9, "No rods")
        y -= 12
    render.glue_drop(canvas, (x + 4, y - 7), 5)
    canvas.setFillGray(0)
    canvas.setFont(FONT, SIZE)
    canvas.drawString(x + 14, y - 9, "Glue the join face" + (" (no rods)" if not rods else ""))


# ── Hole map ───────────────────────────────────────────────────────────────────

def hole_map_wanted(art) -> bool:
    return len(art.pins) >= HOLE_MAP_MIN_RODS


def _screen_turn(art) -> np.ndarray:
    """The 2 x 2 orthogonal matrix that turns the join face's plane coordinates the way the frames show it (mirrored when the
    face is seen from behind), so the map reads like the picture next to it."""
    face = art.face
    s1, s2 = (art.view.R @ face.e1)[:2], (art.view.R @ face.e2)[:2]
    u, _, vt = np.linalg.svd(np.c_[s1, s2])
    return u @ vt


def hole_map_points(art) -> tuple:
    """(rod centres, rod diameters, face outline segments) in the turned plane coordinates (mm)."""
    face = art.face
    turn = _screen_turn(art)
    rods = np.array([[np.dot(p["p"], face.e1), np.dot(p["p"], face.e2)] for p in art.pins]) @ turn.T
    sizes = np.array([float(p["diameter"]) for p in art.pins])
    tris = face.receiver if len(face.receiver) else face.mover
    edges = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    key = np.round(np.sort(edges, axis=1).reshape(-1, 4) * 100).astype(np.int64)
    _, index, count = np.unique(key, axis=0, return_index=True, return_counts=True)
    outline = edges[index[count == 1]] @ turn.T
    return rods, sizes, outline


def draw_hole_map(canvas, art, x: float, top: float, height: float) -> None:
    if art.face is None:
        return
    rods, sizes, outline = hole_map_points(art)
    pts = np.vstack([rods, outline.reshape(-1, 2)]) if len(outline) else rods
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    room_w, room_h = MAP_W, height - SIZE - 6
    span = np.maximum(hi - lo, 1e-6)
    k = min(room_w / span[0], room_h / span[1])
    ox = x + (room_w - span[0] * k) / 2
    oy = top - SIZE - 4 - (room_h - span[1] * k) / 2 - span[1] * k
    to_page = lambda p: (ox + (p[0] - lo[0]) * k, oy + (p[1] - lo[1]) * k)
    canvas.setStrokeGray(0.45)
    canvas.setLineWidth(0.6)
    for a, b in outline:
        canvas.line(*to_page(a), *to_page(b))
    for (rx, ry), diameter in zip(rods, sizes):
        r = max(diameter * k / 2, 1.8)
        cx, cy = to_page((rx, ry))
        render.draw_rod_end(canvas, cx, cy, r)
    canvas.setFillGray(0)
    canvas.setFont(BOLD, SIZE)
    canvas.drawString(x, top - SIZE, f"Hole map: {len(rods)} rods")


# ── Thumbnail ──────────────────────────────────────────────────────────────────

def _thumbnail(scene):
    """The whole model in the finished pose, drawn once per scene and restyled per step."""
    if not hasattr(scene, "_thumbnail"):
        parts = sorted(scene.meshes)
        view = render.View(render.camera_candidates()[rules.THUMB_CAMERA], np.vstack([scene.corners(p) for p in parts]),
                           width=520, aspect=(0.8, 1.6))
        scene._thumbnail = (view, view.render(scene, [(p, p, np.zeros(3)) for p in parts], min_bit_px=rules.PICTURE_MIN_BIT_PX))
    return scene._thumbnail


def draw_thumbnail(canvas, art, x: float, top: float, w: float, h: float) -> None:
    scene = art.extras.get("scene")
    canvas.setFillGray(0)
    canvas.setFont(BOLD, SIZE)
    canvas.drawString(x, top - SIZE, "Where this is")
    if scene is None:
        return
    view, runs = _thumbnail(scene)
    box = render.Box((x, top - h, w, h - SIZE - 3), view)
    moving, section = set(art.mover), set(art.extras.get("section_parts") or ()) | set(art.mover)
    for part in sorted(runs):
        if part not in moving:
            render.stroke_runs(canvas, runs[part], box, *(rules.THUMB_SECTION if part in section else rules.THUMB_REST))
    for part in sorted(moving):
        render.stroke_runs(canvas, runs[part], box, 1.0, rules.BOLD + rules.THUMB_HALO)
        render.stroke_runs(canvas, runs[part], box, 0.0, rules.BOLD)


def draw_strip(canvas, art, left: float, top: float, width: float) -> None:
    """The strip under a step's caption: the "you need" list on the left, the hole map beside it when the join has 5 or more
    rods, and the thumbnail at the right."""
    height = strip_height(art)
    draw_you_need(canvas, art, left, top)
    if hole_map_wanted(art):
        draw_hole_map(canvas, art, left + MAP_X, top, height)
    draw_thumbnail(canvas, art, left + width - THUMB_W, top, THUMB_W, height)
