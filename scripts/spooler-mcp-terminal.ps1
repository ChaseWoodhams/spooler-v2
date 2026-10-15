<#
.SYNOPSIS
  Starts the read-only Spooler MCP server for ONE project, for a terminal Claude session (see docs/assistant-terminal.md).
.EXAMPLE
  claude mcp add spooler -- powershell -NoProfile -File scripts\spooler-mcp-terminal.ps1 -Project 12 -DataDir D:\spooler-copy
.NOTES
  -DataDir has no default so a live install is never reached by accident. Python comes from .venv if present, else PATH.
#>
param(
  [Parameter(Mandatory = $true)][int]$Project,
  [Parameter(Mandatory = $true)][string]$DataDir
)
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $DataDir 'spooler.db'))) { [Console]::Error.WriteLine("No spooler.db in $DataDir"); exit 2 }
$venv = Join-Path $root '.venv\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $venv) { $venv } else { 'python' }
& $python (Join-Path $root 'spooler_mcp.py') --project $Project --data-dir $DataDir
exit $LASTEXITCODE
