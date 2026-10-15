"""Parity checks for the PETG/ASA verification workflow (see tools/PARITY.md).

Pure functions over G-code text: parse what Orca printed, compare two outputs under the spec's bounds, and write readable
diagnostics when they disagree. Nothing here runs Orca, a server or a printer, so the logic is testable without them.

A Spooler result is never compared with itself alone: callers pass `expected`, settings written down independently of the
resolver, so a shared resolver bug cannot pass both paths."""
import json
import re
from pathlib import Path

import gcode_meta
import observed_output

TIME_FLOOR_S, TIME_FRACTION = 1.0, 0.001        # larger of one second or 0.1%
WEIGHT_FLOOR_G, WEIGHT_FRACTION = 0.02, 0.001   # larger of 0.02 g or 0.1%
POSITION_MM = 0.05                               # geometry is serialised to 9 significant figures; this is far above that
START_CODE_ROLES = frozenset({"Custom"})         # the printer's own start/purge moves sit outside every Object and would hide a moved one
# Only these differences are ignored, besides observed_output.IGNORED (names, ids, compatibility lists, colour labels).
COSMETIC = frozenset({"thumbnails", "thumbnails_format", "gcode_label_objects", "print_flow_ratio_calibration"})
_MOVE = re.compile(r"^G[01]\s")
_AXIS = re.compile(r"\b([XYE])(-?[0-9.]+)")


class ParityFailure(AssertionError):
    """Raised with the readable diagnostic text; the same text is written beside the artifacts."""


def parse(text: str) -> dict:
    """Print-affecting facts from one Orca G-code file: footer config, layers, time, weight, per-role moves, extrusion bounds."""
    config = observed_output.footer_of(text) or {}
    layers = re.search(r"^; total layer(?:s count| number)[:=]\s*(\d+)", text, re.M)
    estimates = gcode_meta.parse_estimates(text)
    roles, role = {}, ""
    x = y = None
    bounds = None
    for line in text.splitlines():
        if line.startswith(";TYPE:"):
            role = line[6:].strip()
            continue
        if not _MOVE.match(line):
            continue
        axes = dict(_AXIS.findall(line))
        if "X" in axes:
            x = float(axes["X"])
        if "Y" in axes:
            y = float(axes["Y"])
        if "E" in axes and float(axes["E"]) > 0 and ("X" in axes or "Y" in axes) and role:
            roles[role] = roles.get(role, 0) + 1
            if x is not None and y is not None and role not in START_CODE_ROLES:
                bounds = [x, y, x, y] if bounds is None else [min(bounds[0], x), min(bounds[1], y), max(bounds[2], x), max(bounds[3], y)]
    return {"config": config, "layers": int(layers.group(1)) if layers else None, "time_s": estimates["estimate_s"],
            "weight_g": estimates["estimate_g"], "moves": roles, "bounds": bounds}


def within(a, b, floor: float, fraction: float) -> bool:
    return a is not None and b is not None and abs(a - b) <= max(floor, fraction * max(abs(a), abs(b)))


def compare(reference: dict, other: dict) -> list:
    """Differences between two parsed outputs of the same Plate. Empty means they agree within the spec's bounds."""
    found = []
    ignored = observed_output.IGNORED | COSMETIC
    for key in sorted((set(reference["config"]) | set(other["config"])) - ignored):
        a, b = reference["config"].get(key), other["config"].get(key)
        if a is None or b is None:
            found.append({"what": "config", "key": key, "reference": a, "other": b, "why": "reported by only one output"})
        elif not observed_output.same_value(a, b):
            found.append({"what": "config", "key": key, "reference": a, "other": b, "why": "different value"})
    if reference["layers"] != other["layers"]:
        found.append({"what": "layers", "reference": reference["layers"], "other": other["layers"], "why": "layer counts must match"})
    if not within(reference["time_s"], other["time_s"], TIME_FLOOR_S, TIME_FRACTION):
        found.append({"what": "time_s", "reference": reference["time_s"], "other": other["time_s"], "why": "beyond max(1 s, 0.1%)"})
    if not within(reference["weight_g"], other["weight_g"], WEIGHT_FLOOR_G, WEIGHT_FRACTION):
        found.append({"what": "weight_g", "reference": reference["weight_g"], "other": other["weight_g"], "why": "beyond max(0.02 g, 0.1%)"})
    for role in sorted(set(reference["moves"]) | set(other["moves"])):
        if reference["moves"].get(role, 0) != other["moves"].get(role, 0):
            found.append({"what": "toolpath", "key": role, "reference": reference["moves"].get(role, 0),
                          "other": other["moves"].get(role, 0), "why": "deposition move counts differ"})
    a, b = reference["bounds"], other["bounds"]
    if (a is None) != (b is None) or (a and any(abs(p - q) > POSITION_MM for p, q in zip(a, b))):
        found.append({"what": "position", "reference": a, "other": b, "why": f"extrusion bounds differ by more than {POSITION_MM} mm"})
    return found


def expectation_failures(observed: dict, expected: dict) -> list:
    """`expected` is written independently of Spooler's resolver: {footer key: value}, plus optional `_moves`
    ({role: minimum deposition moves}) and `_layers`. Anything that disagrees fails, whichever path produced the output."""
    found = []
    for key, value in expected.items():
        if key == "_moves":
            for role, minimum in value.items():
                if observed["moves"].get(role, 0) < minimum:
                    found.append({"what": "toolpath", "key": role, "expected": f">= {minimum}", "observed": observed["moves"].get(role, 0),
                                  "why": "expected deposition is missing"})
        elif key == "_layers":
            if observed["layers"] != value:
                found.append({"what": "layers", "expected": value, "observed": observed["layers"], "why": "layer count"})
        elif key not in observed["config"] or not observed_output.same_value(value, observed["config"][key]):
            found.append({"what": "config", "key": key, "expected": value, "observed": observed["config"].get(key), "why": "not what was asked for"})
    return found


def report(label: str, differences: list) -> str:
    lines = [f"PARITY FAILURE: {label}", ""]
    for d in differences:
        subject = d["what"] + (f" {d['key']}" if d.get("key") else "") + (f" [{d['side']}]" if d.get("side") else "")
        left = d["reference"] if "reference" in d else d.get("expected")
        right = d["other"] if "other" in d else d.get("observed")
        lines.append(f"- {subject}: {left!r} vs {right!r} ({d['why']})")
    return "\n".join(lines)


def assert_parity(label: str, reference_text: str, other_text: str, expected: dict, diagnostics: Path) -> dict:
    """Compare two outputs of one Plate against each other AND against independent expectations.

    On failure the readable report, both parsed outputs and the expected settings are written to `diagnostics/<label>/`
    before ParityFailure is raised, so a regression leaves something to read. Returns the parsed reference on success."""
    reference, other = parse(reference_text), parse(other_text)
    differences = compare(reference, other)
    differences += [{**d, "side": "reference"} for d in expectation_failures(reference, expected)]
    differences += [{**d, "side": "other"} for d in expectation_failures(other, expected)]
    if differences:
        folder = Path(diagnostics) / re.sub(r"[^\w.-]+", "_", label)
        folder.mkdir(parents=True, exist_ok=True)
        text = report(label, differences)
        (folder / "report.txt").write_text(text, encoding="utf-8")
        (folder / "expected.json").write_text(json.dumps(expected, indent=2, sort_keys=True), encoding="utf-8")
        for name, parsed in (("reference", reference), ("other", other)):
            (folder / f"{name}.json").write_text(json.dumps(parsed, indent=2, sort_keys=True), encoding="utf-8")
        (folder / "reference.gcode").write_text(reference_text, encoding="utf-8")
        (folder / "other.gcode").write_text(other_text, encoding="utf-8")
        raise ParityFailure(f"{text}\n\nDiagnostics kept in {folder}")
    return reference
