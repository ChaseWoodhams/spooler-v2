"""The kinds of problem an Object can have on a plate, and the one way they are put into words.

The kinds are plain strings because they travel to the page (`GET /api/projects/<id>/status`) and the page compares them
(public/slicer/viewport.js and main.js use these same strings). No imports here beyond the standard library, so both the
nester (which needs shapely) and the project code can use them.
"""

OFF_THE_BED = "off the bed"
OVER_THE_PURGE_AREA = "over the purge area"
OVERLAPS = "overlaps"        # two outlines touch or cross
TOO_CLOSE = "too close"      # two outlines are apart, but closer than the clearance Arrange keeps
FAILED_MODEL = "failed model"
PAIRED = (OVERLAPS, TOO_CLOSE)   # kinds that involve a second Object (`other`), reported for both

# kind → (as a statement about an Object, as a prediction of a plan); "{other}" is the other Object's name
_WORDING = {
    OFF_THE_BED: ("is off the bed", "would be off the bed"),
    OVER_THE_PURGE_AREA: ("is over the purge area", "would be over the purge area"),
    OVERLAPS: ("overlaps {other}", "would overlap another"),
    TOO_CLOSE: ("is too close to {other}", "would be too close to another"),
    FAILED_MODEL: ("failed to import", "failed to import"),
}


def object_names(data: dict) -> dict:
    """object id → the name to call it by."""
    return {o["id"]: o.get("name") or f"Object {o['id']}" for o in data["objects"]}


def say(kind: str, other_name: str | None = None, planned: bool = False) -> str:
    """What a kind of problem says about an Object: "is off the bed", "overlaps lid", "is too close to lid"; with `planned`,
    what a plan would do: "would be off the bed", "would overlap another"."""
    return _WORDING.get(kind, (kind, kind))[1 if planned else 0].format(other=other_name)


def describe(problems: list, names: dict) -> list:
    """Each problem {"key", "problem", "other", …} with "name" (of the Object) and "text" ("cube overlaps lid") added."""
    return [{**p, "name": names.get(p["key"]), "text": f"{names.get(p['key'])} {say(p['problem'], names.get(p['other']))}"}
            for p in problems]


def reason(described: list, shown: int = 3) -> str | None:
    """Described problems in a sentence: "cube is off the bed; lid overlaps cube; and 2 more". A pair is named once."""
    seen, texts = set(), []
    for p in described:
        pair = frozenset((p["key"], p["other"])) if p["problem"] in PAIRED else None
        if pair is not None and pair in seen:
            continue
        seen.add(pair)
        texts.append(p["text"])
    if not texts:
        return None
    return "; ".join(texts[:shown]) + (f"; and {len(texts) - shown} more" if len(texts) > shown else "")


def planned_reason(problems: list, names: dict, count, shown: int = 5) -> str:
    """Why a plan is refused, by kind: "2 objects would be off the bed (cover, knob); 1 object would overlap another (lid)".
    `count` words a number of objects ("2 objects")."""
    parts = []
    for kind in (OFF_THE_BED, OVER_THE_PURGE_AREA, OVERLAPS, TOO_CLOSE):
        keys = sorted({p["key"] for p in problems if p["problem"] == kind})
        if keys:
            named = ", ".join(names.get(k, f"Object {k}") for k in keys[:shown]) + (", …" if len(keys) > shown else "")
            parts.append(f"{count(len(keys), 'object')} {say(kind, planned=True)} ({named})")
    return "; ".join(parts)
