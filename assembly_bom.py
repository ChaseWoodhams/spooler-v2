"""Rod bill of materials for the Assembly guide: what to cut, how many, and what to buy.

    bom = build_bom(pins, plan)     pins: the split result's final pin list; plan: splits.assembly_plan()
    bom.sizes                       one RodSize per connector size, main size first, fallback sizes after
    bom.total_rods                  rods in the build (without spares)
    bom.kind                        "rod" or "pin" (follows the connector label)

Counts come from the pins only, never from settings. `build_bom` raises `BomMismatch` when the per-step rod counts of
the plan do not add up to the pins (a bug, not a user problem), so a guide with a wrong rod page is never written.
"""

import math
from dataclasses import dataclass, field

STOCK_MM = 1000.0        # one stick of stock
CUT_LOSS_MM = 1.5        # lost to the saw at each cut
TOLERANCE_MM = 0.5       # cut length is good to plus or minus this
MIN_SPARES = 2
SPARE_FRACTION = 0.10


class BomMismatch(Exception):
    """The rod counts of the steps do not add up to the rod page."""


@dataclass(frozen=True)
class RodSize:
    label: str               # as the engine names it, e.g. "2 mm rod"
    diameter: float
    depth: float             # hole depth in each part
    length: float            # cut length, 2 x depth - 1
    count: int               # rods the build needs
    spares: int
    fallback: bool
    steps: tuple = ()        # ids of the steps that use this size

    @property
    def to_cut(self) -> int:
        return self.count + self.spares

    @property
    def total_length(self) -> float:
        """Length of the rods the build needs (count x cut length)."""
        return self.count * self.length

    @property
    def per_stick(self) -> int:
        return max(1, int(STOCK_MM // (self.length + CUT_LOSS_MM)))

    @property
    def sticks(self) -> int:
        return math.ceil(self.to_cut / self.per_stick)


@dataclass(frozen=True)
class Bom:
    kind: str
    sizes: tuple = field(default_factory=tuple)

    @property
    def total_rods(self) -> int:
        return sum(s.count for s in self.sizes)


def cut_length(depth: float) -> float:
    return 2 * depth - 1


def spares_for(count: int) -> int:
    return max(MIN_SPARES, math.ceil(count * SPARE_FRACTION - 1e-9))


def _plan_steps(plan: dict) -> list:
    return [step for section in plan.get("sections") or [] for step in section["steps"]]


def build_bom(pins: list, plan: dict) -> Bom | None:
    """The bill of materials, or None when the split needs no rods at all."""
    steps = _plan_steps(plan)
    by_label = {}
    for pin in pins or []:
        by_label.setdefault(pin["connector"], []).append(pin)
    per_step = {}
    for step in steps:
        for label, number in (step.get("connectors") or {}).items():
            per_step[label] = per_step.get(label, 0) + number
    wanted = {label: len(found) for label, found in by_label.items()}
    if per_step != wanted:
        raise BomMismatch("The rod counts of the steps (" + _counts(per_step) + ") do not match the rods of the split ("
                          + _counts(wanted) + "). This is a bug in the guide; the guide was not made.")
    if sum(s.get("rods", 0) for s in steps) != sum(wanted.values()):
        raise BomMismatch("The rods of the steps do not add up to the rods of the split. This is a bug in the guide; the guide was not made.")
    if not by_label:
        return None
    biggest = max(float(found[0]["diameter"]) for found in by_label.values())
    sizes = []
    for label, found in by_label.items():
        diameter, depth = float(found[0]["diameter"]), float(found[0]["depth"])
        used = tuple(step["id"] for step in steps if label in (step.get("connectors") or {}))
        sizes.append(RodSize(label, diameter, depth, cut_length(depth), len(found), spares_for(len(found)),
                             fallback=diameter < biggest, steps=used))
    sizes.sort(key=lambda s: (s.fallback, -s.diameter))
    kind = "pin" if all(label.endswith(" pin") for label in by_label) else "rod"
    return Bom(kind, tuple(sizes))


def _counts(counts: dict) -> str:
    return ", ".join(f"{n} x {label}" for label, n in sorted(counts.items())) or "none"


def inches(mm: float) -> str:
    value = mm / 25.4
    return f"about {value:.0f} in" if value >= 10 else f"about {value:.1f} in"


def mm_text(value: float) -> str:
    return f"{value:g}"
