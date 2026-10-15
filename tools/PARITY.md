# PETG/ASA parity workflow

Repeatable, isolated check that what Spooler **reviews**, **slices**, **exports** and **re-imports** is what the pinned
OrcaSlicer 2.4.2 actually prints. Run it before claiming the reliable-project-settings work complete, and after touching
settings resolution, slicing, export or import.

```
powershell -File tools\Run-Parity.ps1                      # finds .venv and the pinned Orca beside the project
powershell -File tools\Run-Parity.ps1 -Orca <orca-slicer.exe> -Diagnostics <folder>
```

Exit codes: `0` passed with no known gaps, `2` the pinned Orca was **not found** (fast tests ran, the real matrix did **not**:
report that as *not run*, never as passed), `3` the matrix ran but the known gaps below are still open (not a clean pass),
anything else a failure. `-Integrated` also runs the Project service/HTTP suites (staged loss review, export choices, Slice trust);
the browser specs stay separate (`node tests/ui/run.cjs`). Or run the modules directly with `SPOOLER_ORCA` set:
`python -m unittest tests.test_parity_logic tests.test_parity_native`.

## Isolation

Every case builds its own temporary database, data directory and generated 20 mm cubes, and gives direct Orca a fresh
`--datadir`. No Project id, saved profile, printer, queue or SEAT fixture is read or written, and no printer is contacted.

## What it checks

For each scenario: export the Project (one faithful 3MF, or the per-Plate package when one file cannot be faithful); slice each
file with a **direct Orca command that is given nothing but the file**; slice the same Plate in Spooler; compare. Then re-import
each file through the normal reviewed flow, slice the reopened Project in Spooler, and compare with the same direct run again.

| Scenario | Path |
|---|---|
| PETG baseline cubes, ASA baseline cubes | single 3MF |
| Project, Plate and Object overrides layered | single 3MF |
| Nullable retract / lift / wipe Filament overrides, PETG and ASA | single 3MF |
| Temperature, flow and cooling changes | single 3MF |
| The other bed side (High Temp Plate) | single 3MF |
| Rotated and scaled multi-Object Plate | single 3MF |
| Two-Plate Project, PETG and ASA | per-Plate package |
| Two-Plate Project differing in a non-Object process key | per-Plate package |

Each comparison (`tests/parity.py`) covers:

- every print-affecting key of Orca's config footer, canonicalised as `observed_output` does (quotes, number and boolean
  forms, vectors, points); a key reported by only one side is a difference;
- layer count (must match exactly);
- estimated time within the larger of **1 s or 0.1 %**, and filament weight within the larger of **0.02 g or 0.1 %**;
- deposition move counts per role (outer wall, inner wall, infill, ...) and the extrusion bounding box (the printer's own start/purge moves excluded) within 0.05 mm, as
  toolpath and position evidence, so footer equality alone never passes.

Only these differences are ignored: `observed_output.IGNORED` (names, ids, compatibility lists, colour labels) and the thumbnail
and label settings in `parity.COSMETIC`. G-code is never required to be byte-identical; timestamps and elapsed slicing time are
not compared.

### Independent expectations

Each scenario states what it asked for by hand (`expected`): material, temperatures, wall count, infill, bed, a minimum number of
deposition moves, and a layer count worked out from the cubes (20 mm at 0.2 mm is 100 layers). Both outputs must satisfy it, so a resolver bug that both paths share still fails. For example, two outputs that
both say `wall_loops = 1` fail a scenario that asked for 3.

### Failures leave something to read

On any mismatch the run writes `<diagnostics>/<scenario>/` with `report.txt` (what differed, in words), `expected.json`,
`reference.json` and `other.json` (parsed config, layers, time, weight, move counts, bounds) and both G-code files, then fails with
the folder in the message. `tests/test_parity_logic.py` proves, without Orca, that it fails for each regression the ticket lists:
walls or infill disappearing, PETG becoming PLA, ASA identity or settings changing, an Object moving, layer or time or weight
drift beyond the bounds, and the review differing from the inputs sent to Orca.

## Known gaps (run as expected failures)

These are real fidelity problems this workflow found. They stay as `expectedFailure` tests so the matrix reports them; when
one is fixed the test becomes an *unexpected success*, which must then have its decorator removed.

1. **A setting the stock preset does not state is lost on re-import.** `brim_type = no_brim` is written to the exported file and
   honoured by direct Orca, but the review lists it under `uncompared-settings` and does not carry it, so the reopened Project
   slices with `auto_brim`. (Tickets 10 and 12 disclosed this; this test pins it.)
2. **Rotated or scaled Objects drift 0.15 % in estimated time** (1364 s in Spooler against 1362 s in direct Orca), over the
   spec's bound. Layers, toolpath counts and position agree; the likely cause is geometry serialisation precision. The bound has
   been left alone on purpose.

## Limits of the checks

- The Object override case uses a one-wall Project, which prints no Inner wall at all, so Inner wall deposition can only come from
  the Object's own four-wall override reaching Orca.
- A nil Filament override is shown by the resolved printer value (`retraction_length` 0.8) beside the explicit lift. Orca omits nil
  overrides from its footer, so there is no direct footer proof that nil differs from the stock value.
- Positions are compared between the two outputs, not against an independent expected position.
- Every re-import is confirmed with the review's own acknowledgement. The review of an exported file always lists hundreds of
  settings as "uncompared" (the file carries Orca's full configuration), so that list is not a verdict; the footer comparison is.

## Not covered

Physical print quality, printer firmware, BrickLayers-transformed G-code (the baseline is compared, not the rewritten file),
ASA appended onto an existing Project, and multi-nozzle printers. Full calibration generation remains a separate specification.
