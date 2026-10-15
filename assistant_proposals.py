"""Reviewing, accepting and dismissing a staged Assistant Proposal: the app side of the Proposal pipeline.

The Spooler MCP server stages a Proposal (see `proposal_store.py`); this module is the only thing that turns one into a change,
and only when the person accepts it. It is kind-agnostic: orientation, arrange, plan and settings Proposals all go through
`review`, `accept` and `dismiss`, because a Proposal already holds its resulting project data.

  review(project_id, token)           the review sheet's view (the shape Plan plates renders, with a "before" side added)
  accept(project_id, token, body)     body {revision}: apply the stored data with `save_data` as ONE revision, return the project
  dismiss(project_id, token)          throw the Proposal away; changes nothing

A Proposal is only good for the revision it was made at. If the project is at another revision (any edit at all), `review` and
`accept` answer 409 with `superseded: true` and delete the staging: nothing is rebased or merged, the person asks again.
"""
import datapaths
import placement_read
import profile_read
import project_read
import proposal_store
from errors import JobError


class ProposalGone(JobError):
    """A Proposal that cannot be used: unknown or already swept (404), or made for a revision the project has left (409, superseded)."""

    def __init__(self, message: str, status: int, superseded: bool = False):
        super().__init__(message, status)
        self.extra = {"superseded": True} if superseded else {}


def _root():
    return datapaths.projects_dir()


def _load(project_id: int, token) -> dict:
    record = proposal_store.read(_root(), token)
    if record is None or record.get("project_id") != int(project_id):
        raise ProposalGone("This proposal is no longer available. Ask again", 404)
    return record


def _current(project_id: int, proposal: dict, revision=None) -> dict:
    """The project, if the Proposal still fits it. Otherwise it is Superseded: swept, and the caller is told so."""
    import projects
    record = projects.get_project(project_id)
    if record["revision"] != proposal["base_revision"] or (revision is not None and revision != record["revision"]):
        superseded(proposal["token"])
        raise ProposalGone("The project changed since this proposal was made, so it was not applied. Ask again", 409, superseded=True)
    return record


def superseded(token: str) -> None:
    proposal_store.remove(_root(), token)


# ── Review ────────────────────────────────────────────────────────────────────

def _changed_plates(before: dict, after: dict) -> list:
    """Plate ids whose objects differ between two versions of the project data (placement, rotation, plate), in plate order."""
    def objects(data):
        by_plate = {}
        for obj in data.get("objects") or []:
            by_plate.setdefault(obj["plate"], {})[obj["id"]] = (tuple(obj["position"]), tuple(obj["rotation"]), tuple(obj["scale"]))
        return by_plate
    old, new = objects(before), objects(after)
    ids = []
    for data in (before, after):
        for plate in data.get("plates") or []:
            if plate["id"] not in ids and old.get(plate["id"], {}) != new.get(plate["id"], {}):
                ids.append(plate["id"])
    return ids


def _side(project_id: int, record: dict, data: dict, plate_ids: list, moved: set, existing: set) -> list:
    """The plates of one side of a change as the Plan plates sheet draws them: every object on the plate, with its outline."""
    shapes = placement_read._Shapes(project_id, record, data)
    names = {p["id"]: p.get("name") or f"Plate {p['id']}" for p in data.get("plates") or []}

    def outline(obj):
        try:
            return [[round(x, 1), round(y, 1)] for x, y in shapes.polygon(obj)]
        except Exception:                                      # a Model with no mesh (still importing, failed): the sheet draws a box
            return None

    plates = []
    for plate_id in plate_ids:
        if plate_id not in names:
            continue
        objects = [o for o in data.get("objects") or [] if o["plate"] == plate_id]
        found = project_read.plate_filaments(data, plate_id)
        plates.append({"id": plate_id, "name": names[plate_id], "new": plate_id not in existing, "filament": project_read.choice_label(found[0]) if found else None,
                       "parts": len(objects), "turned": sum(1 for o in objects if o["id"] in moved), "time_estimate": None,
                       "objects": [{"id": o["id"], "name": o.get("name"), "model": o["model"], "position": o["position"], "rotation": o["rotation"],
                                    "pinned": bool(o.get("pinned")), "outline": outline(o)} for o in objects]})
    return plates


def review(project_id: int, token) -> dict:
    """What the review sheet shows. For a "placement" Proposal: `before` and `after`, each {plates: [...]} in the Plan plates
    plate shape, limited to the plates the change touches; `plates` and `kept` repeat the after side so the same sheet renders
    it. A "settings" Proposal carries its own `settings` rows instead (stored by the propose tool)."""
    import projects
    proposal = _load(project_id, token)
    record = _current(project_id, proposal)
    view = {**proposal_store.summary(proposal), "revision": proposal["base_revision"], "summary": proposal.get("scope"), "outcome": proposal.get("scope"),
            "kept": [], "refused": None, "settings": proposal.get("settings")}
    if proposal.get("review") == "placement":
        before, after = record["data"], proposal["data"]
        ids = _changed_plates(before, after)
        moved = {o["id"] for o in after.get("objects") or []
                 if (old := next((b for b in before.get("objects") or [] if b["id"] == o["id"]), None)) is None
                 or (old["position"], old["rotation"]) != (o["position"], o["rotation"])}
        before_ids = {p["id"] for p in before.get("plates") or []}
        try:
            bed = list(projects._machine_limits_for(record)["bed"])
        except JobError:
            bed = list(profile_read.DEFAULT_BED)
        side_before = _side(project_id, record, before, ids, set(), before_ids)
        side_after = _side(project_id, record, after, ids, moved, before_ids)
        view.update({"bed": bed, "pack": project_read.pack_for(after), "before": {"plates": side_before},
                     "after": {"plates": side_after}, "plates": side_after})
    return view


# ── Accept and dismiss ────────────────────────────────────────────────────────

def accept(project_id: int, token, body: dict) -> dict:
    """Apply the Proposal's stored data as one revision. `body.revision` is the revision the page showed; it and the Proposal's own
    base revision must both be the project's current one. Returns the project (one revision later, so the page records one undo
    step) plus `proposal`, the card summary of what was applied."""
    import projects
    revision = (body or {}).get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise JobError("Send the revision the proposal was shown at")
    proposal = _load(project_id, token)
    record = _current(project_id, proposal, revision)
    data = projects.validate_data(proposal["data"], record)
    try:
        projects.save_data(project_id, record, data, proposal.get("emptied") or [])
    except JobError as e:
        if e.status == 409:                                    # an edit landed between the check and the write
            superseded(token)
            raise ProposalGone("The project changed since this proposal was made, so it was not applied. Ask again", 409, superseded=True) from None
        raise
    proposal_store.remove(_root(), token)
    return {**projects.get_project(project_id), "proposal": proposal_store.summary(proposal)}


def dismiss(project_id: int, token) -> dict:
    """Throw a Proposal away. Idempotent, and it never touches project data; the page also calls it to clean up a Superseded card."""
    proposal = proposal_store.read(_root(), token)
    if proposal is not None and proposal.get("project_id") == int(project_id):
        proposal_store.remove(_root(), token)
    return {"ok": True}
