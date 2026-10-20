"""Cover overview, parts inventory and last page of the Assembly guide.

    draw_overview(canvas, scene, rect, numbers=True)    the finished model, every part number in a bubble
    draw_inventory(flow, data)                          new pages of 15 parts each (3 across, 5 down), in part-number order
    draw_last_page(flow, data)                          new page: the finished model and the "nothing left over" tick
    label_note(label)                                   the cell's "number is here" or "no number" line
    unnumbered_note(scene, parts)                       the note naming the parts with no printed number, with their size

Layout only; the pictures come from assembly_render and the label and plate facts from the split result
(GuideData.labels: part -> saved label record or None, GuideData.plates: part -> plate name).
"""

import numpy as np

import assembly_render
from assembly_pdf import BOLD, FONT, MIN_FONT, MM, fit, wrap

PER_PAGE, COLUMNS, ROWS = 15, 3, 5
NO_NUMBER = "no number: identify by shape"
NOTHING_LEFT = "No parts or rods left over."
TITLE_H = 12 * MM
NOT_ON_A_PLATE = "not on a plate yet"
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


def part_picture(scene, part: int, label):
    """(Picture, world point of the label or None): the part seen from the side its number is on when it has one."""
    camera = assembly_render.camera_candidates()[0]
    spot = None
    if label and label.get("normal") and label.get("position"):
        camera = assembly_render.unit(2.0 * np.asarray(label["normal"], float) + camera)
        spot = np.asarray(label["position"], float)
    view = assembly_render.View(camera, scene.corners(part), width=500, aspect=(0.8, 1.25))
    return assembly_render.Picture(view, view.render(scene, [(part, part, np.zeros(3))], min_bit_px=12)), spot


def _cell(canvas, scene, part: int, label, plate, show_plates: bool, left: float, top: float, width: float, height: float) -> None:
    pad = 2 * MM
    canvas.setStrokeGray(0.6)
    canvas.setLineWidth(0.5)
    canvas.rect(left, top - height, width, height, stroke=1, fill=0)
    side = min(height - 2 * pad, width * 0.52)
    picture, spot = part_picture(scene, part, label)
    rect = (left + pad, top - pad - side, side, side)
    assembly_render.draw_picture(canvas, picture, *rect)
    if spot is not None:
        mark = assembly_render.Box(rect, picture.view).world(spot)[0]
        canvas.setStrokeGray(0)
        canvas.setLineWidth(1.1)
        canvas.circle(mark[0], mark[1], 3.2, stroke=1, fill=0)
        canvas.circle(mark[0], mark[1], 0.8, stroke=0, fill=1)
    text_left = left + pad + side + 2 * MM
    room = left + width - pad - text_left
    canvas.setFillGray(0)
    canvas.setFont(BOLD, 30)
    canvas.drawString(text_left, top - pad - 24, str(part))
    canvas.setFont(FONT, 10)
    if show_plates:
        size = 10 if plate else MIN_FONT
        canvas.setFont(FONT, size)
        canvas.drawString(text_left, top - pad - 38, fit(f"Plate: {plate}" if plate else NOT_ON_A_PLATE, FONT, size, room))
        canvas.setFont(FONT, 10)
    canvas.setStrokeGray(0)
    canvas.setLineWidth(0.9)
    canvas.rect(text_left, top - pad - 56, 4 * MM, 4 * MM, stroke=1, fill=0)    # tick box
    canvas.setFont(FONT, MIN_FONT)
    canvas.drawString(text_left + 5.5 * MM, top - pad - 53, "have it")
    for n, line in enumerate(wrap(label_note(label), FONT, MIN_FONT, left + width - pad - (left + pad))[:2]):
        canvas.drawString(left + pad, top - pad - side - 11 - n * 11, line)


def draw_inventory(flow, data) -> None:
    numbers = sorted(data.scene.meshes)
    labels, plates = data.labels or {}, data.plates or {}
    show_plates = any(plates.get(p) for p in numbers)
    note = unnumbered_note(data.scene, [p for p in data.plan.get("unlabelled") or [] if p in data.scene.meshes])
    pages = max(1, -(-len(numbers) // PER_PAGE))
    width = flow.content.width / COLUMNS
    for page in range(pages):
        flow.new_page()
        top = flow.reserve(flow.content.height)
        canvas = flow.canvas
        canvas.setFillGray(0)
        canvas.setFont(BOLD, 16)
        canvas.drawString(flow.content.left, top - 8 * MM,
                          "Parts inventory" + (f" ({page + 1} of {pages})" if pages > 1 else ""))
        canvas.setFont(FONT, 10)
        canvas.drawString(flow.content.left + 62 * MM, top - 8 * MM, "Tick each part off when you have found it.")
        lines = [LEGEND] + (wrap(note, FONT, MIN_FONT, flow.content.width) if note and page == 0 else [])
        canvas.setFont(FONT, MIN_FONT)
        for n, line in enumerate(lines):
            canvas.drawString(flow.content.left, top - TITLE_H - 3 - n * LINE_H, line)
        grid_top = top - TITLE_H - len(lines) * LINE_H - 2 * MM
        height = (grid_top - flow.content.bottom) / ROWS
        for slot, part in enumerate(numbers[page * PER_PAGE:(page + 1) * PER_PAGE]):
            row, column = divmod(slot, COLUMNS)
            _cell(canvas, data.scene, part, labels.get(part), plates.get(part), show_plates,
                  flow.content.left + column * width, grid_top - row * height, width, height)


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
