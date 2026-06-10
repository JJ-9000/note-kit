# Install vault-search as a Windows service via NSSM.
# Run from an elevated PowerShell. Idempotent.
#
# Prerequisite: run install_daemon.py first so the venv + config exist.
# This script auto-locates the daemon relative to its own location
# (it ships inside the kit at <vault>\.claude\vault-search\).

$ErrorActionPreference = "Stop"

$NssmPath = "C:\Tools\nssm\nssm.exe"
$ServiceName = "vault-search"
$DaemonDir = $PSScriptRoot   # <vault>\.claude\vault-search (this script's folder)
$VenvPython = Join-Path $DaemonDir ".venv\Scripts\python.exe"
$ServerScript = Join-Path $DaemonDir "server.py"
# Supervisor (NSSM) logs go alongside the daemon's own data dir, which
# install_daemon.py created at <daemon>\data. Keeps everything self-contained.
$DataDir = Join-Path $DaemonDir "data"
$StdoutLog = Join-Path $DataDir "stdout.log"
$StderrLog = Join-Path $DataDir "stderr.log"

if (-not (Test-Path $NssmPath))      { throw "NSSM not found at $NssmPath" }
if (-not (Test-Path $VenvPython))    { throw "venv Python not found at $VenvPython - run install_daemon.py first" }
if (-not (Test-Path $ServerScript))  { throw "server.py not found at $ServerScript" }

if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir | Out-Null
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service '$ServiceName' already exists. Stopping + removing for a clean install."
    & $NssmPath stop $ServiceName confirm | Out-Null
    & $NssmPath remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host "Installing service '$ServiceName'..."
& $NssmPath install $ServiceName $VenvPython $ServerScript
& $NssmPath set $ServiceName AppDirectory $DaemonDir
& $NssmPath set $ServiceName Description "Vault semantic search MCP daemon"
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppStdout $StdoutLog
& $NssmPath set $ServiceName AppStderr $StderrLog
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateBytes 10485760  # 10 MB
& $NssmPath set $ServiceName AppRestartDelay 5000     # 5 sec
& $NssmPath set $ServiceName AppExit Default Restart
& $NssmPath set $ServiceName AppEnvironmentExtra "PYTHONIOENCODING=utf-8"

Write-Host "Starting service..."
& $NssmPath start $ServiceName

Start-Sleep -Seconds 3
$svc = Get-Service -Name $ServiceName
Write-Host ""
Write-Host "Service status: $($svc.Status)"
Write-Host "Service startup: $($svc.StartType)"
