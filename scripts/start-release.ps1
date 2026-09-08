param(
    [switch]$NoLaunch,
    [switch]$OrderedStartupTrial
)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
$runtimeDirectory = Join-Path $packageRoot 'runtime'
$runtimeArchive = Join-Path $runtimeDirectory 'maabangdream-python.zip'
$pythonRoot = Join-Path $runtimeDirectory 'python'
$python = Join-Path $pythonRoot 'python.exe'
$runtimeReady = Join-Path $pythonRoot '.maabangdream-ready'
$mfa = Join-Path $packageRoot 'MFAAvalonia.exe'
$interfaceTemplate = Join-Path $packageRoot 'interface.template.json'
$interfacePath = Join-Path $packageRoot 'interface.json'
$profileManagerPath = Join-Path $packageRoot 'profile-manager.json'
$agent = Join-Path $packageRoot 'agent\server.py'
$profileManager = Join-Path $packageRoot 'agent\profile_manager.py'
$runtimeCheck = Join-Path $PSScriptRoot 'check_runtime.py'
$chartSync = Join-Path $PSScriptRoot 'sync_bestdori_catalog.py'
$chartRoot = Join-Path $packageRoot 'resource\charts'
$chartManifest = Join-Path $chartRoot 'manifest.json'

foreach ($required in @(
    $mfa,
    $interfaceTemplate,
    $agent,
    $profileManager,
    $runtimeCheck,
    $chartSync,
    $chartManifest
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Release package is incomplete: $required"
    }
}

# 运行库未就绪且随包归档也不在（例如用户删掉了运行库又下载了更新包）时，
# 给出明确指引而不是让 Expand-Archive 报难懂的路径错误。
$runtimeMissing = (
    -not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $runtimeReady -PathType Leaf)
)
if (
    $runtimeMissing -and
    -not (Test-Path -LiteralPath $runtimeArchive -PathType Leaf)
) {
    throw (
        'Bundled Python runtime is missing; ' +
        'please re-download the full package (MaaBanGDream-v*-win-x64.zip).'
    )
}

if (
    $runtimeMissing
) {
    $partialRoot = Join-Path $runtimeDirectory 'python.partial'
    foreach ($oldRoot in @($partialRoot, $pythonRoot)) {
        if (Test-Path -LiteralPath $oldRoot) {
            [System.IO.Directory]::Delete($oldRoot, $true)
        }
    }
    Write-Host 'Preparing bundled MaaBanGDream Python runtime ...'
    Expand-Archive `
        -LiteralPath $runtimeArchive `
        -DestinationPath $partialRoot `
        -Force
    $partialPython = Join-Path $partialRoot 'python.exe'
    $condaUnpack = Join-Path $partialRoot 'Scripts\conda-unpack.exe'
    foreach ($requiredRuntimeFile in @($partialPython, $condaUnpack)) {
        if (-not (Test-Path -LiteralPath $requiredRuntimeFile -PathType Leaf)) {
            throw "Bundled Python runtime is incomplete: $requiredRuntimeFile"
        }
    }
    Move-Item -LiteralPath $partialRoot -Destination $pythonRoot
    & (Join-Path $pythonRoot 'Scripts\conda-unpack.exe')
    if ($LASTEXITCODE -ne 0) {
        throw "Bundled Python path repair failed: $LASTEXITCODE"
    }
    [System.IO.File]::WriteAllText(
        $runtimeReady,
        "MaaBanGDream portable Python runtime ready`r`n",
        [System.Text.UTF8Encoding]::new($false)
    )
}

# 首次解压完成后删除随包的 conda-pack 归档：运行库已经落到 runtime/python，
# 归档留着只会白占约 350MB 磁盘，而且会让后续更新误以为需要重新下载它。
if (Test-Path -LiteralPath $runtimeArchive -PathType Leaf) {
    Remove-Item -LiteralPath $runtimeArchive -Force
}

& $python $runtimeCheck --portable --mfa-root $packageRoot
if ($LASTEXITCODE -ne 0) {
    throw "Bundled runtime compatibility check failed: $LASTEXITCODE"
}

$profiles = Join-Path $packageRoot 'profiles'
$recordings = Join-Path $packageRoot 'debug\recordings'
$captures = Join-Path $packageRoot 'screencap'
$maafwDebug = Join-Path $packageRoot 'debug\maafw'
$mfaLogs = Join-Path $packageRoot 'logs'
$instanceConfigDirectory = Join-Path $packageRoot 'config\instances'
foreach ($directory in @(
    $profiles,
    $recordings,
    $captures,
    $maafwDebug,
    $mfaLogs
)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

$interface = Get-Content `
    -LiteralPath $interfaceTemplate `
    -Raw `
    -Encoding utf8 | ConvertFrom-Json
$interface.resource[0].path = @('./resource')
$interface.agent.child_exec = $python.Replace('\', '/')
$interface.agent.child_args = @($agent.Replace('\', '/'))
$interfaceJson = $interface | ConvertTo-Json -Depth 100
[System.IO.File]::WriteAllText(
    $interfacePath,
    $interfaceJson,
    [System.Text.UTF8Encoding]::new($false)
)

$profileManagerConfig = [ordered]@{
    version = 1
    child_exec = $python
    child_args = @($profileManager)
    chart_sync = [ordered]@{
        child_exec = $python
        child_args = @(
            $chartSync
            '--output-root'
            $chartRoot
            '--jacket-server'
            'cn'
            '--jacket-fallback-server'
            'jp,en'
            '--prune-other-difficulties'
        )
        working_directory = $packageRoot
        manifest_path = $chartManifest
    }
    environment = [ordered]@{
        resolution = @(1280, 720)
        dpi = 240
        game_fps = 60
        render_quality = 'standard'
        note_speed = 2.0
    }
    artifact_paths = [ordered]@{
        profiles = $profiles
        realtime_recordings = $recordings
        result_captures = $captures
        maafw_debug = $maafwDebug
        mfa_logs = $mfaLogs
    }
}
$profileManagerJson = $profileManagerConfig | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText(
    $profileManagerPath,
    $profileManagerJson,
    [System.Text.UTF8Encoding]::new($false)
)

if (Test-Path -LiteralPath $instanceConfigDirectory) {
    Get-ChildItem `
        -LiteralPath $instanceConfigDirectory `
        -Filter '*.json' `
        -File | ForEach-Object {
        $instance = Get-Content `
            -LiteralPath $_.FullName `
            -Raw `
            -Encoding utf8 | ConvertFrom-Json
        $instance | Add-Member `
            -NotePropertyName 'ContinueRunningWhenError' `
            -NotePropertyValue $false `
            -Force
        $instance | Add-Member `
            -NotePropertyName 'AdbControlInputType' `
            -NotePropertyValue 'MinitouchAndAdbKey' `
            -Force
        $instanceJson = $instance | ConvertTo-Json -Depth 100
        [System.IO.File]::WriteAllText(
            $_.FullName,
            $instanceJson,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
}

if ($NoLaunch) {
    Write-Host "Release configuration ready: $packageRoot"
    exit 0
}

$env:MAABANGDREAM_MFA_SESSION_ID = [Guid]::NewGuid().ToString('N')
$env:MAABANGDREAM_MFA_ROOT = $packageRoot
if ($OrderedStartupTrial) {
    # 与开发启动脚本一致：候选行为仅本次进程显式启用，普通启动保持默认。
    $env:MAABANGDREAM_ORDERED_STARTUP = '1'
}
try {
    # 浏览器下载的压缩包会给 MFAAvalonia.exe 打上 Zone.Identifier
    # 标记，ShellExecute 启动会弹 SmartScreen 并被取消；先解除该标记。
    Unblock-File -LiteralPath $mfa -ErrorAction SilentlyContinue
    Start-Process -FilePath $mfa -WorkingDirectory $packageRoot
}
finally {
    Remove-Item Env:MAABANGDREAM_MFA_SESSION_ID -ErrorAction SilentlyContinue
    Remove-Item Env:MAABANGDREAM_MFA_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:MAABANGDREAM_ORDERED_STARTUP -ErrorAction SilentlyContinue
}
Write-Host "MaaBanGDream started: $packageRoot"
Write-Host "Ordered startup trial: $([bool]$OrderedStartupTrial)"
