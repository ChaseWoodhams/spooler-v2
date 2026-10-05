"""
File upload to SDCP printers (Centauri Carbon 1).

POST http://<printer>:3030/uploadFile/upload as multipart/form-data in 1 MB
chunks. Every chunk carries the whole file's MD5, its byte offset, the total
size and an upload UUID; the printer verifies the MD5 once the last chunk lands.
Blocking — run in an executor.
"""

import hashlib
import json
import urllib.request
import uuid
from pathlib import Path

UPLOAD_PORT = 3030
CHUNK_SIZE  = 1024 * 1024

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class UploadError(Exception):
    pass


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def encode_multipart(fields: dict, file_field: str, filename: str, data: bytes) -> tuple:
    """Return (body, content_type) for a multipart/form-data request with one file part."""
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        for key, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_file(
    ip: str,
    path: Path,
    remote_name: str,
    progress=None,
    *,
    port: int = UPLOAD_PORT,
    chunk_size: int = CHUNK_SIZE,
    timeout: float = 60,
) -> None:
    """Upload `path` to the printer as `remote_name`. Calls progress(sent, total) after each chunk."""
    path = Path(path)
    total = path.stat().st_size
    md5 = file_md5(path)
    upload_id = uuid.uuid4().hex
    url = f"http://{ip}:{port}/uploadFile/upload"
    offset = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            body, content_type = encode_multipart(
                {"S-File-MD5": md5, "Check": "1", "Offset": str(offset),
                 "Uuid": upload_id, "TotalSize": str(total)},
                "File", remote_name, chunk,
            )
            request = urllib.request.Request(url, data=body, method="POST",
                                             headers={"Content-Type": content_type})
            try:
                with _opener.open(request, timeout=timeout) as resp:
                    reply = json.loads(resp.read() or b"{}")
            except (OSError, ValueError) as e:
                raise UploadError(f"Upload failed at byte {offset} of {total}: {e}") from e
            if not reply.get("success") or reply.get("code") != "000000":
                detail = reply.get("messages") or reply.get("code") or reply
                raise UploadError(f"Printer rejected the upload at byte {offset}: {detail}")
            offset += len(chunk)
            if progress is not None:
                progress(offset, total)
            if offset >= total:
                break
