$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment missing. Run scripts/setup.ps1 first."
}

& $python -m compileall -q (Join-Path $projectRoot 'agent')
& $python -m pytest (Join-Path $projectRoot 'tests') -q
git -C $projectRoot diff --check
