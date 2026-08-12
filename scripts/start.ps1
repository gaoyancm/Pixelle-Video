# AI Media Platform -- one-click launcher
# Double-click start.bat or run: powershell -ExecutionPolicy Bypass -File scripts/start.ps1

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Resolve-Path "$ScriptDir\.."

$API = "$ProjectDir\.venv\Scripts\python.exe"
$APIScript = "$ProjectDir\api\app.py"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  AI Media Platform · launching ..." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# 1. check config.yaml
if (-not (Test-Path "$ProjectDir\config.yaml")) {
    Write-Host "[ERROR] config.yaml not found at $ProjectDir" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# 2. start API backend
Write-Host "[1/3] Starting API server at http://localhost:8000 ..." -ForegroundColor Green
$apiProcess = Start-Process -FilePath $API `
    -ArgumentList $APIScript `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Minimized `
    -PassThru

Start-Sleep -Seconds 4

if ($apiProcess.HasExited) {
    Write-Host "[ERROR] API server failed to start." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "       API server PID: $($apiProcess.Id)" -ForegroundColor Green

# 3. start Streamlit
Write-Host "[2/3] Starting Streamlit at http://localhost:8501 ..." -ForegroundColor Green
$stProcess = Start-Process -FilePath $API `
    -ArgumentList "-m streamlit run pixelle_video\web\home.py --server.port 8501 --server.headless true --browser.gatherUsageStats false" `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Minimized `
    -PassThru

Start-Sleep -Seconds 6
Write-Host "       Streamlit PID: $($stProcess.Id)" -ForegroundColor Green

# 4. open browser
Write-Host "[3/3] Opening browser ..." -ForegroundColor Cyan
Start-Process "http://localhost:8501"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  All services started!" -ForegroundColor Green
Write-Host "  API:   http://localhost:8000/docs" -ForegroundColor White
Write-Host "  Web:   http://localhost:8501" -ForegroundColor White
Write-Host "  Stop:  scripts/stop.bat" -ForegroundColor White
Write-Host "============================================" -ForegroundColor Cyan

"$($apiProcess.Id)|$($stProcess.Id)" | Out-File -FilePath "$env:TEMP\aimedia_pids.txt" -Encoding utf8

Read-Host "Press Enter to close this window (services keep running)"
