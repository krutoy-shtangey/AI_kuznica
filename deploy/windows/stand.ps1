[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Backend','Tunnel')][string]$Component,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$state = Join-Path $root 'storage\windows'
$settingsPath = Join-Path $state 'stand.json'
$tunnelLog = Join-Path $state 'tunnel-lifecycle.log'

function Write-TunnelEvent([string]$Message) {
    $line = '{0:o} pid={1} {2}' -f [DateTimeOffset]::Now, $PID, $Message
    Add-Content -LiteralPath $tunnelLog -Value $line -Encoding UTF8
}

if ($Component -eq 'Tunnel' -and -not $Check) {
    $sessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
    Write-TunnelEvent "Tunnel runner started in Windows session $sessionId."
}
trap {
    if ($Component -eq 'Tunnel' -and -not $Check) {
        $line = $_.InvocationInfo.ScriptLineNumber
        $errorType = $_.Exception.GetType().Name
        Write-TunnelEvent "Tunnel runner stopped at line $line ($errorType)."
    }
    break
}
$settings = if (Test-Path -LiteralPath $settingsPath) {
    Get-Content -Raw -Encoding UTF8 -LiteralPath $settingsPath | ConvertFrom-Json
} else {
    [pscustomobject]@{publicEnabled = $false}
}
if ($settings.publicEnabled -isnot [bool]) { throw 'stand.json requires boolean publicEnabled.' }
$public = $settings.publicEnabled
$standHost = $settings.publicHost
if ($public -or ($Check -and $Component -eq 'Tunnel')) {
    if ($standHost -cne 'oak-update-dealing.ngrok-free.dev') {
        throw 'Public access is restricted to oak-update-dealing.ngrok-free.dev.'
    }
}

if ($Component -eq 'Backend') {
    $python = Join-Path $state 'runtime\venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Prepare the permanent Python environment first.' }
    @'
from pathlib import Path
import sys

assert sys.version_info[:2] == (3, 12)
assert "codex-runtimes" not in str(Path(sys.base_prefix).resolve()).lower()
'@ | & $python -I -B -
    if ($LASTEXITCODE) { throw 'The stand requires permanent Python 3.12 outside the Codex runtime cache.' }
    if ($Check) { Write-Output 'Permanent backend interpreter is ready.'; return }
    $env:REGLAMENT_ALLOW_PUBLIC = if ($public) { '1' } else { '0' }
    $env:REGLAMENT_PUBLIC_HOST = if ($public) { $standHost } else { '' }
    if ($null -ne $settings.smtpNotifications -and $settings.smtpNotifications -isnot [bool]) {
        throw 'stand.json smtpNotifications must be a boolean.'
    }
    $env:SMTP_ENABLED = if ($settings.smtpNotifications -eq $true) { '1' } else { '0' }
    $env:SMTP_SUMMARY_ONLY = '1'
    & (Join-Path $PSScriptRoot 'app.ps1') Run -Mode retrieval-proxyapi -Python $python
    return
}

if (-not $public -and -not $Check) {
    Write-Output 'Public stand is disabled. ngrok was not started.'
    return
}

$ngrok = if ($settings.ngrokExe) { $settings.ngrokExe } else {
    Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\ngrok.exe'
}
$agentConfig = if ($settings.ngrokConfig) { $settings.ngrokConfig } else {
    Join-Path $env:LOCALAPPDATA 'ngrok\ngrok.yml'
}
if (-not [IO.Path]::IsPathRooted($ngrok) -or -not [IO.Path]::IsPathRooted($agentConfig)) {
    throw 'ngrokExe and ngrokConfig must be absolute paths.'
}
& $ngrok config check --config $agentConfig
if ($LASTEXITCODE) {
    if (-not $Check) { Write-TunnelEvent "Agent configuration check failed ($LASTEXITCODE)." }
    throw 'ngrok configuration check failed.'
}

if ($Check) {
    Write-Output "Public hostname and agent validated locally for https://$standHost. No tunnel started."
    return
}
$python = Join-Path $state 'runtime\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Prepare the permanent Python environment first.' }
$probe = @'
from http.client import HTTPConnection
import json
import sys

headers = {"Host": sys.argv[1], "X-Forwarded-Proto": "https"}
conn = HTTPConnection("127.0.0.1", 8002, timeout=2)
try:
    conn.request("GET", "/api/health", headers=headers)
    response = conn.getresponse()
    ok = response.status == 200 and json.load(response).get("model") == "proxyapi-qwen"
    conn.close()
    if not ok:
        sys.exit(1)
    conn.request("GET", "/api/quality/appeals", headers=headers)
    sys.exit(0 if conn.getresponse().status == 403 else 1)
except OSError:
    sys.exit(1)
finally:
    conn.close()
'@
while ($true) {
    $current = Get-Content -Raw -Encoding UTF8 -LiteralPath $settingsPath | ConvertFrom-Json
    if ($current.publicEnabled -isnot [bool] -or -not $current.publicEnabled -or $current.publicHost -cne $standHost) {
        Write-Output 'Public stand disabled or hostname changed.'
        return
    }
    $probe | & $python -I -B - $standHost
    if ($LASTEXITCODE -eq 0) {
        Write-TunnelEvent 'Starting ngrok after backend and admin-route checks passed.'
        & $ngrok http 'http://127.0.0.1:8002' --url "https://$standHost" --inspect=false --config $agentConfig
        Write-TunnelEvent "ngrok exited ($LASTEXITCODE)."
        Write-Warning "ngrok exited ($LASTEXITCODE). Retrying in 10 seconds."
        Start-Sleep -Seconds 10
    } else {
        Start-Sleep -Seconds 5
    }
}

