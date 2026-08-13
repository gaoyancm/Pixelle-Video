# AI Media Platform -- one-click launcher
# Double-click 启动平台.bat or run: powershell -ExecutionPolicy Bypass -File scripts/start.ps1

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Resolve-Path "$ScriptDir\.."

$API = "$ProjectDir\.venv\Scripts\python.exe"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  AI Media Platform · launching ..." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# 0. clean up any stale processes on our ports
Write-Host "[0/3] Cleaning stale processes ..." -ForegroundColor DarkGray
foreach ($port in 8000, 8501) {
    $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $pidToKill = $c.OwningProcess
        if ($pidToKill -and $pidToKill -ne $PID) {
            Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
        }
    }
}
Start-Sleep -Seconds 2

# 1. check config.yaml
if (-not (Test-Path "$ProjectDir\config.yaml")) {
    Write-Host "[ERROR] config.yaml not found at $ProjectDir" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# 2. start API backend
Write-Host "[1/3] Starting API server at http://localhost:8000 ..." -ForegroundColor Green
$apiLog = Join-Path $ProjectDir "logs\api.log"
if (-not (Test-Path (Split-Path $apiLog))) { New-Item -ItemType Directory -Path (Split-Path $apiLog) -Force | Out-Null }

# Force UTF-8 for python stdout (Windows default cp1252 crashes on Chinese chars)
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$apiProcess = Start-Process -FilePath $API `
    -ArgumentList "api\app.py" `
    -WorkingDirectory $ProjectDir `
    -RedirectStandardOutput $apiLog `
    -RedirectStandardError "$apiLog.err" `
    -WindowStyle Hidden `
    -PassThru

# wait for API to become ready (TCP probe, up to 30s)
$apiReady = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if ($apiProcess.HasExited) {
        Write-Host "[ERROR] API server exited during startup (code $($apiProcess.ExitCode))." -ForegroundColor Red
        Write-Host "         Check: is port 8000 free? Is config.yaml valid?" -ForegroundColor Yellow
        Read-Host "Press Enter to exit"
        exit 1
    }
    # TCP probe — much more reliable than Invoke-WebRequest
    $probe = Test-NetConnection -ComputerName "127.0.0.1" -Port 8000 -WarningAction SilentlyContinue -InformationLevel Quiet
    if ($probe) {
        Start-Sleep -Seconds 3  # extra settle for FastAPI lifespan startup
        $apiReady = $true
        break
    }
}
if (-not $apiReady) {
    Write-Host "[ERROR] API server did not become ready in 30s." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "       API server ready (PID $($apiProcess.Id))" -ForegroundColor Green

# 3. start Streamlit
Write-Host "[2/3] Starting Streamlit at http://localhost:8501 ..." -ForegroundColor Green
$stProcess = Start-Process -FilePath $API `
    -ArgumentList "-m streamlit run pixelle_video\web\home.py --server.port 8501 --server.headless true --browser.gatherUsageStats false" `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Minimized `
    -PassThru

Start-Sleep -Seconds 8
Write-Host "       Streamlit PID: $($stProcess.Id)" -ForegroundColor Green

# 4. open browser
Write-Host "[3/3] Opening browser ..." -ForegroundColor Cyan
Start-Process "http://localhost:8501"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  All services started!" -ForegroundColor Green
Write-Host "  API:   http://localhost:8000/docs" -ForegroundColor White
Write-Host "  Web:   http://localhost:8501" -ForegroundColor White
Write-Host "  Stop:  停止平台.bat" -ForegroundColor White
Write-Host "============================================" -ForegroundColor Cyan

"$($apiProcess.Id)|$($stProcess.Id)" | Out-File -FilePath "$env:TEMP\aimedia_pids.txt" -Encoding utf8

Read-Host "Press Enter to close this window (services keep running)"
