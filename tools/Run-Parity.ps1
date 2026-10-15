# Runs the PETG/ASA parity workflow (tools/PARITY.md). Everything happens in disposable temporary directories; no live
# Project, saved profile, printer or SEAT fixture is touched.  Run:  powershell -File tools\Run-Parity.ps1
param(
    [string]$Orca = '',          # the pinned OrcaSlicer 2.4.2 orca-slicer.exe; looked for beside the project when omitted
    [string]$Python = '',        # the project's Python; defaults to .venv in the project root
    [string]$Diagnostics = ''    # where failure diagnostics are kept; defaults under the system temp directory
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot      # the project root (the folder holding server.py and tests\)
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw "Project Python not found at $Python. Pass -Python, or see CODEX-RESTORE.md." }
if (-not $Orca) {
    foreach ($candidate in @((Join-Path $PSScriptRoot 'OrcaSlicer-2.4.2\orca-slicer.exe'), (Join-Path $root '..\tools\OrcaSlicer-2.4.2\orca-slicer.exe'))) {
        if (Test-Path -LiteralPath $candidate) { $Orca = (Resolve-Path -LiteralPath $candidate).Path; break }
    }
}
$env:PYTHONIOENCODING = 'utf-8'
if ($Diagnostics) { $env:SPOOLER_PARITY_DIAGNOSTICS = $Diagnostics }
Push-Location $root
try {
    Write-Host '== Fast parity logic (no Orca needed) =='
    & $Python -m unittest tests.test_parity_logic
    if ($LASTEXITCODE -ne 0) { throw 'Fast parity logic tests failed.' }
    if (-not $Orca -or -not (Test-Path -LiteralPath $Orca)) {
        Write-Host ''
        Write-Warning 'The pinned Orca 2.4.2 was not found, so the real-Orca matrix was NOT RUN. This is not a pass. Pass -Orca <path to orca-slicer.exe>.'
        exit 2
    }
    $env:SPOOLER_ORCA = $Orca
    Write-Host ''
    Write-Host "== Real-Orca matrix ($Orca) =="
    & $Python -m unittest tests.test_parity_native
    if ($LASTEXITCODE -ne 0) { throw 'Real-Orca parity matrix failed. Read the diagnostics folder named in the output.' }
    Write-Host ''
    Write-Host 'Parity matrix finished. Known gaps (see tools/PARITY.md) run as expected failures; they are not passes.'
} finally {
    Pop-Location
}
