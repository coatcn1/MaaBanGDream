param(
    [switch]$Auto,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot

# 本地版本以 update-manifest.json 为准：它只在一次完整应用成功后才被
# 替换，中断的半更新状态不会把版本误报成新版本；找不到时依次回退
# BUILD-INFO.json、interface.json，兼容旧版便携包。
function Get-LocalVersion {
    foreach ($name in @('update-manifest.json', 'BUILD-INFO.json', 'interface.json')) {
        $path = Join-Path $packageRoot $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        try {
            $version = (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json).version
            if ($version) { return [string]$version }
        } catch { }
    }
    return $null
}

# 把 v1.2.3 / 1.2.3 / 1.2.4-dev 归一化成可比较的数值序列。
function ConvertTo-VersionTuple([string]$text) {
    $trimmed = $text.Trim().TrimStart('v', 'V')
    $core = ($trimmed -split '-')[0]
    $parts = @($core -split '\.')
    $result = @()
    foreach ($part in $parts) {
        $numeric = 0
        [void][int]::TryParse($part, [ref]$numeric)
        $result += $numeric
    }
    while ($result.Count -lt 3) { $result += 0 }
    return ,$result
}

function Compare-Version($left, $right) {
    $a = ConvertTo-VersionTuple $left
    $b = ConvertTo-VersionTuple $right
    for ($i = 0; $i -lt 3; $i++) {
        if ($a[$i] -lt $b[$i]) { return -1 }
        if ($a[$i] -gt $b[$i]) { return 1 }
    }
    return 0
}

function Get-LatestRelease {
    # 用 releases/latest 的 HTML 重定向拿 tag，避免 GitHub API 60 次/小时的
    # 未认证限流；离线或超时时返回 null，不阻塞正常启动。
    try {
        $response = Invoke-WebRequest `
            -Uri 'https://github.com/coatcn1/MaaBanGDream/releases/latest' `
            -UseBasicParsing `
            -TimeoutSec 20 `
            -UserAgent 'MaaBanGDream-Updater'
        $uri = $response.BaseResponse.ResponseUri.AbsoluteUri
        if ($uri -match '/releases/tag/(?<tag>[^/?#]+)') {
            return $Matches['tag']
        }
    } catch {
        return $null
    }
    return $null
}

function Test-NewerVersionAvailable([string]$tag) {
    return (Compare-Version $tag $currentVersion) -gt 0
}

function Stop-PackageMfa {
    $processes = @(Get-Process -Name 'MFAAvalonia' -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        try {
            if ($process.Path -and $process.Path.StartsWith($packageRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $process.Id -Force
                Write-Host "已关闭正在运行的 MFA（PID $($process.Id)）。"
            }
        } catch { }
    }
}

$currentVersion = Get-LocalVersion
if (-not $currentVersion) {
    throw '无法从 update-manifest.json / BUILD-INFO.json / interface.json 读取本地版本。'
}
Write-Host "本地版本：$currentVersion"

# 版本升级后安装目录名也跟着变（例如 MaaBanGDream-v1.3.3-win-x64 →
# MaaBanGDream-v1.3.4-win-x64）。目录名带版本号且与清单版本不一致时，
# 在启动 MFA 之前用独立辅助进程改名并重启。改名要求没有进程把该目录
# 当工作目录，所以辅助进程先等启动器退出，再离开目录执行改名。
$folderName = Split-Path -Leaf $packageRoot
$expectedFolder = "MaaBanGDream-v$currentVersion-win-x64"
if ($folderName -match '^MaaBanGDream-v.*-win-x64$' -and $folderName -ne $expectedFolder) {
    $parent = Split-Path -Parent $packageRoot
    $newRoot = Join-Path $parent $expectedFolder
    $helperPath = Join-Path $env:TEMP "maabangdream-rename-$currentVersion.ps1"
    $rootLiteral = $packageRoot.Replace("'", "''")
    $newRootLiteral = $newRoot.Replace("'", "''")
    $parentLiteral = $parent.Replace("'", "''")
    $helper = @"
param()
Set-Location -LiteralPath '$parentLiteral'
foreach (`$attempt in 1..30) {
    if (-not (Test-Path -LiteralPath '$rootLiteral')) { break }
    try {
        Rename-Item -LiteralPath '$rootLiteral' -NewName '$expectedFolder' -ErrorAction Stop
        break
    } catch {
        `"`$(`$_.Exception.GetType().Name): `$(`$_.Exception.Message)`" | Out-File -Append `"`$env:TEMP\maabangdream-rename-trace.txt`" -Encoding utf8
        Start-Sleep -Milliseconds 1000
    }
}
`$launchRoot = if (Test-Path -LiteralPath '$newRootLiteral') { '$newRootLiteral' } else { '$rootLiteral' }
Start-Process -FilePath 'cmd.exe' -WorkingDirectory '$parentLiteral' -ArgumentList '/c','"`$launchRoot\启动 MaaBanGDream.cmd"'
"@
    Set-Content -LiteralPath $helperPath -Value $helper -Encoding utf8
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden `
        -WorkingDirectory $parent `
        -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $helperPath
        )
    # 退出码 2 通知启动器：目录正在改名并由辅助进程重启，不要从旧路径启动。
    exit 2
}

$latestTag = Get-LatestRelease
if (-not $latestTag) {
    Write-Host '无法连接 GitHub（离线或网络受限），跳过更新检查。'
    exit 0
}
$latestVersion = $latestTag.TrimStart('v', 'V')
Write-Host "最新版本：$latestVersion"

if (-not (Test-NewerVersionAvailable $latestTag)) {
    Write-Host '当前已是最新版本，无需更新。'
    exit 0
}

if ($CheckOnly) {
    Write-Host "发现新版本 $latestVersion（当前 $currentVersion）。"
    exit 0
}

if (-not $Auto) {
    $answer = Read-Host "发现新版本 $latestVersion，是否下载并更新？(y/N)"
    if ($answer -notmatch '^(y|Y)') {
        Write-Host '已取消更新。'
        exit 0
    }
}

$assetName = "MaaBanGDream-v$latestVersion-win-x64.zip"
$assetUrl = "https://github.com/coatcn1/MaaBanGDream/releases/download/$latestTag/$assetName"
$shaUrl = "$assetUrl.sha256"
$tempRoot = Join-Path $env:TEMP "maabangdream-update-$latestVersion"
$zipPath = Join-Path $tempRoot $assetName
$partPath = "$zipPath.part"
$staging = Join-Path $tempRoot 'staging'
$stagingRoot = $staging

New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$success = $false
try {
    Write-Host "读取 $assetName 的 SHA256 校验值 ..."
    $shaContent = (Invoke-WebRequest `
        -Uri $shaUrl `
        -UseBasicParsing `
        -TimeoutSec 20 `
        -UserAgent 'MaaBanGDream-Updater').Content
    # GitHub 把 .sha256 按 application/octet-stream 返回，PowerShell 5.1
    # 会把响应体解析成字节数组；这里统一还原成文本再匹配哈希。
    $shaText = if ($shaContent -is [byte[]]) {
        [System.Text.Encoding]::UTF8.GetString($shaContent)
    } else {
        [string]$shaContent
    }
    if ($shaText -notmatch '\b([0-9a-fA-F]{64})\b') {
        throw '发布包缺少可解析的 SHA256 校验值，已中止更新。'
    }
    $expectedSha = $Matches[1].ToLowerInvariant()

    function Invoke-Download {
        param([string]$Url, [string]$Output)
        # curl -C - 从已有 .part 的末尾断点续传；Windows 10/11 自带 curl。
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe -L --fail --retry 5 --retry-all-errors --retry-delay 3 -C - -o $Output $Url
            if ($LASTEXITCODE -ne 0) {
                throw "curl 下载失败（退出码 $LASTEXITCODE），已中止更新。"
            }
        } else {
            $client = New-Object System.Net.WebClient
            $client.Headers.Add('User-Agent', 'MaaBanGDream-Updater')
            $client.DownloadFile($Url, $Output)
        }
    }

    $attempts = 0
    while ($true) {
        Write-Host "下载 $assetName（支持断点续传）..."
        Invoke-Download -Url $assetUrl -Output $partPath
        $actualSha = (Get-FileHash -LiteralPath $partPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha -eq $expectedSha) { break }
        if ($attempts -ge 1) {
            throw '下载文件 SHA256 校验失败，已中止更新。'
        }
        Write-Host 'SHA256 校验失败：删除不完整文件后重新完整下载。'
        Remove-Item -LiteralPath $partPath -Force -ErrorAction SilentlyContinue
        $attempts++
    }
    Move-Item -LiteralPath $partPath -Destination $zipPath -Force

    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $staging
    $inner = Get-ChildItem -LiteralPath $staging -Directory | Select-Object -First 1
    if ($inner) { $staging = $inner.FullName }
    if (-not (Test-Path -LiteralPath (Join-Path $staging 'MFAAvalonia.exe'))) {
        throw '下载包结构异常：缺少 MFAAvalonia.exe，已中止更新。'
    }

    Stop-PackageMfa

    # 只覆盖包内容，保留用户数据目录（config/profiles/debug/logs/备份），
    # 不删除旧包里的其他残留文件。
    $preserve = @('config', 'profiles', 'debug', 'logs', '.maabangdream-backup')
    Get-ChildItem -LiteralPath $staging -Force | ForEach-Object {
        if ($_.Name -in $preserve) { return }
        Copy-Item -LiteralPath $_.FullName -Destination $packageRoot -Recurse -Force
    }
    Write-Host "更新完成：$currentVersion -> $latestVersion"
    $success = $true

    $launcher = Join-Path $packageRoot '启动 MaaBanGDream.cmd'
    if ($Auto -and (Test-Path -LiteralPath $launcher)) {
        Start-Process -FilePath $launcher
    }
} finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($success -and (Test-Path -LiteralPath $tempRoot)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    # 失败时保留 .part：下次运行从断点继续下载，而不是从头再来。
}
