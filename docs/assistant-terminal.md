# Using the Spooler MCP server from a terminal Claude session

A power user can start the same read-only Spooler MCP server the Assistant panel uses, for one named project, from a terminal
`claude` session. Same eleven tools, same guarantees. Proposals made there appear as cards in that project's Assistant panel;
Accept still happens only in the review sheet.

## Start it

From the Spooler folder (use the Python that has Spooler's requirements, e.g. `.venv\Scripts\python.exe`):

```
claude mcp add spooler -- python spooler_mcp.py --project 12 --data-dir "D:\spooler-copy"
```

or the equivalent `.mcp.json` snippet:

```json
{
  "mcpServers": {
    "spooler": {
      "command": "python",
      "args": ["C:\path\to\spooler-v2\spooler_mcp.py", "--project", "12", "--data-dir", "D:\spooler-copy"]
    }
  }
}
```

`python spooler_mcp.py --help` prints the same usage. `scripts/spooler-mcp-terminal.ps1 -Project 12 -DataDir <dir>` runs it directly
(it only checks the arguments and starts the server).

## The data directory is explicit

`--data-dir` is required. There is no default, on purpose: a terminal session must never silently read or stage into a live
install. Pass the directory of the Spooler you want to talk to (the folder holding `spooler.db` and `projects\`). The
Assistant panel only shows terminal Proposals that sit in the data directory of the running app, so to review them there, give the
live data directory deliberately; to experiment safely, point at a copy.

## What it can and cannot do

- Bound to the one `--project`; no tool takes a project id, so no terminal path reaches another project.
- Read-only: SQLite is opened `mode=ro`; no sockets; no import of jobs, printers, Spoolman or push code.
- Propose tools only stage a Proposal file under `<data dir>\projects\.assistant\<token>\`. Nothing here can apply one. Applying
  happens in the app, when the person presses Accept in the review sheet.
- A server started without `--run` names its session `terminal-<random>`. Limits are per session: at most 3 Proposals, and only
  the newest of each kind is kept.

## In the app

While the Assistant panel is open on a project it looks every few seconds for Proposals staged from a terminal session
(`GET /api/projects/<id>/assistant/proposals`) and shows each as a card. Review, Dismiss, Accept and Superseded work as for any
Proposal: any edit to the project, or a newer revision, makes a pending one Superseded (it is swept and not shown). Unaccepted
Proposals are swept after 24 hours and at app startup.
