param(
    [Parameter(Mandatory = $true)]
    [string]$MfaRoot,
    [string]$SourceRoot
)

$ErrorActionPreference = 'Stop'
$MfaRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($MfaRoot)
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$customBranch = 'feature/performance-visual-settings'
$customizationCommit = 'd7b381b2fa6a09e140d925fb1504bac19ca1f921'
$patch = Join-Path $projectRoot 'patches\mfaavalonia-v2.12.0-stop-status.patch'
$deployedAssembly = Join-Path $MfaRoot 'MFAAvalonia.Core.dll'
$deployedExecutable = Join-Path $MfaRoot 'MFAAvalonia.exe'
if (Test-Path -LiteralPath (Join-Path $MfaRoot 'MaaBanGDream.exe')) {
    $deployedExecutable = Join-Path $MfaRoot 'MaaBanGDream.exe'
}
$marker = Join-Path $MfaRoot '.maabangdream-mfa-stop-status.json'
$backupDirectory = Join-Path $MfaRoot '.maabangdream-backup'
$hostFiles = @('MaaBanGDream.exe', 'MaaBanGDream.dll', 'MaaBanGDream.deps.json', 'MaaBanGDream.runtimeconfig.json')
$dependencyNames = @('System.Security.Cryptography.Pkcs.dll', 'Tmds.DBus.Protocol.dll')

function Assert-MfaDeploymentPath {
    param([string]$Path, [string]$AllowedRoot = $MfaRoot)
    $absolute = [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path))
    $allowed = [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($AllowedRoot))
    $prefix = $allowed.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $absolute.Equals($allowed, [StringComparison]::OrdinalIgnoreCase) -and
        -not $absolute.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "MFA deployment path escapes its allowed root: $Path"
    }
    # 字符串归一化不能识别 junction/symlink；逐层查到卷根，连缺失叶的
    # 现存祖先和根目录本身也必须检查，不能沿重解析点写入别处。
    $component = $absolute
    while ($component) {
        $item = $null
        try { $item = Get-Item -LiteralPath $component -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { }
        if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "MFA deployment refuses a reparse point: $component"
        }
        $parent = [IO.Directory]::GetParent($component)
        $component = if ($null -eq $parent) { $null } else { $parent.FullName }
    }
}

function Get-MfaCheckedTree {
    param([string]$Directory)
    Assert-MfaDeploymentPath -Path $Directory
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return }
    $pending = [Collections.Generic.Queue[string]]::new()
    $pending.Enqueue($Directory)
    while ($pending.Count -gt 0) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Dequeue() -Force) {
            # 在枚举下一层之前拒绝目录联接；禁止用 -Recurse 先进入外部目录。
            Assert-MfaDeploymentPath -Path $item.FullName
            if ($item.PSIsContainer) { $pending.Enqueue($item.FullName) }
            $item
        }
    }
}

function Get-MfaDependencyTargetPaths {
    # 根依赖是构建输出的唯一来源；旧 runtime 子目录也可能被宿主解析，
    # 因而只枚举目标目录中的现存 PKCS 副本，不能拿构建树旧子目录做来源。
    $paths = @($dependencyNames)
    $runtimeAssets = Join-Path $MfaRoot 'runtimes'
    if (Test-Path -LiteralPath $runtimeAssets -PathType Container) {
        $rootPrefix = [IO.Path]::GetFullPath($MfaRoot).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        $paths += @(Get-MfaCheckedTree -Directory $runtimeAssets |
            Where-Object { -not $_.PSIsContainer -and $_.Name -eq 'System.Security.Cryptography.Pkcs.dll' } |
            ForEach-Object { $_.FullName.Substring($rootPrefix.Length) })
    }
    return @($paths | Sort-Object -Unique)
}

function Test-MfaFileManifest {
    param($Manifest, [string[]]$Paths)
    if ($null -eq $Manifest -or @($Manifest.PSObject.Properties).Count -ne $Paths.Count) {
        return $false
    }
    foreach ($relativePath in $Paths) {
        $entry = $Manifest.PSObject.Properties[$relativePath]
        $target = Join-Path $MfaRoot $relativePath
        Assert-MfaDeploymentPath -Path $target
        if ($null -eq $entry -or -not (Test-Path -LiteralPath $target -PathType Leaf)) { return $false }
        if ($entry.Value -ne (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash) { return $false }
    }
    return $true
}

function Backup-MfaDeploymentFile {
    param([string]$Path, [string]$RelativePath)
    Assert-MfaDeploymentPath -Path $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $oldHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    $backupPath = Join-Path $backupDirectory ("deployment\$RelativePath.$oldHash.bak")
    Assert-MfaDeploymentPath -Path $backupPath -AllowedRoot $backupDirectory
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backupPath) | Out-Null
    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
        Assert-MfaDeploymentPath -Path $backupPath -AllowedRoot $backupDirectory
        Copy-Item -LiteralPath $Path -Destination $backupPath
    }
    if ((Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash -ne $oldHash) {
        throw "MFA deployment backup hash mismatch: $RelativePath"
    }
}

function Assert-MfaCopiedFile {
    param([string]$Source, [string]$Target)
    Assert-MfaDeploymentPath -Path $Target
    if ((Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash) {
        throw "MFA deployment copy hash mismatch: $Target"
    }
}

# 所有既存目标路径必须在构建、备份或复制之前一次检查；实际写入前仍重验。
foreach ($path in @($MfaRoot, $backupDirectory, $marker, (Join-Path $MfaRoot 'runtimes'),
                    $deployedAssembly, $deployedExecutable,
                    (Join-Path $MfaRoot 'ColorTextBlock.Avalonia.dll'), (Join-Path $MfaRoot 'MFAUpdater.exe'))) {
    Assert-MfaDeploymentPath -Path $path
}
foreach ($name in @($hostFiles) + @($dependencyNames) + @('MFAAvalonia.exe', 'MFAAvalonia.dll', 'MFAAvalonia.deps.json', 'MFAAvalonia.runtimeconfig.json')) {
    Assert-MfaDeploymentPath -Path (Join-Path $MfaRoot $name)
}
Get-MfaCheckedTree -Directory $backupDirectory | Out-Null
$dependencyTargetPaths = @(Get-MfaDependencyTargetPaths)

if (-not $SourceRoot) {
    $SourceRoot = Join-Path $workspaceRoot 'MFAAvalonia'
}

$sourceGit = Join-Path $SourceRoot '.git'
$coreProject = Join-Path $SourceRoot 'MFAAvalonia\MFAAvalonia.csproj'
$desktopProject = Join-Path $SourceRoot 'MFAAvalonia.Desktop\MFAAvalonia.Desktop.csproj'
$updaterProject = Join-Path $SourceRoot 'MFAUpdater\MFAUpdater.csproj'
$applicationIcon = Join-Path $SourceRoot 'MFAAvalonia\Assets\logo.ico'
$taskSource = Join-Path $SourceRoot 'MFAAvalonia\Helper\ValueType\MFATask.cs'
$settingsSource = Join-Path $SourceRoot 'MFAAvalonia\Views\Pages\SettingsView.axaml'
$versionCheckerSource = Join-Path $SourceRoot 'MFAAvalonia\Helper\VersionChecker.cs'
$performanceSettingsView = Join-Path $SourceRoot 'MFAAvalonia\Views\UserControls\Settings\PerformanceProfileSettingsUserControl.axaml'
$performanceSettingsModel = Join-Path $SourceRoot 'MFAAvalonia\ViewModels\UsersControls\Settings\PerformanceProfileSettingsUserControlModel.cs'
$focusHandlerSource = Join-Path $SourceRoot 'MFAAvalonia\Extensions\MaaFW\FocusHandler.cs'

foreach ($required in (
    $MfaRoot,
    $patch,
    $deployedAssembly,
    $deployedExecutable,
    $sourceGit,
    $coreProject,
    $desktopProject,
    $updaterProject,
    $applicationIcon,
    $taskSource,
    $settingsSource,
    $versionCheckerSource,
    $performanceSettingsView,
    $performanceSettingsModel,
    $focusHandlerSource
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "MFA stop-status patch requirement is missing: $required"
    }
}

# This project uses a locally customized MFA build. The settings page and the
# Mirror startup guard must both be present before replacing the runtime DLL.
# Never fall back to the official v2.12.0 DLL: doing so removes the custom page.
if (-not (Select-String -LiteralPath $settingsSource -SimpleMatch 'PerformanceProfileSettingsUserControl' -Quiet)) {
    throw "Refusing to deploy MFA source without the custom performance settings page: $SourceRoot"
}
if (-not (Select-String -LiteralPath $versionCheckerSource -SimpleMatch 'SupportsSelectedResourceUpdateSource' -Quiet)) {
    throw "Refusing to deploy MFA source without the custom Mirror startup guard: $SourceRoot"
}

& git -C $SourceRoot merge-base --is-ancestor $customizationCommit HEAD
if ($LASTEXITCODE -ne 0) {
    throw "MFA source does not contain the required $customBranch customization: $customizationCommit"
}

$sourceCommit = (& git -C $SourceRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Unable to resolve the custom MFA source commit: $SourceRoot"
}

& git -C $SourceRoot apply --reverse --check $patch 2>$null
$alreadyPatched = $LASTEXITCODE -eq 0
if (-not $alreadyPatched) {
    & git -C $SourceRoot apply --check $patch
    if ($LASTEXITCODE -ne 0) {
        throw 'The customized MFAAvalonia source does not accept the stop-status patch.'
    }
    & git -C $SourceRoot apply $patch
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to apply the MFAAvalonia stop-status patch.'
    }
}

$taskSourceHash = (Get-FileHash -LiteralPath $taskSource -Algorithm SHA256).Hash
# Git quotes non-ASCII paths as C-style octal escapes by default.  Passing
# those quoted strings to Join-Path/Test-Path produces an illegal Windows
# path (for example docs/zh/\345...md).  Emit the real Unicode paths before
# hashing the complete customized MFA worktree.
$sourceFingerprintEntries = & git -c core.quotePath=false -C $SourceRoot ls-files -co --exclude-standard |
    Sort-Object |
    ForEach-Object {
        $relativePath = $_
        $absolutePath = Join-Path $SourceRoot $relativePath
        if (Test-Path -LiteralPath $absolutePath -PathType Leaf) {
            "$relativePath=$((Get-FileHash -LiteralPath $absolutePath -Algorithm SHA256).Hash)"
        }
        else {
            "$relativePath=<deleted>"
        }
    }
if ($LASTEXITCODE -ne 0) {
    throw "Unable to enumerate the custom MFA source worktree: $SourceRoot"
}
$fingerprintBytes = [Text.Encoding]::UTF8.GetBytes(
    [string]::Join("`n", $sourceFingerprintEntries)
)
$fingerprintHasher = [Security.Cryptography.SHA256]::Create()
try {
    $fingerprintHash = $fingerprintHasher.ComputeHash($fingerprintBytes)
}
finally {
    $fingerprintHasher.Dispose()
}
# BitConverter is available in Windows PowerShell 5.1; Convert.ToHexString
# and SHA256.HashData are only available on newer .NET runtimes.
$customSourceFingerprint = [BitConverter]::ToString($fingerprintHash).Replace('-', '')
if (Test-Path -LiteralPath $marker) {
    try {
        $metadata = Get-Content -LiteralPath $marker -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        # 损坏或旧标记只能触发重新部署，不能把缺依赖的运行目录视作完成。
        $metadata = $null
    }
    $currentHash = (Get-FileHash -LiteralPath $deployedAssembly -Algorithm SHA256).Hash
    $currentExecutableHash = (Get-FileHash -LiteralPath $deployedExecutable -Algorithm SHA256).Hash
    if (
        $metadata.source_commit -eq $sourceCommit -and
        $metadata.task_source_sha256 -eq $taskSourceHash -and
        $metadata.custom_source_fingerprint -eq $customSourceFingerprint -and
        $metadata.patched_sha256 -eq $currentHash -and
        $metadata.patched_executable_sha256 -eq $currentExecutableHash -and
        $metadata.desktop_host -eq 'MaaBanGDream' -and
        (Test-Path -LiteralPath (Join-Path $MfaRoot 'MFAUpdater.exe')) -and
        $metadata.updater_sha256 -eq (Get-FileHash -LiteralPath (Join-Path $MfaRoot 'MFAUpdater.exe') -Algorithm SHA256).Hash -and
        (Test-Path -LiteralPath (Join-Path $MfaRoot 'ColorTextBlock.Avalonia.dll')) -and
        $metadata.markdown_sha256 -eq (Get-FileHash -LiteralPath (Join-Path $MfaRoot 'ColorTextBlock.Avalonia.dll') -Algorithm SHA256).Hash -and
        $metadata.dependency_manifest_version -eq 1 -and
        (Test-MfaFileManifest -Manifest $metadata.dependency_files_sha256 -Paths $dependencyTargetPaths) -and
        (Test-MfaFileManifest -Manifest $metadata.host_files_sha256 -Paths $hostFiles) -and
        $metadata.customization_commit -eq $customizationCommit
    ) {
        Write-Host 'Customized MFA runtime and branding are already deployed.'
        return
    }
}

$sdks = & dotnet --list-sdks 2>$null
if (-not ($sdks -match '^10\.')) {
    throw 'Building the customized MFAAvalonia stop-status fix requires .NET SDK 10.'
}

& dotnet build $desktopProject -c Release -p:Platform=x64 -p:MaaBanGDreamPackageBuild=true --no-self-contained
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to build the customized MFAAvalonia runtime.'
}
$updaterPublish = Join-Path $SourceRoot 'bin\UpdaterPublish'
& dotnet publish $updaterProject -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true -p:PublishTrimmed=true -p:TrimMode=link -o $updaterPublish
if ($LASTEXITCODE -ne 0) { throw 'Unable to publish the customized portable updater.' }
$builtUpdater = Join-Path $updaterPublish 'MFAUpdater.exe'
if (-not (Test-Path -LiteralPath $builtUpdater)) { throw 'Portable updater executable was not produced.' }

$builtAssembly = Join-Path $SourceRoot 'MFAAvalonia\bin\x64\Release\net10.0\MFAAvalonia.Core.dll'
$builtMarkdownAssembly = Join-Path (Split-Path -Parent $builtAssembly) 'ColorTextBlock.Avalonia.dll'
$builtExecutable = Join-Path $SourceRoot 'bin\x64\Release\MaaBanGDream.exe'
if (-not (Test-Path -LiteralPath $builtAssembly)) {
    throw "Customized MFAAvalonia assembly was not produced: $builtAssembly"
}
if (-not (Test-Path -LiteralPath $builtExecutable)) {
    throw "Customized MFAAvalonia executable was not produced: $builtExecutable"
}
if (-not (Test-Path -LiteralPath $builtMarkdownAssembly)) {
    throw "Customized Markdown assembly was not produced: $builtMarkdownAssembly"
}
$builtHostRoot = Split-Path -Parent $builtExecutable
foreach ($hostFile in $hostFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $builtHostRoot $hostFile) -PathType Leaf)) {
        throw "Branded desktop host file is missing: $hostFile"
    }
}
foreach ($dependencyName in $dependencyNames) {
    $builtDependency = Join-Path $builtHostRoot $dependencyName
    if (-not (Test-Path -LiteralPath $builtDependency -PathType Leaf)) {
        throw "Customized MFA dependency was not produced at the desktop build root: $builtDependency"
    }
}

Assert-MfaDeploymentPath -Path $backupDirectory
New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
# 任何覆盖前撤销旧成功标记；中途复制/核验失败会抛出，启动器不能继续。
if (Test-Path -LiteralPath $marker -PathType Leaf) {
    Backup-MfaDeploymentFile -Path $marker -RelativePath '.maabangdream-mfa-stop-status.json'
    Assert-MfaDeploymentPath -Path $marker
    Remove-Item -LiteralPath $marker -Force
}
foreach ($relativePath in $dependencyTargetPaths) {
    $target = Join-Path $MfaRoot $relativePath
    $source = Join-Path $builtHostRoot (Split-Path -Leaf $relativePath)
    Backup-MfaDeploymentFile -Path $target -RelativePath $relativePath
    Assert-MfaDeploymentPath -Path $target
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Assert-MfaDeploymentPath -Path $target
    Copy-Item -LiteralPath $source -Destination $target -Force
    Assert-MfaCopiedFile -Source $source -Target $target
}
$currentHash = (Get-FileHash -LiteralPath $deployedAssembly -Algorithm SHA256).Hash
$backupAssembly = Join-Path $backupDirectory "MFAAvalonia.Core.$currentHash.dll"
Assert-MfaDeploymentPath -Path $backupAssembly -AllowedRoot $backupDirectory
if (-not (Test-Path -LiteralPath $backupAssembly)) {
    Copy-Item -LiteralPath $deployedAssembly -Destination $backupAssembly
}
$currentExecutableHash = (Get-FileHash -LiteralPath $deployedExecutable -Algorithm SHA256).Hash
$backupExecutable = Join-Path $backupDirectory "MFAAvalonia.$currentExecutableHash.exe"
Assert-MfaDeploymentPath -Path $backupExecutable -AllowedRoot $backupDirectory
if (-not (Test-Path -LiteralPath $backupExecutable)) {
    Copy-Item -LiteralPath $deployedExecutable -Destination $backupExecutable
}

Assert-MfaDeploymentPath -Path $deployedAssembly
Copy-Item -LiteralPath $builtAssembly -Destination $deployedAssembly -Force
# 文本组件的布局修复不在 Core DLL 中，必须单独备份并同步。
$deployedMarkdownAssembly = Join-Path $MfaRoot 'ColorTextBlock.Avalonia.dll'
if (Test-Path -LiteralPath $deployedMarkdownAssembly) {
    $oldMarkdownHash = (Get-FileHash -LiteralPath $deployedMarkdownAssembly -Algorithm SHA256).Hash
    Assert-MfaDeploymentPath -Path (Join-Path $backupDirectory "ColorTextBlock.Avalonia.$oldMarkdownHash.dll") -AllowedRoot $backupDirectory
    Copy-Item -LiteralPath $deployedMarkdownAssembly -Destination (Join-Path $backupDirectory "ColorTextBlock.Avalonia.$oldMarkdownHash.dll") -Force
}
Assert-MfaDeploymentPath -Path $deployedMarkdownAssembly
Copy-Item -LiteralPath $builtMarkdownAssembly -Destination $deployedMarkdownAssembly -Force
$deployedUpdater = Join-Path $MfaRoot 'MFAUpdater.exe'
if (Test-Path -LiteralPath $deployedUpdater) {
    $oldUpdaterHash = (Get-FileHash -LiteralPath $deployedUpdater -Algorithm SHA256).Hash
    Assert-MfaDeploymentPath -Path (Join-Path $backupDirectory "MFAUpdater.$oldUpdaterHash.exe") -AllowedRoot $backupDirectory
    Copy-Item -LiteralPath $deployedUpdater -Destination (Join-Path $backupDirectory "MFAUpdater.$oldUpdaterHash.exe") -Force
}
Assert-MfaDeploymentPath -Path $deployedUpdater
Copy-Item -LiteralPath $builtUpdater -Destination $deployedUpdater -Force
# 新宿主需要同时部署自己的依赖清单和托管入口，不能只改 EXE 文件名。
foreach ($hostFile in $hostFiles) {
    Backup-MfaDeploymentFile -Path (Join-Path $MfaRoot $hostFile) -RelativePath $hostFile
    Assert-MfaDeploymentPath -Path (Join-Path $MfaRoot $hostFile)
    Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $builtExecutable) $hostFile) -Destination (Join-Path $MfaRoot $hostFile) -Force
    Assert-MfaCopiedFile -Source (Join-Path $builtHostRoot $hostFile) -Target (Join-Path $MfaRoot $hostFile)
}
$deployedExecutable = Join-Path $MfaRoot 'MaaBanGDream.exe'
foreach ($legacyHostFile in @('MFAAvalonia.exe', 'MFAAvalonia.dll', 'MFAAvalonia.deps.json', 'MFAAvalonia.runtimeconfig.json')) {
    $legacyHostPath = Join-Path $MfaRoot $legacyHostFile
    if (Test-Path -LiteralPath $legacyHostPath -PathType Leaf) {
        $legacyHash = (Get-FileHash -LiteralPath $legacyHostPath -Algorithm SHA256).Hash
        Assert-MfaDeploymentPath -Path (Join-Path $backupDirectory "$legacyHostFile.$legacyHash") -AllowedRoot $backupDirectory
        Copy-Item -LiteralPath $legacyHostPath -Destination (Join-Path $backupDirectory "$legacyHostFile.$legacyHash") -Force
        Assert-MfaDeploymentPath -Path $legacyHostPath
        Remove-Item -LiteralPath $legacyHostPath -Force
    }
}
$patchedHash = (Get-FileHash -LiteralPath $deployedAssembly -Algorithm SHA256).Hash
$patchedExecutableHash = (Get-FileHash -LiteralPath $deployedExecutable -Algorithm SHA256).Hash
Assert-MfaCopiedFile -Source $builtAssembly -Target $deployedAssembly
Assert-MfaCopiedFile -Source $builtMarkdownAssembly -Target $deployedMarkdownAssembly
Assert-MfaCopiedFile -Source $builtUpdater -Target $deployedUpdater
$dependencyHashes = [ordered]@{}
foreach ($relativePath in $dependencyTargetPaths) {
    $dependencyHashes[$relativePath] = (Get-FileHash -LiteralPath (Join-Path $MfaRoot $relativePath) -Algorithm SHA256).Hash
}
$hostHashes = [ordered]@{}
foreach ($hostFile in $hostFiles) {
    $hostHashes[$hostFile] = (Get-FileHash -LiteralPath (Join-Path $MfaRoot $hostFile) -Algorithm SHA256).Hash
}
[ordered]@{
    source_kind = 'custom-performance-profile-settings'
    source_branch = $customBranch
    source_commit = $sourceCommit
    customization_commit = $customizationCommit
    task_source_sha256 = $taskSourceHash
    custom_source_fingerprint = $customSourceFingerprint
    patch = 'mfaavalonia-v2.12.0-stop-status.patch'
    patched_sha256 = $patchedHash
    patched_executable_sha256 = $patchedExecutableHash
    desktop_host = 'MaaBanGDream'
    updater_sha256 = (Get-FileHash -LiteralPath $deployedUpdater -Algorithm SHA256).Hash
    markdown_sha256 = (Get-FileHash -LiteralPath $deployedMarkdownAssembly -Algorithm SHA256).Hash
    dependency_manifest_version = 1
    dependency_files_sha256 = $dependencyHashes
    host_files_sha256 = $hostHashes
    backup = $backupAssembly
    backup_executable = $backupExecutable
} | ConvertTo-Json -Depth 5 | ForEach-Object {
    Assert-MfaDeploymentPath -Path $marker
    Set-Content -LiteralPath $marker -Value $_ -Encoding utf8
}

Write-Host "Customized MFAAvalonia runtime deployed with branding, performance settings, and stop-status fix: $patchedHash / $patchedExecutableHash"
