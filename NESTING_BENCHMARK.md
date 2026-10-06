# Measurement: outline nester versus packing.pack

Ticket 15 of the Plates v2 spec. Measured 2026-10-06 in the app's own environment (`.venv`, Python 3.12.10, Shapely 2.1.2 with GEOS 3.13.1, numpy 2.5.3), on a copy of project 2 read from `data/projects/2/models/*/model.npz` and the objects in `spooler.db` (read-only; nothing live was written). Code: `nesting.py`, benchmark `tools/bench_nesting.py`, tests `tests/test_nesting.py`.

Setup: each object keeps its saved tilt (rotation about X and Y) and the nester chooses the turn about Z. Rules as measured in `research/brim-support-measurements.md`: pair gap = max(b_i, b_j), 3 mm from the bed edge, b from the purge area; plain parts use 6 mm (today's `GAP_MM`), parts needing tree supports 22 mm. `packing.pack` is run with `gap=6` (plain) and 20 (supported group), as the callers do. Angles: every 15 degrees plus the angles that lay each part's three longest hull edges along an axis (up to 6 more). Times are best of 3 (one run for 51 and 85 parts) on this machine.

| Case | `packing.pack` | nest, convex hull | nest, silhouette |
|---|---|---|---|
| Project 2, 17 parts, all plain | 1 plate, 0.00 s | 1 plate, 0.48 s | 1 plate, 0.35 s |
| Project 2, 17 parts, every 3rd supported (6 of 17) | 2 plates (6/11), 0.00 s | 1 plate, 0.44 s | 1 plate, 0.31 s |
| 34 parts (project 2 twice), plain | 2 plates (17/17), 0.00 s | 2 plates (19/15), 1.07 s | 2 plates (20/14), 0.96 s |
| 51 parts (three times), plain | 3 plates (18/27/6) | 2 plates (17/34), 4.3 s | 2 plates (18/33), 2.7 s |
| 85 parts (five times), plain | 4 plates (22/14/23/26) | 4 plates (16/21/35/13), 9.5 s | 4 plates (18/17/39/11), 7.0 s |

Closest pair of outlines in every nested layout is at least the required clearance (6.0x mm for plain pairs); nothing is within 3 mm of the bed edge or inside the purge area; no part was oversize. `packing.pack` reaches 6.00 mm between footprint rectangles; between the parts' outlines its closest pair is 5.5 to 5.6 mm (silhouettes padded 0.2 mm), because rectangles ignore shape.

Building a silhouette outline costs about 0.1 s per part (union of the mesh's triangles seen from above; 1.7 s for 17 parts); the convex hull is instant. In real use a silhouette would be cached per model and orientation; the nesting times above exclude it.

## Decision: convex hull

Use the convex hull as the outline for now.

- The silhouette nests no better where it counts: the same plate count in every case, a few parts more on the first plate at 34 to 85 parts (20 against 19, 18 against 16), and 10 to 35 percent faster. The parts are mostly small and fairly convex, so little is gained by tucking parts into each other's hollows.
- Ticket 14's 22 mm reach was measured from the part's convex hull, so it is only established for a hull outline. Supports fill a concave part's hollows, so reach beyond a true silhouette is larger and unmeasured.
- A silhouette is the way to get interlocking later (`silhouette_outline` exists and is tested but unused). Revisit when a project with long concave parts appears, or when support reach from a silhouette has been measured.

## Not verified

- The saved SEAT parts: `data/splits` is empty (the `splits` table has no rows and there is no `parts` folder), so only project 2 was used, with copies standing in for a larger set.
- Time inside the running server (the nester is not wired in), time with a pinned object in the middle of a plate (tested for correctness, not timed), and a time budget with early exit (not added).
- Supported and plain parts mixed on one plate: the nester allows it per spec; today's flow sets supports per plate.
