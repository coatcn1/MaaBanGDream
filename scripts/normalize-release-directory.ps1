param([switch]$Inline)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot

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

$currentVersion = Get-LocalVersion
if (-not $currentVersion) {
    throw 'Unable to read the local version from the release metadata.'
}

$folderName = Split-Path -Leaf $packageRoot
$expectedFolder = "MaaBanGDream-v$currentVersion-win-x64"
if ($folderName -notmatch '^MaaBanGDream-v.*-win-x64$' -or $folderName -eq $expectedFolder) {
    if ($Inline) { return $packageRoot }
    exit 0
}

$parent = Split-Path -Parent $packageRoot
$newRoot = Join-Path $parent $expectedFolder
if ($Inline) {
    # 更新器与 PowerShell 的工作目录都在安装目录外，改名后继续准备新路径。
    Set-Location -LiteralPath $parent
    foreach ($attempt in 1..30) {
        try {
            Rename-Item -LiteralPath $packageRoot -NewName $expectedFolder -ErrorAction Stop
            return $newRoot
        } catch {
            if ($attempt -eq 30) {
                Write-Warning "Unable to rename the install directory: $($_.Exception.Message)"
                return $packageRoot
            }
            Start-Sleep -Milliseconds 1000
        }
    }
}
$helperPath = Join-Path $env:TEMP "maabangdream-rename-$currentVersion.ps1"
$rootLiteral = $packageRoot.Replace("'", "''")
$newRootLiteral = $newRoot.Replace("'", "''")
$parentLiteral = $parent.Replace("'", "''")
$launcherName = (-join [char[]](0x542F, 0x52A8)) + ' MaaBanGDream.cmd'
$launcherLiteral = $launcherName.Replace("'", "''")
$helper = @"
param()
Set-Location -LiteralPath '$parentLiteral'
foreach (`$attempt in 1..30) {
    if (-not (Test-Path -LiteralPath '$rootLiteral')) { break }
    try {
        Rename-Item -LiteralPath '$rootLiteral' -NewName '$expectedFolder' -ErrorAction Stop
        break
    } catch {
        Start-Sleep -Milliseconds 1000
    }
}
`$launchRoot = if (Test-Path -LiteralPath '$newRootLiteral') { '$newRootLiteral' } else { '$rootLiteral' }
`$restart = Join-Path `$launchRoot 'scripts\restart-release.ps1'
if (Test-Path -LiteralPath `$restart) {
    & `$restart
} else {
    `$launcher = Join-Path `$launchRoot '$launcherLiteral'
    Start-Process -FilePath `$launcher -WorkingDirectory `$launchRoot -WindowStyle Hidden
}
"@
[System.IO.File]::WriteAllText(
    $helperPath,
    $helper,
    [System.Text.UTF8Encoding]::new($true)
)
Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden `
    -WorkingDirectory $parent `
    -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $helperPath
    )

# 退出码 2 通知批处理启动器：目录将由辅助进程改名并重新启动。
exit 2
