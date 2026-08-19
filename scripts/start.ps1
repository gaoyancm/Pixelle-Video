# AI Media Platform -- one-click launcher
# Double-click 启动平台.bat or run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 [-NoBrowser]
#
# Startup topology (task 4):
#   0. preflight (static private-GPU validation, no network)
#   1. port guard (report and exit on foreign listeners, never kill them)
#   2. FastAPI           + /health HTTP probe
#   3. Media Worker      (only when media_jobs.enabled) + --check readiness probe
#   4. Streamlit         + HTTP probe
#   5. browser (optional)
#
# Any failure only reaps the processes started by *this* run and exits non-zero.

param(
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [string]$ConfigPath = "",
    [string]$PythonPath = "",
    [int]$ApiPort = 8000,
    [int]$UiPort = 8501
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path "$ScriptDir\..").Path
$API = if ($PythonPath) { $PythonPath } else { Join-Path $ProjectDir ".venv\Scripts\python.exe" }
$RunDir = Join-Path $ProjectDir ".run"
$PidFile = Join-Path $RunDir "services.json"
$ConfigFile = if ($ConfigPath) { $ConfigPath } else { Join-Path $ProjectDir "config.yaml" }

# Run everything from the project root so `python -m pixelle_video.*` resolves
# the package and relative paths (config.yaml, logs/, data/) line up.
Set-Location -LiteralPath $ProjectDir

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  AI Media Platform · launching ..." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# --- helpers ---------------------------------------------------------------

# PIDs started by THIS run, reaped on any failure.
$script:started = New-Object System.Collections.ArrayList

function Stop-Started {
    foreach ($id in $script:started) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
}

function Fail {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    Stop-Started
    if (-not $CheckOnly) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    exit 1
}

function Write-ServicePid {
    param([string]$Role, [int]$Pid, [string]$Marker)
    $services = @{}
    if (Test-Path $PidFile) {
        try { $services = Get-Content $PidFile -Raw | ConvertFrom-Json -AsHashtable } catch { $services = @{} }
    }
    $services[$Role] = @{ pid = $Pid; marker = $Marker }
    $services | ConvertTo-Json | Set-Content -Path $PidFile -Encoding utf8
}

function Test-ProjectProcessAlive {
    # A stale PID is only "ours" if it is still alive AND its command line
    # carries the project marker. Anything else is a dead/foreign PID.
    param([int]$Pid, [string]$Marker)
    $proc = Get-Process -Id $Pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$Pid" -ErrorAction Stop).CommandLine
    } catch {
        return $false
    }
    return (
        $null -ne $cmd -and
        $cmd -like "*$Marker*" -and
        $cmd -like "*$ProjectDir*"
    )
}

function Test-PortFree {
    # Loopback connect + bind probes work without the elevated CIM permission
    # required by Get-NetTCPConnection. They never kill the owning process.
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync("127.0.0.1", $Port)
        if ($connection.Wait(500) -and $client.Connected) {
            Write-Host "[ERROR] Port $Port is already in use by another process." -ForegroundColor Red
            Write-Host "        Refusing to start; the foreign process was NOT killed." -ForegroundColor Yellow
            return $false
        }
    } catch {
        # No listener accepted the loopback connection; continue with bind probe.
    } finally {
        $client.Dispose()
    }
    $probe = $null
    try {
        $probe = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Any,
            $Port
        )
        $probe.Start()
    } catch [System.Net.Sockets.SocketException] {
        Write-Host "[ERROR] Port $Port is already in use by another process." -ForegroundColor Red
        Write-Host "        Refusing to start; the foreign process was NOT killed." -ForegroundColor Yellow
        return $false
    } finally {
        if ($null -ne $probe) { $probe.Stop() }
    }
    return $true
}

# --- 0. stale-project-process guard (prevents duplicate workers) -----------

if (Test-Path $PidFile) {
    try {
        $stale = Get-Content $PidFile -Raw | ConvertFrom-Json -AsHashtable
        foreach ($role in @("api", "worker", "streamlit")) {
            if ($stale.ContainsKey($role)) {
                $entry = $stale[$role]
                if (Test-ProjectProcessAlive -Pid ([int]$entry.pid) -Marker ([string]$entry.marker)) {
                    Fail "A previous $role is still running (PID $($entry.pid)). Stop it first with 停止平台.bat."
                }
            }
        }
    } catch {
        # Unreadable/stale pid file: ignore and overwrite below.
    }
}

# --- 1. preflight ----------------------------------------------------------

Write-Host "[0/4] Preflight (private-GPU configuration) ..." -ForegroundColor DarkGray
if (-not (Test-Path -LiteralPath $ConfigFile)) {
    Fail "config file not found: $ConfigFile"
}
& $API -m pixelle_video.services.preflight --config $ConfigFile --check-network
$preflightExit = $LASTEXITCODE
if ($preflightExit -eq 1) {
    Fail "Preflight failed; fix config.yaml before starting."
}
$restrictedMode = $preflightExit -eq 2
if ($restrictedMode) {
    Write-Host "       restricted mode: API/UI only; private media jobs are rejected" -ForegroundColor Yellow
} else {
    Write-Host "       preflight passed" -ForegroundColor Green
}

# --- 2. port guard ---------------------------------------------------------

Write-Host "[1/4] Checking ports $ApiPort / $UiPort ..." -ForegroundColor DarkGray
if (-not (Test-PortFree $ApiPort)) { Fail "port $ApiPort occupied" }
if (-not (Test-PortFree $UiPort)) { Fail "port $UiPort occupied" }
if ($CheckOnly) {
    Write-Host "       launcher guards passed; no service was started" -ForegroundColor Green
    exit $preflightExit
}

# --- 3. API + /health probe ------------------------------------------------

Write-Host "[2/4] Starting API server at http://localhost:$ApiPort ..." -ForegroundColor Green
New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
$apiLog = Join-Path $ProjectDir "logs\api.log"
New-Item -ItemType Directory -Path (Split-Path $apiLog) -Force | Out-Null

$apiProcess = Start-Process -FilePath $API `
    -ArgumentList @((Join-Path $ProjectDir "api\app.py"), "--port", $ApiPort) `
    -WorkingDirectory $ProjectDir `
    -RedirectStandardOutput $apiLog `
    -RedirectStandardError "$apiLog.err" `
    -WindowStyle Hidden `
    -PassThru
$null = $script:started.Add($apiProcess.Id)
Write-ServicePid -Role "api" -Pid $apiProcess.Id -Marker $ProjectDir

$apiReady = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if ($apiProcess.HasExited) {
        Fail "API server exited during startup (code $($apiProcess.ExitCode)). Check logs\api.log.err"
    }
    try {
        # Real HTTP probe: /health must return status=healthy, not just TCP.
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/health" -UseBasicParsing -TimeoutSec 2
        $body = $resp.Content | ConvertFrom-Json
        if ($resp.StatusCode -eq 200 -and $body.status -eq "healthy") {
            $apiReady = $true
            break
        }
    } catch {
        # keep polling
    }
}
if (-not $apiReady) {
    Fail "API server did not become healthy in 30s."
}
Write-Host "       API healthy (PID $($apiProcess.Id))" -ForegroundColor Green

# --- 4. Media Worker (only when enabled) -----------------------------------

$mediaJobsEnabled = (& $API -c "from pixelle_video.config.loader import load_config_dict; print('1' if (load_config_dict(r'$ConfigFile') or {}).get('media_jobs', {}).get('enabled') else '0')" 2>$null | Select-Object -Last 1).Trim()

if ($mediaJobsEnabled -eq "1" -and -not $restrictedMode) {
    Write-Host "[3/4] Starting Media Worker ..." -ForegroundColor Green

    # Readiness probe: must reach the DB and load the processor registry.
    & $API -m pixelle_video.media_jobs.worker_cli --config $ConfigFile --check
    if ($LASTEXITCODE -ne 0) {
        Fail "Media Worker readiness probe failed (cannot reach DB / load processors)."
    }
    Write-Host "       worker readiness probe passed" -ForegroundColor Green

    $workerLog = Join-Path $ProjectDir "logs\worker.log"
    $workerProcess = Start-Process -FilePath $API `
        -ArgumentList "-m pixelle_video.media_jobs.worker_cli --config `"$ConfigFile`"" `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $workerLog `
        -RedirectStandardError "$workerLog.err" `
        -WindowStyle Hidden `
        -PassThru
    $null = $script:started.Add($workerProcess.Id)
    Write-ServicePid -Role "worker" -Pid $workerProcess.Id -Marker "worker_cli"
    Start-Sleep -Seconds 2
    if ($workerProcess.HasExited) {
        Fail "Media Worker exited immediately (code $($workerProcess.ExitCode)). Check logs\worker.log.err"
    }
    Write-Host "       Worker started (PID $($workerProcess.Id))" -ForegroundColor Green
} elseif ($restrictedMode) {
    Write-Host "[3/4] Media Worker skipped (restricted mode: no usable private GPU capacity)" -ForegroundColor Yellow
} else {
    Write-Host "[3/4] Media Worker skipped (media_jobs.enabled=false)" -ForegroundColor DarkGray
}

# --- 5. Streamlit + HTTP probe ---------------------------------------------

Write-Host "[4/4] Starting Streamlit at http://localhost:$UiPort ..." -ForegroundColor Green
$stProcess = Start-Process -FilePath $API `
    -ArgumentList "-m streamlit run `"$ProjectDir\pixelle_video\web\home.py`" --server.port $UiPort --server.headless true --browser.gatherUsageStats false" `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Minimized `
    -PassThru
$null = $script:started.Add($stProcess.Id)
Write-ServicePid -Role "streamlit" -Pid $stProcess.Id -Marker $ProjectDir

$uiReady = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$UiPort" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $uiReady = $true; break }
    } catch {
        # keep polling
    }
}
if (-not $uiReady) {
    Fail "Streamlit UI did not become reachable in 40s."
}
Write-Host "       UI ready (PID $($stProcess.Id))" -ForegroundColor Green

# --- 6. browser (optional) -------------------------------------------------

if (-not $NoBrowser) {
    Start-Process "http://localhost:$UiPort"
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
if ($restrictedMode) {
    Write-Host "  Restricted mode started (API/UI; private media disabled)." -ForegroundColor Yellow
} else {
    Write-Host "  All services started!" -ForegroundColor Green
}
Write-Host "  API:   http://localhost:$ApiPort/docs" -ForegroundColor White
Write-Host "  Web:   http://localhost:$UiPort" -ForegroundColor White
Write-Host "  Stop:  停止平台.bat" -ForegroundColor White
Write-Host "============================================" -ForegroundColor Cyan
