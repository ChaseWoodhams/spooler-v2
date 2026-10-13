"""The rod page of the Assembly guide: a table of rod sizes, each drawn true size, a 100 mm ruler and the tools list.

    draw_rod_page(flow, bom)        starts a new page on the PageFlow and draws the page (flowing on if it runs long)

Everything on this page is in millimetres at 1:1 (the page title, table and tools text excepted), so a printout can be
checked against a real rod. The ruler bar is a filled rectangle exactly 100 mm wide.
"""

from assembly_bom import CUT_LOSS_MM, STOCK_MM, TOLERANCE_MM, Bom, RodSize, inches, mm_text
from assembly_pdf import BOLD, FONT, MM

RULER_MM = 100
RULER_NOTE = "This bar must measure 100 mm; print at 100% / Actual size. Do not scale the page."
DONT_MIX = "Do not mix sizes: the holes of this size are different."

COLUMNS = (("Size", 0), ("Cut length", 44), ("Rods", 74), ("Spares", 90), ("Total length", 106), ("Stock to buy", 136))   # x in mm


def tools_lines(kind: str) -> list:
    cutting = "A saw or cutting disc to cut the rods" if kind == "rod" else "Scissors or a cutter for the filament"
    return [
        cutting,
        "A file to deburr both ends of every rod",
        "Glue for plastic: a gel cyanoacrylate for a quick hold or a two-part epoxy for strength",
        "Masking tape and a marker for the Section letters",
        "A few rubber bands or clamps",
        "Test-fit every rod before gluing.",
        "Hold each glued join until it grips, then let a Section cure before it is joined to the rest (check your glue's label for times).",
    ]


def material(kind: str, size: RodSize) -> str:
    if kind == "rod":
        return f"Rod stock: steel, brass or carbon fibre, diameter {mm_text(size.diameter)} mm"
    return f"Filament or plastic pin, diameter {mm_text(size.diameter)} mm"


def stock_text(size: RodSize) -> str:
    return f"{size.sticks} x {STOCK_MM:g} mm ({inches(STOCK_MM)})"


def _row(flow, size: RodSize, kind: str) -> None:
    c, left = flow.canvas, flow.content.left
    tall = 4 if size.fallback else 3
    top = flow.reserve(tall * 5 * MM + 2 * MM)
    y = top - 4 * MM
    c.setFont(BOLD, 10)
    c.drawString(left + COLUMNS[0][1] * MM, y, size.label + ("  (fallback)" if size.fallback else ""))
    c.setFont(FONT, 10)
    cells = (f"{mm_text(size.length)} mm +/-{TOLERANCE_MM:g}", str(size.count), str(size.spares),
             f"{mm_text(size.total_length)} mm", None)
    for (name, x), text in zip(COLUMNS[1:], cells):
        if text is not None:
            c.drawString(left + x * MM, y, text)
    c.drawString(left + COLUMNS[5][1] * MM, y, f"{size.sticks} stick" + ("" if size.sticks == 1 else "s"))
    c.setFont(FONT, 9)
    c.drawString(left, y - 5 * MM, material(kind, size) + ".")
    c.drawString(left + COLUMNS[4][1] * MM, y - 5 * MM, f"({inches(size.total_length)})")
    c.drawString(left + COLUMNS[5][1] * MM, y - 5 * MM, f"{stock_text(size)}")
    c.drawString(left, y - 10 * MM,
                 f"Cut {size.to_cut}: {size.count} for the build plus {size.spares} spares. "
                 f"One stick gives {size.per_stick} cuts ({CUT_LOSS_MM:g} mm lost to each cut).")
    if size.fallback:
        steps = ", ".join(size.steps) or "none"
        c.setFont(BOLD, 9)
        c.drawString(left, y - 15 * MM, f"Fallback size, used in step{'' if len(size.steps) == 1 else 's'} {steps}. {DONT_MIX}")
    c.setLineWidth(0.3)
    c.line(left, top - tall * 5 * MM - 2 * MM + 1, flow.content.right, top - tall * 5 * MM - 2 * MM + 1)


def _drawing(flow, size: RodSize) -> None:
    """The rod true size: a side view with its length, and an end-on circle to lay a real rod against."""
    c = flow.canvas
    length, diameter = size.length * MM, size.diameter * MM
    top = flow.reserve(diameter + 12 * MM)
    left = flow.content.left
    mid = top - 8 * MM - diameter / 2
    c.setLineWidth(0.6)
    c.setFillGray(0.85)
    c.rect(left, mid - diameter / 2, length, diameter, stroke=1, fill=1)
    c.setFillGray(0)
    # dimension line above the rod
    y = mid + diameter / 2 + 2.5 * MM
    c.setLineWidth(0.4)
    c.line(left, y, left + length, y)
    for x in (left, left + length):
        c.line(x, y - 1.2 * MM, x, y + 1.2 * MM)
    c.setFont(FONT, 9)
    c.drawString(left + length + 3 * MM, y - 3, f"{mm_text(size.length)} mm")
    # end-on view at true diameter
    cx = left + 80 * MM
    c.setFillGray(0.85)
    c.circle(cx, mid, diameter / 2, stroke=1, fill=1)
    c.setFillGray(0)
    c.drawString(cx + diameter / 2 + 3 * MM, mid - 3, f"{mm_text(size.diameter)} mm across")
    c.drawString(left + 120 * MM, mid - 3, f"{size.label}, true size")


def _ruler(flow) -> None:
    c = flow.canvas
    top = flow.reserve(22 * MM)
    left = flow.content.left
    y = top - 10 * MM
    c.setFillGray(0.2)
    c.rect(left, y, RULER_MM * MM, 3 * MM, stroke=0, fill=1)
    c.setFillGray(0)
    c.setLineWidth(0.4)
    for mark in range(0, RULER_MM + 1, 10):
        c.line(left + mark * MM, y, left + mark * MM, y - (2.5 if mark % 50 == 0 else 1.5) * MM)
    c.setFont(FONT, 9)
    c.drawString(left, y + 5 * MM, RULER_NOTE)
    for mark in (0, 50, 100):
        c.drawCentredString(left + mark * MM, y - 6 * MM, str(mark))


def draw_rod_page(flow, bom: Bom) -> None:
    flow.new_page()
    c, left = flow.canvas, flow.content.left
    word = "Rods" if bom.kind == "rod" else "Pins"
    flow.paragraph(f"{word} to cut", BOLD, 18, gap=2 * MM)
    flow.paragraph(f"Cut every {bom.kind} to its length, good to plus or minus {TOLERANCE_MM:g} mm. "
                   f"Counts include spares; sizes below are drawn true size.", FONT, 10, gap=2 * MM)
    top = flow.reserve(6 * MM)
    c.setFont(BOLD, 9)
    for name, x in COLUMNS:
        c.drawString(left + x * MM, top - 4 * MM, name)
    c.setLineWidth(0.6)
    c.line(left, top - 5 * MM, flow.content.right, top - 5 * MM)
    for size in bom.sizes:
        _row(flow, size, bom.kind)
        _drawing(flow, size)
    flow.y -= 2 * MM
    _ruler(flow)
    flow.paragraph("Tools and consumables", BOLD, 12, gap=1 * MM, keep_with=30)
    for line in tools_lines(bom.kind):
        flow.paragraph(line, FONT, 10, indent=4 * MM, gap=0.5 * MM)
