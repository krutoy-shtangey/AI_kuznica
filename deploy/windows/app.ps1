[CmdletBinding()]
param(
    [ValidateSet('Start','Stop','Status','Restart','Logs','Run','Backup')]
    [string]$Action = 'Status',
    [ValidateSet('demo-offline','disabled','retrieval-offline','retrieval-proxyapi')][string]$Mode = 'demo-offline',
    [switch]$Follow,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$state = Join-Path $root 'storage\windows'
$python = if ($Python) {
    (Resolve-Path -LiteralPath $Python).Path
} elseif (Test-Path -LiteralPath (Join-Path $state 'runtime\venv-ready') -PathType Leaf) {
    Join-Path $state 'runtime\venv\Scripts\python.exe'
} else {
    Join-Path $root '.venv\Scripts\python.exe'
}
$privateMode = $Mode -in @('retrieval-offline', 'retrieval-proxyapi')
$port = switch ($Mode) { 'retrieval-offline' { 8001 } 'retrieval-proxyapi' { 8002 } default { 8000 } }
$url = "http://127.0.0.1:$port"
$run = Join-Path $state $(if ($privateMode) { 'run-retrieval' } else { 'run' })
$info = Join-Path $run 'server.json'
$log = Join-Path $root $(if ($privateMode) { 'logs\windows\retrieval\server.log' } else { 'logs\windows\server.log' })
$env:PYTHONUTF8 = '1'

function Read-State {
    if (Test-Path -LiteralPath $info) {
        return Get-Content -Raw -LiteralPath $info | ConvertFrom-Json
    }
    return $null
}
function Stop-App {
    $saved = Read-State
    if (-not $saved) { Write-Output 'No managed server state.'; return }
    if ($privateMode -and $saved.mode -ne $Mode) { throw "Use -Mode $($saved.mode) to stop the running server." }
    New-Item -ItemType File -Force -Path (Join-Path $run 'stop') | Out-Null
    for ($i=0; $i -lt 160; $i++) {
        if (-not (Test-Path -LiteralPath $info)) { Write-Output 'Stopped.'; return }
        Start-Sleep -Milliseconds 250
    }
    throw 'Stop timeout. Inspect the supervisor; no unrelated process was killed.'
}
function Start-App {
    if (Read-State) { throw 'Managed state already exists. Use Status or Stop before starting.' }
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup.ps1 first.' }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'frontend\dist\index.html'))) { throw 'Frontend build missing. Run setup.ps1.' }
    # Dependency check must fail synchronously, before launching a background process.
    & $python -c 'import fastapi, uvicorn, openai, dotenv, certifi'
    if ($LASTEXITCODE) { throw 'Backend dependencies missing. Run setup.ps1.' }
    if ($privateMode) {
        if (-not (Test-Path -LiteralPath (Join-Path $state 'index\CURRENT'))) { throw 'Local index missing. Run prepare-retrieval.ps1.' }
        & $python -c 'import torch, sentence_transformers, docx, numpy, snowballstemmer'
        if ($LASTEXITCODE) { throw 'Retrieval dependencies missing. Run prepare-retrieval.ps1.' }
    }
    $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
    try { $probe.Server.ExclusiveAddressUse = $true; $probe.Start() } finally { $probe.Stop() }
    New-Item -ItemType Directory -Force -Path $run,(Split-Path $log) | Out-Null
    $supervisor = Join-Path $PSScriptRoot 'server.py'
    Start-Process -FilePath $python -ArgumentList @('-B', ('"' + $supervisor + '"'), '--mode', $Mode) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path (Split-Path $log) 'supervisor.out.log') -RedirectStandardError (Join-Path (Split-Path $log) 'supervisor.err.log') | Out-Null
    # Allow a cold CPU model load after Windows starts.
    for ($i=0; $i -lt 180; $i++) {
        Start-Sleep -Milliseconds 500
        $saved = Read-State
        if ($saved) {
            try {
                $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 1
                $expected = if ($privateMode) { 'configured' } elseif ($Mode -eq 'demo-offline') { 'demo' } else { 'disabled' }
                $model = switch ($Mode) { 'retrieval-offline' { 'offline-fixture' } 'retrieval-proxyapi' { 'proxyapi-qwen' } }
                if ($health.status -eq 'ok' -and $health.retrieval -eq $expected -and
                    (-not $privateMode -or $health.model -eq $model)) {
                    Write-Output "Running: $url ($Mode)"
                    return
                }
            } catch { }
        }
    }
    throw 'Startup did not become healthy. Inspect Logs and supervisor.err.log.'
}

switch ($Action) {
    'Start' { Start-App }
    'Stop' { Stop-App }
    'Restart' { Stop-App; Start-App }
    'Run' { & $python -B (Join-Path $PSScriptRoot 'server.py') --mode $Mode; if ($LASTEXITCODE) { throw 'Supervisor failed.' } }
    'Status' {
        $saved = Read-State
        if ($saved) {
            $saved | Format-List
            foreach ($processId in @($saved.supervisor_pid, $saved.child_pid)) {
                Get-Process -Id $processId -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,@{Name='WorkingSetMB';Expression={[math]::Round($_.WorkingSet64/1MB,1)}}
            }
        }
        try {
            $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 3
            Write-Output ("HTTP health: " + ($health | ConvertTo-Json -Compress))
        } catch { Write-Output 'HTTP unavailable.' }
    }
    'Logs' {
        if (-not (Test-Path -LiteralPath $log)) { throw 'No server log yet.' }
        Get-Content -LiteralPath $log -Encoding UTF8 -Tail 80 -Wait:$Follow
    }
    'Backup' {
        foreach ($folder in @('run', 'run-retrieval')) {
            if (Test-Path -LiteralPath (Join-Path $state "$folder\server.json")) { throw 'Stop both managed servers before backup.' }
        }
        foreach ($activePort in @(8000, 8001, 8002)) {
            $probe = [Net.Sockets.TcpClient]::new()
            try { $probe.Connect('127.0.0.1', $activePort); throw "Port $activePort is still active. Stop all writers." }
            catch [Net.Sockets.SocketException] { } finally { $probe.Dispose() }
        }
        $backups = Join-Path $root 'backups'
        New-Item -ItemType Directory -Force -Path $backups | Out-Null
        $archive = Join-Path $backups ('windows-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.tar.gz')
        Push-Location $root
        try { & $python -B -m deploy.state backup --state-dir $state --archive $archive; if ($LASTEXITCODE) { throw 'Backup failed.' } }
        finally { Pop-Location }
        Write-Output $archive
    }
}
