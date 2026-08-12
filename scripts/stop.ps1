# AI Media Platform -- stop all services

$pidFile = "$env:TEMP\aimedia_pids.txt"

Write-Host "============================================" -ForegroundColor Yellow
Write-Host "  AI Media Platform · stopping ..." -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Yellow

if (Test-Path $pidFile) {
    $pids = Get-Content $pidFile -Raw
    foreach ($pid in $pids -split '\|') {
        if ($pid -match '^\d+$') {
            try {
                Stop-Process -Id ([int]$pid) -Force -ErrorAction SilentlyContinue
                Write-Host "  Stopped PID: $pid" -ForegroundColor Green
            } catch {
                Write-Host "  PID $pid already gone" -ForegroundColor Gray
            }
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# fallback: kill uvicorn/streamlit by name
foreach ($name in @("python", "streamlit")) {
    $procs = Get-Process $name -ErrorAction SilentlyContinue | Where-Object {
        $_.MainWindowTitle -match "api\.app\.py|streamlit|uvicorn"
    }
    foreach ($p in $procs) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Write-Host "  Cleaned up: $name (PID: $($p.Id))" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "  All services stopped." -ForegroundColor Green
