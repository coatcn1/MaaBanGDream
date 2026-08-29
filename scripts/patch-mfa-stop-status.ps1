param(
    [Parameter(Mandatory = $true)]
    [string]$MfaRoot,
    [string]$SourceRoot
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$customBranch = 'feature/performance-profile-settings'
$customizationCommit = 'd7b381b2fa6a09e140d925fb1504bac19ca1f921'
$patch = Join-Path $projectRoot 'patches\mfaavalonia-v2.12.0-stop-status.patch'
$deployedAssembly = Join-Path $MfaRoot 'MFAAvalonia.Core.dll'
$marker = Join-Path $MfaRoot '.maabangdream-mfa-stop-status.json'
$backupDirectory = Join-Path $MfaRoot '.maabangdream-backup'

if (-not $SourceRoot) {
    $SourceRoot = Join-Path $workspaceRoot 'MFAAvalonia'
}

$sourceGit = Join-Path $SourceRoot '.git'
$project = Join-Path $SourceRoot 'MFAAvalonia\MFAAvalonia.csproj'
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
    $sourceGit,
    $project,
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
    $metadata = Get-Content -LiteralPath $marker -Raw -Encoding utf8 | ConvertFrom-Json
    $currentHash = (Get-FileHash -LiteralPath $deployedAssembly -Algorithm SHA256).Hash
    if (
        $metadata.source_commit -eq $sourceCommit -and
        $metadata.task_source_sha256 -eq $taskSourceHash -and
        $metadata.custom_source_fingerprint -eq $customSourceFingerprint -and
        $metadata.patched_sha256 -eq $currentHash -and
        $metadata.customization_commit -eq $customizationCommit
    ) {
        Write-Host 'Customized MFA stop-status patch is already deployed.'
        return
    }
}

$sdks = & dotnet --list-sdks 2>$null
if (-not ($sdks -match '^10\.')) {
    throw 'Building the customized MFAAvalonia stop-status fix requires .NET SDK 10.'
}

& dotnet build $project -c Release --no-self-contained
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to build the customized MFAAvalonia assembly.'
}

$builtAssembly = Join-Path $SourceRoot 'MFAAvalonia\bin\Release\net10.0\MFAAvalonia.Core.dll'
if (-not (Test-Path -LiteralPath $builtAssembly)) {
    throw "Customized MFAAvalonia assembly was not produced: $builtAssembly"
}

New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
$currentHash = (Get-FileHash -LiteralPath $deployedAssembly -Algorithm SHA256).Hash
$backupAssembly = Join-Path $backupDirectory "MFAAvalonia.Core.$currentHash.dll"
if (-not (Test-Path -LiteralPath $backupAssembly)) {
    Copy-Item -LiteralPath $deployedAssembly -Destination $backupAssembly
}

Copy-Item -LiteralPath $builtAssembly -Destination $deployedAssembly -Force
$patchedHash = (Get-FileHash -LiteralPath $deployedAssembly -Algorithm SHA256).Hash
[ordered]@{
    source_kind = 'custom-performance-profile-settings'
    source_branch = $customBranch
    source_commit = $sourceCommit
    customization_commit = $customizationCommit
    task_source_sha256 = $taskSourceHash
    custom_source_fingerprint = $customSourceFingerprint
    patch = 'mfaavalonia-v2.12.0-stop-status.patch'
    patched_sha256 = $patchedHash
    backup = $backupAssembly
} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding utf8

Write-Host "Customized MFAAvalonia deployed with performance settings and stop-status fix: $patchedHash"
