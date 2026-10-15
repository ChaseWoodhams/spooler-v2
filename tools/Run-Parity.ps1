# Runs the PETG/ASA parity workflow (tools/PARITY.md). Everything happens in disposable temporary directories; no live
# Project, saved profile, printer or SEAT fixture is touched.  Run:  powershell -File tools\Run-Parity.ps1
#
# Exit codes: 0 passed with no known gaps; 2 the pinned Orca was not found (the real matrix was NOT RUN); 3 the matrix ran but
# known gaps are still open (not a clean pass); anything else is a failure.
param(
    [string]$Orca = '',          # the pinned OrcaSlicer 2.4.2 orca-slicer.exe; looked for beside the project when omitted
    [string]$Python = '',        # the project's Python; defaults to .venv in the project root
    [string]$Diagnostics = '',   # where failure diagnostics are kept; defaults under the system temp directory
    [switch]$Integrated          # also run the Project service/HTTP suites (staged loss review, export choices, Slice trust, ...)
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
    $log = @()
    $ErrorActionPreference = 'Continue'    # unittest reports on stderr; Windows PowerShell 5.1 would treat each line as an error
    & $Python -m unittest tests.test_parity_native 2>&1 | ForEach-Object { Write-Host $_; $log += "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($code -ne 0) { throw 'Real-Orca parity matrix failed. Read the diagnostics folder named in the output.' }
    if ($Integrated) {
        Write-Host ''
        Write-Host '== Integrated Project suites (real Orca on) =='
        $env:ORCA_SLICER_EXE = $Orca
        $modules = Get-ChildItem (Join-Path $root 'tests') -Filter 'test_*.py' |
            Where-Object { $_.BaseName -match 'setting|review|filament|import|intake|orca|override|validat|object_scope|3mf|projects|effective|plate_filament|slice_snap|copies|workflow|problem|draft|observed|export|gcode_meta' } |
            ForEach-Object { "tests.$($_.BaseName)" }
        & $Python -m unittest @modules
        if ($LASTEXITCODE -ne 0) { throw 'Integrated Project suites failed.' }
        Write-Host 'The browser specs are separate: node tests/ui/run.cjs (set NODE_PATH to the project node_modules).'
    }
    Write-Host ''
    if ($log -match 'expected failures=') {
        Write-Warning 'The matrix ran, but KNOWN GAPS are still open (expected failures above; see tools/PARITY.md). That is NOT a clean pass.'
        exit 3
    }
    Write-Host 'Parity matrix passed with no known gaps.'
} finally {
    Pop-Location
}
