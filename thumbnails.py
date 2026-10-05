"""
G-code thumbnails: a small picture of the sliced plate that printers show on their screen and in file lists.

OrcaSlicer's command line doesn't draw them (only the GUI does), so Spooler renders the placed meshes itself
with a tiny software renderer (orthographic camera, lambert shading, z-buffer, 2× supersampling) and writes the
picture into the G-code in OrcaSlicer's own format:

    ; THUMBNAIL_BLOCK_START
    ; thumbnail begin 144x144 <base64 length>
    ; <base64 PNG, 78 characters per line>
    ; thumbnail end
    ; THUMBNAIL_BLOCK_END

The sizes come from the machine preset's `thumbnails` setting ("144x144", "300x300/PNG", …).
"""

import base64
import math
import os
import re
import struct
import tempfile
import zlib
from pathlib import Path

import numpy as np

SUPERSAMPLE  = 2
MAX_SPLIT    = 400         # a triangle is sampled with at most this many steps per edge
COLOR        = (214, 118, 52)   # model colour (Spooler's accent orange)
LIGHT        = np.array([-0.35, -0.55, 0.75])
YAW_DEG      = 35.0        # camera swings this far round Z from the front...
TILT_DEG     = 38.0        # ...and looks down at the plate from this far above the horizon
_SIZE        = re.compile(r"^\s*(\d{2,4})\s*[x×]\s*(\d{2,4})\s*(?:/\s*(\w+))?\s*$", re.I)


def parse_sizes(machine: dict) -> list:
    """[(width, height)] the machine preset asks for. Only PNG pictures are written."""
    raw = machine.get("thumbnails") or []
    entries = [raw] if isinstance(raw, str) else list(raw)
    if str(machine.get("thumbnails_format") or "PNG").upper() != "PNG":
        return []
    sizes = []
    for entry in entries:
        for part in str(entry).split(","):
            match = _SIZE.match(part)
            if match and (match.group(3) or "PNG").upper() == "PNG":
                size = (int(match.group(1)), int(match.group(2)))
                if size not in sizes and max(size) <= 1024:
                    sizes.append(size)
    return sizes


def _camera() -> np.ndarray:
    """Rows: screen right, screen up, towards the viewer (bed coordinates: X right, Y back, Z up).
    The scene is turned about Z, then seen from above at TILT_DEG."""
    yaw, tilt = math.radians(YAW_DEG), math.radians(TILT_DEG)
    rotate_z = np.array([[math.cos(yaw), -math.sin(yaw), 0], [math.sin(yaw), math.cos(yaw), 0], [0, 0, 1]])
    right = np.array([1.0, 0.0, 0.0])
    forward = np.array([0.0, math.cos(tilt), -math.sin(tilt)])   # from the eye into the scene
    up = np.array([0.0, math.sin(tilt), math.cos(tilt)])
    return np.array([right, up, -forward]) @ rotate_z.T


def render(meshes: list, width: int, height: int) -> bytes:
    """Draw meshes [(vertices (N, 3), triangles (M, 3))] in bed millimetres; returns a PNG with a transparent background."""
    view = _camera()
    pieces = []
    for verts, tris in meshes:
        if len(tris):
            pieces.append((np.asarray(verts, dtype=np.float64) @ view.T, np.asarray(tris, dtype=np.int64), np.asarray(verts, dtype=np.float64)))
    if not pieces:
        return blank_png(width, height)
    low = np.min([p[0].min(axis=0) for p in pieces], axis=0)
    high = np.max([p[0].max(axis=0) for p in pieces], axis=0)
    w, h = width * SUPERSAMPLE, height * SUPERSAMPLE
    margin = 0.06
    scale = min(w * (1 - 2 * margin) / max(high[0] - low[0], 1e-6), h * (1 - 2 * margin) / max(high[1] - low[1], 1e-6))
    offset_x = (w - (high[0] - low[0]) * scale) / 2 - low[0] * scale
    offset_y = (h - (high[1] - low[1]) * scale) / 2 + high[1] * scale
    light = LIGHT / np.linalg.norm(LIGHT)

    depth = np.full(w * h, -np.inf)
    shade = np.zeros(w * h)
    for cam, tris, world in pieces:
        a, b, c = (cam[tris[:, i]] for i in range(3))
        normal = np.cross(world[tris[:, 1]] - world[tris[:, 0]], world[tris[:, 2]] - world[tris[:, 0]])
        length = np.linalg.norm(normal, axis=1)
        ok = length > 1e-12
        normal[ok] /= length[ok, None]
        lit = 0.35 + 0.65 * np.abs(normal @ light)   # two-sided, so a mesh with flipped faces still reads
        edge = np.maximum.reduce([np.linalg.norm((p - q)[:, :2], axis=1) for p, q in ((a, b), (b, c), (c, a))]) * scale
        steps = np.clip(np.ceil(edge * 1.5).astype(int), 1, MAX_SPLIT)
        for k in np.unique(steps):
            pick = steps == k
            grid = [(i / k, j / k) for i in range(k + 1) for j in range(k + 1 - i)]
            u = np.array([g[0] for g in grid])[None, :, None]
            v = np.array([g[1] for g in grid])[None, :, None]
            points = a[pick][:, None, :] * (1 - u - v) + b[pick][:, None, :] * u + c[pick][:, None, :] * v
            px = np.rint(points[..., 0] * scale + offset_x).astype(int).ravel()
            py = np.rint(offset_y - points[..., 1] * scale).astype(int).ravel()
            pz = points[..., 2].ravel()
            tone = np.repeat(lit[pick], len(grid))
            inside = (px >= 0) & (px < w) & (py >= 0) & (py < h)
            index = py[inside] * w + px[inside]
            pz, tone = pz[inside], tone[inside]
            order = np.argsort(pz)              # the nearest sample for a pixel is written last
            index, pz, tone = index[order], pz[order], tone[order]
            keep = pz > depth[index]
            depth[index[keep]] = pz[keep]
            shade[index[keep]] = tone[keep]

    covered = np.isfinite(depth).reshape(h, w)
    # close single-pixel holes left between samples
    padded = np.pad(covered, 1)
    neighbours = sum(np.roll(np.roll(padded, dy, 0), dx, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy)[1:-1, 1:-1]
    tone_image = shade.reshape(h, w)
    filled = ~covered & (neighbours >= 6)
    if filled.any():
        blurred = sum(np.roll(np.roll(np.pad(tone_image, 1), dy, 0), dx, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1))[1:-1, 1:-1] / 9
        tone_image = np.where(filled, blurred * 1.5, tone_image)
        covered = covered | filled
    rgb = np.clip(tone_image[..., None] * np.array(COLOR, dtype=float), 0, 255)
    alpha = covered.astype(float)
    rgba = np.concatenate([rgb * alpha[..., None], alpha[..., None] * 255], axis=2)
    small = rgba.reshape(height, SUPERSAMPLE, width, SUPERSAMPLE, 4).mean(axis=(1, 3))
    a_out = small[..., 3]
    color = np.where(a_out[..., None] > 0, small[..., :3] / np.maximum(a_out[..., None] / 255, 1e-6), 0)
    out = np.concatenate([np.clip(color, 0, 255), a_out[..., None]], axis=2).astype(np.uint8)
    return encode_png(out)


def encode_png(rgba: np.ndarray) -> bytes:
    height, width = rgba.shape[:2]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + rgba[y].tobytes() for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def blank_png(width: int, height: int) -> bytes:
    return encode_png(np.zeros((height, width, 4), dtype=np.uint8))


def block(png: bytes, width: int, height: int) -> str:
    text = base64.b64encode(png).decode()
    lines = [text[i:i + 78] for i in range(0, len(text), 78)]
    return "".join(["; THUMBNAIL_BLOCK_START\n", f"; thumbnail begin {width}x{height} {len(text)}\n",
                    *(f"; {line}\n" for line in lines), "; thumbnail end\n", "; THUMBNAIL_BLOCK_END\n\n"])


def has_thumbnail(path: Path) -> bool:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return "thumbnail begin" in fh.read(64 * 1024)


def inject(path: Path, blocks: str) -> bool:
    """Put `blocks` right after the header block (or at the top) of a G-code file, streaming it so large files stay cheap.
    Returns False when the file already has a thumbnail."""
    if has_thumbnail(path):
        return False
    fd, tmp = tempfile.mkstemp(prefix=".thumb-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out, path.open("rb") as src:
            head = src.read(64 * 1024)
            marker = head.find(b"; HEADER_BLOCK_END")
            if marker >= 0:
                cut = head.find(b"\n", marker)
                cut = len(head) if cut < 0 else cut + 1
                while head[cut:cut + 1] in (b"\n", b"\r"):  # keep the blank line after the header where it is
                    cut += 1
            else:
                cut = 0
            out.write(head[:cut])
            out.write(blocks.encode("ascii"))
            out.write(head[cut:])
            while True:
                data = src.read(4 * 1024 * 1024)
                if not data:
                    break
                out.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def add_to_gcode(path: Path, machine: dict, meshes: list) -> int:
    """Render and embed the thumbnails a machine preset asks for. Returns how many were written."""
    sizes = parse_sizes(machine)
    if not sizes or not meshes:
        return 0
    blocks = "".join(block(render(meshes, w, h), w, h) for w, h in sizes)
    return len(sizes) if inject(path, blocks) else 0
