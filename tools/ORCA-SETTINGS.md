# Pinned settings validation

The browser and Project/preset edit paths use `public/orca/settings.json`, generated
from OrcaSlicer **v2.4.2**. `options` contains tab-classified controls;
`definitions` also records source-only FFF definitions for inspecting inherited
values. A source-only definition or a key present in a preset does not grant edit
permission. Develop, readonly, script and unclassified controls remain excluded.

The generator retains copied enum choices, nullable declarations, numeric
constants/referenced bounds, ratio targets and literal-limit metadata, and writes
Unicode without transliteration. `scopes` records the pinned tab classification;
the Object allowlist is the union of comment-stripped `PrintObjectConfig` and
`PrintRegionConfig` declarations in pinned `PrintConfig.hpp`. Runtime editing
requires both this membership and an editable typed Process definition; macro-only
or Develop fields are not exposed.

Primary sources:

- [PrintConfig.cpp](https://github.com/OrcaSlicer/OrcaSlicer/blob/v2.4.2/src/libslic3r/PrintConfig.cpp): SHA256 `f635101adb6da3ee657656c6fcaa9449871b7c17446b5e2eb59b560a0eb836aa`; constants and definitions, and `validate()` numeric-token/width/spiral invariants.
- [libslic3r.h](https://github.com/OrcaSlicer/OrcaSlicer/blob/v2.4.2/src/libslic3r/libslic3r.h#L68): SHA256 `670fb8854137c1119be599848a1cd6f28a868a04005f45e5aa2b02d6d04c4ef9`; `MAX_LINE_WIDTH_MULTIPLIER = 5`.
- [ConfigManipulation.cpp](https://github.com/OrcaSlicer/OrcaSlicer/blob/v2.4.2/src/slic3r/GUI/ConfigManipulation.cpp): SHA256 `454e20b9b13fa5cd24b41db62c4fbabd12bbdd55060c7460acbd9752a056a13f`; scarf height versus layer height (lines525–532), infill lock versus skin depth (lines538–549), and ordered recommended temperature limits. Orca offers automatic corrections; Spooler returns suggestions without applying them.

Line widths accept Orca's automatic zero sentinel. Relative tokens use the field's
pinned range, not a universal 100% ceiling. The effective width uses the selected
nozzle: bridge width cannot exceed the smallest nozzle; other tested widths cannot
exceed five times the largest nozzle. `max_literal` is retained as Orca GUI metadata,
not misrepresented as an unconditional runtime physical limit. `max_temp = 1500`
is the declared schema limit, not a claim about CC1 heater capability.

Infill depth coupling applies while Locked Zag is selected; inactive saved values
remain available. Project validation composes Project → Plate → Object first,
then compares coupled issues with the previous saved configuration. Unchanged
legacy errors survive an unrelated save and remain visible in read-only review.
The follow-on scope ticket owns explicit resolution and output blocking.

Service regressions cover Project saves and reviews, user preset import/save,
malformed/nonfinite values, copied enums, contextual widths, legacy values,
temperature constants, scarf and infill coupling, and Filament temperature ranges.
The two browser suites exercise controls and Project HTTP-save rejection using
disposable routed API fixtures.

Native reference verification on October 14, 2026 used the installed 2.4.2 binary,
separate disposable data directories and real Elegoo PETG/ASA presets. Both 20 mm
cube runs with 150% outer-wall width exited 0, emitted G-code, retained the relative
width in the G-code configuration and reported the correct material identity.
This confirms those reference inputs; no printer hardware was exercised.


Object scope source: [PrintConfig.hpp](https://github.com/OrcaSlicer/OrcaSlicer/blob/v2.4.2/src/libslic3r/PrintConfig.hpp),
SHA256 `26c193d279772f4a9f4d2222bcf3e17e12335af0634d51c5cde22a1e0010a89e`.
Regenerate with `python tools/orca_settings.py`; comments and other configuration
classes cannot admit Object controls. Scope is derived from declarations, never
from a historical count. `travel_speed` belongs to Project/Plate; `wall_loops`
is an eligible Object setting.

Saved disallowed or unknown overrides remain in their original scope. Review
reports their original value and owner; the Object editor also lists hidden old
controls. `POST /api/projects/{id}/settings/resolve` takes the current revision,
scope, affected id, kind, key and remove/move action. Moves validate the value at
the destination, report existing destination values before replacement, and
require an explicit replacement choice. Commit checks Project identity and revision.
New output is blocked for affected Plates with unresolved saved overrides; old
Slice references and artifacts remain readable. Read-only inherited preset
metadata is still reported separately from authored overrides.

Native verification: `SPOOLER_ORCA=<bundled executable> python -m unittest
 tests.test_object_scope_orca` runs only disposable Projects/data directories.
The PETG cube's Object override from one wall to four increases actual Inner-wall
deposition commands and matches an independent Orca invocation using a common
four-wall process with the Object override removed. This is a representative
Object-effect check, not a claim about all Orca features or printer firmware.

## Import loss review

Every 3MF import path (new single-Model Project, new whole Project, append, plain
3MF) is staged under `projects/.import-staging/<token>/` and inspected from the
original archive by `orca_3mf.inspect` before any Project, Split or Model folder
changes. `POST /api/projects/import-review` (raw body; `target`, `draft_token`,
`scale`, `up`, `plate`, `quick` in the query) returns the report; `.../<token>/confirm`
commits it and `DELETE .../<token>` cancels. Report version 1 lists `losses`
(scope `model`, `object` or `plate`, affected `name`, stable `code`) and `blockers`
(`unmatched-material`, `unmatched-printer`, `malformed-settings`,
`malformed-project-settings`, `nothing-printable`, `unreadable-geometry`). The
binding echoed by confirm covers the content hash, the proposed conversion digest,
the report version and the target's id, `created_at` and revision; the transaction
re-checks the target before moved Model folders are published, and a failure
removes only what that commit moved. Accepted records (report, notes, original
archive) live in `projects/<id>/imports/<token>/` and are listed at
`GET /api/projects/<id>/imports`. A material or printer substitution is a blocker,
never a loss; resolving it belongs to the next import ticket. The older direct
routes refuse any file that has losses or blockers (HTTP 409 `import-review-required`).

### Imported material and Plate settings (ticket 10)

An unmatched material (or printer) stays a blocker, now with `resolutions`: the file's own `imported` configuration (available only when
the file names a material and its nozzle fits the printer) and every stock `preset` for the printer with the material, nozzle and the
file-versus-preset differences of the main PETG/ASA settings. `POST /api/projects/import-review/<token>/resolve` takes
`{material: {choice: "imported" | "preset", preset, accept_material_change}, printer, conflicts: "convert"}`, re-stages the conversion and
returns the review with a new binding (an earlier confirmation is stale). A preset of another material than the file's is refused unless
`accept_material_change` is true, and a resolution that changes the material requires the acknowledgement at confirmation. A refused choice
leaves the review as it was. The file's own configuration is stored as an immutable, content-addressed Filament snapshot (Filament tab keys
only, validated like edits, full vectors and nullable `nil` preserved; scripts and `*_gcode` stay blocked).

New Project: the resolved material becomes the Project's locked profile. Append: the file's process differences against the target's
effective process become Plate overrides on the Plates it brings, Object differences become Object overrides only for Object-eligible
keys, and its material becomes a snapshot-backed Model Filament choice (none when it equals the Project's). A different printer, nozzle or
bed blocks (`incompatible-machine`, `incompatible-bed`, options `separate-project` / `convert`). The report keeps `differences`
(blocked scripts, rejected values, `uncompared-settings` the chosen presets do not state, conversions) and the applied `resolution`;
both are retained with the accepted import record and neither claims full fidelity.

## Settings actually used (review rows)

`projects.effective_settings(project, plate, revision, object_id=, search=, changed_only=)` and
`GET /api/projects/{id}/plates/{plate}/effective-settings?revision=&object=&search=&changed=1` add a
`review` block built by `effective_settings.review` from the same resolved layers and provenance
the Slice uses. Each row carries `inherited` (preset), `requested` (top Project/Plate/Object
override), `effective`, `source`, the ordered `chain`, `changed` (Plate view: differs from the
preset; Object view: differs from the Plate), `enforced`/`enforced_reason` (bed selection and
BrickLayers), `inactive_reason` from the pinned dependency metadata (the saved value is kept),
`reset_to` (the parent that removing the top override restores, or the Orca default),
`editable`/`read_only_reason` and the owning `control`. Search matches labels and keys.
Keys with no pinned control and all machine values are listed read-only and passed through
unchanged; chamber keys carry a note that Spooler does not claim chamber heater control.
Issues gain a `control` link (null and `read_only` for inherited ones). Values are expected
pre-slice inputs, never labelled as measured output. Specs: `tests/test_effective_review.py`,
`tests/ui/effective-review.spec.cjs`.

## Faithful single-file export (ticket 11)

`projects.export_plan(project)` / `GET /api/projects/{id}/export-plan` returns a read-only plan (`export_plan.py`, version 1) built
from `effective_settings` for every nonempty Plate: `faithful`, per-Plate `material` and `fingerprint`, the eligible
`object_overrides` carried, `differences` (effective machine/process/filament values that are not common to all Plates),
`blockers` (unresolved saved settings, Models still importing, nothing to export, no Orca for the template) and `limitations`
(BrickLayers post-processing is not reproduced by an Orca file). Compatibility is decided on effective values: a Plate override
or an Object override equal to what the Plate already resolves to changes nothing and is neither a difference nor Object
metadata. Non-Object differences are never pushed into Object metadata. The plan's `fingerprint` also covers names, bed and the
Plate/Object layout; `revision` is the Project revision.

`projects.export_project(project, revision, fingerprint)` / `GET .../export.3mf?revision=&fingerprint=` generates only for a
current, faithful plan (409 `export-plan-stale`, `export-review-required` or the plan's blockers). The legacy
`GET .../export.3mf` and `export_orca_project()` make the same plan now and either return the single file or the same conflict;
they never fall back to a thinner file or another file type. `representation` is `single-3mf`; tickets 12 and 13 add other
representations beside it using the same plan, fingerprint and per-Plate effective results.

Generation writes the effective machine/process/filament as the three JSON inputs of an isolated Orca `--export-3mf` run
(data directory `slicer-data-export`) so Orca supplies the complete project configuration, then overlays the effective values
canonically typed (`orca_3mf.config_value`: booleans `1`/`0`, null `nil`, vectors stay lists). Object metadata uses
`orca_3mf.metadata_value` (vectors joined by `;`). An unreadable template (the earlier code silently fell back because Orca refuses
a bare tetrahedron and binary STL) or an authored setting Orca's template has no key for is a 409, never a thinner file.
Geometry is the Slice's own `place()` result carried as centred mesh plus transform at nine significant figures; Plates are
numbered 1..n and each Plate's Objects stand at Orca's Plate-grid origin (`orca_3mf.plate_origin`), which `orca_3mf.parse`
removes again on import for an instance that lies inside its own Plate cell.

Native check: `SPOOLER_ORCA=<exe> python -m unittest tests.test_project_export_native` exports a disposable three-Plate PETG
Project (one Object override, a no-op Plate override, a Plate-id gap), slices the 3MF with a direct Orca command given no
Spooler settings, and compares Inner-wall deposition commands and footer values with Spooler's own Slices.
