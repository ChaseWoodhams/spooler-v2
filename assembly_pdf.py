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

    def reserve(self, height: float, section: str | None = None) -> float:
        """Room for a block `height` tall; returns its top y and moves the cursor below it."""
        if height > self.content.height:
            raise ValueError("A block is taller than the safe area")
        if self.y - height < self.content.bottom:
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


def step_text(step: dict) -> str:
    action = f"Join Section {step['section']} ({parts_text(step['parts'])})" if step["kind"] == "section" else f"Add {parts_text(step['parts'])}"
    receiver = step["receiver"]
    where = f"the {len(receiver)} parts built so far" if len(receiver) > 4 else parts_text(receiver)
    how = "glue the face, no rods" if step["glue_only"] else plural(step["rods"], "rod")
    return f"{step['id']}: {action} to {where} along {direction_text(step['direction'])}, {how}."


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
    flow.y = area.bottom   # the cover is a page of its own


def _steps(flow: PageFlow, data: GuideData, progress) -> None:
    sections, total, done = data.plan["sections"], max(data.plan["summary"]["steps"], 1), 0
    flow.new_page()
    for index, section in enumerate(sections):
        flow.y -= 4 * MM if index else 0
        title = f"Section {section['letter']}" + (" (main)" if section["main"] else "")
        flow.paragraph(title, BOLD, 16, keep_with=40, section=section["letter"])
        flow.paragraph(f"Made of {parts_text(section['parts'])}", FONT, 10, gap=2)
        if section["note"]:
            flow.paragraph(section["note"], FONT, 10, gap=2)
        for step in section["steps"]:
            flow.paragraph(step_text(step), FONT, 10, indent=6 * MM, gap=1.5 * MM)
            done += 1
            progress(f"Writing steps ({done} of {total})", 20 + 70 * done // total)
        if not section["main"]:
            flow.paragraph(f"Set aside as {section['letter']} (write {section['letter']} on a piece of tape).",
                           BOLD, 10, indent=6 * MM, gap=1 * MM)


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
    _steps(flow, data, progress)
    progress("Saving the PDF", 95)
    canvas.showPage()
    canvas.save()
    return flow.pages
