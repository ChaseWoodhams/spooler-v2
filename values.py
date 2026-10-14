"""Small value helpers shared by the settings, export and review modules (pure; no I/O)."""
import hashlib
import json
import re


def first(value):
    """The first entry of a vector setting; anything else as it is."""
    return value[0] if isinstance(value, list) and value else value


def digest(payload, *, length: int | None = None, compact: bool = True, default=None) -> str:
    """The hex SHA-256 of `payload` as canonical JSON (sorted keys; `compact` drops the separators' spaces), cut to `length`.
    The same call always yields the same text, so stored fingerprints stay comparable."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":") if compact else None, default=default)
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def safe_filename(text, fallback: str = "", strip: str = " ") -> str:
    """`text` with every run of characters unsafe in a download name replaced by "_", trimmed of `strip`, or `fallback` when nothing is left."""
    return re.sub(r"[^\w .-]+", "_", text or "").strip(strip) or fallback
