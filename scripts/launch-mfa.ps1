param(
    [string]$MfaRoot,
    [string]$CondaRoot,
    [string]$EnvironmentName = 'maabangdream'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $MfaRoot) {
    $MfaRoot = Join-Path $workspaceRoot '.tools\MFAAvalonia-profile-v3'
}
if (-not $CondaRoot) {
    $CondaRoot = Join-Path $workspaceRoot '.tools\Miniconda3'
}

$mfaExe = Join-Path $MfaRoot 'MFAAvalonia.exe'
$sourceInterface = Join-Path $projectRoot 'interface.json'
$sourceResource = Join-Path $projectRoot 'resource'
$deployedInterface = Join-Path $MfaRoot 'interface.json'
$deployedProfileManager = Join-Path $MfaRoot 'profile-manager.json'
$deployedResource = Join-Path $MfaRoot 'resource\resource'
$python = Join-Path $CondaRoot "envs\$EnvironmentName\python.exe"
$agent = Join-Path $projectRoot 'agent\server.py'
$profileManager = Join-Path $projectRoot 'agent\profile_manager.py'
$profileDirectory = Join-Path $projectRoot 'profiles'
$recordingDirectory = Join-Path $projectRoot 'debug\recordings'
$captureDirectory = Join-Path $projectRoot 'screencap'
$maafwDebugDirectory = Join-Path $MfaRoot 'debug'
$mfaLogDirectory = Join-Path $MfaRoot 'logs'
$instanceConfigDirectory = Join-Path $MfaRoot 'config\instances'
$mfaStopStatusPatch = Join-Path $PSScriptRoot 'patch-mfa-stop-status.ps1'

foreach ($required in ($mfaExe, $sourceInterface, $sourceResource, $python, $agent, $profileManager, $mfaStopStatusPatch)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required MaaBanGDream runtime path is missing: $required"
    }
}

Get-Process MFAAvalonia -ErrorAction SilentlyContinue | Stop-Process

$dotnetRuntimes = & dotnet --list-runtimes 2>$null
if (-not ($dotnetRuntimes -match '^Microsoft\.NETCore\.App 10\.')) {
    throw 'MFAAvalonia 2.12.0 requires .NET Runtime 10. Install it with: winget install --id Microsoft.DotNet.Runtime.10 --exact'
}

# MFAAvalonia reads interface.json and resources beside its executable. Keep
# source code in the repository, but always refresh this ignored deployment copy.
New-Item -ItemType Directory -Force -Path $deployedResource | Out-Null
foreach ($runtimeDirectory in ($profileDirectory, $recordingDirectory, $captureDirectory, $maafwDebugDirectory, $mfaLogDirectory)) {
    New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
}
Copy-Item -Path (Join-Path $sourceResource '*') -Destination $deployedResource -Recurse -Force

$interface = Get-Content -LiteralPath $sourceInterface -Raw -Encoding utf8 | ConvertFrom-Json
$interface.resource[0].path = @('./resource/resource')
$interface.agent.child_exec = $python.Replace('\', '/')
$interface.agent.child_args = @($agent.Replace('\', '/'))
$interface | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $deployedInterface -Encoding utf8

# The custom MFA settings page reads this ignored, machine-local sidecar. It is
# deliberately generated here so neither usernames nor repository paths enter Git.
$profileManagerConfig = [ordered]@{
    version = 1
    child_exec = $python
    child_args = @($profileManager)
    environment = [ordered]@{
        resolution = @(1280, 720)
        dpi = 240
        game_fps = 60
        render_quality = 'standard'
        note_speed = 2.0
    }
    artifact_paths = [ordered]@{
        profiles = $profileDirectory
        realtime_recordings = $recordingDirectory
        result_captures = $captureDirectory
        maafw_debug = $maafwDebugDirectory
        mfa_logs = $mfaLogDirectory
    }
}
$profileManagerConfig | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $deployedProfileManager -Encoding utf8

# MFA defaults to continuing the queue when a MaaFramework task fails. That
# converts Tasker.Task.Failed into a misleading "all tasks completed" message.
# Preserve the framework result so MFA uses its native failure log/toast path.
if (Test-Path -LiteralPath $instanceConfigDirectory) {
    Get-ChildItem -LiteralPath $instanceConfigDirectory -Filter '*.json' -File | ForEach-Object {
        $instanceConfig = Get-Content -LiteralPath $_.FullName -Raw -Encoding utf8 | ConvertFrom-Json
        $instanceConfig | Add-Member -NotePropertyName 'ContinueRunningWhenError' -NotePropertyValue $false -Force
        # MaaTouch's injected events are silently ignored by the game's live
        # screen on LDPlayer 9 after emulator restarts, while Minitouch stays
        # reliable.  The ADB device probe resets InputMethods on every MFA
        # start, so pin the input mode here (the UI setting overrides it).
        $instanceConfig | Add-Member -NotePropertyName 'AdbControlInputType' -NotePropertyValue 'MinitouchAndAdbKey' -Force
        $instanceConfig | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $_.FullName -Encoding utf8
    }
}

# MFAAvalonia 2.12.0 checks a failed Maa job before its cancellation token.
# With strict failure propagation enabled, that race reports a user stop as a
# failure. Deploy the pinned one-line upstream-compatible status fix once.
& $mfaStopStatusPatch -MfaRoot $MfaRoot

# Every Agent child launched by this MFA process inherits the same session id.
# The ALAS conflict guard uses it to allow cleanup only after a first warning
# in this exact MFA session; restarting MFA invalidates that authorization.
$env:MAABANGDREAM_MFA_SESSION_ID = [Guid]::NewGuid().ToString('N')
try {
    Start-Process -FilePath $mfaExe -WorkingDirectory $MfaRoot
}
finally {
    Remove-Item Env:MAABANGDREAM_MFA_SESSION_ID -ErrorAction SilentlyContinue
}

Write-Host "MFAAvalonia started with MaaBanGDream $($interface.version)"
Write-Host "Project: $projectRoot"
Write-Host "Deployment: $MfaRoot"
Write-Host "Conda environment: $EnvironmentName ($python)"
