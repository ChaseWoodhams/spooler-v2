"""The front and back of the Assembly guide: cover overview, Section tree and the finished page's two views.

    section_tree(plan)                          the Sections as nodes {letter, main, parts, depth, parent, step, rods}, main first; [] for one Section
    draw_tree(flow, plan)                       the Section tree pages, laid out through the page flow (nothing for one Section)
    section_colours(plan)                       {part: (r, g, b)}: each part in the colour of the Section that holds it
    overview_art(scene, camera, colours, plan)  the finished model's lines: creases lightly, part boundaries heavier
    draw_overview(canvas, scene, rect, ...)     the model in a rectangle; with a plan, coloured by Section and with badges
    finished_cameras(scene)                     the two cameras of the finished page: the cover's, then the largest unseen face
    build_minutes(plan), duration_text(m)       the rough build-time estimate shown on the cover
    place_badges(points, radius, rect)          [(anchor, centre)]: badge places that do not overlap

Layout only; the drawing rules (colours, weights, allowances) are in assembly_rules.
"""

from dataclasses import dataclass

import numpy as np

import assembly_render as render
import assembly_rules as rules
from assembly_pdf import BOLD, FONT, MIN_FONT, MM, parts_text, plural

AXES = [np.array(v, float) for v in ((1, 0, 0), (0, 1, 0), (0, 0, 1), (-1, 0, 0), (0, -1, 0), (0, 0, -1))]
TREE_ROW_H = 17 * MM
TREE_HEAD_H = 14 * MM
TREE_INDENT = 14 * MM
CHIP_R = 5.5 * MM


# ── Section tree ───────────────────────────────────────────────────────────────

def own_parts(section: dict, plan: dict) -> list:
    """The parts a Section holds itself: its parts less the ones that came in with the Sections joined into it."""
    joined = {p for step in section["steps"] if step["kind"] == "section" for p in step["parts"]}
    return sorted(set(section["parts"]) - joined)


def section_tree(plan: dict) -> list:
    """The Sections as a tree from the main Section down, depth first, each Section's children in the order its steps join
    them. A Section that no step joins hangs from the main Section. Empty when the split has a single Section."""
    sections = plan["sections"]
    if len(sections) < 2:
        return []
    by_letter = {s["letter"]: s for s in sections}
    main = next((s for s in sections if s["main"]), sections[-1])
    children = {s["letter"]: [] for s in sections}
    joins_at = {}
    for section in sections:
        for step in section["steps"]:
            if step["kind"] == "section" and step["section"] in by_letter and step["section"] not in joins_at:
                children[section["letter"]].append(step["section"])
                joins_at[step["section"]] = (section["letter"], step["id"])
    for section in sections:
        if section is not main and section["letter"] not in joins_at:
            children[main["letter"]].append(section["letter"])
            joins_at[section["letter"]] = (main["letter"], None)
    nodes = []

    def walk(letter: str, depth: int) -> None:
        section = by_letter[letter]
        parent, step = joins_at.get(letter, (None, None))
        nodes.append({"letter": letter, "main": section["main"], "parts": own_parts(section, plan), "depth": depth,
                      "parent": parent, "step": step, "rods": sum(s["rods"] for s in section["steps"])})
        for child in children[letter]:
            walk(child, depth + 1)
    walk(main["letter"], 0)
    return nodes


def colour_of(letter: str, plan: dict) -> tuple:
    letters = sorted(s["letter"] for s in plan["sections"])
    return rules.SECTION_COLOURS[letters.index(letter) % len(rules.SECTION_COLOURS)]


def section_colours(plan: dict) -> dict:
    """{part: colour} with each part in the colour of the Section that holds it."""
    out = {}
    for section in plan["sections"]:
        for part in own_parts(section, plan):
            out[part] = colour_of(section["letter"], plan)
    return out


def part_sections(plan: dict) -> dict:
    return {part: section["letter"] for section in plan["sections"] for part in own_parts(section, plan)}


def _heading(flow, text: str, intro: str = "") -> None:
    top = flow.reserve(TREE_HEAD_H)
    c = flow.canvas
    c.setFillGray(0)
    c.setFont(BOLD, 16)
    c.drawString(flow.content.left, top - 8 * MM, text)
    if intro:
        c.setFont(FONT, MIN_FONT)
        c.drawString(flow.content.left, top - 12.5 * MM, intro)


def draw_tree(flow, plan: dict) -> None:
    """The Section tree on new pages: a row per Section with its colour and letter, its parts and the step where it joins its
    parent, indented under that parent. Rows go through the flow, so a long tree takes more pages."""
    nodes = section_tree(plan)
    if not nodes:
        return
    c = flow.canvas
    flow.new_page(carry=False)
    _heading(flow, "Section tree", "Each Section is built on its own, then joined to the Section above it at the step shown.")
    rows, page = {}, flow.pages
    for node in nodes:
        if flow.y - TREE_ROW_H < flow.content.bottom:
            flow.new_page(carry=False)
            _heading(flow, "Section tree (continued)")
            page = flow.pages
        top = flow.reserve(TREE_ROW_H)
        x = flow.content.left + node["depth"] * TREE_INDENT
        mid = top - TREE_ROW_H / 2
        if node["parent"] is not None:
            parent_top, parent_page = rows.get(node["parent"], (flow.content.top, page))
            stem = x - TREE_INDENT / 2
            c.setStrokeGray(0.45)
            c.setLineWidth(1)
            c.line(stem, mid, x - 1 * MM, mid)
            c.line(stem, mid, stem, parent_top - TREE_ROW_H / 2 - CHIP_R if parent_page == page else flow.content.top)
        rows[node["letter"]] = (top, page)
        colour = colour_of(node["letter"], plan)
        c.setFillColorRGB(*colour)
        c.circle(x + CHIP_R, mid, CHIP_R, stroke=0, fill=1)
        c.setFillGray(1)
        c.setFont(BOLD, 14)
        c.drawCentredString(x + CHIP_R, mid - 5, node["letter"])
        c.setFillGray(0)
        text_x = x + 2 * CHIP_R + 3 * MM
        c.setFont(BOLD, 12)
        c.drawString(text_x, mid + 4, f"Section {node['letter']}" + (" (main)" if node["main"] else ""))
        c.setFont(FONT, 10)
        rods = f", {plural(node['rods'], 'rod')}" if node["rods"] else ""
        c.drawString(text_x, mid - 8, f"Holds {parts_text(node['parts'])}{rods}")
        c.setFont(FONT, 10)
        if node["parent"] is None:
            c.drawString(text_x + 90 * MM, mid + 4, "The other Sections join it.")
        else:
            at = f" at step {node['step']}" if node["step"] else ""
            c.drawString(text_x + 90 * MM, mid + 4, f"It joins Section {node['parent']}{at}.")


# ── Rough build time ───────────────────────────────────────────────────────────

def build_minutes(plan: dict) -> int:
    """A rough build time: a fixed allowance for each step and for letting each side Section cure."""
    sides = sum(1 for section in plan["sections"] if not section["main"])
    return plan["summary"]["steps"] * rules.BUILD_MIN_PER_STEP + sides * rules.CURE_MIN_PER_SIDE_SECTION


def duration_text(minutes: int) -> str:
    minutes = int(round(minutes / 5.0)) * 5
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h" + (f" {rest} min" if rest else "")


# ── Badges ─────────────────────────────────────────────────────────────────────

def place_badges(points: list, radius: float, rect) -> list:
    """[(anchor, centre)] for badges of `radius` wanted at `points` inside `rect` (x, y, w, h): each badge keeps its point
    when it can, else moves (deterministically, in rings) to the nearest room that overlaps no earlier badge."""
    x0, y0, w, h = rect
    placed = []
    for point in points:
        x, y = float(point[0]), float(point[1])
        best = (x, y)
        found = False
        for ring in range(0, 40):
            for step in range(1 if ring == 0 else 12):
                angle = step * np.pi / 6
                cx, cy = x + ring * radius * 0.5 * np.cos(angle), y + ring * radius * 0.5 * np.sin(angle)
                cx, cy = min(max(cx, x0 + radius), x0 + w - radius), min(max(cy, y0 + radius), y0 + h - radius)
                if all(np.hypot(cx - px, cy - py) >= 2 * radius for _, (px, py) in placed):
                    best, found = (cx, cy), True
                    break
            if found:
                break
        placed.append(((x, y), best))
    return placed


def needs_leader(anchor, centre, radius: float) -> bool:
    return bool(np.hypot(centre[0] - anchor[0], centre[1] - anchor[1]) > rules.BADGE_LEADER_MIN * radius)


# ── Cameras of the finished model ──────────────────────────────────────────────

def exposed_area(scene, direction) -> float:
    """The area of the model's faces that face `direction` and can be seen from it (faces inside joins are hidden)."""
    parts = sorted(scene.meshes)
    view = render.View(direction, np.vstack([scene.corners(p) for p in parts]), width=rules.EXPOSED_PX)
    depth = np.full((view.H, view.W), -np.inf, np.float32)
    for part in parts:
        mesh = scene.meshes[part]
        render.raster_depth(view.project(mesh.vertices), mesh.faces, view.W, view.H, depth)
    total = 0.0
    for part in parts:
        mesh = scene.meshes[part]
        facing = mesh.face_normals @ direction > rules.FACE_ALIGN
        if not facing.any():
            continue
        spot = view.project(mesh.triangles[facing].mean(axis=1))
        xi, yi = np.floor(spot[:, 0]).astype(int), np.floor(spot[:, 1]).astype(int)
        inside = (xi >= 0) & (xi < view.W) & (yi >= 0) & (yi < view.H)
        seen = inside & (spot[:, 2] >= depth[yi.clip(0, view.H - 1), xi.clip(0, view.W - 1)] - view.bias)
        total += float(mesh.area_faces[facing][seen].sum())
    return total


def finished_cameras(scene) -> tuple:
    """(first, second): the cover's usual diagonal view, then a diagonal view onto the largest face the first one cannot see
    (the seating surface when it faces that way), leaning a little toward the sides the first view sees."""
    first = render.camera_candidates()[0]
    unseen = [d for d in AXES if float(d @ first) <= rules.SEES_FACING]
    best = max(unseen, key=lambda d: exposed_area(scene, d))
    lean = sum((np.sign(first[i]) * AXES[i] for i in range(3) if abs(best[i]) < 0.5), np.zeros(3))
    return first, render.unit(best + rules.UNSEEN_TILT * lean)


# ── The model's lines ──────────────────────────────────────────────────────────

@dataclass
class OverviewStroke:
    part: int
    kind: str                   # "crease" or "join" (a part boundary)
    colour: "tuple | None"      # (r, g, b), or None to draw in `grey`
    grey: float
    weight: float
    runs: list


@dataclass
class OverviewArt:
    view: render.View
    strokes: list


def boundary_lines(scene, parts, plan: "dict | None" = None) -> dict:
    """{part: world lines (n, 2, 3)}: the edges where parts meet. Found on the faces of touching parts along the axes, and along
    the direction of any step that joins them."""
    parts = set(parts)
    steps = [step for section in (plan or {}).get("sections", []) for step in section["steps"]]
    lines = {}
    for pair in sorted(scene.touching):
        a, b = pair
        if a not in parts or b not in parts:
            continue
        tries = [(a, b, u) for u in AXES] + [(b, a, u) for u in AXES]
        for step in steps:
            u = render.unit(step["direction"])
            if a in step["parts"] and b in step["receiver"]:
                tries.append((a, b, u))
            if b in step["parts"] and a in step["receiver"]:
                tries.append((b, a, u))
        for lead, back, u in tries:
            face = render.contact_face(scene, [lead], [back], u)
            if face is None:
                continue
            for part, against in ((lead, face.receiver), (back, face.mover)):
                found = scene.join_edges(part, face, against, u)
                if len(found):
                    lines.setdefault(part, []).append(found)
    return {part: np.concatenate(found) for part, found in lines.items()}


def overview_art(scene, camera, colours: dict, plan: "dict | None" = None, width: "int | None" = None) -> OverviewArt:
    """The finished model seen from `camera`: every part's lines at the picture weight, the part boundaries on top of them one
    or more weight steps heavier. `colours` is {part: (r, g, b)} (a part not in it is drawn in grey)."""
    parts = sorted(scene.meshes)
    view = render.View(camera, np.vstack([scene.corners(p) for p in parts]), width=width or rules.OVERVIEW_PX, aspect=(0.7, 1.4))
    joins = {("j", p): found for p, found in boundary_lines(scene, parts, plan).items()}
    items = [(p, p, np.zeros(3)) for p in parts] + [(key, key[1], np.zeros(3)) for key in joins]
    runs = view.render(scene, items, min_bit_px=rules.PICTURE_MIN_BIT_PX, lines=joins)
    grey, weight = rules.PICTURE
    heavy = weight + rules.OVERVIEW_BOUNDARY_STEPS * rules.JOIN_EDGE_STEP
    strokes = [OverviewStroke(p, "crease", colours.get(p), grey, weight, runs[p]) for p in parts]
    strokes += [OverviewStroke(key[1], "join", colours.get(key[1]), rules.OVERVIEW_BOUNDARY_GREY, heavy, runs[key]) for key in joins]
    return OverviewArt(view, strokes)


def _draw_strokes(canvas, art: OverviewArt, box) -> None:
    for stroke in art.strokes:
        if not stroke.runs:
            continue
        if stroke.colour:
            canvas.setStrokeColorRGB(*stroke.colour)
        else:
            canvas.setStrokeGray(stroke.grey)
        canvas.setLineWidth(stroke.weight)
        canvas.setLineCap(1)
        path = canvas.beginPath()
        for a, b in stroke.runs:
            start, end = box.px(np.array([a, b]))
            path.moveTo(*start)
            path.lineTo(*end)
        canvas.drawPath(path, stroke=1, fill=0)


def _badge(canvas, centre, anchor, text: str, colour, radius: float, filled: bool) -> None:
    ring = tuple(colour) if colour else (0, 0, 0)
    if needs_leader(anchor, centre, radius):
        canvas.setStrokeColorRGB(*ring)
        canvas.setLineWidth(rules.LEADER_WEIGHT)
        canvas.line(anchor[0], anchor[1], centre[0], centre[1])
        canvas.setFillColorRGB(*ring)
        canvas.circle(anchor[0], anchor[1], 1.4, stroke=0, fill=1)
    canvas.setStrokeColorRGB(*ring)
    canvas.setLineWidth(1.6 if colour else 1.0)
    canvas.setFillColorRGB(*ring) if filled else canvas.setFillGray(1)
    canvas.circle(centre[0], centre[1], radius, stroke=1, fill=1)
    canvas.setFillGray(1 if filled else 0)
    canvas.setFont(BOLD, MIN_FONT)
    canvas.drawCentredString(centre[0], centre[1] - 3.2, text)


def draw_overview(canvas, scene, rect, numbers: bool = True, camera=None, plan: "dict | None" = None, colours: bool = False) -> None:
    """The finished model inside `rect` (x, y, w, h in points). With `numbers`, a badge per part number; with `colours` (and a
    plan), each Section in its own colour with its letter on a filled badge. Crowded badges move apart on leader lines."""
    camera = render.camera_candidates()[0] if camera is None else camera
    tint = section_colours(plan) if colours and plan else {}
    art = overview_art(scene, camera, tint, plan)
    box = render.Box(rect, art.view)
    _draw_strokes(canvas, art, box)
    parts = sorted(scene.meshes)
    wanted = []        # (text, colour, filled, world point)
    if numbers:
        wanted += [(str(p), tint.get(p), False, scene.meshes[p].vertices.mean(axis=0)) for p in parts]
    if tint:
        for section in plan["sections"]:
            own = own_parts(section, plan)
            if own:
                wanted.append((section["letter"], colour_of(section["letter"], plan), True,
                               np.mean([scene.meshes[p].vertices.mean(axis=0) for p in own], axis=0)))
    if not wanted:
        return
    anchors = [tuple(box.world(point)[0]) for _, _, _, point in wanted]
    radius = rules.BADGE_OVERVIEW_RADIUS
    placed = place_badges(anchors, radius, rect)
    for (text, colour, filled, _), (anchor, centre) in zip(wanted, placed):
        _badge(canvas, centre, anchor, text, colour, radius, filled)


def draw_legend(canvas, plan: dict, x: float, y: float, width: float) -> float:
    """A row of colour chips with each Section's letter, wrapped to `width`; `y` is its top. Returns the height used."""
    line_h, cursor, row = 7 * MM, x, 0
    for section in sorted(plan["sections"], key=lambda s: s["letter"]):
        label = f"Section {section['letter']}" + (" (main)" if section["main"] else "")
        need = 2 * 3.2 * MM + 2 * MM + len(label) * 5.2 + 5 * MM
        if cursor + need > x + width and cursor > x:
            cursor, row = x, row + 1
        mid = y - row * line_h - line_h / 2
        canvas.setFillColorRGB(*colour_of(section["letter"], plan))
        canvas.circle(cursor + 3.2 * MM, mid, 3.2 * MM, stroke=0, fill=1)
        canvas.setFillGray(1)
        canvas.setFont(BOLD, MIN_FONT)
        canvas.drawCentredString(cursor + 3.2 * MM, mid - 3.2, section["letter"])
        canvas.setFillGray(0)
        canvas.setFont(FONT, 10)
        canvas.drawString(cursor + 7.5 * MM, mid - 3.5, label)
        cursor += need
    return (row + 1) * line_h
