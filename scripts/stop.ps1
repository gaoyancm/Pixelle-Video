# AI Media Platform -- stop services started by start.ps1
#
# Only processes recorded in .run\services.json AND confirmed to belong to this
# project (alive + command line matches the recorded marker) are stopped.
# There is deliberately no name/port-based fallback kill: foreign processes are
# never touched.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path "$ScriptDir\..").Path
$PidFile = Join-Path $ProjectDir ".run\services.json"

Write-Host "============================================" -ForegroundColor Yellow
Write-Host "  AI Media Platform · stopping ..." -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Yellow

if (-not (Test-Path $PidFile)) {
    Write-Host "  No services.json found; nothing recorded to stop." -ForegroundColor DarkGray
    exit 0
}

try {
    $services = Get-Content $PidFile -Raw | ConvertFrom-Json -AsHashtable
} catch {
    Write-Host "  services.json is unreadable; nothing to stop." -ForegroundColor Yellow
    exit 0
}

$stoppedAny = $false
foreach ($role in @("worker", "streamlit", "api")) {
    if (-not $services.ContainsKey($role)) { continue }
    $entry = $services[$role]
    $pid0 = [int]$entry.pid
    $marker = [string]$entry.marker

    $proc = Get-Process -Id $pid0 -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "  [$role] PID $pid0 already gone" -ForegroundColor DarkGray
        continue
    }

    # Ownership check: only stop if the command line matches our marker.
    $isOurs = $false
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$pid0" -ErrorAction Stop).CommandLine
        $isOurs = (
            $null -ne $cmd -and
            $cmd -like "*$marker*" -and
            $cmd -like "*$ProjectDir*"
        )
    } catch {
        $isOurs = $false
    }

    if (-not $isOurs) {
        Write-Host "  [$role] PID $pid0 is NOT a project process; leaving it alone." -ForegroundColor Yellow
        continue
    }

    # Graceful first, then force after a short grace window.
    Write-Host "  [$role] stopping PID $pid0 ..." -ForegroundColor Green
    Stop-Process -Id $pid0 -ErrorAction SilentlyContinue
    $gone = $false
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Milliseconds 300
        if (-not (Get-Process -Id $pid0 -ErrorAction SilentlyContinue)) { $gone = $true; break }
    }
    if (-not $gone) {
        Write-Host "  [$role] graceful stop timed out; forcing." -ForegroundColor Yellow
        Stop-Process -Id $pid0 -Force -ErrorAction SilentlyContinue
    }
    Write-Host "  [$role] stopped" -ForegroundColor Green
    $stoppedAny = $true
}

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue

Write-Host ""
if ($stoppedAny) {
    Write-Host "  All project services stopped." -ForegroundColor Green
} else {
    Write-Host "  No running project services found." -ForegroundColor DarkGray
}
