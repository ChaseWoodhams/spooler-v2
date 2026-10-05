"""
Build the Centauri Carbon printer model the slicer viewport draws around the bed and toolpath.

The source is the community CAD assembly of the printer (a STEP export, ~230 MB). This script tessellates it,
drops what never shows (electronics under the deck, fasteners, tiny parts), decimates what is left, splits it
into the groups that move independently while printing and writes one small gzip file for the browser:

  frame     fixed           everything that does not move
  shell     fixed           side/back panels, door and lid (drawn see-through, can be hidden)
  gantry    moves in Y      the X rods and the two Y-rod sliders
  toolhead  moves in X, Y   the whole toolhead
  bed       moves in Z      the bed carriage with the build plate

Coordinates are millimetres in printer space (X/Y as in G-code, origin at the front-left of the printable
area). Frame, shell and gantry are placed for a nozzle tip at z = 0; the toolhead is relative to its nozzle
tip; the bed is relative to the top of the build plate. The viewport puts the nozzle at (x, y, H) and the bed
at H - z for a head position (x, y, z).

Printer-space calibration: the printable area (0..256 in X and Y) is centred on the heated plate, the nozzle
tip is the lowest point of the toolhead and z = 0 is the top of the build plate.

It needs packages the Spooler environment does not have; use a separate one:
  python -m venv cadenv && cadenv\\Scripts\\pip install cascadio trimesh fast_simplification

usage: cadenv\\Scripts\\python tools/build_printer_model.py cc_v150.step [--out public/printer/centauri-carbon.spp.gz]
"""

import argparse
import gzip
import json
import re
import struct
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import trimesh

PRINTABLE = (256.0, 256.0)
# First matching ancestor name decides the group of a part.
GROUP_RULES = [
    ("skip", re.compile(r"^(CC_Electronics|fasteners)\b")),
    ("toolhead", re.compile(r"^Toolhead v")),
    ("gantry", re.compile(r"^xaxis_assm\b")),
    ("bed", re.compile(r"^Bed_assm\b")),
    ("shell", re.compile(r"^(CC_Panels|CC_door|CC_glass top)\b")),
]
SKIP_PART = re.compile(r"screw|washer|\bnut\b|bhcs|shcs|iso 7380|din \d|iso 10669|ziptie", re.I)
MIN_PART_MM = 4.0         # parts smaller than this (bounding box diagonal) are dropped, except on the toolhead
KEEP_TRIANGLES = 1200     # parts at or below this are kept as tessellated
KEEP_RATIO = 0.3          # larger parts are decimated to this share, but never below KEEP_TRIANGLES
COLOR_STEP = 24           # colours are rounded to merge parts into fewer draw calls


def load(step: Path, linear: float, angular: float) -> trimesh.Scene:
    import cascadio
    with tempfile.TemporaryDirectory() as tmp:
        glb = Path(tmp) / "model.glb"
        cascadio.step_to_glb(str(step), str(glb), linear, angular)
        return trimesh.load(glb, force="scene")


def part_color(mesh: trimesh.Trimesh) -> tuple:
    visual = mesh.visual
    color = None
    if hasattr(visual, "material"):
        color = getattr(visual.material, "baseColorFactor", None)
        if color is None and hasattr(visual.material, "main_color"):
            color = visual.material.main_color
    elif visual.kind == "vertex" or visual.kind == "face":
        color = visual.main_color
    color = np.asarray(color if color is not None else (180, 180, 180, 255), dtype=float)[:3]
    if color.max() <= 1.0:
        color = color * 255
    return tuple(int(min(255, round(c / COLOR_STEP) * COLOR_STEP)) for c in color)


def world_parts(scene: trimesh.Scene):
    """Yield (group, path, color, vertices in mm, faces) for every part instance in the assembly."""
    parent = {child: par for par, child, *_ in scene.graph.to_edgelist()}
    for node in scene.graph.nodes_geometry:
        matrix, geometry = scene.graph[node]
        chain, walk = [], node
        while walk in parent:
            chain.append(walk)
            walk = parent[walk]
        if any(SKIP_PART.search(name) for name in chain):
            continue
        group = "frame"
        for name in reversed(chain):
            rule = next((g for g, pattern in GROUP_RULES if pattern.search(name)), None)
            if rule:
                group = rule
                break
        if group == "skip":
            continue
        mesh = scene.geometry[geometry]
        vertices = trimesh.transform_points(mesh.vertices, matrix) * 1000.0
        yield group, chain, part_color(mesh), vertices, np.asarray(mesh.faces)


def simplify(vertices: np.ndarray, faces: np.ndarray):
    mesh = trimesh.Trimesh(vertices, faces, process=False)
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    count = len(mesh.faces)
    if count <= KEEP_TRIANGLES:
        return mesh.vertices, mesh.faces
    import fast_simplification
    target = max(KEEP_TRIANGLES, int(count * KEEP_RATIO))
    points, triangles = fast_simplification.simplify(mesh.vertices.astype(np.float32), mesh.faces.astype(np.int32),
                                                     target_reduction=1 - target / count)
    return points, triangles


def calibrate(parts: list) -> dict:
    """Printer-space anchors in CAD millimetres: nozzle tip, build plate top and the printable-area centre."""
    def named(pattern):
        found = [p for p in parts if any(re.search(pattern, name) for name in p[1])]
        if not found:
            raise SystemExit(f"calibration part not found: {pattern}")
        return np.vstack([p[3] for p in found])

    head = np.vstack([p[3] for p in parts if p[0] == "toolhead"])
    low = head[:, 2].min()
    tip = head[head[:, 2] < low + 0.5]
    nozzle = [float((tip[:, 0].min() + tip[:, 0].max()) / 2), float((tip[:, 1].min() + tip[:, 1].max()) / 2), float(low)]
    plate = named(r"^buildplate")
    heated = named(r"^Component67\b")      # the heater sheet under the build plate
    centre = (heated.min(0) + heated.max(0)) / 2
    return {
        "nozzle": [round(v, 3) for v in nozzle],
        "plate_top": round(float(plate[:, 2].max()), 3),
        "origin": [round(float(centre[0]) - PRINTABLE[0] / 2, 3), round(float(centre[1]) - PRINTABLE[1] / 2, 3)],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "public" / "printer" / "centauri-carbon.spp.gz")
    parser.add_argument("--linear", type=float, default=0.5, help="tessellation tolerance in millimetres")
    parser.add_argument("--angular", type=float, default=0.8, help="tessellation angle tolerance in radians")
    args = parser.parse_args()

    scene = load(args.step, args.linear, args.angular)
    parts = list(world_parts(scene))
    cal = calibrate(parts)        # before dropping small parts: the nozzle tip is one of them
    parts = [p for p in parts if p[0] == "toolhead" or np.linalg.norm(np.ptp(p[3], axis=0)) >= MIN_PART_MM]
    ox, oy = cal["origin"]
    nx, ny, nz = cal["nozzle"]
    # CAD millimetres → the group's own frame (see the module docstring). The bed sits 0.1 mm low so the
    # viewport's plate and grid draw over it without z-fighting.
    offsets = {
        "frame": (ox, oy, nz), "shell": (ox, oy, nz), "gantry": (ox, ny, nz),
        "toolhead": (nx, ny, nz), "bed": (ox, oy, cal["plate_top"] + 0.1),
    }

    buckets = defaultdict(lambda: ([], [], [0]))
    before = after = 0
    for group, _chain, color, vertices, faces in parts:
        points, triangles = simplify(vertices - np.array(offsets[group]), faces)
        before += len(faces)
        after += len(triangles)
        verts, tris, count = buckets[(group, color)]
        verts.append(np.asarray(points, dtype=np.float32))
        tris.append(np.asarray(triangles, dtype=np.uint32) + count[0])
        count[0] += len(points)

    chunks, blobs = [], []
    for (group, color), (verts, tris, _) in sorted(buckets.items()):
        points = np.vstack(verts)
        index = np.vstack(tris).astype("<u4")
        lo, hi = points.min(0), points.max(0)
        span = np.where(hi - lo > 0, hi - lo, 1)
        quantized = np.round((points - lo) / span * 65535).astype("<u2")
        chunks.append({"group": group, "color": "#%02x%02x%02x" % color, "vertices": len(points), "triangles": len(index),
                       "min": [round(float(v), 4) for v in lo], "max": [round(float(v), 4) for v in hi]})
        raw = quantized.tobytes()
        blobs.append(raw + b"\0" * (-len(raw) % 4))
        blobs.append(index.tobytes())

    printer_nozzle = [round(nx - ox, 2), round(ny - oy, 2), round(nz - cal["plate_top"], 2)]
    header = json.dumps({
        "format": 1, "name": "Elegoo Centauri Carbon", "source": args.step.name,
        "calibration": {**cal, "cad_pose_head": printer_nozzle}, "chunks": chunks,
    }, separators=(",", ":")).encode()
    header += b" " * (-len(header) % 4)
    data = b"SPP1" + struct.pack("<I", len(header)) + header + b"".join(blobs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(gzip.compress(data, 9))
    print(f"{len(parts)} parts, {before:,} → {after:,} triangles in {len(chunks)} chunks; "
          f"{len(data) / 1e6:.1f} MB raw, {args.out.stat().st_size / 1e6:.1f} MB gzip → {args.out}")
    print(f"calibration {cal}; head in the CAD pose at X{printer_nozzle[0]} Y{printer_nozzle[1]} Z{printer_nozzle[2]}")


if __name__ == "__main__":
    main()
