# Removes the autostart entries created by install-autostart.ps1.
# Services that are already running keep running; use stop-local.ps1 to stop them.
$ErrorActionPreference = "Stop"

$taskName = "Spooler Supervisor"
$shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Spooler Supervisor.lnk"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task '$taskName'."
}
if (Test-Path -LiteralPath $shortcut) {
    Remove-Item -LiteralPath $shortcut
    Write-Host "Removed Startup shortcut."
}
