"""
SDCP protocol constants and helpers shared by all printer backends.
"""

import time
import uuid

# ── SDCP command codes ─────────────────────────────────────────────────────────

CMD_STATUS       = 0
CMD_ATTRS        = 1
CMD_START        = 128
CMD_PAUSE        = 129
CMD_STOP         = 130
CMD_RESUME       = 131
CMD_LIST_FILES   = 258
CMD_DELETE_FILES = 259
CMD_CAMERA       = 386
CMD_MOVE_AXES    = 401
CMD_HOME_AXES    = 402
CMD_LIGHT        = 403
CMD_CANVAS       = 324

# ── PrintInfo.Status (CC1 values; the other backends report the same codes) ─────

PRINT_IDLE      = 0
PRINT_STOPPING  = 7
PRINT_COMPLETE  = 9
PRINT_CANCELLED = frozenset({8, 14})
PRINT_ENDED     = frozenset({PRINT_IDLE, PRINT_COMPLETE, *PRINT_CANCELLED})
PRINT_WORKING   = frozenset({1, 2, 3, 4, 5, 6, 10, 12, 13, 15, 16, 18, 19, 20, 21})


def classify_print_end(code: int, layer: int = 0, total_layers: int = 0) -> tuple:
    """(outcome, detail) for a print whose status has moved to one of PRINT_ENDED."""
    if code == PRINT_COMPLETE:
        return "completed", None
    if code in PRINT_CANCELLED:
        return "cancelled", "Print stopped"
    if total_layers and layer >= total_layers:
        return "completed", None
    return "unknown", "The printer went idle without reporting how the print ended"


def make_msg(cmd: int, data: dict, mainboard_id: str = "") -> dict:
    msg_id = str(uuid.uuid4()).replace("-", "")[:32]
    return {
        "Id": msg_id,
        "Data": {
            "Cmd":         cmd,
            "Data":        data,
            "RequestID":   msg_id,
            "MainboardID": mainboard_id,
            "TimeStamp":   int(time.time()),
            "From":        "Web",
        },
        "Topic": f"sdcp/request/{mainboard_id}",
    }


def decode_printinfo(pi: dict) -> dict:
    """Elegoo firmware sends some field names as space-separated hex bytes.

    E.g. '54 6F 74 61 6C 45 78 74 72 75 73 69 6F 6E 00' → 'TotalExtrusion'
    """
    result = {}
    for k, v in pi.items():
        parts = k.split()
        if (len(parts) > 1 and
                all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p)
                    for p in parts)):
            try:
                decoded = bytes(int(p, 16) for p in parts).rstrip(b"\x00").decode("utf-8")
                result[decoded] = v
                continue
            except Exception:
                pass
        result[k] = v
    return result


def deep_merge(base: dict, incoming: dict, max_keys: int = 500) -> dict:
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v, max_keys)
        else:
            base[k] = v
        if len(base) > max_keys:
            break
    return base
