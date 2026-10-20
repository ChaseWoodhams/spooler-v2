"""Page layout of the Assembly guide PDF.

This module only lays pages out; the plan comes from assembly_plan.py and the job around it lives in splits.py.
Later parts of the guide (rod page, parts inventory, drawn step pages) are added by drawing onto a `PageFlow`:

    flow.new_page()                 start a fresh page (after the cover, every page gets the footer)
    top = flow.reserve(h, section="B")  room for a block `h` points tall inside the safe area; starts a new page when
                                    it does not fit, and returns the block's top y (draw downwards from it). Passing
                                    `section` says a block of that Section starts here: the footer names the Sections
                                    on the page
    flow.paragraph(text, ...)       wrapped text that flows across pages (also takes `section`)
    flow.area                       the safe area (a Rect); flow.canvas is the reportlab canvas

`render_guide(path, data)` builds the whole PDF from a `GuideData`.
"""

from dataclasses import dataclass

from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdfcanvas

MM = mm
PAPERS = {"a4": A4, "letter": letter}
SAFE_WIDTH_MM, SAFE_HEIGHT_MM = 190, 265    # works on both A4 and Letter
FOOTER_HEIGHT = 9 * MM                       # room kept free under the content for the footer
MIN_FONT = 9                                 # nothing smaller is printed
COMPRESS = True                              # tests switch it off to read the text back out of the PDF
FONT, BOLD = "Helvetica", "Helvetica-Bold"


@dataclass(frozen=True)
class Rect:
    left: float
    bottom: float
    right: float
    top: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.top - self.bottom


@dataclass(frozen=True)
class GuideData:
    model_name: str
    split_id: int
    plan: dict                 # splits.assembly_plan(): sections, summary, code
    paper: str                 # "a4" or "letter"
    plan_line: str = "Automatic plan"
    bom: object = None         # assembly_bom.Bom, or None when the split needs no rods (then there is no rod page)
    scene: object = None       # assembly_render.Scene: the parts' meshes and rods the step drawings come from
    labels: dict = None        # part number -> the saved label record (result.json parts[i]["label"]) or None
    plates: dict = None        # part number -> name of the plate it is on; a part not on a plate is left out


def safe_area(paper: str) -> Rect:
    width, height = PAPERS[paper]
    left, bottom = (width - SAFE_WIDTH_MM * MM) / 2, (height - SAFE_HEIGHT_MM * MM) / 2
    return Rect(left, bottom, left + SAFE_WIDTH_MM * MM, bottom + SAFE_HEIGHT_MM * MM)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def printable(text) -> str:
    """Text the standard PDF fonts can draw (Latin-1 range); anything else becomes a question mark."""
    return str(text).encode("cp1252", "replace").decode("cp1252")


def fit(text: str, font: str, size: float, width: float) -> str:
    text = printable(text)
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "...", font, size) > width:
        text = text[:-1]
    return text + "..."


def wrap(text: str, font: str, size: float, width: float) -> list:
    lines, line = [], ""
    for word in printable(text).split():
        trial = f"{line} {word}".strip()
        if line and stringWidth(trial, font, size) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line] if line else lines


class _GuideCanvas(pdfcanvas.Canvas):
    """A canvas that remembers each page so footers can say "page n of N" once the page count is known."""

    footer = None            # set by render_guide: footer(canvas, page_number, page_count, sections)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []
        self.page_sections = []      # Sections that appear on the page being drawn

    def showPage(self):
        self._saved.append((dict(self.__dict__), list(self.page_sections)))
        self._startPage()

    def save(self):
        count = len(self._saved)
        for number, (state, sections) in enumerate(self._saved, start=1):
            self.__dict__.update(state)
            if number > 1 and self.footer:
                self.footer(self, number, count, sections)
            super().showPage()
        super().save()


class PageFlow:
    """Pages for one guide: a cursor that runs down the safe area and starts a new page when a block does not fit."""

    def __init__(self, canvas, paper: str):
        self.canvas = canvas
        self.area = safe_area(paper)
        self.content = Rect(self.area.left, self.area.bottom + FOOTER_HEIGHT, self.area.right, self.area.top)
        self.y = self.content.top
        self.section = None
        self.pages = 1

    def new_page(self, carry: bool = True) -> None:
        self.canvas.showPage()
        self.pages += 1
        self.y = self.content.top
        self.canvas.page_sections = [self.section] if carry and self.section else []

    def reserve(self, height: float, section: str | None = None, slack: float = 0) -> float:
        """Room for a block `height` tall; returns its top y and moves the cursor below it. `slack` is empty space at the
        end of the block (a trailing gap) that may hang below the content area, so it never pushes the block to a new page."""
        if height - slack > self.content.height:
            raise ValueError("A block is taller than the safe area")
        if self.y - height + slack < self.content.bottom:
            self.new_page(carry=section is None)
        if section:
            self.section = section
            if section not in self.canvas.page_sections:
                self.canvas.page_sections.append(section)
        top = self.y
        self.y -= height
        return top

    def paragraph(self, text: str, font: str = FONT, size: float = 10, indent: float = 0, gap: float = 0,
                  keep_with: float = 0, section: str | None = None) -> None:
        """Wrapped text. `keep_with` is extra height that must fit on the same page below the first line."""
        size = max(size, MIN_FONT)
        leading = size * 1.3
        lines = wrap(text, font, size, self.content.width - indent)
        if not lines:
            return
        if keep_with:
            self.reserve(leading + keep_with, section)
            self.y += leading + keep_with
        for line in lines:
            top = self.reserve(leading, section)
            self.canvas.setFont(font, size)
            self.canvas.drawString(self.content.left + indent, top - size, line)
        self.y -= gap


def _footer(data: GuideData):
    def draw(canvas, number: int, count: int, sections: list) -> None:
        area = safe_area(data.paper)
        label = ("Section " if len(sections) == 1 else "Sections ") + ", ".join(sections) if sections else ""
        right = f"page {number} of {count}"
        size = MIN_FONT
        canvas.setFont(FONT, size)
        canvas.setLineWidth(0.4)
        canvas.line(area.left, area.bottom + 6 * MM, area.right, area.bottom + 6 * MM)
        tail = "  |  ".join(part for part in (f"Split {data.split_id}", f"Plan {data.plan['code']}", label) if part)
        room = area.width - stringWidth(right, FONT, size) - stringWidth(f"  |  {tail}", FONT, size) - 12
        canvas.drawString(area.left, area.bottom + 2 * MM, f"{fit(data.model_name, FONT, size, max(room, 40))}  |  {tail}")
        canvas.drawRightString(area.right, area.bottom + 2 * MM, right)
    return draw


def direction_text(vector) -> str:
    axis = next((i for i, v in enumerate(vector) if abs(v) > 0.999), None)
    if axis is None:
        return ", ".join(f"{v:.2f}" for v in vector)
    return ("-" if vector[axis] < 0 else "+") + "XYZ"[axis]


def parts_text(parts) -> str:
    parts = list(parts)
    return f"part {parts[0]}" if len(parts) == 1 else "parts " + ", ".join(str(p) for p in parts)


SMALL_GROUP = 4   # a built-so-far group of up to this many parts is named by its parts


def built_groups(plan: dict) -> dict:
    """{frozenset of parts: what to call that built group}: the step that finished it ("the stack from A3"), or the
    Section when a finished Section is the group. The first step (in plan order) that makes a group names it."""
    groups = {}
    for section in plan["sections"]:
        groups.setdefault(frozenset(section["parts"]), f"Section {section['letter']}")
    for section in plan["sections"]:
        for step in section["steps"]:
            groups.setdefault(frozenset(step["receiver"]) | frozenset(step["parts"]), f"the stack from {step['id']}")
    return groups


def receiver_text(receiver, groups: dict | None = None) -> str:
    """What a step joins to: the parts when the group is small, else the step that finished the group."""
    if len(receiver) <= SMALL_GROUP:
        return parts_text(receiver)
    return (groups or {}).get(frozenset(receiver)) or f"the {len(receiver)} parts built so far"


def step_text(step: dict, groups: dict | None = None) -> str:
    action = f"Join Section {step['section']} ({parts_text(step['parts'])})" if step["kind"] == "section" else f"Add {parts_text(step['parts'])}"
    receiver = step["receiver"]
    where = receiver_text(receiver, groups)
    how = "glue the face, no rods" if step["glue_only"] else plural(step["rods"], "rod")
    return f"{step['id']}: {action} to {where} along {direction_text(step['direction'])}, {how}."


# ── Warnings: the step-page code calls these ───────────────────────────────────

def step_warning_lines(step: dict) -> list:
    """The warnings for one step as printable lines (a heavy join etc.). Empty when there are none."""
    return [w["message"] for w in step.get("warnings") or []]


def plan_note_lines(plan: dict) -> list:
    """Plan-level notes for the front of the step list, e.g. parts with no number printed on them."""
    return [w["message"] for w in plan.get("warnings") or [] if w["kind"] == "unlabelled"]


def _cover(flow: PageFlow, data: GuideData) -> None:
    c, area, summary = flow.canvas, flow.area, data.plan["summary"]
    c.setFont(BOLD, 30)
    c.drawString(area.left, area.top - 40 * MM, fit(data.model_name, BOLD, 30, area.width))
    c.setFont(FONT, 16)
    c.drawString(area.left, area.top - 52 * MM, "Assembly guide")
    counts = ", ".join([plural(summary["parts"], "part"), plural(summary["sections"], "Section"),
                        plural(summary["steps"], "step"), plural(summary["rods"], "rod")])
    c.setFont(FONT, 12)
    c.drawString(area.left, area.top - 70 * MM, counts)
    c.drawString(area.left, area.top - 78 * MM, data.plan_line)
    c.drawString(area.left, area.top - 86 * MM, f"Plan code {data.plan['code']}")
    c.drawString(area.left, area.top - 94 * MM, f"Paper {'Letter' if data.paper == 'letter' else 'A4'}")
    c.setFont(FONT, MIN_FONT)
    c.drawString(area.left, area.top - 110 * MM, f"Split {data.split_id}. Compare the plan code on two printed copies to see that they are the same plan.")
    if not summary["rods"]:
        c.drawString(area.left, area.top - 118 * MM, "Glue the faces: no rods needed.")
    if data.scene is not None:
        import assembly_inventory
        assembly_inventory.draw_overview(c, data.scene, (area.left, area.bottom + 5 * MM, area.width, area.height - 125 * MM))
    flow.y = area.bottom   # the cover is a page of its own


STEP_TITLE_H = 11 * MM       # the step number and what is done, above the frames
SECTION_HEADER_H = 36 * MM
STEP_GAP = 5 * MM
TICK_MM = 5          # side of a step's tick box
SET_ASIDE_H = 7 * MM


def step_title(step: dict, groups: dict | None = None) -> str:
    receiver = step["receiver"]
    where = receiver_text(receiver, groups)
    moved = f"Section {step['section']} ({parts_text(step['parts'])})" if step["kind"] == "section" else parts_text(step["parts"])
    return f"{'Join' if step['kind'] == 'section' else 'Add'} {moved} to {where}"


def step_caption(step: dict, groups: dict | None = None, depth: str = "") -> str:
    receiver = step["receiver"]
    where = receiver_text(receiver, groups)
    moved = f"Section {step['section']}" if step["kind"] == "section" else parts_text(step["parts"])
    if step["glue_only"]:
        return f"No rods: glue the face of {where} and slide {moved} on."
    return f"Push {plural(step['rods'], 'rod')} into the holes of {where}, glue the face, slide {moved} on." + (f" {depth}" if depth else "")


def _room(block: float, aside: float = 0) -> tuple:
    """(height, slack) to reserve for a step block `block` tall (its trailing gap included). The set-aside line of a side
    Section's last step stands where that gap would, so the two are not added."""
    return (block - STEP_GAP + aside, 0) if aside else (block, STEP_GAP)


def _step_block(flow: PageFlow, art, step: dict, groups: dict, depth: str = "") -> tuple:
    """(height of the whole step block, caption lines): number and title, two frames, caption."""
    import assembly_render
    lines = [(FONT, line) for line in wrap(step_caption(step, groups, depth), FONT, 10.5, flow.content.width)]
    lines += [(BOLD, line) for text in step_warning_lines(step) for line in wrap(f"Warning: {text}", BOLD, 10, flow.content.width)]
    import assembly_extras
    return (STEP_TITLE_H + assembly_render.step_height(art, flow.content.width) + 3 * MM + len(lines) * 10.5 * 1.3
            + assembly_extras.STRIP_GAP + assembly_extras.strip_height(art) + STEP_GAP), lines


def _draw_step_block(flow: PageFlow, art, step: dict, top: float, lines: list, groups: dict) -> None:
    import assembly_extras
    import assembly_render
    c, left = flow.canvas, flow.content.left
    c.setFillGray(0)
    c.setFont(BOLD, 26)
    c.drawString(left, top - 9 * MM, step["id"])
    c.setFont(BOLD, 12)
    c.drawString(left + 17 * MM, top - 7 * MM, fit(step_title(step, groups), BOLD, 12, flow.content.width - 17 * MM - 8 * MM))
    c.setLineWidth(1)
    c.rect(flow.content.right - TICK_MM * MM, top - 2 * MM - TICK_MM * MM, TICK_MM * MM, TICK_MM * MM, stroke=1, fill=0)
    frames = assembly_render.draw_step(c, art, left, top - STEP_TITLE_H, flow.content.width)
    assembly_extras.draw_step_extras(c, art, left, top - STEP_TITLE_H, flow.content.width)
    y = top - STEP_TITLE_H - frames - 3 * MM
    c.setFillGray(0)
    for font, line in lines:
        y -= 10.5 * 1.3
        c.setFont(font, 10.5 if font == FONT else 10)
        c.drawString(left, y + 3, line)
    assembly_extras.draw_strip(c, art, left, y - assembly_extras.STRIP_GAP, flow.content.width)


def _section_header(flow: PageFlow, section: dict, picture, top: float) -> None:
    import assembly_render
    c, left = flow.canvas, flow.content.left
    letter = section["letter"]
    c.setFillGray(0)
    c.setFont(BOLD, 44)
    c.drawString(left, top - 15 * MM, letter)
    text_left = left + 22 * MM
    c.setFont(BOLD, 16)
    c.drawString(text_left, top - 8 * MM, f"Section {letter}" + (" (main)" if section["main"] else ""))
    c.setFont(FONT, 10)
    y = top - 14 * MM
    for line in wrap(f"Made of {parts_text(section['parts'])}", FONT, 10, flow.content.width - 22 * MM - 60 * MM)[:3]:
        c.drawString(text_left, y, line)
        y -= 13
    if section["note"]:
        for line in wrap(section["note"], FONT, 10, flow.content.width - 22 * MM - 60 * MM)[:2]:
            c.drawString(text_left, y, line)
            y -= 13
    rods = sum(step["rods"] for step in section["steps"])
    if rods:
        c.setFont(BOLD, 10)
        c.drawString(text_left + 55 * MM, top - 8 * MM, f"{plural(rods, 'rod')} in this Section")
    assembly_render.draw_picture(c, picture, flow.content.right - 56 * MM, top - SECTION_HEADER_H + 2 * MM, 56 * MM, SECTION_HEADER_H - 4 * MM)


def _steps(flow: PageFlow, data: GuideData, progress) -> None:
    """Section headers inline, then each Section's steps, as many to a page as fit, none split across pages. A Section
    starts on the page the last one ended on when its header and first step fit there; the gap that follows a step
    is not counted when the step is the last thing that fits."""
    import assembly_extras
    import assembly_render
    if data.scene is None:
        raise ValueError("The guide needs the part previews of the split")
    sections, total, done, camera = data.plan["sections"], max(data.plan["summary"]["steps"], 1), 0, None
    import assembly_bom
    groups = built_groups(data.plan)
    flow.new_page()
    c = flow.canvas
    for index, section in enumerate(sections):
        picture = assembly_render.section_picture(data.scene, section["parts"])
        steps = section["steps"]
        aside = SET_ASIDE_H if not section["main"] else 0
        prepared = None
        depth_due = bool(data.bom)       # the first step with rods in the Section says how deep a rod sits
        if steps:
            view = assembly_render.choose_view(data.scene, steps[0], camera)
            camera = view.camera
            art = assembly_render.prepare_step(data.scene, steps[0], camera, close_up=view.close_up)
            assembly_extras.attach(art, data.scene, section["parts"])
            depth = assembly_bom.depth_text(data.bom, steps[0]["connectors"]) if depth_due and steps[0]["rods"] else ""
            depth_due = depth_due and not depth
            prepared = (art, *_step_block(flow, art, steps[0], groups, depth))
        first_h, slack = _room(prepared[1], aside if len(steps) == 1 else 0) if prepared else (aside, 0)
        top = flow.reserve((STEP_GAP if index else 0) + SECTION_HEADER_H + first_h, section=section["letter"], slack=slack)
        if index:
            if top < flow.content.top - 1 * MM:
                c.setStrokeGray(0)
                c.setLineWidth(2)
                c.line(flow.content.left, top - 2 * MM, flow.content.right, top - 2 * MM)
            top -= STEP_GAP
        _section_header(flow, section, picture, top)
        y = top - SECTION_HEADER_H
        for n, step in enumerate(steps):
            if n:
                view = assembly_render.choose_view(data.scene, step, camera)
                camera = view.camera
                art = assembly_render.prepare_step(data.scene, step, camera, close_up=view.close_up)
                assembly_extras.attach(art, data.scene, section["parts"])
                depth = assembly_bom.depth_text(data.bom, step["connectors"]) if depth_due and step["rods"] else ""
                depth_due = depth_due and not depth
                prepared = (art, *_step_block(flow, art, step, groups, depth))
                last = n == len(steps) - 1
                height, slack = _room(prepared[1], aside if last else 0)
                y = flow.reserve(height, section=section["letter"], slack=slack)
            art, height, lines = prepared
            _draw_step_block(flow, art, step, y, lines, groups)
            if n == len(steps) - 1 and aside:
                c.setFillGray(0)
                c.setFont(BOLD, 10)
                c.drawString(flow.content.left, y - (height - STEP_GAP) - 5 * MM, f"Set aside as {section['letter']} (write {section['letter']} on a piece of tape).")
            done += 1
            progress(f"Drawing steps ({done} of {total})", 20 + 70 * done // total)
        if not steps and aside:
            c.setFillGray(0)
            c.setFont(BOLD, 10)
            c.drawString(flow.content.left, top - SECTION_HEADER_H - 5 * MM, f"Set aside as {section['letter']} (write {section['letter']} on a piece of tape).")


def render_guide(path, data: GuideData, progress=None) -> int:
    """Write the guide to `path`. `progress(stage_text, percent)` is called along the way. Returns the page count."""
    progress = progress or (lambda stage, percent: None)
    progress("Laying out the cover", 5)
    canvas = _GuideCanvas(str(path), pagesize=PAPERS[data.paper], pageCompression=COMPRESS, invariant=1)
    canvas.footer = _footer(data)
    canvas.setTitle(printable(f"{data.model_name} - assembly guide"))
    canvas.setSubject(f"plan {data.plan['code']}")
    canvas.setCreator("Spooler")
    flow = PageFlow(canvas, data.paper)
    _cover(flow, data)
    if data.bom:
        import assembly_rod_page
        canvas.setViewerPreference("PrintScaling", "None")   # the rod page is true size: ask viewers not to scale
        assembly_rod_page.draw_rod_page(flow, data.bom)
    if data.scene is not None:
        import assembly_inventory
        assembly_inventory.draw_inventory(flow, data)
    _steps(flow, data, progress)
    if data.scene is not None:
        assembly_inventory.draw_last_page(flow, data)
    progress("Saving the PDF", 95)
    canvas.showPage()
    canvas.save()
    return flow.pages
