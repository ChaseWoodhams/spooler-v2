<#
.SYNOPSIS
Starts the Spooler supervisor automatically when you sign in to Windows.

.DESCRIPTION
Registers a per-user scheduled task that runs supervisor.py in the background at
sign-in and restarts it if it exits abnormally. No administrator rights are
required. If Task Scheduler refuses the registration, a shortcut in the Startup
folder is created instead.

.PARAMETER StartNow
Also start the supervisor immediately.
#>
param([switch]$StartNow)

$ErrorActionPreference = "Stop"

$taskName    = "Spooler Supervisor"
$description = "Keeps Spoolman and the Spooler dashboard running."
$appDir      = $PSScriptRoot
$pythonw     = Join-Path $appDir ".venv\Scripts\pythonw.exe"
$supervisor  = Join-Path $appDir "supervisor.py"
$shortcut    = Join-Path ([Environment]::GetFolderPath("Startup")) "Spooler Supervisor.lnk"

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Spooler's Python environment was not found at $pythonw"
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$registered = $false
try {
    $action    = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$supervisor`"" -WorkingDirectory $appDir
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description $description -Force | Out-Null
    $registered = $true
    Write-Host "Registered scheduled task '$taskName' (runs when $user signs in)."
} catch {
    Write-Warning "Task Scheduler registration failed ($($_.Exception.Message)); using a Startup shortcut instead."
}

if ($registered) {
    if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut }
} else {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($shortcut)
    $link.TargetPath = $pythonw
    $link.Arguments = "`"$supervisor`""
    $link.WorkingDirectory = $appDir
    $link.WindowStyle = 7
    $link.Description = $description
    $link.Save()
    Write-Host "Created Startup shortcut: $shortcut"
}

if ($StartNow) {
    if ($registered) {
        Start-ScheduledTask -TaskName $taskName
    } else {
        Start-Process -FilePath $pythonw -ArgumentList "`"$supervisor`"" -WorkingDirectory $appDir -WindowStyle Hidden
    }
    Write-Host "Supervisor started."
}
