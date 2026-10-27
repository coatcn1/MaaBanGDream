from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
PATCHER = ROOT / "scripts/patch-mfa-stop-status.ps1"
PKCS = "System.Security.Cryptography.Pkcs.dll"
TMDS = "Tmds.DBus.Protocol.dll"
OLD_PKCS = f"runtimes/win/lib/net8.0/{PKCS}"
HOST_FILES = ("MaaBanGDream.exe", "MaaBanGDream.dll", "MaaBanGDream.deps.json", "MaaBanGDream.runtimeconfig.json")
pytestmark = pytest.mark.skipif(shutil.which("pwsh") is None, reason="需要 PowerShell 7")


def put(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


@pytest.fixture
def layout(tmp_path):
    source = tmp_path / "fake source"
    runtime = tmp_path / "fake runtime"
    (source / ".git").mkdir(parents=True)
    tracked = {
        "MFAAvalonia/MFAAvalonia.csproj": b"core",
        "MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj": b"desktop",
        "MFAUpdater/MFAUpdater.csproj": b"updater",
        "MFAAvalonia/Assets/logo.ico": b"icon",
        "MFAAvalonia/Helper/ValueType/MFATask.cs": b"patched task",
        "MFAAvalonia/Views/Pages/SettingsView.axaml": b"PerformanceProfileSettingsUserControl",
        "MFAAvalonia/Helper/VersionChecker.cs": b"SupportsSelectedResourceUpdateSource",
        "MFAAvalonia/Views/UserControls/Settings/PerformanceProfileSettingsUserControl.axaml": b"view",
        "MFAAvalonia/ViewModels/UsersControls/Settings/PerformanceProfileSettingsUserControlModel.cs": b"model",
        "MFAAvalonia/Extensions/MaaFW/FocusHandler.cs": b"focus",
    }
    for relative, value in tracked.items():
        put(source, relative, value)
    put(source, "MFAAvalonia/bin/x64/Release/net10.0/MFAAvalonia.Core.dll", b"new core")
    put(source, "MFAAvalonia/bin/x64/Release/net10.0/ColorTextBlock.Avalonia.dll", b"new markdown")
    put(source, "bin/UpdaterPublish/MFAUpdater.exe", b"new updater")
    build = source / "bin/x64/Release"
    for name in HOST_FILES:
        put(build, name, ("new " + name).encode())
        put(runtime, name, ("old " + name).encode())
    for name in (PKCS, TMDS):
        put(build, name, ("new " + name).encode())
        put(runtime, name, ("old " + name).encode())
    put(build, OLD_PKCS, b"stale source subdirectory must never be copied")
    put(runtime, OLD_PKCS, b"old nested pkcs")
    put(runtime, "MFAAvalonia.Core.dll", b"old core")
    return dict(source=source, runtime=runtime, build=build, tracked=list(tracked))


def run_patcher(layout, tmp_path, *, corrupt_copy=False):
    payload = tmp_path / "layout.json"
    payload.write_text(json.dumps({**{k: str(v) for k, v in layout.items() if k != "tracked"},
                                   "tracked": layout["tracked"], "patcher": str(PATCHER),
                                   "corrupt_copy": corrupt_copy}), encoding="utf-8")
    harness = tmp_path / "invoke.ps1"
    harness.write_text("""param([string]$Fixture)
$ErrorActionPreference = 'Stop'
$data = Get-Content -Raw -LiteralPath $Fixture | ConvertFrom-Json
$global:buildCalls = 0
function global:git {
    if ($args -contains 'rev-parse') { 'test-source-commit' }
    elseif ($args -contains 'ls-files') { $data.tracked }
    $global:LASTEXITCODE = 0
}
function global:dotnet {
    if ($args -contains '--list-sdks') { '10.0.100 [fake SDK]' }
    elseif ($args -contains 'build') { $global:buildCalls += 1 }
    $global:LASTEXITCODE = 0
}
function global:Copy-Item {
    param([string]$LiteralPath, [string]$Destination, [switch]$Force)
    Microsoft.PowerShell.Management\\Copy-Item -LiteralPath $LiteralPath -Destination $Destination -Force:$Force
    if ($data.corrupt_copy -and $Destination -eq (Join-Path $data.runtime 'Tmds.DBus.Protocol.dll')) {
        [IO.File]::WriteAllText($Destination, 'corrupted deployment')
    }
}
try {
    & $data.patcher -MfaRoot $data.runtime -SourceRoot $data.source
    @{ok=$true; builds=$global:buildCalls} | ConvertTo-Json -Compress
} catch {
    @{ok=$false; builds=$global:buildCalls; error=$_.Exception.Message} | ConvertTo-Json -Compress
    exit 1
}
""", encoding="utf-8-sig")
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(harness), str(payload)],
                            capture_output=True, text=True, encoding="utf-8")
    final = json.loads(result.stdout.strip().splitlines()[-1])
    return final


def marker(layout):
    return layout["runtime"] / ".maabangdream-mfa-stop-status.json"


def test_deploys_root_dependencies_repairs_nested_pkcs_and_backs_up(layout, tmp_path):
    result = run_patcher(layout, tmp_path)
    assert result["ok"], result
    for name in (PKCS, TMDS):
        assert (layout["runtime"] / name).read_bytes() == (layout["build"] / name).read_bytes()
    assert (layout["runtime"] / OLD_PKCS).read_bytes() == (layout["build"] / PKCS).read_bytes()
    backups = [path.read_bytes() for path in (layout["runtime"] / ".maabangdream-backup").rglob("*") if path.is_file()]
    assert b"old nested pkcs" in backups
    assert ("old " + PKCS).encode() in backups
    assert ("old " + TMDS).encode() in backups
    assert ("old MaaBanGDream.deps.json").encode() in backups
    assert run_patcher(layout, tmp_path) == {"ok": True, "builds": 0}


@pytest.mark.parametrize("name", [PKCS, TMDS])
def test_missing_root_build_dependency_fails_before_overwrite(layout, tmp_path, name):
    (layout["build"] / name).unlink()
    result = run_patcher(layout, tmp_path)
    assert not result["ok"]
    assert (layout["runtime"] / "MFAAvalonia.Core.dll").read_bytes() == b"old core"
    assert not marker(layout).exists()


@pytest.mark.parametrize("relative", [PKCS, TMDS, OLD_PKCS, "MaaBanGDream.deps.json"])
@pytest.mark.parametrize("fault", ["missing", "tampered"])
def test_marker_does_not_skip_missing_or_changed_dependency(layout, tmp_path, relative, fault):
    assert run_patcher(layout, tmp_path)["ok"]
    target = layout["runtime"] / relative
    target.unlink() if fault == "missing" else target.write_bytes(b"tampered")
    result = run_patcher(layout, tmp_path)
    assert result["ok"] and result["builds"] == 1, result
    assert (layout["runtime"] / PKCS).read_bytes() == (layout["build"] / PKCS).read_bytes()


def test_old_marker_without_dependency_manifest_is_rebuilt(layout, tmp_path):
    assert run_patcher(layout, tmp_path)["ok"]
    data = json.loads(marker(layout).read_text(encoding="utf-8-sig"))
    for key in list(data):
        if "dependency" in key or key == "host_files_sha256":
            del data[key]
    marker(layout).write_text(json.dumps(data), encoding="utf-8")
    result = run_patcher(layout, tmp_path)
    assert result["ok"] and result["builds"] == 1, result


def test_copy_hash_failure_does_not_write_success_marker(layout, tmp_path):
    result = run_patcher(layout, tmp_path, corrupt_copy=True)
    assert not result["ok"], result
    assert not marker(layout).exists()


def test_rebuild_failure_invalidates_previous_success_marker(layout, tmp_path):
    assert run_patcher(layout, tmp_path)["ok"]
    put(layout["source"], "MFAAvalonia/Helper/ValueType/MFATask.cs", b"changed task")
    result = run_patcher(layout, tmp_path, corrupt_copy=True)
    assert not result["ok"] and result["builds"] == 1, result
    assert not marker(layout).exists()
    assert list((layout["runtime"] / ".maabangdream-backup").rglob(".maabangdream-mfa-stop-status.json.*.bak"))


def test_new_stale_runtime_pkcs_copy_invalidates_marker(layout, tmp_path):
    assert run_patcher(layout, tmp_path)["ok"]
    extra = "runtimes/win/lib/net9.0/" + PKCS
    put(layout["runtime"], extra, b"unexpected stale pkcs")
    result = run_patcher(layout, tmp_path)
    assert result["ok"] and result["builds"] == 1, result
    assert (layout["runtime"] / extra).read_bytes() == (layout["build"] / PKCS).read_bytes()


def test_corrupt_marker_is_not_treated_as_success(layout, tmp_path):
    assert run_patcher(layout, tmp_path)["ok"]
    marker(layout).write_text("not valid JSON", encoding="utf-8")
    result = run_patcher(layout, tmp_path)
    assert result["ok"] and result["builds"] == 1, result


def test_real_desktop_build_dependencies_are_copied_from_root(layout, tmp_path):
    configured = os.environ.get("MBD_TEST_MFA_BUILD_ROOT")
    if not configured:
        pytest.skip("未配置真实 MFA 构建目录")
    actual = Path(configured)
    for name in (PKCS, TMDS):
        put(layout["build"], name, (actual / name).read_bytes())
    nested_source = actual / OLD_PKCS
    if nested_source.is_file():
        put(layout["build"], OLD_PKCS, nested_source.read_bytes())
    result = run_patcher(layout, tmp_path)
    assert result["ok"], result
    for name in (PKCS, TMDS):
        assert (layout["runtime"] / name).read_bytes() == (actual / name).read_bytes()
    assert (layout["runtime"] / OLD_PKCS).read_bytes() == (actual / PKCS).read_bytes()


def make_link(tmp_path, link, target, kind="Junction"):
    if os.name != "nt":
        pytest.skip("仅 Windows 验证目录联接与符号链接")
    helper = tmp_path / "create-link.ps1"
    helper.write_text("""param([string]$Link, [string]$Target, [string]$Kind)
$ErrorActionPreference = 'Stop'
New-Item -ItemType $Kind -Path $Link -Value $Target | Out-Null
""", encoding="utf-8-sig")
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(helper), str(link), str(target), kind],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode and kind == "SymbolicLink":
        pytest.skip("当前 Windows 环境不允许创建符号链接")
    assert result.returncode == 0, result.stderr


def test_runtime_junction_is_rejected_before_any_deployment_write(layout, tmp_path):
    outside = tmp_path / "protected outside"
    (layout["runtime"] / "runtimes").rename(outside)
    make_link(tmp_path, layout["runtime"] / "runtimes", outside)
    result = run_patcher(layout, tmp_path)
    assert not result["ok"], result
    assert (outside / "win/lib/net8.0" / PKCS).read_bytes() == b"old nested pkcs"
    assert (layout["runtime"] / "MFAAvalonia.Core.dll").read_bytes() == b"old core"
    assert not marker(layout).exists()


@pytest.mark.parametrize("location,kind", [
    ("root", "Junction"), ("ancestor", "Junction"), ("runtimes", "SymbolicLink"),
    ("backup", "Junction"), ("backup-child", "Junction"),
])
def test_linked_directories_are_rejected_before_writing(layout, tmp_path, location, kind):
    runtime = layout["runtime"]
    if location == "root":
        linked = tmp_path / "linked runtime"
        make_link(tmp_path, linked, runtime, kind)
        layout["runtime"] = linked
    elif location == "ancestor":
        outside = tmp_path / "protected parent"
        outside.mkdir()
        runtime.rename(outside / "runtime")
        runtime = outside / "runtime"
        linked = tmp_path / "linked parent"
        make_link(tmp_path, linked, outside, kind)
        layout["runtime"] = linked / "runtime"
    elif location == "runtimes":
        outside = tmp_path / "protected runtimes"
        (runtime / "runtimes").rename(outside)
        make_link(tmp_path, runtime / "runtimes", outside, kind)
    else:
        outside = tmp_path / "protected backups"
        outside.mkdir()
        linked = runtime / ".maabangdream-backup"
        if location == "backup-child":
            linked = linked / "deployment/runtimes"
            linked.parent.mkdir(parents=True)
        make_link(tmp_path, linked, outside, kind)
    result = run_patcher(layout, tmp_path)
    assert not result["ok"] and result["builds"] == 0, result
    assert (runtime / "MFAAvalonia.Core.dll").read_bytes() == b"old core"
    assert (runtime / PKCS).read_bytes() == ("old " + PKCS).encode()
    assert not (runtime / ".maabangdream-mfa-stop-status.json").exists()
    if location.startswith("backup"):
        assert not list(outside.iterdir())


@pytest.mark.parametrize("relative", [PKCS, "MFAAvalonia.Core.dll", "MaaBanGDream.deps.json",
                                      ".maabangdream-mfa-stop-status.json", "backup-leaf"])
def test_linked_leaf_files_are_rejected_before_writing(layout, tmp_path, relative):
    runtime = layout["runtime"]
    if relative == "backup-leaf":
        old_value = (runtime / PKCS).read_bytes()
        digest = hashlib.sha256(old_value).hexdigest().upper()
        target = runtime / ".maabangdream-backup/deployment" / f"{PKCS}.{digest}.bak"
    else:
        target = runtime / relative
        old_value = target.read_bytes() if target.is_file() else b"{}"
    protected = put(tmp_path, "protected file", old_value)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    make_link(tmp_path, target, protected, "SymbolicLink")
    result = run_patcher(layout, tmp_path)
    assert not result["ok"] and result["builds"] == 0, result
    assert protected.read_bytes() == old_value
    assert (runtime / "MFAAvalonia.Core.dll").read_bytes() == b"old core"


@pytest.mark.parametrize("relative", [PKCS, OLD_PKCS, "MFAAvalonia.Core.dll", "MaaBanGDream.deps.json",
                                      ".maabangdream-mfa-stop-status.json", "backup-leaf"])
def test_junction_at_leaf_path_is_rejected_before_writing(layout, tmp_path, relative):
    runtime = layout["runtime"]
    if relative == "backup-leaf":
        digest = hashlib.sha256((runtime / PKCS).read_bytes()).hexdigest().upper()
        target = runtime / ".maabangdream-backup/deployment" / f"{PKCS}.{digest}.bak"
    else:
        target = runtime / relative
    protected = tmp_path / "protected leaf directory"
    put(protected, "sentinel", b"unchanged")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    make_link(tmp_path, target, protected)
    result = run_patcher(layout, tmp_path)
    assert not result["ok"] and result["builds"] == 0, result
    assert "reparse point" in result["error"], result
    assert sorted(path.name for path in protected.iterdir()) == ["sentinel"]
    assert (protected / "sentinel").read_bytes() == b"unchanged"
    assert (runtime / TMDS).read_bytes() == ("old " + TMDS).encode()


def test_valid_marker_does_not_bypass_new_runtime_junction(layout, tmp_path):
    assert run_patcher(layout, tmp_path)["ok"]
    old_marker = marker(layout).read_bytes()
    protected = tmp_path / "protected deployed runtimes"
    (layout["runtime"] / "runtimes").rename(protected)
    make_link(tmp_path, layout["runtime"] / "runtimes", protected)
    result = run_patcher(layout, tmp_path)
    assert not result["ok"] and result["builds"] == 0, result
    assert "reparse point" in result["error"], result
    assert marker(layout).read_bytes() == old_marker
    assert (protected / "win/lib/net8.0" / PKCS).read_bytes() == (layout["build"] / PKCS).read_bytes()


@pytest.mark.parametrize("relative,allowed", [("new/missing.dll", True), ("../fake runtime sibling/file.dll", False),
                                             ("new/../../outside/file.dll", False)])
def test_deployment_guard_rejects_normalized_path_escape(layout, tmp_path, relative, allowed):
    harness = tmp_path / "guard.ps1"
    harness.write_text("""param([string]$Script, [string]$MfaRoot, [string]$Target)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
$definition = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Assert-MfaDeploymentPath'}, $true)
Invoke-Expression $definition.Extent.Text
try { Assert-MfaDeploymentPath -Path $Target; 'true' } catch { 'false' }
""", encoding="utf-8-sig")
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(harness), str(PATCHER),
                             str(layout["runtime"]), str(layout["runtime"] / relative)],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert (result.stdout.strip() == "true") is allowed
