"""
Packing parts onto build plates.

Each part is a footprint rectangle. Rectangles are placed largest first with MaxRects (best short side fit),
turning a part a quarter turn when that fits better, keeping GAP_MM between parts, MARGIN_MM from the bed
edge and clear of the nozzle purge area. A part goes on the first plate of its group with room, otherwise
on a new plate, so parts that must print with different settings never share a plate.
"""

GAP_MM = 6.0
SUPPORT_GAP_MM = 20.0  # tree supports branch out past a part's footprint; closer parts make OrcaSlicer's toolpaths collide
MARGIN_MM = 3.0


def _contains(outer, inner) -> bool:
    return (outer[0] <= inner[0] + 1e-9 and outer[1] <= inner[1] + 1e-9
            and outer[0] + outer[2] >= inner[0] + inner[2] - 1e-9 and outer[1] + outer[3] >= inner[1] + inner[3] - 1e-9)


class _Bin:
    """Free space on one plate as maximal free rectangles (x, y, width, depth)."""

    def __init__(self, width: float, depth: float, blocked=()):
        self.free = [(0.0, 0.0, width, depth)]
        for rect in blocked:
            self.occupy(rect)

    def find(self, width: float, depth: float):
        best = None
        for turned in (False, True) if abs(width - depth) > 1e-9 else (False,):
            w, d = (depth, width) if turned else (width, depth)
            for fx, fy, fw, fd in self.free:
                if w <= fw + 1e-9 and d <= fd + 1e-9:
                    score = tuple(sorted((fw - w, fd - d)))
                    if best is None or score < best[0]:
                        best = (score, (fx, fy, w, d), turned)
        return None if best is None else best[1:]

    def occupy(self, rect) -> None:
        x, y, w, d = rect
        pieces = []
        for fx, fy, fw, fd in self.free:
            if x >= fx + fw or x + w <= fx or y >= fy + fd or y + d <= fy:
                pieces.append((fx, fy, fw, fd))
                continue
            if x > fx:
                pieces.append((fx, fy, x - fx, fd))
            if x + w < fx + fw:
                pieces.append((x + w, fy, fx + fw - x - w, fd))
            if y > fy:
                pieces.append((fx, fy, fw, y - fy))
            if y + d < fy + fd:
                pieces.append((fx, y + d, fw, fy + fd - y - d))
        pieces = [p for p in pieces if p[2] > 1e-9 and p[3] > 1e-9]
        self.free = [p for i, p in enumerate(pieces)
                     if not any(j != i and _contains(q, p) and (q != p or j < i) for j, q in enumerate(pieces))]


def pack(items: list, bed, exclude=None, gap: float = GAP_MM, margin: float = MARGIN_MM) -> list:
    """items: [{"key", "size": (width, depth), "group"}]; bed and exclude: (x0, y0, x1, y1) in bed millimetres.
    Returns plates in group order (groups in order of first appearance) as
    [{"group", "items": [{"key", "center": (x, y), "turned": bool, "oversize": bool}]}].
    A part that can't fit even on an empty plate gets a plate of its own, centred, marked oversize."""
    bx0, by0, bx1, by1 = bed
    ox, oy = bx0 + margin, by0 + margin
    width, depth = bx1 - bx0 - 2 * margin + gap, by1 - by0 - 2 * margin + gap
    blocked = []
    if exclude:
        ex0, ey0, ex1, ey1 = exclude
        blocked.append((ex0 - ox, ey0 - oy, ex1 - ex0 + gap, ey1 - ey0 + gap))

    groups = list(dict.fromkeys(item["group"] for item in items))
    ordered = sorted(items, key=lambda i: (groups.index(i["group"]), -max(i["size"]), -i["size"][0] * i["size"][1]))
    plates = []  # [{"group", "bin", "items"}]
    for item in ordered:
        w, d = float(item["size"][0]) + gap, float(item["size"][1]) + gap
        placed = None
        for plate in (p for p in plates if p["group"] == item["group"] and p["bin"] is not None):
            found = plate["bin"].find(w, d)
            if found:
                placed = plate, found
                break
        if placed is None:
            plate = {"group": item["group"], "bin": _Bin(width, depth, blocked), "items": []}
            found = plate["bin"].find(w, d)
            if found is None:
                plate["bin"] = None  # nothing else goes on an oversize part's plate
                plate["items"].append({"key": item["key"], "center": ((bx0 + bx1) / 2, (by0 + by1) / 2), "turned": False, "oversize": True})
                plates.append(plate)
                continue
            plates.append(plate)
            placed = plate, found
        plate, ((x, y, rw, rd), turned) = placed
        plate["bin"].occupy((x, y, rw, rd))
        plate["items"].append({"key": item["key"], "center": (ox + x + (rw - gap) / 2, oy + y + (rd - gap) / 2),
                               "turned": turned, "oversize": False})
    plates.sort(key=lambda p: groups.index(p["group"]))
    return [{"group": p["group"], "items": p["items"]} for p in plates]
