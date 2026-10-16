"""Assistant tuning: the knobs that shape an Assistant run, kept in one versioned section of the Assistant settings file.

Shape (later groups are added to GROUPS, nothing else is reworked):
  GROUPS[name] = {"label", "hint", "fields": {key: field}}      what a group is: defaults, bounds, wording
  field        = {"label", "hint", "type": "int", "unit", "default", "min", "max"}   (other types add their own checks in `_check`)
  file         = assistant_settings.json -> "tuning": {"version": 1, "<group>": {"<key>": value}}   only saved keys are stored
  load()       -> (values, notes)     values = {group: {key: value}} with every default filled in; never raises. A bad key falls back
                                       to its default and adds a note; unknown groups and keys are ignored.
  view()       -> what the Settings route returns: version, values, per-group field descriptions, notes
  save(body)   -> {group: {key: value}} merged into the stored section after validating all of it; ValueError (AssistantError) names the bound
  reset(group) -> drop one group's saved keys, or all of them with "all"
  snapshot()   -> a deep copy of the values: a run takes it once at its start and reads only that, so editing never changes a run
                  already working.
"""
import copy

import assistant
import spooler_mcp

VERSION = spooler_mcp.TUNING_VERSION

# One hint per Proposal kind, in the order of spooler_mcp.PROPOSAL_KINDS, which owns the list of kinds and their words.
_KIND_HINTS = {
    "orientation": "Propose a better orientation for Objects.",
    "arrange": "Propose an Arrange of the Objects on Plates.",
    "plan": "Propose Plan plates for the whole project.",
    "settings": "Propose slicer setting changes.",
}
assert tuple(_KIND_HINTS) == spooler_mcp.PROPOSAL_KINDS, "every Proposal kind needs a hint here, in the tool server's order"

GROUPS = {
    "limits": {
        "label": "Limits",
        "hint": "How long, how deep and how much an Assistant run may take. Each applies to the next run only.",
        "fields": {
            "deadline_seconds": {"label": "Run deadline", "type": "int", "unit": "seconds", "default": 60, "min": 5, "max": 600,
                                 "hint": "An Assistant run that has not answered by then is stopped and nothing is changed."},
            "max_turns": {"label": "CLI turn cap", "type": "int", "unit": "turns", "default": 12, "min": 1, "max": 50,
                          "hint": "The most tool-using turns the CLI may take in one run."},
            "proposals_per_run": {"label": "Proposals per run", "type": "int", "unit": "Proposals", "default": 3, "min": 1, "max": 10,
                                  "hint": "The most Proposals one run may stage."},
            "question_chars": {"label": "Longest question", "type": "int", "unit": "characters", "default": 2000, "min": 100, "max": 10000,
                               "hint": "The longest question the panel accepts."},
        },
    },
    "instructions": {
        "label": "Instructions",
        "hint": "The Assistant's whole instruction text. Empty means the built-in text. Applies to the next run only. The Assistant's powers do not "
                "depend on this text: its tools, read-only access to the one project, Proposals that are only staged, and treating quoted names "
                "and notes as data stay fixed in Spooler.",
        "fields": {
            "text": {"label": "Instruction text", "type": "text", "unit": "characters", "default": "", "min": 0, "max": 8000,
                     "hint": "Spooler adds a short fixed block after it (the Proposals-per-run cap), which cannot be edited."},
        },
    },
    "model": {
        "label": "Model",
        "hint": "Which Claude model an Assistant run uses. Blank means the CLI's own default. The name is not checked here: a model the CLI rejects ends the run with its message.",
        "fields": {
            "name": {"label": "Model name", "type": "str", "unit": "text", "default": "", "min": 0, "max": 100,
                     "hint": "For example claude-sonnet-4-5; one word, no spaces."},
        },
    },
    "prompt_context": {
        "label": "Prompt context",
        "hint": "What each question sends along. The revision, the Model list (ids and names) and the project name are always sent. Applies to the next question.",
        "fields": {
            "send_purposes": {"label": "Model Purposes", "type": "bool", "default": True,
                              "hint": "Send each Model's Purpose."},
            "send_purpose_notes": {"label": "Model Purpose notes", "type": "bool", "default": True,
                                   "hint": "Send the note written on each Purpose, and on the project default Purpose."},
            "send_default_purpose": {"label": "Project default Purpose", "type": "bool", "default": True,
                                     "hint": "Send the Purpose that Models without their own use."},
            "history_enabled": {"label": "Chat history", "type": "bool", "default": True,
                                "hint": "Send the earlier turns of this chat. Off, every question stands alone."},
            "history_turns": {"label": "History turns kept", "type": "int", "unit": "turns", "default": 12, "min": 1, "max": 50,
                              "hint": "The most earlier turns sent; the oldest are dropped first."},
            "history_chars": {"label": "History characters kept", "type": "int", "unit": "characters", "default": 6000, "min": 200, "max": 20000,
                              "hint": "The most characters of history sent in all."},
            "turn_chars": {"label": "Characters per turn", "type": "int", "unit": "characters", "default": 1500, "min": 100, "max": 5000,
                           "hint": "Longer turns are cut to this."},
        },
    },
    "proposal_kinds": {
        "label": "Proposal kinds",
        "hint": "Which kinds of Proposal the Assistant may make. A kind switched off is taken away from its tools, and it tells the person so. Reading tools are unaffected. "
                "Applies to the next run and to terminal sessions.",
        "fields": {kind: {"label": spooler_mcp.KIND_WORDS[kind], "type": "bool", "default": True, "hint": hint} for kind, hint in _KIND_HINTS.items()},
    },
}


def defaults() -> dict:
    return {group: {key: field["default"] for key, field in spec["fields"].items()} for group, spec in GROUPS.items()}


def _check(group: str, key: str, value):
    """The value if it fits the field, else raises ValueError with the bound in the message."""
    field = GROUPS[group]["fields"][key]
    if field["type"] == "text":
        if not isinstance(value, str):
            raise ValueError(f"{field['label']} must be text")
        if len(value) > field["max"]:
            raise ValueError(f"{field['label']} cannot be longer than {field['max']} {field['unit']} (got {len(value)})")
        return value if value.strip() else ""
    if field["type"] == "str":
        if not isinstance(value, str):
            raise ValueError(f"{field['label']} must be text")
        value = value.strip()
        if len(value) > field["max"]:
            raise ValueError(f"{field['label']} cannot be longer than {field['max']} characters")
        if value and (value.startswith("-") or not value.isprintable() or any(c.isspace() for c in value)):
            raise ValueError(f"{field['label']} must be one word without spaces and not start with a dash")
        return value
    if field["type"] == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{field['label']} must be on or off")
        return value
    ok = isinstance(value, int) and not isinstance(value, bool)
    if not ok and isinstance(value, float) and value.is_integer():
        value, ok = int(value), True
    if not ok:
        raise ValueError(f"{field['label']} must be a whole number from {field['min']} to {field['max']} {field['unit']}")
    if value < field["min"]:
        raise ValueError(f"{field['label']} cannot be below {field['min']} {field['unit']} (got {value})")
    if value > field["max"]:
        raise ValueError(f"{field['label']} cannot be above {field['max']} {field['unit']} (got {value})")
    return value


def _stored() -> tuple:
    """(the saved section as a dict, notes). A section that cannot be used at all is {} with a note."""
    raw, damaged = assistant.read_raw_checked()
    if damaged:
        return {}, [damaged]
    section = raw.get("tuning")
    if section is None:
        return {}, []
    if not isinstance(section, dict):
        return {}, ["The saved tuning could not be read, so the defaults are in use."]
    if section.get("version", VERSION) != VERSION:
        return {}, [f"The saved tuning is version {str(section.get('version'))[:20]}, which this Spooler does not know, so the defaults are in use."]
    return section, []


def load() -> tuple:
    section, notes = _stored()
    values = defaults()
    for group, spec in GROUPS.items():
        saved = section.get(group)
        if saved is None:
            continue
        if not isinstance(saved, dict):
            notes.append(f"{spec['label']}: the saved values could not be read, so the defaults are in use.")
            continue
        for key, field in spec["fields"].items():
            if key not in saved:
                continue
            try:
                values[group][key] = _check(group, key, saved[key])
            except ValueError:
                notes.append(f"{spec['label']}, {field['label']}: the saved value is not allowed, so the default ({field['default']}) is in use.")
    return values, notes


def snapshot() -> dict:
    return copy.deepcopy(load()[0])


def view() -> dict:
    values, notes = load()
    out = _view(values, notes)
    out["groups"]["instructions"]["fields"]["text"]["builtin"] = builtin_instructions()
    return out


def _view(values, notes) -> dict:
    return {"version": VERSION, "values": values, "defaults": defaults(), "notes": notes,
            "groups": {group: {"label": spec["label"], "hint": spec["hint"], "fields": copy.deepcopy(spec["fields"])} for group, spec in GROUPS.items()}}


def builtin_instructions() -> str:
    """The built-in instruction text the page shows beside the editor (the runner owns it)."""
    import assistant_runner
    return assistant_runner.SYSTEM_PROMPT.strip()


def save(body, reset=None) -> None:
    """Validate everything first, then write once: a refused save changes nothing. `reset` is a group name or "all", applied before `body`."""
    if body is not None and not isinstance(body, dict):
        raise assistant.AssistantError("tuning must be an object of groups")
    if reset is not None and reset != "all" and reset not in GROUPS:
        raise assistant.AssistantError(f"Unknown tuning group: {str(reset)[:40]}")
    checked = {}
    for group, items in (body or {}).items():
        if group not in GROUPS:
            raise assistant.AssistantError(f"Unknown tuning group: {str(group)[:40]}")
        if not isinstance(items, dict):
            raise assistant.AssistantError(f"{GROUPS[group]['label']} must be an object of values")
        for key, value in items.items():
            if key not in GROUPS[group]["fields"]:
                raise assistant.AssistantError(f"Unknown {GROUPS[group]['label']} setting: {str(key)[:40]}")
            try:
                checked.setdefault(group, {})[key] = _check(group, key, value)
            except ValueError as e:
                raise assistant.AssistantError(str(e)) from None
    raw = assistant.read_raw()
    section, _ = _stored()
    section = {"version": VERSION, **{g: dict(v) for g, v in section.items() if g in GROUPS and isinstance(v, dict)}}
    for group in (list(GROUPS) if reset == "all" else [reset] if reset else []):
        section.pop(group, None)
    for group, items in checked.items():
        section.setdefault(group, {}).update(items)
    raw["tuning"] = section
    assistant.write_raw(raw)
