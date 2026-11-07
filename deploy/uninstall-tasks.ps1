<#
.SYNOPSIS
  Stops Tally Automation and removes its scheduled tasks and firewall rule.
  Data, documents, backups and .env are not touched.

      powershell -ExecutionPolicy Bypass -File deploy\uninstall-tasks.ps1   (as administrator)
#>
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$taskPath = "\TallyAutomation\"
foreach ($name in "Tally Automation", "Tally Automation backup") {
    $task = Get-ScheduledTask -TaskPath $taskPath -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskPath $taskPath -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskPath $taskPath -TaskName $name -Confirm:$false
        Write-Host "Removed task: $name"
    }
}
# Stopping the task ends the supervisor; end the app process it started as well.
$root = Split-Path -Parent $PSScriptRoot
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*-m tally_automation*" -and $_.ExecutablePath -like "$root*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Stopped app process $($_.ProcessId)" }
Get-NetFirewallRule -DisplayName "Tally Automation (tailnet only)" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Done. Data, documents, backups and .env were left in place."
