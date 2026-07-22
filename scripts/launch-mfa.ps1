param(
    [string]$MfaRoot,
    [string]$CondaRoot,
    [string]$EnvironmentName = 'maabangdream'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $MfaRoot) {
    $MfaRoot = Join-Path $workspaceRoot '.tools\MFAAvalonia'
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

foreach ($required in ($mfaExe, $sourceInterface, $sourceResource, $python, $agent, $profileManager)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required MaaBanGDream runtime path is missing: $required"
    }
}

$dotnetRuntimes = & dotnet --list-runtimes 2>$null
if (-not ($dotnetRuntimes -match '^Microsoft\.NETCore\.App 10\.')) {
    throw 'MFAAvalonia 2.12.0 requires .NET Runtime 10. Install it with: winget install --id Microsoft.DotNet.Runtime.10 --exact'
}

# MFAAvalonia reads interface.json and resources beside its executable. Keep
# source code in the repository, but always refresh this ignored deployment copy.
New-Item -ItemType Directory -Force -Path $deployedResource | Out-Null
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
}
$profileManagerConfig | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $deployedProfileManager -Encoding utf8

Get-Process MFAAvalonia -ErrorAction SilentlyContinue | Stop-Process
Start-Process -FilePath $mfaExe -WorkingDirectory $MfaRoot

Write-Host "MFAAvalonia started with MaaBanGDream $($interface.version)"
Write-Host "Project: $projectRoot"
Write-Host "Deployment: $MfaRoot"
Write-Host "Conda environment: $EnvironmentName ($python)"
