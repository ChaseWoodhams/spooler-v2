"""Deterministic Plate input resolution; I/O belongs to the Project boundary.

Contract version 1 keeps common CLI profile JSON separate from Object metadata.
Provenance lists are in precedence order, with the last entry the winner.
"""
import copy
import hashlib
import json

import brick_post


def resolve(record, plate, settings, bed, alternate=None, issues=None):
    data, profile = record["data"], record["data"]["profile"]
    objects = [copy.deepcopy(o) for o in data["objects"] if o["plate"] == plate["id"]]
    provenance = {kind: {} for kind in ("machine", "process", "filament")}
    values = {kind: {} for kind in provenance}

    def layer(kind, incoming, source, reason=None):
        for key, value in sorted(incoming.items()):
            values[kind][key] = copy.deepcopy(value)
            provenance[kind].setdefault(key, []).append(
                {"source": source, "value": copy.deepcopy(value), **({"reason": reason} if reason else {})})

    layer("machine", settings["machine"], "preset")
    layer("process", settings["process"], "preset")
    layer("process", profile.get("process") or {}, "project")
    layer("process", plate.get("overrides") or {}, "plate")
    layer("process", {"curr_bed_type": bed}, "bed", "Project bed selection")
    bricks = brick_post.effective(profile.get("bricklayers"), plate.get("bricklayers"))
    if bricks:
        layer("process", brick_post.REQUIRED_PROCESS, "bricklayers", "Required by BrickLayers post-processing")
    layer("filament", alternate if alternate is not None else settings["filament"],
          "alternate-preset" if alternate is not None else "preset")
    if alternate is None:
        layer("filament", profile.get("filament") or {}, "project")
    payload = {**values, "objects": objects, "bricklayers": bricks,
               "geometry": {str(m["id"]): m.get("sha256") for m in record["models"] if m["id"] in {o["model"] for o in objects}}}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"version": 1, "project_id": record["id"], "revision": record["revision"], "plate_id": plate["id"],
            "fingerprint": fingerprint, **values, "objects": objects, "bricklayers": bricks,
            "provenance": provenance, "issues": copy.deepcopy(issues or []), "evidence": "expected-inputs"}
