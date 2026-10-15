"""OrcaSlicer's setting dependency rules, evaluated on the server.

The rules are data in public/slicer/rules-data.js, the same file the browser (rules.js) imports, so there is one copy. This
module reads that file (plain JSON after `export default`) and evaluates it with the same semantics as rules.js; the parity
test tests/test_rules_parity.py runs both over the same states.

Public API:
    evaluate(values)                 -> Toggles(hidden, disabled)   frozensets of keys
    explain(values, key)             -> KeyState(key, visible, greyed, inert, reason)
    inert_changes(values, changes)   -> {key: reason} for proposed changes that would have no effect once applied

`values` maps a setting key to its effective string value; a missing key means unset (the browser's `undefined`).
Numbers are accepted and read as their string form."""
import json
import re
from collections import namedtuple
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "public" / "slicer" / "rules-data.js"

Toggles = namedtuple("Toggles", "hidden disabled")
KeyState = namedtuple("KeyState", "key visible greyed inert reason")
Rule = namedtuple("Rule", "what group keys when because")

_PREFIX = re.compile(r"\A.*?^export default\s*", re.S | re.M)
_FLOAT = re.compile(r"\s*[+-]?(?:Infinity|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)")


def _load() -> tuple:
    text = _PREFIX.sub("", DATA_FILE.read_text(encoding="utf-8"), count=1).strip().rstrip(";")
    return tuple(Rule(r["what"], r.get("group", ""), tuple(r["keys"]), r["when"], r.get("because")) for r in json.loads(text))


RULES = _load()


def _parse_float(value) -> float:
    """JavaScript's `parseFloat(value) || 0`: the leading number of the string, so "15%" is 15 and "abc" is 0."""
    if value is None:
        return 0.0
    match = _FLOAT.match(str(value))
    if not match:
        return 0.0
    number = float(match.group(0).replace("Infinity", "inf"))
    return number if number == number else 0.0


def _text(value):
    return None if value is None else str(value)


def _truthy(value) -> bool:
    return value is not None and value is not False and value != "" and value != 0


def _eval(expr, values):
    if not isinstance(expr, list):
        return expr
    op, args = expr[0], expr[1:]
    if op == "val":
        return _text(values.get(args[0]))
    if op == "num":
        return _parse_float(values.get(args[0]))
    if op == "on":
        return _text(values.get(args[0])) in ("1", "true")
    if op == "off":
        return _text(values.get(args[0])) in (None, "none", "disabled_fuzzy")
    if op == "true":
        return True
    if op == "false":
        return False
    if op == "list":
        return list(args[0])
    if op == "and":
        return all(_truthy(_eval(a, values)) for a in args)
    if op == "or":
        return any(_truthy(_eval(a, values)) for a in args)
    if op == "not":
        return not _truthy(_eval(args[0], values))
    left, right = _eval(args[0], values), _eval(args[1], values)
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "gt":
        return left > right
    if op == "lt":
        return left < right
    if op == "in":
        return left in right
    if op == "startsWith":
        return isinstance(left, str) and left.startswith(right)
    if op == "endsWith":
        return isinstance(left, str) and left.endswith(right)
    raise ValueError(f"unknown rule operator {op}")


def _decide(values) -> dict:
    """{'line': {key: (shown, rule)}, 'field': {key: (enabled, rule)}}: the last rule for a key and kind decides, as in OrcaSlicer."""
    state = {"line": {}, "field": {}}
    for rule in RULES:
        result = _truthy(_eval(rule.when, values))
        for key in rule.keys:
            state[rule.what][key] = (result, rule)
    return state


def evaluate(values) -> Toggles:
    state = _decide(values)
    return Toggles(frozenset(k for k, (shown, _) in state["line"].items() if not shown),
                   frozenset(k for k, (enabled, _) in state["field"].items() if not enabled))


def _drivers(expr, found=None) -> list:
    """Setting keys a condition reads, in order of first appearance."""
    found = [] if found is None else found
    if isinstance(expr, list):
        if expr[0] in ("val", "num", "on", "off"):
            if expr[1] not in found:
                found.append(expr[1])
        elif expr[0] != "list":
            for part in expr[1:]:
                _drivers(part, found)
    return found


def _reason(key: str, kind: str, rule: Rule, values) -> str:
    drivers = [d for d in _drivers(rule.when) if d != key]
    current = ", ".join(f"{d}={_text(values.get(d)) if values.get(d) is not None else 'unset'}" for d in drivers)
    cause = rule.because or "the settings it depends on switch it off"
    return f"no effect because {cause}; {key} is {'hidden' if kind == 'line' else 'greyed out'} by " + (current or "a fixed rule")


def explain(values, key: str) -> KeyState:
    """Whether `key` is shown, greyed out, or inert (hidden or greyed out, so OrcaSlicer ignores it) under `values`, and why."""
    state = _decide(values)
    reason, hidden, greyed = None, False, False
    if key in state["line"] and not state["line"][key][0]:
        hidden = True
        reason = _reason(key, "line", state["line"][key][1], values)
    if key in state["field"] and not state["field"][key][0]:
        greyed = True
        if reason is None:
            reason = _reason(key, "field", state["field"][key][1], values)
    return KeyState(key, not hidden, greyed, hidden or greyed, reason)


def inert_changes(values, changes) -> dict:
    """Of the proposed `changes` ({key: value}), those that would do nothing once applied on top of `values`, with the reason."""
    after = {**values, **changes}
    inert = {}
    for key in changes:
        state = explain(after, key)
        if state.inert:
            inert[key] = state.reason
    return inert
