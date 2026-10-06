"""
Compare nesting.nest (convex hull and true silhouette outlines) with packing.pack on a project's objects.

    python tools/bench_nesting.py [--project 2] [--copies 1] [--supported-every 0] [--repeat 3]

Works on a copy: the project's database row and model files are copied to a temporary folder and the live data is
only opened read-only. DATA_DIR defaults to ./data. Prints, per method, the plates used, the closest pair of parts
(true outline to outline, in mm) and the wall time. With --supported-every N every Nth part is a part that needs
tree supports: nest gives it SUPPORT_CLEARANCE_MM, pack puts those parts in a group packed with SUPPORT_GAP_MM.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon, box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nesting
import packing
import projects

BED, PURGE = (0.0, 0.0, 256.0, 256.0), (246.0, 0.0, 256.0, 20.0)


def copy_project(data_dir: Path, project_id: int, into: Path) -> tuple:
    """(objects, {model id: (verts, tris)}) read from a copy of the project."""
    source = sqlite3.connect((data_dir / "spooler.db").as_uri() + "?mode=ro", uri=True)
    row = source.execute("SELECT data FROM projects WHERE id = ?", (project_id,)).fetchone()
    source.close()
    objects = json.loads(row[0])["objects"]
    meshes = {}
    for obj in objects:
        folder = data_dir / "projects" / str(project_id) / "models" / str(obj["model"])
        target = into / "models" / str(obj["model"])
        target.mkdir(parents=True, exist_ok=True)
        if obj["model"] not in meshes:
            shutil.copyfile(folder / "model.npz", target / "model.npz")
            with np.load(target / "model.npz") as z:
                meshes[obj["model"]] = (z["verts"].astype(float), np.asarray(z["tris"]))
    return objects, meshes


def build_parts(objects, meshes, kind, copies, supported_every):
    """One entry per part: its outline at the saved rotation (so Z turns are the nester's to choose)."""
    parts = []
    for n in range(copies):
        for index, obj in enumerate(objects):
            verts, tris = meshes[obj["model"]]
            placed = projects.place(verts, {**obj, "rotation": [obj["rotation"][0], obj["rotation"][1], 0.0], "position": [0.0, 0.0]})
            outline = nesting.hull_outline(placed[:, :2]) if kind == "hull" else nesting.silhouette_outline(placed, tris)
            size = tuple(float(v) for v in (placed.max(axis=0) - placed.min(axis=0))[:2])
            supported = bool(supported_every) and (n * len(objects) + index) % supported_every == 0
            parts.append({"key": (n, obj["id"]), "outline": outline, "size": size, "supported": supported})
    return parts


def placed_shape(part, item):
    poly = affinity.rotate(Polygon(part["outline"]), item.get("angle", 90.0 if item["turned"] else 0.0), origin="centroid")
    x0, y0, x1, y1 = poly.bounds
    return affinity.translate(poly, item["center"][0] - (x0 + x1) / 2, item["center"][1] - (y0 + y1) / 2)


def report(label, plates, parts, shape_parts, seconds):
    by_key = {p["key"]: p for p in parts}
    shapes_by_key = {p["key"]: p for p in shape_parts}
    closest, worst_edge, in_purge = float("inf"), float("inf"), 0
    for plate in plates:
        shapes = [placed_shape(shapes_by_key[i["key"]], i) for i in plate["items"] if not i["oversize"]]
        for n, poly in enumerate(shapes):
            x0, y0, x1, y1 = poly.bounds
            worst_edge = min(worst_edge, x0, y0, BED[2] - x1, BED[3] - y1)
            in_purge += poly.distance(box(*PURGE)) < 1e-6
            closest = min([closest] + [poly.distance(other) for other in shapes[n + 1:]])
    oversize = sum(i["oversize"] for p in plates for i in p["items"])
    counts = "/".join(str(len(p["items"])) for p in plates)
    print(f"{label:34} plates {len(plates):2} ({counts})  closest pair {closest:6.2f} mm  bed edge {worst_edge:5.2f} mm  "
          f"in purge {in_purge}  oversize {oversize}  time {seconds:7.2f} s")
    return len(plates)


def timed(fn, repeat):
    best, out = float("inf"), None
    for _ in range(repeat):
        start = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - start)
    return out, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, default=2)
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--supported-every", type=int, default=0)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    data_dir = Path(os.environ.get("DATA_DIR", "data")).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        objects, meshes = copy_project(data_dir, args.project, Path(tmp))
        print(f"project {args.project}: {len(objects)} objects x {args.copies} = {len(objects) * args.copies} parts, "
              f"supported every {args.supported_every or 'never'}")
        hulls = build_parts(objects, meshes, "hull", args.copies, args.supported_every)
        start = time.perf_counter()
        sils = build_parts(objects, meshes, "silhouette", args.copies, args.supported_every)
        print(f"outlines: silhouettes took {time.perf_counter() - start:.2f} s, "
              f"corners per part: hull {np.mean([len(p['outline']) for p in hulls]):.0f}, "
              f"silhouette {np.mean([len(p['outline']) for p in sils]):.0f}")
        group = lambda p: "supports" if p["supported"] else "plain"
        rects = [{"key": p["key"], "size": p["size"], "group": group(p)} for p in hulls]

        def run_pack():
            return [plate for g in dict.fromkeys(r["group"] for r in rects)
                    for plate in packing.pack([r for r in rects if r["group"] == g], BED, PURGE,
                                              gap=packing.SUPPORT_GAP_MM if g == "supports" else packing.GAP_MM)]

        plates, seconds = timed(run_pack, args.repeat)
        report("packing.pack (rectangles)", plates, hulls, hulls, seconds)
        report("  same layout, padded silhouettes", plates, hulls, sils, 0.0)
        for label, parts in (("nest, convex hull", hulls), ("nest, silhouette", sils)):
            items = [{"key": p["key"], "outline": p["outline"], "group": "all",
                      "clearance": nesting.SUPPORT_CLEARANCE_MM if p["supported"] else nesting.CLEARANCE_MM} for p in parts]
            plates, seconds = timed(lambda: nesting.nest(items, BED, PURGE), args.repeat)
            report(label, plates, parts, sils if label.endswith("silhouette") else hulls, seconds)
            # distances against the silhouette are the truth the slicer sees; hulls are checked above against hulls
            if label.endswith("hull"):
                report("  same layout, padded silhouettes", plates, parts, sils, 0.0)


if __name__ == "__main__":
    main()
