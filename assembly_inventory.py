"""Cover overview, parts inventory and last page of the Assembly guide.

    draw_overview(canvas, scene, rect, numbers=True)    the finished model, every part number in a bubble
    draw_inventory(flow, data)                          new pages of cards, 3 across, in part-number order (long thin parts take taller rows)
    draw_last_page(flow, data)                          new page: the finished model and the "nothing left over" tick
    card_views(scene, part), card_is_tall(scene, part)   the pictures on a card, and whether its cell is taller
    label_note(label)                                   the cell's "number is here" or "no number" line
    unnumbered_note(scene, parts)                       the note naming the parts with no printed number, with their size

Layout only; the pictures come from assembly_render and the label and plate facts from the split result
(GuideData.labels: part -> saved label record or None, GuideData.plates: part -> plate name).
"""

import numpy as np

import assembly_render
import assembly_rules
from assembly_pdf import BOLD, FONT, MIN_FONT, MM, fit, wrap

PER_PAGE, COLUMNS, ROWS = 15, 3, 5
NO_NUMBER = "no number: identify by shape"
NOTHING_LEFT = "No parts or rods left over."
TITLE_H = 12 * MM
NOT_ON_A_PLATE = "not on a plate yet"
STRIP_CAPTION = "All parts at one scale:"
LEGEND = "Small circle on a picture: where the printed number is."
LINE_H = 11


def label_note(label) -> str:
    """What the inventory says about a part's printed number."""
    if not label:
        return NO_NUMBER
    if label.get("face") == "bed":
        return "number is here (the face that printed on the bed)"
    if label.get("neighbour") is not None:
        return f"number is here (face against part {label['neighbour']})"
    return "number is here"


def largest_mm(scene, part: int) -> int:
    """The part's largest overall dimension in millimetres, rounded."""
    return int(round(float(np.ptp(scene.meshes[part].bounds, axis=0).max())))


def unnumbered_note(scene, parts) -> str:
    """The note about parts with no printed number: each with its size, so it can be found by measuring. Empty when none."""
    parts = sorted(parts)
    if not parts:
        return ""
    who = ", ".join(f"Part {p} ({largest_mm(scene, p)} mm)" for p in parts)
    return f"No number printed on: {who}. Tell them apart by size and shape."


def _bubble(canvas, x: float, y: float, text: str, radius: float = 8.5) -> None:
    radius = max(radius, 4.2 + 2.6 * len(text))
    canvas.setStrokeGray(0)
    canvas.setFillGray(1)
    canvas.setLineWidth(1.0)
    canvas.circle(x, y, radius, stroke=1, fill=1)
    canvas.setFillGray(0)
    canvas.setFont(BOLD, MIN_FONT)
    canvas.drawCentredString(x, y - 3.2, text)


def _spread(points: list, radius: float, rect) -> list:
    """Nudge bubble centres apart (deterministically) so no two overlap and all stay inside `rect`."""
    x0, y0, w, h = rect
    placed = []
    for x, y in points:
        best = (x, y)
        for ring in range(0, 12):
            for step in range(0 if ring == 0 else 8):
                angle = step * np.pi / 4
                cx, cy = x + ring * radius * np.cos(angle), y + ring * radius * np.sin(angle)
                cx, cy = min(max(cx, x0 + radius), x0 + w - radius), min(max(cy, y0 + radius), y0 + h - radius)
                if all(np.hypot(cx - px, cy - py) >= 2 * radius for px, py in placed):
                    best = (cx, cy)
                    break
            else:
                continue
            break
        placed.append(best)
    return placed


def draw_overview(canvas, scene, rect, numbers: bool = True) -> None:
    """The finished model in its assembled pose inside `rect` (x, y, w, h in points), with a bubble per part number."""
    parts = sorted(scene.meshes)
    picture = assembly_render.section_picture(scene, parts, width=900)
    x, y, w, h = rect
    assembly_render.draw_picture(canvas, picture, x, y, w, h)
    if not numbers:
        return
    box = assembly_render.Box(rect, picture.view)
    centres = [box.world(scene.meshes[p].vertices.mean(axis=0))[0] for p in parts]
    for part, (cx, cy) in zip(parts, _spread([tuple(c) for c in centres], 9, rect)):
        _bubble(canvas, cx, cy, str(part))


def part_picture(scene, part: int, label, second: bool = False):
    """(Picture, world point of the label or None): the part seen from the side its number is on when it has one.
    `second` is the other angle given to a thin part: from the opposite corner, and without the label mark."""
    camera = assembly_render.camera_candidates()[3 if second else 0]
    spot = None
    if label and not second and label.get("normal") and label.get("position"):
        camera = assembly_render.unit(2.0 * np.asarray(label["normal"], float) + camera)
        spot = np.asarray(label["position"], float)
    view = assembly_render.View(camera, scene.corners(part), width=500, aspect=(1.0, 3.0) if second else (0.8, 1.25))
    return assembly_render.Picture(view, view.render(scene, [(part, part, np.zeros(3))], min_bit_px=assembly_rules.PICTURE_MIN_BIT_PX)), spot


def _extents(scene, part: int) -> list:
    return sorted(float(e) for e in np.ptp(scene.meshes[part].bounds, axis=0))


def card_views(scene, part: int) -> int:
    """How many pictures the card carries: a second, different angle for a part narrower than the width threshold."""
    return 2 if _extents(scene, part)[1] < assembly_rules.THIN_WIDTH_MM else 1


def card_is_tall(scene, part: int) -> bool:
    """Whether the card needs a taller cell: a thin part, or one much longer than it is wide."""
    _, middle, longest = _extents(scene, part)
    return card_views(scene, part) > 1 or longest >= assembly_rules.TALL_ASPECT * max(middle, 1e-6)


def _strip(canvas, scene, numbers, left: float, top: float, width: float) -> None:
    """Every part in one row at one common scale: each is fitted to the same cube, so the pictures are comparable."""
    side = min(assembly_rules.STRIP_MM * MM, width / len(numbers))
    reach = 0.5 * max(largest_mm(scene, p) for p in numbers)
    camera = assembly_render.camera_candidates()[0]
    for slot, part in enumerate(numbers):
        centre = scene.meshes[part].bounds.mean(axis=0)
        cube = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]) * reach + centre
        view = assembly_render.View(camera, cube, width=240, aspect=(1.0, 1.0))
        picture = assembly_render.Picture(view, view.render(scene, [(part, part, np.zeros(3))], min_bit_px=assembly_rules.PICTURE_MIN_BIT_PX))
        assembly_render.draw_picture(canvas, picture, left + slot * side, top - side, side, side)


def _cell(canvas, scene, part: int, label, plate, show_plates: bool, left: float, top: float, width: float, height: float) -> None:
    pad = 2 * MM
    canvas.setStrokeGray(assembly_rules.CARD_BORDER[0])
    canvas.setLineWidth(assembly_rules.CARD_BORDER[1])
    canvas.rect(left, top - height, width, height, stroke=1, fill=0)
    tall, views = card_is_tall(scene, part), card_views(scene, part)
    # An ordinary card has its picture on the left and its facts on the right; a tall card has its pictures across the top
    # and its facts in a band under them.
    if tall:
        pic_w, pic_h = width - 2 * pad, height - 2 * pad - 3 * LINE_H - 7 * MM
        text_left, text_top = left + pad + 12 * MM, top - pad - pic_h
        number_at = (left + pad, text_top - 30)
        note_top = text_top - 46
        rows = (10, 24, 36)
    else:
        pic_w = pic_h = min(height - 2 * pad, width * 0.52)
        text_left, text_top = left + pad + pic_w + 2 * MM, top - pad
        number_at = (text_left, text_top - 24)
        note_top = top - pad - pic_h - 11
        rows = (38, 56, 68)
    room = left + width - pad - text_left
    each = pic_h / views
    for n in range(views):
        picture, spot = part_picture(scene, part, label, second=bool(n))
        rect = (left + pad, top - pad - (n + 1) * each, pic_w, each)
        assembly_render.draw_picture(canvas, picture, *rect)
        if spot is not None:
            mark = assembly_render.Box(rect, picture.view).world(spot)[0]
            canvas.setStrokeGray(0)
            canvas.setLineWidth(1.1)
            canvas.circle(mark[0], mark[1], 3.2, stroke=1, fill=0)
            canvas.circle(mark[0], mark[1], 0.8, stroke=0, fill=1)
    canvas.setFillGray(0)
    canvas.setFont(BOLD, 30)
    canvas.drawString(*number_at, str(part))
    plate_row, tick_row, size_row = rows
    if show_plates:
        size = 10 if plate else MIN_FONT
        canvas.setFont(FONT, size)
        canvas.drawString(text_left, text_top - plate_row, fit(f"Plate: {plate}" if plate else NOT_ON_A_PLATE, FONT, size, room))
    canvas.setStrokeGray(0)
    canvas.setLineWidth(0.9)
    canvas.rect(text_left, text_top - tick_row, 4 * MM, 4 * MM, stroke=1, fill=0)    # tick box
    canvas.setFont(FONT, MIN_FONT)
    canvas.drawString(text_left + 5.5 * MM, text_top - tick_row + 3, "have it")
    canvas.drawString(text_left, text_top - size_row, fit(f"Largest: {largest_mm(scene, part)} mm", FONT, MIN_FONT, room))
    for n, line in enumerate(wrap(label_note(label), FONT, MIN_FONT, width - 2 * pad)[:2]):
        canvas.drawString(left + pad, note_top - n * LINE_H, line)


def _layout(scene, numbers, first_lines, later_lines, strip: float, flow, top: float) -> list:
    """The pages of the inventory: [(lines, grid_top, [(parts, height)])]. Rows of three in part-number order; a row is as
    tall as its tallest card, and a page takes rows until the next would not fit, so a card is never split."""
    rows = [numbers[i:i + COLUMNS] for i in range(0, len(numbers), COLUMNS)]
    pages = []
    while rows or not pages:
        first = not pages
        lines = first_lines if first else later_lines
        grid_top = top - TITLE_H - len(lines) * LINE_H - 2 * MM - (strip if first else 0)
        room = grid_top - flow.content.bottom
        base = room / ROWS
        taken, used = [], 0.0
        while rows:
            tall = any(card_is_tall(scene, p) for p in rows[0])
            height = min(base * (assembly_rules.TALL_FACTOR if tall else 1.0), room)
            if taken and used + height > room + 0.01:
                break
            taken.append((rows.pop(0), height))
            used += height
        pages.append((lines, grid_top, taken))
    return pages


def draw_inventory(flow, data) -> None:
    numbers = sorted(data.scene.meshes)
    labels, plates = data.labels or {}, data.plates or {}
    show_plates = any(plates.get(p) for p in numbers)
    note = unnumbered_note(data.scene, [p for p in data.plan.get("unlabelled") or [] if p in data.scene.meshes])
    width = flow.content.width / COLUMNS
    first_lines = [LEGEND] + (wrap(note, FONT, MIN_FONT, flow.content.width) if note else []) + [STRIP_CAPTION]
    strip = assembly_rules.STRIP_MM * MM + 2 * MM
    pages = _layout(data.scene, numbers, first_lines, [LEGEND], strip, flow, flow.content.bottom + flow.content.height)
    for page, (lines, grid_top, rows) in enumerate(pages):
        flow.new_page()
        top = flow.reserve(flow.content.height)
        canvas = flow.canvas
        canvas.setFillGray(0)
        canvas.setFont(BOLD, 16)
        canvas.drawString(flow.content.left, top - 8 * MM,
                          "Parts inventory" + (f" ({page + 1} of {len(pages)})" if len(pages) > 1 else ""))
        canvas.setFont(FONT, 10)
        canvas.drawString(flow.content.left + 62 * MM, top - 8 * MM, "Tick each part off when you have found it.")
        canvas.setFont(FONT, MIN_FONT)
        for n, line in enumerate(lines):
            canvas.drawString(flow.content.left, top - TITLE_H - 3 - n * LINE_H, line)
        if page == 0:
            _strip(canvas, data.scene, numbers, flow.content.left, top - TITLE_H - len(lines) * LINE_H, flow.content.width)
        y = grid_top
        for parts, height in rows:
            for column, part in enumerate(parts):
                _cell(canvas, data.scene, part, labels.get(part), plates.get(part), show_plates,
                      flow.content.left + column * width, y, width, height)
            y -= height


def draw_last_page(flow, data) -> None:
    flow.new_page()
    top = flow.reserve(flow.content.height)
    canvas, left = flow.canvas, flow.content.left
    canvas.setFillGray(0)
    canvas.setFont(BOLD, 24)
    canvas.drawString(left, top - 10 * MM, "Finished")
    draw_overview(canvas, data.scene, (left, top - 215 * MM, flow.content.width, 195 * MM), numbers=False)
    canvas.setLineWidth(1.4)
    canvas.rect(left, top - 235 * MM, 7 * MM, 7 * MM, stroke=1, fill=0)
    canvas.setFont(BOLD, 14)
    canvas.drawString(left + 11 * MM, top - 233 * MM, NOTHING_LEFT)
