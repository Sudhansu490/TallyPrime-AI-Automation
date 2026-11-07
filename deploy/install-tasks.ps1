<#
.SYNOPSIS
  Registers Tally Automation to start with Windows on Computer A, plus a daily backup.

.DESCRIPTION
  Run once, in PowerShell opened with "Run as administrator", from the project folder:

      powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1

  Creates two scheduled tasks under \TallyAutomation\ that run as the current Windows
  user, whether or not anyone is logged in (no password is stored: S4U logon):

  * "Tally Automation"         – at system startup (after 1 minute, so Tailscale is up),
                                 runs deploy\run-app.ps1, which keeps the app running.
  * "Tally Automation backup"  – daily at -BackupTime: python -m tally_automation backup.

  Optional: -AllowTailnet adds a Windows Firewall rule admitting APP_PORT only from the
  Tailscale range 100.64.0.0/10 (needed for Computer B; nothing else is opened).

  Runs "python -m tally_automation check --no-tally" first and stops if it reports a problem.
  Remove everything again with deploy\uninstall-tasks.ps1.
#>
#Requires -RunAsAdministrator
param(
    [string]$BackupTime = "02:00",
    [switch]$AllowTailnet,
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "tally_env\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python environment not found at $python" }

Write-Host "Checking the installation..."
Push-Location $root
try {
    & $python -m tally_automation check --no-tally
    if ($LASTEXITCODE -ne 0) { throw "Fix the problems reported above, then run this script again." }
} finally { Pop-Location }

$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$taskPath = "\TallyAutomation\"

# The app: at startup, kept running by run-app.ps1; never stopped by a time limit.
$appTrigger = New-ScheduledTaskTrigger -AtStartup
$appTrigger.Delay = "PT1M"
$appAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$root\deploy\run-app.ps1`"" `
    -WorkingDirectory $root
$appSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "Tally Automation" -TaskPath $taskPath -Action $appAction -Trigger $appTrigger `
    -Principal $principal -Settings $appSettings -Force `
    -Description "Tally Automation web application (loopback/Tailscale only). Project: $root" | Out-Null

# Daily backup.
$backupTrigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
$backupAction = New-ScheduledTaskAction -Execute $python -Argument "-m tally_automation backup" -WorkingDirectory $root
$backupSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Tally Automation backup" -TaskPath $taskPath -Action $backupAction -Trigger $backupTrigger `
    -Principal $principal -Settings $backupSettings -Force `
    -Description "Daily copy of the Tally Automation database and documents to BACKUP_DIR." | Out-Null

if ($AllowTailnet) {
    $rule = "Tally Automation (tailnet only)"
    Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
        -RemoteAddress "100.64.0.0/10" -Profile Any | Out-Null
    Write-Host "Firewall: TCP $Port allowed from 100.64.0.0/10 only."
}

Start-ScheduledTask -TaskPath $taskPath -TaskName "Tally Automation"
Write-Host "Registered and started. Check: Get-ScheduledTask -TaskPath $taskPath | Get-ScheduledTaskInfo"
Write-Host "The app answers at http://127.0.0.1:$Port/ within about a minute (supervisor log: data\logs\supervisor.log)."
