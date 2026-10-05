# Stops the Spooler supervisor together with Spoolman and Spooler.
$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python (Join-Path $PSScriptRoot "supervisor.py") --stop
exit $LASTEXITCODE
