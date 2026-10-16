<#
.SYNOPSIS
  Run Python test modules one at a time, each with a time cap, and print one line per module.

.EXAMPLE
  .\scripts\run-tests.ps1 assistant_tuning assistant_ask
  .\scripts\run-tests.ps1 -All -TimeoutSec 600

.NOTES
  Modules may be named test_x, x or tests.test_x. A module that outlives the cap is stopped by PID with its children
  (only processes this script started) and reported as TIMEOUT. Exit code 0 only when every module is OK.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)][string[]]$Modules,
    [switch]$All,
    [int]$TimeoutSec = 300
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Spooler's Python environment was not found at $python" }

if ($All) { $Modules = Get-ChildItem (Join-Path $root "tests") -Filter "test_*.py" | Sort-Object Name | ForEach-Object { $_.BaseName } }
if (-not $Modules) { throw "Name at least one module (for example assistant_ask), or pass -All." }

function Stop-Tree([int]$ProcessId) { & taskkill.exe /PID $ProcessId /T /F *> $null }

$results = @()
foreach ($raw in $Modules) {
    $name = ($raw -replace '^tests\.', '') -replace '^(test_)?', 'test_'
    $out = Join-Path $env:TEMP "run-tests-$name.txt"
    $started = Get-Date
    $proc = Start-Process -FilePath $python -ArgumentList @("-m", "unittest", "tests.$name") -WorkingDirectory $root `
        -RedirectStandardOutput "$out.out" -RedirectStandardError $out -NoNewWindow -PassThru
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        Stop-Tree $proc.Id
        $state, $detail = "TIMEOUT", "no result in ${TimeoutSec}s"
    } else {
        $proc.WaitForExit()      # lets the redirected output finish flushing before it is read
        $text = (Get-Content $out -Raw -ErrorAction SilentlyContinue) + (Get-Content "$out.out" -Raw -ErrorAction SilentlyContinue)
        $ran = [regex]::Match($text, 'Ran (\d+) tests? in ([\d.]+)s')
        $bad = [regex]::Matches($text, '(?m)^(ERROR|FAIL): (\S+)') | ForEach-Object { $_.Groups[2].Value } | Select-Object -Unique
        # The result comes from unittest's own summary line: Windows PowerShell 5.1 does not reliably report the exit code here.
        if ($ran.Success -and $text -match '(?m)^OK\b') { $state, $detail = "OK", "$($ran.Groups[1].Value) tests" }
        elseif ($ran.Success) { $state, $detail = "FAILED", ("{0} of {1} tests: {2}" -f @($bad).Count, $ran.Groups[1].Value, ($bad -join ", ")) }
        else { $state, $detail = "NO RESULT", "exit $($proc.ExitCode); see $out" }
    }
    $secs = [int]((Get-Date) - $started).TotalSeconds
    "{0,-9} {1,-34} {2,4}s  {3}" -f $state, $name, $secs, $detail
    $results += $state
}

$notOk = @($results | Where-Object { $_ -ne "OK" }).Count
"{0} of {1} modules OK" -f ($results.Count - $notOk), $results.Count
exit ([int]($notOk -gt 0))
