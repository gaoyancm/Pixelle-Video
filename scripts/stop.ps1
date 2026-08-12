# AI Media Platform — 停止所有服务
# 双击或在 PowerShell 中运行

Write-Host "🛑 停止 AI Media Platform ..." -ForegroundColor Yellow

# 1. 从 PID 文件精确清理
$pidFile = "$env:TEMP\aimedia_pids.txt"
if (Test-Path $pidFile) {
    $pids = Get-Content $pidFile -Raw
    foreach ($pid in $pids -split '\|') {
        if ($pid -match '^\d+$') {
            try {
                Stop-Process -Id ([int]$pid) -Force -ErrorAction SilentlyContinue
                Write-Host "  已停止进程 PID: $pid" -ForegroundColor Green
            } catch {
                Write-Host "  进程 PID: $pid 已不存在" -ForegroundColor Gray
            }
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# 2. 兜底：按进程名清理残留
$patterns = @("python", "streamlit", "uvicorn")
foreach ($pattern in $patterns) {
    $procs = Get-Process $pattern -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match "api/app.py|streamlit run|pixelle_video"
    }
    foreach ($proc in $procs) {
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            Write-Host "  已清理残留进程: $($proc.ProcessName) (PID: $($proc.Id))" -ForegroundColor Yellow
        } catch {}
    }
}

Write-Host ""
Write-Host "✅ 所有服务已停止。" -ForegroundColor Green
