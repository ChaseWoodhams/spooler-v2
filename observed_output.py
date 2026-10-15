"""Compare what OrcaSlicer reported in a finished G-code file with the settings a Slice was prepared with.

The evidence is the configuration footer Orca appends to its G-code (between CONFIG_BLOCK_START and CONFIG_BLOCK_END). It is
Orca's own account of the configuration it resolved, nothing more: it does not show Object-scope effects, post-processing or
what a printer's firmware does with the G-code, and a file without a footer yields no evidence at all rather than a pass.

Canonicalization (the only things treated as equal without being identical):
  * surrounding quotes and Orca's \\n / \\" escapes are removed; whitespace around a value is ignored;
  * numbers compare by value (1.0 = 1 = 1.000), percentages likewise, and true/1 and false/0 are the same boolean;
  * a vector compares element by element whether Orca joined it with "," or ";".
Ignored as cosmetic or identity (IGNORED): preset names and ids, compatibility lists, colour labels.
Known benign normalization is recorded (never silently dropped) in `normalized`; see _normalization.
Anything else that differs is a `mismatch`, shown by setting and the scope that set the expected value.
"""
import re

import brick_post

VERSION = 1
FOOTER_START, FOOTER_END = "; CONFIG_BLOCK_START", "; CONFIG_BLOCK_END"
TAIL_BYTES = 2 * 1024 * 1024   # the footer is a few hundred kB at most and is the last thing in Orca's file

IGNORED = frozenset({
    "name", "from", "inherits", "instantiation", "type", "setting_id", "version", "filament_id",
    "compatible_printers", "compatible_printers_condition", "compatible_prints", "compatible_prints_condition",
    "filament_settings_id", "print_settings_id", "printer_settings_id", "default_filament_profile", "default_print_profile",
    "filament_colour", "extruder_colour",
})

LIMITS = (
    "Orca's reported configuration was compared with the saved inputs. This does not show every Object-scope effect, "
    "and it says nothing about what the printer's firmware does with the G-code.",
)
POST_NOTE = ("The configuration examined is Orca's own output before BrickLayers rewrote the G-code (Orca's baseline). "
             "It is not evidence that the transformed G-code behaves as intended.")

_BOOLEANS = {"true": "1", "1": "1", "false": "0", "0": "0"}
_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?%?$", re.I)
_POINT = re.compile(r"^([+-]?[\d.]+)[x,]([+-]?[\d.]+)$")
_ESCAPE = re.compile(r'\\(n|"|\\)')


# ── Reading the evidence ───────────────────────────────────────────────────────

def read_footer(path) -> dict | None:
    """Orca's reported configuration as {key: raw text}, or None when the file has no configuration footer."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    start, end = text.rfind(FOOTER_START), text.rfind(FOOTER_END)
    if start < 0 or end < start:
        return None
    return dict(re.findall(r"^; ([A-Za-z0-9_]+) = (.*?)\s*$", text[start:end], re.M))


# ── Canonicalization ───────────────────────────────────────────────────────────

def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return _ESCAPE.sub(lambda m: {"n": "\n", '"': '"', "\\": "\\"}[m.group(1)], text).strip()


def _same_element(expected, observed) -> bool:
    a, b = _unquote(str(expected)), _unquote(str(observed))
    if a == b:
        return True
    if a.lower() in _BOOLEANS and b.lower() in _BOOLEANS:
        return _BOOLEANS[a.lower()] == _BOOLEANS[b.lower()]
    if _NUMBER.match(a) and _NUMBER.match(b) and a.endswith("%") == b.endswith("%"):
        return float(a.rstrip("%")) == float(b.rstrip("%"))
    if (pa := _POINT.match(a)) and (pb := _POINT.match(b)):   # Orca writes a lone point as x,y and a polygon as XxY,XxY
        return (float(pa.group(1)), float(pa.group(2))) == (float(pb.group(1)), float(pb.group(2)))
    return False


def _same(expected, observed: str) -> bool:
    if not isinstance(expected, list):
        return _same_element(expected, observed)
    if not expected:
        return not _unquote(observed)
    if len(expected) == 1:
        return _same_element(expected[0], observed)
    for separator in (",", ";"):
        parts = observed.split(separator)
        if len(parts) == len(expected) and all(_same_element(e, o) for e, o in zip(expected, parts)):
            return True
    return False


def _show(value) -> str:
    return ",".join(str(v) for v in value) if isinstance(value, list) else str(value)


def _is_nil(value) -> bool:
    items = value if isinstance(value, list) else [value]
    return bool(items) and all(v is None or str(v) == "nil" for v in items)


# ── Known benign normalization ─────────────────────────────────────────────────

def _choices(definition) -> list:
    return [str(choice[0]) for choice in (definition or {}).get("enum") or []]


def _normalization(key, kind, expected, observed, source, definition):
    """The reason a difference is a documented, benign normalization by Orca, or None when it is a real mismatch."""
    if key == "thumbnails" and re.fullmatch(r"\d+x\d+(,\d+x\d+)*", _show(expected).replace(";", ",")) \
            and re.fullmatch(r"\d+x\d+/[A-Z]+(,\d+x\d+/[A-Z]+)*", _unquote(observed).replace(";", ",")):
        return "Orca appends the image format to each thumbnail size."
    if key == "enable_prime_tower" and _BOOLEANS.get(_show(expected).lower()) == "1" and _BOOLEANS.get(_unquote(observed).lower()) == "0":
        return "Orca turns the prime tower off when a Plate prints with one filament."
    # An inherited preset value that is not a choice of the pinned enum (a vendor file's spelling): Orca falls back to the enum's
    # default, its first declared choice. Only that fallback is benign; a user-set value, or any other change, is a mismatch.
    choices = _choices(definition)
    if choices and isinstance(expected, str) and expected not in choices and source in ("preset", "alternate-preset") \
            and _unquote(observed) == choices[0]:
        return (f"The inherited preset value “{expected}” is not a valid OrcaSlicer 2.4.2 choice; "
                f"Orca replaced it with its default “{choices[0]}”.")
    return None


# ── The comparison ─────────────────────────────────────────────────────────────

def _scope(provenance, kind, key) -> str:
    chain = ((provenance or {}).get(kind) or {}).get(key) or []
    return chain[-1]["source"] if chain else "preset"


def _expected_values(snapshot) -> list:
    """(kind, key, expected value, key whose footer line is read, scope) for every saved input that can be compared.

    A nullable Filament override that is `nil` leaves the inherited machine/process value in force and Orca omits the override
    from its footer, so it is compared through the inherited key. A set override replaces the inherited key's value."""
    schema = snapshot["_schema"]
    rows, overridden = [], {}
    for key, value in (snapshot.get("filament") or {}).items():
        inherits = (schema.get(key) or {}).get("inherits_from")
        if inherits and (schema.get(key) or {}).get("nullable") and not _is_nil(value):
            overridden[(inherits["kind"], inherits["key"])] = (value, _scope(snapshot.get("provenance"), "filament", key))
    for kind in ("machine", "process", "filament"):
        for key, value in (snapshot.get(kind) or {}).items():
            if key in IGNORED:
                continue
            scope = _scope(snapshot.get("provenance"), kind, key)
            if kind == "filament" and (schema.get(key) or {}).get("nullable") and _is_nil(value):
                continue                              # nil override: judged through the inherited key
            if (kind, key) in overridden:
                value, scope = overridden[(kind, key)]
            rows.append((kind, key, value, key, scope))
    present = {(kind, key) for kind, key, *_ in rows}
    for (kind, key), (value, scope) in overridden.items():   # an override of a key the machine/process preset does not state
        if (kind, key) not in present and key not in IGNORED:
            rows.append((kind, key, value, key, scope))
    return rows


def check(snapshot: dict, gcode, schema: dict, *, fingerprint: str | None = None, source: str | None = None, post_processed: bool = False) -> dict:
    """Compare the saved `snapshot` (effective_settings.snapshot) with the footer of `gcode`, a pathlib.Path to Orca's own output."""
    definitions = {**schema.get("definitions", {}), **schema.get("options", {})}
    report = {"version": VERSION, "fingerprint": fingerprint, "evidence": "orca-config-footer", "source": source or gcode.name,
              "compared": 0, "matched": 0, "differences": [], "normalized": [], "unavailable": [], "ignored": sorted(IGNORED),
              "limits": list(LIMITS), "post_processing": None, "object_overrides": None}
    if post_processed:
        identity = snapshot.get("postprocessor") or brick_post.identity()
        report["post_processing"] = {"kind": "bricklayers", "identity": identity, "transform_verified": False, "note": POST_NOTE}
        report["limits"].append("BrickLayers' transformation of the G-code is not verified by this check.")
    changed = [{"object": o["id"], "keys": sorted(o.get("overrides") or {})} for o in snapshot.get("objects") or [] if o.get("overrides")]
    if changed:
        report["object_overrides"] = {"state": "unavailable", "objects": changed,
                                      "note": "Object-scope settings are not part of the footer, so their effect is not checked here."}
    footer = read_footer(gcode)
    if footer is None:
        return _finish(report, "unavailable", "No configuration was found in the G-code Orca produced, so nothing was compared.")

    for kind, key, expected, read_key, scope in _expected_values({**snapshot, "_schema": definitions}):
        if read_key not in footer:
            report["unavailable"].append({"kind": kind, "key": key, "scope": scope, "expected": _show(expected),
                                          "reason": "Orca did not report this setting."})
            continue
        report["compared"] += 1
        observed = footer[read_key]
        if _same(expected, observed):
            report["matched"] += 1
            continue
        row = {"kind": kind, "key": key, "scope": scope, "expected": _show(expected), "observed": _unquote(observed)}
        reason = _normalization(key, kind, expected, observed, scope, definitions.get(key))
        if reason:
            report["normalized"].append({**row, "reason": reason})
        else:
            report["differences"].append({**row, "effect": "print-affecting"})
    if not report["compared"]:
        return _finish(report, "unavailable", "Orca's configuration did not include any of the saved settings, so nothing was compared.")
    return _finish(report, "mismatch" if report["differences"] else "normalized" if report["normalized"] else "consistent")


LABELS = {
    "consistent": "Orca reported the expected settings (configuration compared; not a check of every Object effect or of firmware)",
    "normalized": "Orca reported the expected settings apart from documented normalization (listed below)",
    "mismatch": "Not verified: Orca reported settings that differ from the expected inputs",
    "unavailable": "Output evidence unavailable: not verified",
}


def _finish(report, state, reason=None):
    report.update(state=state, trusted=state in ("consistent", "normalized"), label=LABELS[state])
    if reason:
        report["reason"] = reason
    return report


def unavailable(reason: str) -> dict:
    """The review of a Slice that has no saved output check (made before it existed, or uploaded G-code)."""
    return _finish({"version": VERSION, "evidence": "none", "compared": 0, "matched": 0, "differences": [], "normalized": [],
                    "unavailable": [], "limits": list(LIMITS), "post_processing": None, "object_overrides": None, "source": None}, "unavailable", reason)


def summary(report: dict, limit: int = 20) -> dict:
    """The part of a report kept on the Slice entry: enough to gate the queue and label the plate."""
    return {"state": report["state"], "trusted": report["trusted"], "label": report["label"], "compared": report["compared"],
            "difference_count": len(report["differences"]), "normalized_count": len(report["normalized"]),
            "unavailable_count": len(report["unavailable"]),
            "differences": [{k: d[k] for k in ("kind", "key", "scope", "expected", "observed")} for d in report["differences"][:limit]]}


# Public name for callers outside this module (the parity workflow compares printed values the same way).
same_value = _same
