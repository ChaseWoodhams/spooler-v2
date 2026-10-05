"""Grab one JPEG frame from a printer's MJPEG camera stream."""

import http.client
import urllib.parse

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_BYTES = 4 * 1024 * 1024


def grab_frame(camera_url: str, timeout: float = 8) -> bytes | None:
    """The first complete JPEG in the stream, or None if none arrived. Blocking."""
    parsed = urllib.parse.urlparse(camera_url)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        buffer = b""
        while len(buffer) < MAX_BYTES:
            chunk = resp.read1(65536)
            if not chunk:
                return None
            buffer += chunk
            start = buffer.find(SOI)
            if start >= 0:
                end = buffer.find(EOI, start + 2)
                if end >= 0:
                    return buffer[start:end + 2]
        return None
    finally:
        conn.close()
