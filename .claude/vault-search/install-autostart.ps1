# Register the vault-search daemon to auto-launch at logon via Task Scheduler.
# No admin required - this is the user-level alternative to install-service.ps1
# (NSSM), which needs elevation but adds crash-restart supervision.
#
# Prerequisite: run install_daemon.py first so the venv exists.
# Idempotent: re-running replaces the existing task.
#
# Usage:
#   .\install-autostart.ps1              # register the logon task and start now
#   .\install-autostart.ps1 -Uninstall   # remove the logon task

param([switch]$Uninstall)

$ErrorActionPreference = "Stop"

$TaskName = "note-kit-vault-search"
$DaemonDir = $PSScriptRoot   # <vault>\.claude\vault-search (this script's folder)
$Launcher = Join-Path $DaemonDir "daemonctl.py"
$VenvPythonW = Join-Path $DaemonDir ".venv\Scripts\pythonw.exe"   # windowless - no console flash at logon

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed logon task '$TaskName'."
    } else {
        Write-Host "No logon task '$TaskName' to remove."
    }
    return
}

if (-not (Test-Path $VenvPythonW)) { throw "venv interpreter not found at $VenvPythonW - run install_daemon.py first" }
if (-not (Test-Path $Launcher))    { throw "daemonctl.py not found at $Launcher" }

# The task runs daemonctl start, which exits once the daemon is detached and
# healthy (or after its 90s warmup window), so the task itself is short-lived.
# The 10-minute limit is headroom for a first run that downloads the model.
$action = New-ScheduledTaskAction -Execute $VenvPythonW `
    -Argument "`"$Launcher`" start" -WorkingDirectory $DaemonDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Write-Host "Registered logon task '$TaskName' (runs daemonctl start at logon)."

Start-ScheduledTask -TaskName $TaskName
Write-Host "Started the task; the daemon is launching in the background."
Write-Host "Verify: .\.venv\Scripts\python.exe daemonctl.py status"
