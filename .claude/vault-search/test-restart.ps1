# Test NSSM auto-restart: kill the daemon, verify NSSM restarts it within 10s.
$ErrorActionPreference = "Continue"

$conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if (-not $conn) { throw "no listener on 8765" }
$oldPid = $conn.OwningProcess
"old pid: $oldPid"

Stop-Process -Id $oldPid -Force
$killTime = Get-Date
"killed at: $killTime"

$restoredAt = $null
while ((Get-Date) - $killTime -lt [TimeSpan]::FromSeconds(20)) {
    Start-Sleep -Milliseconds 500
    $newConn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($newConn -and $newConn.OwningProcess -ne $oldPid) {
        $restoredAt = Get-Date
        $elapsed = ($restoredAt - $killTime).TotalSeconds
        "port reopened by pid $($newConn.OwningProcess) after $([math]::Round($elapsed, 2))s"
        break
    }
}
if (-not $restoredAt) { "FAIL: port did not reopen within 20s" }

# Wait for /health 200
$healthAt = $null
while ((Get-Date) - $killTime -lt [TimeSpan]::FromSeconds(120)) {
    try {
        $resp = Invoke-WebRequest -Uri http://127.0.0.1:8765/health -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) {
            $healthAt = Get-Date
            $elapsed = ($healthAt - $killTime).TotalSeconds
            "/health returned 200 after $([math]::Round($elapsed, 2))s"
            "$($resp.Content)"
            break
        }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $healthAt) { "FAIL: /health did not return 200 within 120s" }
