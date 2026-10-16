"""Support material per Object, read from sliced G-code.

Orca reports only plate totals, so the support is summed from the G-code: the filament pushed out by every move with
`;TYPE:Support` or `;TYPE:Support interface`, attributed to the Object named by the surrounding `EXCLUDE_OBJECT_START/END`
markers (research/02-measuring-support-and-time.md). Only moves that travel in X or Y and push filament forward count, so
retractions and unretracts are ignored; G2/G3 arcs count (arc fitting is on). Support outside any Object (rare) is in the
plate total only.
"""

import math
from pathlib import Path

SUPPORT_TYPES = ("support", "support interface")
DEFAULT_DIAMETER_MM, DEFAULT_DENSITY = 1.75, 1.24


def _number(text: str, default: float) -> float:
    try:
        return float(text.strip().strip('"').split(",")[0])
    except ValueError:
        return default


def measure(path: Path) -> dict:
    """{"support_mm", "support_g", "objects": {name: {"support_mm", "support_g"}}} for one G-code file."""
    relative_e, e = False, 0.0
    feature, owner = "", None
    totals: dict = {}
    outside = 0.0
    diameter, density = DEFAULT_DIAMETER_MM, DEFAULT_DENSITY
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line[0] == ";":
                text = line[1:].strip()
                if text.startswith("TYPE:"):
                    feature = text[5:].strip().lower()
                elif text.startswith("filament_diameter"):
                    diameter = _number(text.partition("=")[2], diameter)
                elif text.startswith("filament_density"):
                    density = _number(text.partition("=")[2], density)
                continue
            code = line.split(";", 1)[0].split()
            if not code:
                continue
            name = code[0].upper()
            if name == "M82":
                relative_e = False
            elif name == "M83":
                relative_e = True
            elif name == "EXCLUDE_OBJECT_START":
                owner = next((w[5:] for w in code[1:] if w.upper().startswith("NAME=")), None)
            elif name == "EXCLUDE_OBJECT_END":
                owner = None
            elif name == "G92":
                for word in code[1:]:
                    if word[0].upper() == "E":
                        e = _number(word[1:], 0.0)
            elif name in ("G0", "G1", "G2", "G3"):
                words = {w[0].upper(): w[1:] for w in code[1:] if len(w) > 1}
                if "E" not in words:
                    continue
                value = _number(words["E"], 0.0)
                de = value if relative_e else value - e
                if not relative_e:
                    e = value
                if de > 0 and ("X" in words or "Y" in words) and feature in SUPPORT_TYPES:
                    if owner is None:
                        outside += de
                    else:
                        totals[owner] = totals.get(owner, 0.0) + de
    per_mm = math.pi * (diameter / 2) ** 2 * density / 1000   # grams per mm of filament
    return {"support_mm": sum(totals.values()) + outside, "support_g": (sum(totals.values()) + outside) * per_mm,
            "objects": {n: {"support_mm": mm, "support_g": mm * per_mm} for n, mm in totals.items()}}
