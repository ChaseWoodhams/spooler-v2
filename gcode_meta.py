"""
G-code file helpers: printer-safe file names and slicer estimates (time, filament).
"""

import re
from pathlib import Path

ALLOWED_SUFFIXES = (".gcode",)
MAX_NAME_LEN     = 120
HEAD_BYTES       = 64 * 1024
TAIL_BYTES       = 512 * 1024   # Orca/Prusa put estimates just before the trailing config block

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\\/:*?"<>|]')

_TOTAL_TIME  = re.compile(r"total estimated time:\s*([\ddhms ]+)", re.I)
_NORMAL_TIME = re.compile(r"estimated printing time \(normal mode\)\s*=\s*([\ddhms ]+)", re.I)
_CURA_TIME   = re.compile(r"^(?:PRINT\.)?TIME:\s*(\d+)$", re.I)
_GRAMS       = re.compile(r"^(total )?filament used \[g\]\s*=\s*([\d.,\s]+)$", re.I)
_MILLIMETRES = re.compile(r"^filament used \[mm\]\s*=\s*([\d.,\s]+)$", re.I)
_CURA_METRES = re.compile(r"^filament used:\s*([\d.]+)\s*m$", re.I)
_DURATION    = re.compile(r"(\d+)\s*([dhms])", re.I)


def safe_gcode_name(name: str) -> str:
    """Return a bare, printer-safe .gcode file name, or raise ValueError."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _UNSAFE_CHARS.sub("_", name)
    if not name.lower().endswith(ALLOWED_SUFFIXES):
        raise ValueError("Only .gcode files can be queued")
    if name.startswith("."):
        raise ValueError("The file needs a name before the .gcode extension")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"File name is too long (maximum {MAX_NAME_LEN} characters)")
    return name


def parse_duration(text: str) -> int | None:
    """'1d 2h 3m 4s' → seconds."""
    scale = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = _DURATION.findall(text or "")
    return sum(int(n) * scale[unit.lower()] for n, unit in parts) if parts else None


def _sum_numbers(text: str) -> float | None:
    values = [float(v) for v in re.split(r"[,\s]+", text.strip()) if v]
    return round(sum(values), 2) if values else None


def parse_estimates(text: str) -> dict:
    """Extract slicer estimates from G-code comments (Orca/Elegoo, PrusaSlicer, Cura)."""
    best: dict = {}  # field → (priority, value)

    def offer(field: str, priority: int, value) -> None:
        if value is not None and priority > best.get(field, (0, None))[0]:
            best[field] = (priority, value)

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(";"):
            continue
        body = line.lstrip(";").strip()
        if m := _TOTAL_TIME.search(body):
            offer("estimate_s", 3, parse_duration(m.group(1)))
        elif m := _NORMAL_TIME.search(body):
            offer("estimate_s", 2, parse_duration(m.group(1)))
        elif m := _CURA_TIME.match(body):
            offer("estimate_s", 1, int(m.group(1)))
        elif m := _GRAMS.match(body):
            offer("estimate_g", 2 if m.group(1) else 1, _sum_numbers(m.group(2)))
        elif m := _MILLIMETRES.match(body):
            offer("estimate_mm", 2, _sum_numbers(m.group(1)))
        elif m := _CURA_METRES.match(body):
            offer("estimate_mm", 1, round(float(m.group(1)) * 1000, 1))

    return {field: best.get(field, (0, None))[1] for field in ("estimate_s", "estimate_g", "estimate_mm")}


def read_estimates(path: Path) -> dict:
    """Parse estimates from the head and tail of a G-code file without reading all of it."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(HEAD_BYTES)
        tail = b""
        if size > HEAD_BYTES:
            fh.seek(max(HEAD_BYTES, size - TAIL_BYTES))
            tail = fh.read()
    return parse_estimates((head + b"\n" + tail).decode("utf-8", errors="replace"))
