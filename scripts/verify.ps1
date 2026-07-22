$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$condaRoot = Join-Path $workspaceRoot '.tools\Miniconda3'
$environmentName = 'maabangdream'
$python = Join-Path $condaRoot "envs\$environmentName\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Conda environment '$environmentName' missing. Run scripts/setup.ps1 first."
}

& $python -m compileall -q (Join-Path $projectRoot 'agent') (Join-Path $projectRoot 'scripts')
if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed.' }

& $python (Join-Path $projectRoot 'scripts\check_runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Runtime compatibility check failed.' }

$testTemp = Join-Path $projectRoot ".local\pytest-$PID"
New-Item -ItemType Directory -Force -Path $testTemp | Out-Null
& $python -m pytest (Join-Path $projectRoot 'tests') -q -p no:cacheprovider --basetemp $testTemp
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }

# Codex and the interactive Windows account can own the shared worktree under
# different local SIDs. Scope the trust exception to this one invocation; do
# not mutate the user's global Git configuration.
git -c "safe.directory=$projectRoot" -C $projectRoot diff --check
if ($LASTEXITCODE -ne 0) { throw 'Git diff check failed.' }
