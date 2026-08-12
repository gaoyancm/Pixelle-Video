# AI Media Platform — 一键启动脚本
# 双击此文件或在 PowerShell 中运行：
#   powershell -ExecutionPolicy Bypass -File scripts/start.ps1

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Resolve-Path "$ScriptDir\..\06-source\pixelle-video"

Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   AI Media Platform · 启动中...     ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan

# 1. 确认 config.yaml 存在
if (-not (Test-Path "$ProjectDir\config.yaml")) {
    Write-Host "❌ 未找到 config.yaml，请先配置。" -ForegroundColor Red
    exit 1
}

# 2. 启动 API 后端
Write-Host "🚀 启动 API 服务 (http://localhost:8000) ..." -ForegroundColor Green
$apiProcess = Start-Process -FilePath "$ProjectDir\.venv\Scripts\python.exe" `
    -ArgumentList "$ProjectDir\api\app.py" `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Minimized `
    -PassThru

Start-Sleep -Seconds 3

if ($apiProcess.HasExited) {
    Write-Host "❌ API 服务启动失败，请查看错误日志。" -ForegroundColor Red
    exit 1
}
Write-Host "✅ API 服务已启动 (PID: $($apiProcess.Id))" -ForegroundColor Green

# 3. 启动 Streamlit
Write-Host "🎨 启动 Streamlit 页面 (http://localhost:8501) ..." -ForegroundColor Green
$streamlitProcess = Start-Process -FilePath "$ProjectDir\.venv\Scripts\python.exe" `
    -ArgumentList "-m streamlit run pixelle_video\web\home.py --server.port 8501 --server.headless true" `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Minimized `
    -PassThru

Start-Sleep -Seconds 5

# 4. 打开浏览器
Write-Host "🌐 打开浏览器..." -ForegroundColor Cyan
Start-Process "http://localhost:8501"

Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   ✅ 全部服务已启动                  ║" -ForegroundColor Cyan
Write-Host "║   API:   http://localhost:8000/docs  ║" -ForegroundColor White
Write-Host "║   页面:  http://localhost:8501       ║" -ForegroundColor White
Write-Host "║   停止:  运行 scripts\stop.ps1       ║" -ForegroundColor White
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# 保存 PID 到文件（用于 stop.ps1 精确杀进程）
"$($apiProcess.Id)|$($streamlitProcess.Id)" | Out-File -FilePath "$env:TEMP\aimedia_pids.txt" -Encoding utf8

Read-Host "按 Enter 关闭此窗口（服务将继续在后台运行）"
