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
        out.append((start, end, max(diameter * box.pt_per_mm, 3.0) + 1.4))
    return out


def zoom_circle(art, frame):
    """Where the zoom circle goes in frame 1: the position with the least drawing under it among those that keep clear of
    every rod. Returns ((x, y), radius, magnification) or None (no rods, or no room)."""
    rods = rod_segments(art, frame)
    if not rods:
        return None
    ends = np.vstack([np.vstack([s, e]) for s, e, _ in rods])
    focus = ends.mean(axis=0)
    spread = float(np.linalg.norm(ends - focus, axis=1).max())
    magnification = float(np.floor(np.clip(rules.ZOOM_FILL * zoom_radius() / max(spread, rules.ZOOM_MIN_SPREAD_PT), *rules.ZOOM_RANGE) * 2) / 2)
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


def draw_zoom(canvas, art, frame) -> None:
    zoom = zoom_circle(art, frame)
    if zoom is None:
        return
    (cx, cy), r, k = zoom
    box = render.Box(frame, art.view)
    rods = render._rods(art, box)
    ends = np.vstack([box.world(np.array([a, b])) for a, b, _ in rods])
    focus = ends.mean(axis=0)
    toward = focus - np.array([cx, cy])
    gap = float(np.linalg.norm(toward))
    if gap > r + 4:                                       # a thin leader from the circle to the rods it magnifies
        edge = np.array([cx, cy]) + toward / gap * r
        canvas.setStrokeGray(rules.ZOOM_LEADER[0])
        canvas.setLineWidth(rules.ZOOM_LEADER[1])
        canvas.line(*edge, *(focus - toward / gap * 2))
    canvas.saveState()
    clip = canvas.beginPath()
    clip.circle(cx, cy, r)
    canvas.clipPath(clip, stroke=0, fill=0)
    canvas.setFillGray(1)
    canvas.circle(cx, cy, r, stroke=0, fill=1)
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
    render._draw_rods(canvas, box, rods)
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
        canvas.setFillGray(1)
        canvas.circle(cx, cy, r + 1.3, stroke=0, fill=1)
        canvas.setFillGray(0)
        canvas.circle(cx, cy, r, stroke=0, fill=1)
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
