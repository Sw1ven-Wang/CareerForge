[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status')][string]$Action = 'Start',
    [string]$Model = 'gpt-6.1-terra'
)
$ErrorActionPreference = 'Stop'
$sourceDir = $PSScriptRoot
$runtimeDir = Join-Path $sourceDir 'private\local-runtime'
$pythonExe = Join-Path $sourceDir '.venv\Scripts\python.exe'
$pidFile = Join-Path $runtimeDir 'web-process.json'
$env:CAREERFORGE_MODE = 'development'
$env:CAREERFORGE_STATE_DIR = $runtimeDir
$env:CAREERFORGE_DATABASE_URL = 'sqlite+pysqlite:///' + ((Join-Path $runtimeDir 'development.db') -replace '\\', '/')
$env:CAREERFORGE_API_URL = 'http://127.0.0.1:8000'
$env:MODEL_NAME = $Model
$env:CAREERFORGE_MODEL_TIMEOUT = '300'
$env:CAREERFORGE_MODEL_STREAMING = '1'
$env:CAREERFORGE_ROLE_MODELS = '{}'
$env:PYTHONIOENCODING = 'utf-8'
$env:EMBEDDING_BASE_URL = ''
$env:EMBEDDING_API_KEY = ''
$env:EMBEDDING_MODEL = ''
$env:RERANK_URL = ''
$deskUrl = 'http://127.0.0.1:8001'
if ($Action -eq 'Stop') {
    $journalPath = Join-Path $runtimeDir 'web-jobs.json'
    if (Test-Path -LiteralPath $journalPath) {
        $jobs = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
        $runningJobs = @($jobs.PSObject.Properties.Value | Where-Object { $_.status -eq 'running' })
        if ($runningJobs.Count -gt 0) { throw 'A learning task is still running. Wait for it to finish before stopping the desk.' }
    }
    if (Test-Path -LiteralPath $pidFile) {
        $saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        $process = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
        if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq ([datetimeoffset]$saved.started).UtcDateTime.Ticks) {
            Stop-Process -Id $saved.pid
            Wait-Process -Id $saved.pid -Timeout 10 -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $pidFile
    }
    Write-Output 'Learning desk stopped.'
    exit 0
}
if ($Action -eq 'Status') {
    $result = Invoke-RestMethod "$deskUrl/desk/overview" -Headers @{'X-CareerForge-Desk'='1'}
    @{url=$deskUrl;model=$result.model;knowledge=$result.publications.Count;plans=$result.plans.Count} | ConvertTo-Json
    exit 0
}
if (!(Test-Path -LiteralPath (Join-Path $runtimeDir 'client.json'))) {
    throw 'Run run-local.ps1 Start first to restore the local delivery instance.'
}
if (Get-NetTCPConnection -State Listen -LocalPort 8001 -ErrorAction SilentlyContinue) {
    if (Test-Path -LiteralPath $pidFile) {
        $saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        $process = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
        if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq ([datetimeoffset]$saved.started).UtcDateTime.Ticks) {
            Write-Output "Learning desk already running: $deskUrl"
            exit 0
        }
    }
    throw 'Port 8001 is occupied by another process.'
}
$process = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'uvicorn', 'careerforge.local_web:create_local_web', '--factory', '--host', '127.0.0.1', '--port', '8001', '--timeout-graceful-shutdown', '330') -WorkingDirectory $sourceDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'web.stdout.log') -RedirectStandardError (Join-Path $runtimeDir 'web.stderr.log') -PassThru
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-WebRequest $deskUrl -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $listener = Get-NetTCPConnection -State Listen -LocalPort 8001 | Select-Object -First 1
            $serverProcess = Get-Process -Id $listener.OwningProcess
            @{pid=$serverProcess.Id;started=$serverProcess.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
            Write-Output "CareerForge learning desk: $deskUrl"
            exit 0
        }
    } catch {
        if ($attempt -eq 29) { throw "Desk did not start. See $runtimeDir\web.stderr.log" }
    }
}
