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
$deployedResource = Join-Path $MfaRoot 'resource\resource'
$python = Join-Path $CondaRoot "envs\$EnvironmentName\python.exe"
$agent = Join-Path $projectRoot 'agent\server.py'

foreach ($required in ($mfaExe, $sourceInterface, $sourceResource, $python, $agent)) {
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

Get-Process MFAAvalonia -ErrorAction SilentlyContinue | Stop-Process
Start-Process -FilePath $mfaExe -WorkingDirectory $MfaRoot

Write-Host "MFAAvalonia started with MaaBanGDream $($interface.version)"
Write-Host "Project: $projectRoot"
Write-Host "Deployment: $MfaRoot"
Write-Host "Conda environment: $EnvironmentName ($python)"
