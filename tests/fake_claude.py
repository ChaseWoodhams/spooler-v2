"""A fake `claude` CLI for tests of the Assistant runner (tickets 12 to 17). Standard library only.

As a helper:

    from tests.fake_claude import make_fake_claude, read_record
    cli = make_fake_claude(some_dir, {"calls": [{"tool": "get_project"}], "answer": "Plate 1 is fine."})
    # point the Assistant's CLI setting at `cli`, ask through the project API, then
    record = read_record(some_dir)      # what the run looked like from the CLI's side

`make_fake_claude` writes a launcher the runner can start like claude.exe (a .cmd shim on Windows, an executable script
elsewhere) next to the scenario file. Run as a script (`fake_claude.py <scenario.json> <claude args...>`) it behaves like the
real CLI for the recipe in the spec: reads the prompt from stdin, reads the per-run `--mcp-config`, starts the REAL Spooler
MCP server it names over stdio, prints a `system/init` event, calls the scripted tools, and prints stream-json events and a
final `result`. It writes `record.json` beside the scenario: argv, cwd, what was in cwd, stdin, selected environment, the MCP
config and system prompt file contents, the `init` event it printed and each tool result (so tests assert on the CLI's view).

Scenario keys (all optional):
  calls            [{"tool": "get_project", "arguments": {}}]   tools to call, in order, through the real server
  answer           final text (default "OK")
  result           dict merged over the final result event (e.g. {"is_error": true, "result": "...usage limit..."})
  init_tools       list of full tool names to report in system/init instead of what the server lists
  extra_init_tools names appended to what is reported (e.g. ["Bash"] to look like a leaked built-in tool)
  mcp_status       status reported for the spooler server (default "connected")
  skip_server      true: do not start the server (tool calls then report an error)
  hang_after_init  seconds to sleep after init (a stalled run)
  hang_after_calls seconds to sleep after the scripted calls, server still running (a run that proposed, then stalled)
  exit_code        exit with this code after the events; with no_result true no result event is printed
  no_result        true: print no final result
  unknown_events   true: print events of unknown types and subtypes the runner must ignore
  step_delay       seconds to sleep between events (lets a test watch progress)
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RECORD = "record.json"
SCENARIO = "scenario.json"
ENV_KEYS = ["ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "API_TIMEOUT_MS", "CLAUDE_CODE_MAX_RETRIES",
            "MCP_TIMEOUT", "MCP_TOOL_TIMEOUT", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "NODE_EXTRA_CA_CERTS"]


# ── Test-side helpers ─────────────────────────────────────────────────────────

def make_fake_claude(folder, scenario: dict | None = None) -> Path:
    """Write the scenario and a launcher into `folder`; return the launcher's path (what the CLI setting points at)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / SCENARIO).write_text(json.dumps(scenario or {}), encoding="utf-8")
    (folder / RECORD).unlink(missing_ok=True)
    script = Path(__file__).resolve()
    if os.name == "nt":
        launcher = folder / "claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" "{folder / SCENARIO}" %*\r\n', encoding="utf-8")
    else:
        launcher = folder / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{folder / SCENARIO}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def read_record(folder) -> dict:
    return json.loads((Path(folder) / RECORD).read_text(encoding="utf-8"))


def set_scenario(folder, scenario: dict) -> None:
    (Path(folder) / SCENARIO).write_text(json.dumps(scenario), encoding="utf-8")


# ── The fake CLI ──────────────────────────────────────────────────────────────

class ServerClient:
    """Speaks MCP to the real Spooler server over stdio, as the CLI does."""

    def __init__(self, spec: dict):
        env = {**os.environ, **(spec.get("env") or {})}
        self.proc = subprocess.Popen([spec["command"], *(spec.get("args") or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the MCP server closed")
        return json.loads(line)

    def notify(self, method):
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def emit(event: dict) -> None:
    sys.stdout.buffer.write((json.dumps(event) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv) -> int:
    scenario_path, args = Path(argv[1]), argv[2:]
    folder = scenario_path.parent
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    delay = float(scenario.get("step_delay") or 0)
    record = {"argv": args, "cwd": os.getcwd(), "cwd_entries": sorted(os.listdir(".")), "pid": os.getpid(),
              "env": {k: os.environ[k] for k in ENV_KEYS if k in os.environ}, "tool_results": []}
    record["stdin"] = sys.stdin.buffer.read().decode("utf-8", "replace")

    def flag(name):
        return args[args.index(name) + 1] if name in args else None

    config_path, prompt_path = flag("--mcp-config"), flag("--system-prompt-file")
    config = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {}
    record["mcp_config"] = config
    record["system_prompt"] = Path(prompt_path).read_text(encoding="utf-8") if prompt_path and Path(prompt_path).is_file() else None
    save = lambda: (folder / RECORD).write_text(json.dumps(record), encoding="utf-8")
    save()

    server, listed = None, []
    spec = (config.get("mcpServers") or {}).get("spooler")
    if spec and not scenario.get("skip_server"):
        server = ServerClient(spec)
        record["server_pid"] = server.proc.pid          # lets a test prove the whole tree is gone after Cancel or the deadline
        save()
        server.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fake-claude", "version": "0"}})
        server.notify("notifications/initialized")
        listed = [t["name"] for t in server.request("tools/list")["result"]["tools"]]
    names = scenario.get("init_tools")
    if names is None:
        names = [f"mcp__spooler__{n}" for n in listed]
    names = names + list(scenario.get("extra_init_tools") or [])
    init = {"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": "fake-session", "tools": names,
            "mcp_servers": [{"name": "spooler", "status": scenario.get("mcp_status", "connected"), "source": "dynamic"}], "model": "fake-model",
            "permissionMode": "dontAsk"}
    record["init"] = init
    save()
    emit(init)
    if scenario.get("unknown_events"):
        emit({"type": "system", "subtype": "ui_invalidate", "event": "ui.render"})
        emit({"type": "rate_limit_event", "info": {}})
        emit({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "..."}}})
    if scenario.get("hang_after_init"):
        time.sleep(float(scenario["hang_after_init"]))

    for number, call in enumerate(scenario.get("calls") or [], start=1):
        time.sleep(delay)
        tool_id = f"toolu_{number}"
        emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": f"mcp__spooler__{call['tool']}",
                                                                                 "input": call.get("arguments") or {}}]}})
        if server is None:
            reply = {"result": {"content": [{"type": "text", "text": "no server"}], "isError": True}}
        else:
            reply = server.request("tools/call", {"name": call["tool"], "arguments": call.get("arguments") or {}})
        result = reply.get("result") or {"content": [{"type": "text", "text": json.dumps(reply.get("error"))}], "isError": True}
        record["tool_results"].append({"tool": call["tool"], "is_error": bool(result.get("isError")), "text": result["content"][0]["text"]})
        save()
        emit({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": result["content"]}]}})
    if scenario.get("hang_after_calls"):
        time.sleep(float(scenario["hang_after_calls"]))
    if server:
        server.close()

    time.sleep(delay)
    answer = scenario.get("answer", "OK")
    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}})
    if not scenario.get("no_result"):
        emit({"type": "result", "subtype": "success", "is_error": False, "result": answer, "num_turns": 2, "total_cost_usd": 0,
              "duration_ms": 1, "permission_denials": [], "stop_reason": "end_turn", "session_id": "fake-session", **(scenario.get("result") or {})})
    record["finished"] = True
    save()
    return int(scenario.get("exit_code") or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
