"""Test package setup.

The Assistant falls back to `claude` on PATH when `cli_path` is empty, and that CLI talks to Jev and is billable. A test that
forgets a fake `cli_path` (or a settings save that drops it) would run the real one. So during tests the PATH lookup for `claude`
finds nothing. A deliberate live run sets SPOOLER_ALLOW_REAL_CLAUDE=1 before starting.
"""
import os
import shutil

_which = shutil.which


def _which_without_claude(cmd, *args, **kwargs):
    if os.path.basename(str(cmd)).lower().split(".")[0] == "claude" and os.environ.get("SPOOLER_ALLOW_REAL_CLAUDE") != "1":
        return None
    return _which(cmd, *args, **kwargs)


shutil.which = _which_without_claude
