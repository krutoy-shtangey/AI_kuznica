[CmdletBinding()]
param([string]$Python = 'py', [string]$Npm = 'npm.cmd')
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $root
try {
    $pythonArgs = @()
    if ($Python -eq 'py') { $pythonArgs = @('-3.12') }
    'import sys; assert sys.version_info[:2] == (3,12), "Python 3.12 required"' | & $Python @pythonArgs -
    if ($LASTEXITCODE) { throw 'Python 3.12 not available. Supply -Python with its full path.' }
    if (-not (Test-Path '.venv\Scripts\python.exe')) {
        & $Python @pythonArgs -m venv .venv
        if ($LASTEXITCODE) { throw 'venv creation failed.' }
    }
    & '.\.venv\Scripts\python.exe' -m ensurepip --upgrade
    if ($LASTEXITCODE) { throw 'pip bootstrap failed.' }
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
    if ($LASTEXITCODE) { throw 'Backend dependency installation failed.' }
    & '.\.venv\Scripts\python.exe' -m pip check
    if ($LASTEXITCODE) { throw 'Backend dependencies are inconsistent.' }
    & $Npm --version
    if ($LASTEXITCODE) { throw 'Install Node.js with npm first.' }
    'const [a,b]=process.versions.node.split(".").map(Number);if(a<22||(a===22&&b<12))process.exit(1)' | node -
    if ($LASTEXITCODE) { throw 'Node.js 22.12 or newer required.' }
    & $Npm --prefix frontend ci --no-audit --no-fund
    if ($LASTEXITCODE) { throw 'npm ci failed.' }
    & $Npm --prefix frontend run build
    if ($LASTEXITCODE) { throw 'Frontend production build failed.' }
    foreach ($name in @('storage','documents','index','models','run')) {
        New-Item -ItemType Directory -Force -Path (Join-Path 'storage\windows' $name) | Out-Null
    }
    Write-Output 'Setup complete. No generative model or retrieval weights were downloaded.'
} finally { Pop-Location }
