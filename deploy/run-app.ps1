<#
.SYNOPSIS
  Keeps Tally Automation running: starts it, and starts it again if it stops.

.DESCRIPTION
  Started by the "Tally Automation" scheduled task (see install-tasks.ps1).
  - Waits until the database schema is current (refuses to loop on a schema error).
  - Restarts the app 10 seconds after it exits.
  - Stops trying after 5 exits within 5 minutes (a configuration problem, not a crash);
    see data\logs\supervisor.log and run "python -m tally_automation check".
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "tally_env\Scripts\python.exe"
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "supervisor.log"

function Write-Log([string]$message) {
    $line = "{0:yyyy-MM-ddTHH:mm:ssK} {1}" -f (Get-Date), $message
    Add-Content -Path $log -Value $line -Encoding utf8
}

Set-Location $root
# The app logs to data\logs\app.log (rotated); its console output would only grow.
$env:LOG_TO_CONSOLE = "false"
$recent = New-Object System.Collections.Generic.List[datetime]
Write-Log "supervisor started (pid $PID)"
while ($true) {
    $started = Get-Date
    Write-Log "starting application"
    # Start-Process, not "&": Windows PowerShell 5.1 turns a native program's stderr into errors.
    $process = Start-Process -FilePath $python -ArgumentList "-m", "tally_automation" -WorkingDirectory $root `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput (Join-Path $logDir "app-stdout.log") -RedirectStandardError (Join-Path $logDir "app-stderr.log")
    $code = $process.ExitCode
    Write-Log "application exited with code $code after $([int]((Get-Date) - $started).TotalSeconds) s"

    $recent.Add((Get-Date))
    $cutoff = (Get-Date).AddMinutes(-5)
    $recent.RemoveAll([Predicate[datetime]]{ param($t) $t -lt $cutoff }) | Out-Null
    if ($recent.Count -ge 5) {
        Write-Log "5 exits within 5 minutes: giving up. Run 'python -m tally_automation check' to find the cause."
        exit 1
    }
    Start-Sleep -Seconds 10
}
