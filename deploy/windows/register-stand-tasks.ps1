[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runner = Join-Path $PSScriptRoot 'stand.ps1'
& $runner Backend -Check

$names = @{
    Backend = 'Reglament AI Backend'
    Tunnel = 'Reglament AI Tunnel'
}
foreach ($name in $names.Values) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        throw "Task '$name' already exists. Review it in Task Scheduler; it was not replaced."
    }
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$credential = Get-Credential -UserName $user -Message 'Enter the Windows account password (not the PIN) for the two boot tasks.'
if (-not $credential -or $credential.UserName -ine $user) {
    throw 'Use the current Windows account that owns the files and ngrok configuration.'
}
$trigger = New-ScheduledTaskTrigger -AtStartup
$windowsPowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
foreach ($component in @('Backend', 'Tunnel')) {
    $settingsArgs = @{
        StartWhenAvailable = $true
        MultipleInstances = 'IgnoreNew'
        ExecutionTimeLimit = [TimeSpan]::Zero
        RestartCount = 60
        RestartInterval = New-TimeSpan -Minutes 1
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
        Disable = $component -eq 'Tunnel'
    }
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $runner +
        '" -Component ' + $component
    $taskArgs = @{
        TaskName = $names[$component]
        Action = New-ScheduledTaskAction -Execute $windowsPowerShell -Argument $arguments -WorkingDirectory $root
        Trigger = $trigger
        Settings = New-ScheduledTaskSettingsSet @settingsArgs
        User = $user
        Password = $credential.GetNetworkCredential().Password
        RunLevel = 'Limited'
    }
    try {
        Register-ScheduledTask @taskArgs | Out-Null
    } finally {
        $taskArgs.Remove('Password')
    }
    Write-Output "Registered $($names[$component])."
}
Write-Output 'The tunnel task is disabled. Start the backend task only after stopping the manually running backend.'

