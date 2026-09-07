param(
    [switch]$Auto,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot

# 本地版本以 interface.json 为准；找不到时回退 BUILD-INFO.json。
function Get-LocalVersion {
    $interface = Join-Path $packageRoot 'interface.json'
    if (Test-Path -LiteralPath $interface) {
        try {
            $version = (Get-Content -LiteralPath $interface -Raw | ConvertFrom-Json).version
            if ($version) { return [string]$version }
        } catch { }
    }
    $buildInfo = Join-Path $packageRoot 'BUILD-INFO.json'
    if (Test-Path -LiteralPath $buildInfo) {
        try {
            $version = (Get-Content -LiteralPath $buildInfo -Raw | ConvertFrom-Json).version
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
    throw '无法从 interface.json / BUILD-INFO.json 读取本地版本。'
}
Write-Host "本地版本：$currentVersion"

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
$tempRoot = Join-Path $env:TEMP "maabangdream-update-$latestVersion"
$zipPath = Join-Path $tempRoot $assetName
$staging = Join-Path $tempRoot 'staging'

New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
try {
    Write-Host "下载 $assetName ..."
    $client = New-Object System.Net.WebClient
    $client.Headers.Add('User-Agent', 'MaaBanGDream-Updater')
    $client.DownloadFile($assetUrl, $zipPath)

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

    $launcher = Join-Path $packageRoot '启动 MaaBanGDream.cmd'
    if ($Auto -and (Test-Path -LiteralPath $launcher)) {
        Start-Process -FilePath $launcher
    }
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
