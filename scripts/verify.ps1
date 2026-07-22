$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment missing. Run scripts/setup.ps1 first."
}

& $python -m compileall -q (Join-Path $projectRoot 'agent') (Join-Path $projectRoot 'scripts')
if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed.' }

& $python (Join-Path $projectRoot 'scripts\check_runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Runtime compatibility check failed.' }

& $python -m pytest (Join-Path $projectRoot 'tests') -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }

git -C $projectRoot diff --check
if ($LASTEXITCODE -ne 0) { throw 'Git diff check failed.' }
