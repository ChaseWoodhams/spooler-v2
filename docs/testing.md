# Testing Spooler

Run the Python tests with `scripts\run-tests.ps1`, from `spooler-v2`:

```
.\scripts\run-tests.ps1 assistant_tuning assistant_ask      # modules by name, one at a time
.\scripts\run-tests.ps1 -All -TimeoutSec 600                # every tests\test_*.py
```

It prints one line per module (`OK`, `FAILED` with the failing test names, or `TIMEOUT`) and exits non-zero unless all are `OK`. A module over the cap (default 300 s) is stopped by PID, with its children.

## Rules that cost time when broken

- **One module at a time.** Running several together, or alongside Playwright, has exhausted memory and hung runs for 40+ minutes on this machine.
- **Stop stray test processes by PID,** never by image name (`taskkill /IM`, `Stop-Process -Name`). That also stops the live Spooler and other sessions' work. A user-level hook blocks it.
- **Loopback stalls.** `127.0.0.1` intermittently times out here (`WinError 10054` or `10060` in a test's own HTTP call). Rerun that module alone once before treating it as a failure.
- **Never the live data folder.** Tests use temp folders; never point anything at `spooler-v2\data`.
- **No real `claude`.** `tests\__init__.py` makes the PATH lookup for `claude` find nothing, so a test that loses its fake `cli_path` fails loudly. A deliberate live run needs `SPOOLER_ALLOW_REAL_CLAUDE=1` and the user's go-ahead, because it is billable.
- **Python is the venv's:** `.venv\Scripts\python.exe` (there is no `python` on PATH).

## Browser specs

`tests\ui\*.spec.cjs` need `NODE_PATH=<spooler-v2>\node_modules`. Run one spec at a time (`npm run test:ui` runs them all). Most Assistant specs stub the server routes.

## Known failures (last checked 2026-10-15; recheck before relying on it)

Unrelated to the Assistant: `test_arrange`, `test_nester_fits`, `test_outbox`, `test_quick_print`, `test_shell_smoke`, `test_slicer` (a `UnicodeEncodeError` printing an arrow at `slicer.py:909`), `test_splits`. Specs: `assistant-panel` (third check, "Check saves what is typed first") and `plan-panel`. Compare failing test *names* with the tip you branched from, not counts.

## Worktrees

Feature worktrees live in the sibling folder `..\worktrees\`, not inside `spooler-v2`. `worktrees/` is git-ignored here as a safety net.
