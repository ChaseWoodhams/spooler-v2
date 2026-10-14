# Pinned settings validation

The browser and Project/preset edit paths use `public/orca/settings.json`, generated
from OrcaSlicer **v2.4.2**. `options` contains tab-classified controls;
`definitions` also records source-only FFF definitions for inspecting inherited
values. A source-only definition or a key present in a preset does not grant edit
permission. Develop, readonly, script and unclassified controls remain excluded.

The generator retains copied enum choices, nullable declarations, numeric
constants/referenced bounds, ratio targets and literal-limit metadata, and writes
Unicode without transliteration. `scopes` records the pinned tab classification;
the Object allowlist is a separate extension.

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
