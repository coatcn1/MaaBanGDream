# 构建 Native Realtime Engine V2。
#
# 优先使用 MSVC（vswhere + vcvars64），没有时回退到 conda 环境内的 zig
# 便携工具链（zig 0.16 捆绑 mingw-w64 头文件与 libc++，已验证产出的
# .pyd 能被固定 CPython 3.12 x64 环境导入）。
#
# 用法：.\scripts\build_native_realtime.ps1 [-Clean]

param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $repoRoot
$condaRoot = Join-Path $workspaceRoot ".tools\Miniconda3"
$nativeRoot = Join-Path $repoRoot "native\realtime"
$python = Join-Path $condaRoot "envs\maabangdream\python.exe"
$buildRoot = Join-Path $nativeRoot "build"
$outputDir = Join-Path $repoRoot "agent\realtime\native"

if (-not (Test-Path $python)) {
    throw "固定 Conda Python 不存在：$python"
}

function Find-MsvcVcvars {
    $vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $root = & $vswhere -latest -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath 2>$null
        if ($root) {
            $vcvars = Join-Path $root "VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path $vcvars) {
                return $vcvars
            }
        }
    }
    return $null
}

function Invoke-NativeCommand {
    param([scriptblock]$Block)
    & $Block
    if ($LASTEXITCODE -ne 0) {
        throw "native build command failed with exit code $LASTEXITCODE"
    }
}

if ($Clean -and (Test-Path $buildRoot)) {
    Remove-Item -Recurse -Force $buildRoot
}
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

$vcvars = Find-MsvcVcvars
if ($vcvars) {
    Write-Host "[build] using MSVC: $vcvars"
    Invoke-NativeCommand {
        cmd /c "`"$vcvars`" && cmake -S `"$nativeRoot`" -B `"$buildRoot`" -A x64 && cmake --build `"$buildRoot`" --config Release"
    }
    Invoke-NativeCommand {
        cmd /c "`"$vcvars`" && ctest --test-dir `"$buildRoot`" -C Release --output-on-failure"
    }
    $pyd = Get-ChildItem -Path $buildRoot -Recurse -Filter "maabangdream_realtime.pyd" |
        Where-Object { $_.FullName -match "Release" } |
        Select-Object -First 1
    if (-not $pyd) {
        throw "MSVC build finished but no .pyd was found"
    }
    Copy-Item $pyd.FullName (Join-Path $outputDir "maabangdream_realtime.pyd") -Force
} else {
    Write-Host "[build] MSVC not found; using portable zig toolchain"
    & $python -m pip install --quiet pybind11 ziglang 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "cannot install portable build dependencies"
    }

    $sitePackages = & $python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    $zig = Join-Path $sitePackages "ziglang\zig.exe"
    $pybindInclude = Join-Path $sitePackages "pybind11\include"
    $pythonInclude = & $python -c "import sysconfig; print(sysconfig.get_paths()['include'])"
    $pythonLibs = & $python -c "import sysconfig, os; print(os.path.join(sysconfig.get_config_var('prefix'), 'libs'))"
    $pythonLibName = & $python -c "import sys; print('python' + str(sys.version_info.major) + str(sys.version_info.minor))"

    $sources = Get-ChildItem -Path (Join-Path $nativeRoot "src") -Filter "*.cpp" |
        Sort-Object Name
    $sourceArgs = @($sources | ForEach-Object { $_.FullName })
    $includeArgs = @(
        "-I$(Join-Path $nativeRoot 'include')",
        "-I$(Join-Path $nativeRoot 'third_party')",
        "-I$pythonInclude",
        "-I$pybindInclude"
    )
    $pydPath = Join-Path $outputDir "maabangdream_realtime.pyd"

    $compileArgs = @(
        "c++", "-target", "x86_64-windows-gnu", "-O2", "-shared",
        "-std=c++17", "-DNDEBUG",
        "-Wno-nullability-completeness"
    ) + $includeArgs + $sourceArgs + @(
        "-o", $pydPath, "-L$pythonLibs", "-l$pythonLibName"
    )
    Write-Host "[build] zig c++ $($compileArgs.Count) arguments"
    & $zig @compileArgs
    if ($LASTEXITCODE -ne 0) {
        throw "zig build failed with exit code $LASTEXITCODE"
    }

    # 测试可执行文件。
    $testSources = Get-ChildItem -Path (Join-Path $nativeRoot "tests") -Filter "*.cpp" |
        Sort-Object Name
    $testCompileArgs = @(
        "c++", "-target", "x86_64-windows-gnu", "-O1", "-std=c++17",
        "-Wno-nullability-completeness"
    ) + $includeArgs + @("-I$(Join-Path $nativeRoot 'tests')") +
        @($testSources | ForEach-Object { $_.FullName }) +
        @($sources | Where-Object { $_.Name -ne "bindings.cpp" } |
            ForEach-Object { $_.FullName }) +
        @("-o", (Join-Path $buildRoot "mbdr_tests.exe"))
    & $zig @testCompileArgs
    if ($LASTEXITCODE -ne 0) {
        throw "zig test build failed with exit code $LASTEXITCODE"
    }
    & (Join-Path $buildRoot "mbdr_tests.exe")
    if ($LASTEXITCODE -ne 0) {
        throw "native unit tests failed"
    }
}

# 产物自检：目标环境可导入且版本一致。
$import = & $python -c @"
import sys
sys.path.insert(0, r'$outputDir')
import maabangdream_realtime as native
print(native.version())
"@
if ($LASTEXITCODE -ne 0) {
    throw "built .pyd failed to import in the fixed CPython environment"
}
Write-Host "[build] ok: maabangdream_realtime $($import.Trim()) -> $outputDir"
