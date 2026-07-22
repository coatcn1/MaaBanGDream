param(
    [string]$CondaRoot,
    [string]$EnvironmentName = 'maabangdream'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $CondaRoot) {
    $CondaRoot = Join-Path $workspaceRoot '.tools\Miniconda3'
}

$conda = Join-Path $CondaRoot 'Scripts\conda.exe'
$environmentRoot = Join-Path $CondaRoot "envs\$EnvironmentName"
$python = Join-Path $environmentRoot 'python.exe'

if (-not (Test-Path -LiteralPath $conda)) {
    throw "Miniconda missing: $conda. See README section 'Conda 环境固定配置'."
}

if (Test-Path -LiteralPath $python) {
    & $conda install --name $EnvironmentName --override-channels --channel conda-forge python=3.12 pip --yes
} else {
    & $conda create --name $EnvironmentName --override-channels --channel conda-forge python=3.12 pip --yes
}
if ($LASTEXITCODE -ne 0) { throw 'Conda environment creation/update failed.' }

& $python -m pip install -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }

& $python -c "import maa, cv2; print('environment=$EnvironmentName'); print('python=ready'); print('MaaFramework Python binding ready')"
if ($LASTEXITCODE -ne 0) { throw 'Conda environment validation failed.' }

Write-Host "Conda root: $CondaRoot"
Write-Host "Environment: $EnvironmentName"
Write-Host "Python: $python"
