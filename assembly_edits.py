"""
Editing the assembly plan: Move and Reorder edits, stored with the split and replayed through the step validator.

Public pieces (later tickets build on these)
  replay(model, base_plan, edits, keep_going=False)
                                      Replay: the plan after the edits (`.plan`), the edits that held (`.kept`) and the ones
                                      that did not (`.dropped`, each with its index and EditRefused). Strict by default (stops
                                      at the first failing edit); `keep_going=True` skips failing edits ("Try to keep")
  apply_edit(model, base_plan, edits, edit) -> Plan
                                      one more edit on top of an edit list, or EditRefused
  valid_receivers(model, plan, parts) the units `parts` may be moved to, each already checked by the validator
  reorder_for_step(plan, parts, direction)
                                      the Reorder edit that moves a step up or down
  load(folder) / save(folder, fingerprint, edits) / reset(folder)
                                      the edit file, `guide_edits.json`: {"version", "fingerprint", "edits": [...]}
  plan_line(info)                     "Automatic plan" or "Edited plan (N edits)" for the cover
  EditRefused(kind, message, parts)   a JobError (status 409) carrying the typed reason; `kind` is a validator kind
                                      (rod_direction, rod_side, blocked, disconnected, interlock, anomaly) or one of
                                      unknown_unit, base, inside, same_receiver, bad_order

An edit names sets of part numbers, never Section letters (letters change with the plan):
  {"op": "move", "parts": [12], "to": [15, 16]}      attach part 12 (or the finished Section made of exactly these parts)
                                                     to the unit made of exactly "to" (a Section, or a single part, which
                                                     then becomes the start of a new Section)
  {"op": "reorder", "section": [..], "order": [[3], [5], [6, 7]]}
                                                     the Section made of exactly "section" is built in this order: the first
                                                     entry is the part it starts from, the rest are added one after another

A plan is edited as a tree: a Section is a start part plus the items added to it in order (a part, or a finished Section).
After every edit the whole tree is rebuilt, so steps regroup by the automatic rule (up to 3 parts, same receiver, same
direction) and every step goes through the validator again.
"""

import copy
import hashlib
import json
import os
from pathlib import Path

import numpy as np

import assembly_plan as planner
import jobs

FILE = "guide_edits.json"
VERSION = 1
OPS = ("move", "reorder")


class EditRefused(jobs.JobError):
    def __init__(self, kind: str, message: str, parts=()):
        super().__init__(message, 409)
        self.kind, self.parts = kind, tuple(parts)

    @property
    def extra(self) -> dict:
        return {"kind": self.kind, "parts": list(self.parts)}


# ── The edit file ──────────────────────────────────────────────────────────────

def load(folder: Path) -> dict | None:
    try:
        record = json.loads((Path(folder) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("edits"), list):
        return None
    return record


def save(folder: Path, fingerprint: str, edits: list) -> None:
    path = Path(folder) / FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": VERSION, "fingerprint": fingerprint, "edits": edits}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def reset(folder: Path) -> bool:
    try:
        (Path(folder) / FILE).unlink()
        return True
    except OSError:
        return False


def plan_line(info: dict | None) -> str:
    """The cover's plan line: edits count only while they are applied."""
    if info and info.get("state") == "applied" and info.get("count"):
        n = info["count"]
        return f"Edited plan ({n} edit{'s' if n != 1 else ''})"
    return "Automatic plan"


# ── The plan as an editable tree ───────────────────────────────────────────────

class Node:
    """A Section: the part it starts from and the items added to it in order (a part number, or a finished Node)."""

    def __init__(self, start: int, items=None):
        self.start, self.items = start, list(items or [])

    @property
    def parts(self) -> frozenset:
        return frozenset([self.start]).union(*[_parts(i) for i in self.items])

    def entries(self) -> list:
        return [self.start] + self.items


def _parts(item) -> frozenset:
    return item.parts if isinstance(item, Node) else frozenset([item])


def tree_from_plan(plan) -> list:
    """The root Sections (those no step joins into another) of a Plan, as Nodes."""
    by_letter = {s.letter: s for s in plan.sections}
    joined = {st.section for s in plan.sections for st in s.steps if st.section}

    def build(section):
        items = []
        for step in section.steps:
            if step.kind == "section":
                items.append(build(by_letter[step.section]))
            else:
                items.extend(step.parts)
        return Node(section.start_part, items)
    return [build(s) for s in plan.sections if s.letter not in joined]


def _words(parts) -> str:
    parts = sorted(parts)
    return f"part {parts[0]}" if len(parts) == 1 else "parts " + ", ".join(str(p) for p in parts)


# ── Rebuilding steps from the tree ─────────────────────────────────────────────

def _heavy_key(model, node: Node) -> tuple:
    return (-round(sum(model.volume[p] for p in node.parts), 1), -len(node.parts), min(node.parts))


def _stop(failure: planner.Failure):
    return EditRefused(failure.kind, failure.message, failure.parts)


def build(model, roots: list, fallback_reason: str = ""):
    """The Plan for a tree, every step checked by the validator. Raises EditRefused with the first problem."""
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            pairs = planner._joints_between(model, a.parts, b.parts)
            if pairs:
                x, y = pairs[0]
                raise EditRefused("disconnected", f"{_words(a.parts).capitalize()} would be left on its own, but part {x} touches part {y} "
                                                  f"at a cut, so they have to be joined.", (x, y))
    builder = planner._Planner(model)

    def emit(node: Node, draft) -> None:
        draft.parts |= node.parts
        draft.start = node.start
        receiver = {node.start}
        group, start, group_u = [], None, None

        def flush():
            nonlocal group
            if group:
                builder._make_step(draft, group, start, group_u, "part", None)
                receiver.update(group)
                group = []
        for item in node.items:
            if isinstance(item, Node):
                flush()
                sub = planner._Draft()
                emit(item, sub)
                builder.finished.append(sub)
                u, failure = planner.find_direction(model, item.parts, receiver)
                if failure:
                    raise _stop(failure)
                builder._make_step(draft, item.parts, receiver, np.asarray(u), "section", sub)
                receiver.update(item.parts)
                continue
            u, failure = planner.find_direction(model, [item], receiver)
            if group and not failure and len(group) < planner.MAX_PARTS_PER_STEP and np.allclose(u, group_u) \
                    and not any(g in model.partners.get(item, ()) for g in group) \
                    and planner.validate_step(model, [item], receiver | set(group), u) is None:
                group.append(item)
                continue
            flush()
            u, failure = planner.find_direction(model, [item], receiver)
            if failure:
                raise _stop(failure)
            group, start, group_u = [item], frozenset(receiver), np.asarray(u)
        flush()

    main = min(roots, key=lambda r: _heavy_key(model, r))
    main_draft = planner._Draft(main=True)
    emit(main, main_draft)
    for root in sorted((r for r in roots if r is not main), key=lambda r: min(r.parts)):
        side = planner._Draft(disconnected=True)
        emit(root, side)
        builder.finished.append(side)
    builder.finished.append(main_draft)
    sections, fingerprint = planner._finish(builder.finished)
    return planner.Plan(sections, fingerprint, fallback_reason)


# ── Applying one edit to a tree ────────────────────────────────────────────────

def _find(roots: list, parts):
    """(item, owner Node or None, index) for the unit made of exactly `parts`: a finished Section, or a part added to a Section.
    A part that a Section starts from is ('start', node, 0)."""
    wanted = frozenset(parts)

    def walk(node, owner, index):
        if node.parts == wanted:
            return node, owner, index
        if len(wanted) == 1 and node.start in wanted:
            return "start", node, 0
        for i, item in enumerate(node.items):
            found = walk(item, node, i) if isinstance(item, Node) else ((item, node, i) if item in wanted and len(wanted) == 1 else None)
            if found:
                return found
        return None
    for i, root in enumerate(roots):
        found = walk(root, None, i)
        if found:
            return found
    return None


def _unit(roots, parts, what: str):
    parts = sorted({int(p) for p in parts or []})
    found = _find(roots, parts) if parts else None
    if not found:
        raise EditRefused("unknown_unit", f"{what} {_words(parts) if parts else '(none given)'} is not a part or finished Section of this plan.", parts)
    return found


def _collapse(roots: list, node: Node) -> None:
    """A Section that has lost all its items is just its start part again."""
    if node.items:
        return
    for root_owner in [r for r in roots] + [n for r in roots for n in _nodes(r)]:
        if node in root_owner.items:
            root_owner.items[root_owner.items.index(node)] = node.start
            return


def _nodes(node: Node):
    for item in node.items:
        if isinstance(item, Node):
            yield item
            yield from _nodes(item)


def _move(roots: list, edit: dict) -> None:
    x, x_owner, x_index = _unit(roots, edit.get("parts"), "Move:")
    r, r_owner, r_index = _unit(roots, edit.get("to"), "Move to:")
    x_parts, r_parts = sorted(_parts(x)) if x != "start" else [], sorted(_parts(r)) if r != "start" else []
    if x == "start":
        part = sorted(edit["parts"])[0]
        raise EditRefused("base", f"Part {part} is the part its Section starts from, so it can't be moved on its own. "
                                  f"Reorder the Section so another part starts it, or move the whole Section.", [part])
    if r == "start":
        raise EditRefused("unknown_unit", f"Move to: {_words(edit['to'])} is not a Section or an added part. Name the whole Section it starts.", edit["to"])
    whole = frozenset(x_parts)
    if r is x or (r.parts <= whole if isinstance(r, Node) else r in whole):
        raise EditRefused("inside", f"{_words(x_parts).capitalize()} can't be attached to itself or to something inside it.", x_parts)
    if r is x_owner:
        raise EditRefused("same_receiver", f"{_words(x_parts).capitalize()} is already attached to {_words(r_parts)}. "
                                           f"To change when it goes on, reorder the steps instead.", x_parts)
    # take it out
    if x_owner is None:
        roots.remove(x)
    else:
        x_owner.items.pop(x_index)
        _collapse(roots, x_owner)
    # put it on
    if isinstance(r, Node):
        r.items.append(x)
        return
    holder = next((n for n in [root for root in roots] + [n for root in roots for n in _nodes(root)] if r in n.items), None)
    if holder is None:   # the receiving part is a lone Section's start that collapsed under it
        raise EditRefused("unknown_unit", f"Move to: {_words(r_parts)} is no longer available after taking the part out.", r_parts)
    holder.items[holder.items.index(r)] = Node(r, [x])


def _reorder(roots: list, edit: dict) -> None:
    node, _owner, _index = _unit(roots, edit.get("section"), "Reorder:")
    if not isinstance(node, Node):
        raise EditRefused("unknown_unit", f"Reorder: {_words(edit.get('section') or [])} is not a Section of this plan.", edit.get("section") or [])
    order = [sorted(int(p) for p in entry) for entry in edit.get("order") or []]
    current = {tuple(sorted(_parts(e))): e for e in node.entries()}
    if sorted(map(tuple, order)) != sorted(current) or len(order) != len(current):
        raise EditRefused("bad_order", "That order doesn't list exactly the parts and finished Sections this Section is built from.", sorted(node.parts))
    entries = [current[tuple(entry)] for entry in order]
    if isinstance(entries[0], Node):
        raise EditRefused("base", "A finished Section can't be the part a Section starts from: it has to be joined on.", sorted(entries[0].parts))
    node.start, node.items = entries[0], entries[1:]


def _apply_to_tree(roots: list, edit: dict) -> None:
    op = edit.get("op") if isinstance(edit, dict) else None
    if op == "move":
        _move(roots, edit)
    elif op == "reorder":
        _reorder(roots, edit)
    else:
        raise EditRefused("bad_order", "Unknown edit: only Move and Reorder are possible.")


def _clean(edit: dict) -> dict:
    """The edit as stored: sorted part lists only."""
    if not isinstance(edit, dict) or edit.get("op") not in OPS:
        raise EditRefused("bad_order", "Unknown edit: only Move and Reorder are possible.")
    if edit.get("op") == "move":
        return {"op": "move", "parts": sorted({int(p) for p in edit.get("parts") or []}), "to": sorted({int(p) for p in edit.get("to") or []})}
    return {"op": "reorder", "section": sorted({int(p) for p in edit.get("section") or []}),
            "order": [sorted(int(p) for p in entry) for entry in edit.get("order") or []]}


# ── Replaying a list of edits ──────────────────────────────────────────────────

class Replay:
    def __init__(self, plan, kept: list, dropped: list):
        self.plan, self.kept, self.dropped = plan, kept, dropped   # dropped: [(index, edit, EditRefused)]

    @property
    def ok(self) -> bool:
        return not self.dropped


def replay(model, base_plan, edits: list, keep_going: bool = False) -> Replay:
    """Apply `edits` in order to the automatic plan, rebuilding and validating after each one."""
    roots, plan, kept, dropped = tree_from_plan(base_plan), base_plan, [], []
    for index, edit in enumerate(edits):
        trial = copy.deepcopy(roots)
        try:
            edit = _clean(edit)
            _apply_to_tree(trial, edit)
            plan = build(model, trial, base_plan.fallback_reason)
        except EditRefused as refused:
            dropped.append((index, edit, refused))
            if not keep_going:
                break
            continue
        except (TypeError, ValueError, AttributeError):
            dropped.append((index, edit, EditRefused("bad_order", "This edit is not readable.")))
            if not keep_going:
                break
            continue
        roots = trial
        kept.append(edit)
    if dropped and not keep_going:
        return Replay(None, kept, dropped)
    return Replay(plan if kept else base_plan, kept, dropped)


def apply_edit(model, base_plan, edits: list, edit: dict):
    """The plan after `edits` plus one more `edit`; raises EditRefused when the new one is not allowed. Returns (plan, stored edit)."""
    done = replay(model, base_plan, edits)
    if not done.ok:
        raise done.dropped[0][2]
    roots = tree_from_plan(base_plan)
    for old in done.kept:
        _apply_to_tree(roots, old)
    edit = _clean(edit)
    _apply_to_tree(roots, edit)
    return build(model, roots, base_plan.fallback_reason), edit


def current_tree(model, base_plan, edits: list) -> list:
    roots = tree_from_plan(base_plan)
    for old in edits:
        _apply_to_tree(roots, old)
    return roots


def valid_receivers(model, base_plan, edits: list, parts) -> list:
    """Where `parts` (a part or finished Section) can go: [{"parts": [...], "kind": "section"|"part"}], validator-checked."""
    parts = sorted({int(p) for p in parts})
    roots = current_tree(model, base_plan, edits)
    x = _unit(roots, parts, "Move:")[0]
    candidates = []

    def visit(node):
        candidates.append((sorted(node.parts), "section"))
        for item in node.items:
            if isinstance(item, Node):
                visit(item)
            else:
                candidates.append(([item], "part"))
    for root in roots:
        visit(root)
    found = []
    for target, kind in candidates:
        if target == parts:
            continue
        placed = set(target)
        if planner.find_direction(model, parts, placed)[1] is not None and not (kind == "section" and not placed.isdisjoint(parts)):
            continue
        try:
            apply_edit(model, base_plan, edits, {"op": "move", "parts": parts, "to": target})
        except EditRefused:
            continue
        found.append({"parts": target, "kind": kind})
    return found


def reorder_for_step(model, base_plan, edits: list, parts, direction: str) -> dict:
    """The Reorder edit that moves the step made of `parts` one step "up" (earlier) or "down" (later) in its Section.
    Moving the first step up makes its part the new start, and the old start comes right after it."""
    if direction not in ("up", "down"):
        raise EditRefused("bad_order", "Direction must be up or down.")
    parts = sorted({int(p) for p in parts})
    plan = replay(model, base_plan, edits).plan or base_plan
    for section in plan.sections:
        for i, step in enumerate(section.steps):
            if sorted(step.parts) != parts:
                continue
            blocks = [[[section.start_part]]]   # block 0 is the start; each later block is one step
            for st in section.steps:
                blocks.append([sorted(st.parts)] if st.kind == "section" else [[p] for p in sorted(st.parts)])
            j = i + 1
            target = j - 1 if direction == "up" else j + 1
            if target < 0 or target >= len(blocks):
                raise EditRefused("bad_order", f"{_words(parts).capitalize()} is already the {'first' if direction == 'up' else 'last'} step.", parts)
            blocks[j], blocks[target] = blocks[target], blocks[j]
            if target == 0 and (len(blocks[0]) != 1 or step.kind == "section"):
                raise EditRefused("base", "Only a single part can become the part its Section starts from.", parts)
            return {"op": "reorder", "section": sorted(section.parts), "order": [entry for block in blocks for entry in block]}
    raise EditRefused("unknown_unit", f"{_words(parts).capitalize()} is not a step of this plan.", parts)
