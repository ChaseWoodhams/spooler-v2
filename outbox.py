"""
Durable queue for Spoolman filament deductions.

A deduction that cannot reach Spoolman (service stopped, restarting, or timing
out) is written to disk and replayed once Spoolman answers again, so filament
usage is not lost while the inventory service is unavailable.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from persistence import DATA_DIR, _atomic_write

OUTBOX_FILE   = DATA_DIR / "spoolman_outbox.json"
USE_TIMEOUT_S = 10


class PermanentError(Exception):
    """Spoolman rejected the request; retrying it cannot succeed."""


def post_use(base_url: str, spool_id: int, amount_g: float, timeout: float = USE_TIMEOUT_S) -> dict:
    """PUT /spool/{id}/use. Raises PermanentError on 4xx; any other error is retryable."""
    req = urllib.request.Request(
        f"{base_url}/api/v1/spool/{spool_id}/use",
        data=json.dumps({"use_weight": round(amount_g, 1)}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            raise PermanentError(f"Spoolman returned HTTP {e.code} for spool {spool_id}") from e
        raise


class Outbox:
    def __init__(self, path: Path):
        self.path  = path
        self._lock = threading.RLock()
        self._count: int | None = None

    def _load(self) -> list:
        try:
            items = json.loads(self.path.read_text())
        except FileNotFoundError:
            items = []
        except (OSError, json.JSONDecodeError) as e:
            # Keep an unreadable queue for inspection rather than overwriting it.
            broken = self.path.with_name(f"{self.path.name}.unreadable-{int(time.time())}")
            print(f"[Spoolman] Deduction queue unreadable ({e}); moved to {broken.name}")
            try:
                os.replace(self.path, broken)
            except OSError:
                pass
            items = []
        if not isinstance(items, list):
            items = []
        self._count = len(items)
        return items

    def _save(self, items: list) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps(items, indent=2))
        self._count = len(items)

    def enqueue(
        self,
        amount_g: float,
        printer_id: str,
        error: str,
        spool_id: int | None = None,
        location: str | None = None,
    ) -> dict:
        """Queue a deduction for a spool ID, or for whichever spool is at `location` when replayed."""
        entry = {
            "id":         uuid.uuid4().hex,
            "spool_id":   spool_id,
            "location":   location,
            "amount_g":   round(float(amount_g), 1),
            "printer_id": printer_id,
            "created_at": time.time(),
            "attempts":   0,
            "last_error": error,
        }
        with self._lock:
            items = self._load()
            items.append(entry)
            self._save(items)
        return entry

    def pending(self) -> list:
        with self._lock:
            return self._load()

    def pending_count(self) -> int:
        """Cached count — safe to call from the event loop while a flush holds the lock."""
        if self._count is None:
            try:
                items = json.loads(self.path.read_text())
                self._count = len(items) if isinstance(items, list) else 0
            except (OSError, json.JSONDecodeError):
                self._count = 0
        return self._count

    def flush(self, send) -> dict:
        """Replay queued deductions in order through send(entry) -> Spoolman spool dict.

        Stops at the first retryable failure (Spoolman still unavailable) so the
        remaining entries keep their order. Entries Spoolman rejects outright
        are removed and returned in "dropped".
        """
        sent, dropped = [], []
        with self._lock:
            items = self._load()
            remaining = list(items)
            for entry in items:
                try:
                    result = send(entry)
                except PermanentError as e:
                    remaining.remove(entry)
                    dropped.append({**entry, "last_error": str(e)})
                except Exception as e:
                    entry["attempts"] += 1
                    entry["last_error"] = str(e) or type(e).__name__
                    break
                else:
                    remaining.remove(entry)
                    sent.append((entry, result))
            if items:
                self._save(remaining)
        return {"sent": sent, "dropped": dropped, "remaining": len(remaining)}


outbox = Outbox(OUTBOX_FILE)
