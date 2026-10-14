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
