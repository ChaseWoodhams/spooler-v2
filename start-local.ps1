# Starts the Spooler supervisor, which launches and watches Spoolman and Spooler,
# then waits until both answer their health checks.
# Safe to run repeatedly: a second supervisor exits at once if one is already running.
$ErrorActionPreference = "Stop"

$spoolerPath = $PSScriptRoot
$python      = Join-Path $spoolerPath ".venv\Scripts\python.exe"
$pythonw     = Join-Path $spoolerPath ".venv\Scripts\pythonw.exe"
$supervisor  = Join-Path $spoolerPath "supervisor.py"

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Spooler's Python environment was not found at $pythonw"
}

Start-Process -FilePath $pythonw -ArgumentList "`"$supervisor`"" -WorkingDirectory $spoolerPath -WindowStyle Hidden

& $python $supervisor --wait 120
if ($LASTEXITCODE -ne 0) {
    throw "Services did not become healthy. See logs\supervisor.log in Spooler's data directory."
}
