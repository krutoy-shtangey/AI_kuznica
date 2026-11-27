[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Backend','Tunnel')][string]$Component,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$state = Join-Path $root 'storage\windows'
$settingsPath = Join-Path $state 'stand.json'
$settings = if (Test-Path -LiteralPath $settingsPath) {
    Get-Content -Raw -Encoding UTF8 -LiteralPath $settingsPath | ConvertFrom-Json
} else {
    [pscustomobject]@{publicEnabled = $false}
}
if ($settings.publicEnabled -isnot [bool]) { throw 'stand.json requires boolean publicEnabled.' }
$public = $settings.publicEnabled
$standHost = $settings.publicHost
if ($public -or ($Check -and $Component -eq 'Tunnel')) {
    if ($standHost -cnotmatch '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ngrok(?:-free)?\.(?:app|dev)$') {
        throw 'Set publicHost to the exact assigned ngrok hostname, without scheme or path.'
    }
    if ($settings.judges -isnot [array] -or $settings.judges.Count -eq 0) {
        throw 'Set judges to a nonempty array of approved OAuth email addresses.'
    }
    foreach ($email in $settings.judges) {
        if ($email -isnot [string] -or $email -cnotmatch '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' -or
            $email -match '@(?:example\.(?:com|net|org|invalid)|.*\.invalid)$') {
            throw 'Replace example addresses with approved judges before using the tunnel.'
        }
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
    $env:SMTP_ENABLED = '0'
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
if ($LASTEXITCODE) { throw 'ngrok configuration check failed.' }

$approved = ($settings.judges | ForEach-Object { "'" + $_ + "'" }) -join ', '
$policy = @{
    on_http_request = @(
        @{actions = @(@{type = 'oauth'; config = @{provider = 'google'}})}
        @{
            expressions = @("!(actions.ngrok.oauth.identity.email in [$approved])")
            actions = @(@{type = 'deny'; config = @{status_code = 403}})
        }
    )
}
if ($Check) {
    Write-Output "OAuth allowlist validated locally for https://$standHost. No tunnel started."
    return
}
$policyPath = Join-Path $state 'stand-ngrok-policy.json'
[IO.File]::WriteAllText($policyPath, ($policy | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
$python = Join-Path $state 'runtime\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Prepare the permanent Python environment first.' }
$deadline = [DateTime]::UtcNow.AddMinutes(3)
$ready = $false
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
do {
    $probe | & $python -I -B - $standHost
    $ready = $LASTEXITCODE -eq 0
    if (-not $ready) { Start-Sleep -Seconds 2 }
} until ($ready -or [DateTime]::UtcNow -ge $deadline)
if (-not $ready) { throw 'The backend did not confirm the public host and blocked admin routes. Task Scheduler can retry.' }

& $ngrok http 'http://127.0.0.1:8002' --url "https://$standHost" --inspect=false --config $agentConfig --traffic-policy-file $policyPath
throw "ngrok exited ($LASTEXITCODE). Task Scheduler can retry."

