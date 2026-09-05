from __future__ import annotations

import json
from pathlib import Path

from scripts.check_runtime import load_json


ROOT = Path(__file__).parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_release_launcher_extracts_only_package_local_runtime():
    launcher = read("scripts/start-release.ps1")

    assert "$runtimeDirectory = Join-Path $packageRoot 'runtime'" in launcher
    assert "Join-Path $runtimeDirectory 'maabangdream-python.zip'" in launcher
    assert "Join-Path $runtimeDirectory 'python'" in launcher
    assert "Expand-Archive" in launcher
    assert "conda-unpack.exe" in launcher
    assert "check_runtime.py" in launcher
    assert "--portable --mfa-root $packageRoot" in launcher
    assert "Invoke-WebRequest" not in launcher
    assert "Start-Process" in launcher


def test_release_launcher_generates_machine_local_paths_at_first_run():
    launcher = read("scripts/start-release.ps1")

    assert "interface.template.json" in launcher
    assert "$interface.agent.child_exec = $python.Replace" in launcher
    assert "$interface.agent.child_args" in launcher
    assert "profile-manager.json" in launcher
    assert "ContinueRunningWhenError" in launcher
    assert "MinitouchAndAdbKey" in launcher
    assert "MAABANGDREAM_MFA_SESSION_ID" in launcher
    assert "$env:MAABANGDREAM_MFA_ROOT = $packageRoot" in launcher


def test_launchers_write_json_without_bom_and_check_tolerates_bom():
    launcher = read("scripts/start-release.ps1")
    developer_launcher = read("scripts/launch-mfa.ps1")
    checker = read("scripts/check_runtime.py")

    for script in (launcher, developer_launcher):
        assert "[System.Text.UTF8Encoding]::new($false)" in script
    assert "Set-Content -LiteralPath $interfacePath -Encoding utf8" not in launcher
    assert "Set-Content -LiteralPath $deployedInterface -Encoding utf8" not in developer_launcher
    assert "utf-8-sig" in checker
    assert "Unblock-File -LiteralPath $mfa" in launcher


def test_runtime_check_loads_bom_prefixed_interface(tmp_path):
    path = tmp_path / "interface.json"
    path.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps({"interface_version": 2}).encode("utf-8")
    )

    assert load_json(path)["interface_version"] == 2


def test_release_builder_uses_clean_sources_and_excludes_private_state():
    builder = read("scripts/build-windows-release.ps1")
    validator = read("scripts/check_release_package.py")

    assert "ls-files -- agent resource" in builder
    assert "--self-contained true" in builder
    assert "conda-pack.exe" in builder
    assert "status --porcelain" in builder
    assert "[switch]$AllowDirty" in builder
    assert "PerformanceProfileSettingsUserControl" in builder
    assert "SupportsSelectedResourceUpdateSource" in builder
    assert "build_native_realtime.ps1" in builder
    assert r"agent\realtime\native\maabangdream_realtime.pyd" in builder
    assert "maabangdream_realtime.pyd" in validator
    assert "native realtime extension" in validator
    assert r"packaging\profiles" in builder
    for private_name in (
        "config",
        "logs",
        "profiles",
        "debug",
        "screencap",
        ".maabangdream-backup",
        "profile-manager.json",
        "appsettings.json",
    ):
        assert private_name in builder
        if private_name != "profiles":
            assert private_name in validator


def test_seed_profiles_are_pinned_and_free_of_machine_paths():
    profiles_dir = ROOT / "packaging" / "profiles"
    assert profiles_dir.is_dir()
    selection = json.loads(
        (profiles_dir / "selection.json").read_text(encoding="utf-8")
    )
    assert selection["pinned"]["Expert"] == "expert-20260905233716.json"
    for path in profiles_dir.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        for marker in (r"E:\game", r"D:\Documents", r"C:\Users"):
            assert marker not in text


def test_release_readme_documents_sources_and_first_run():
    release_readme = read("docs/release-package.md")
    project_readme = read("README.md")

    assert "启动 MaaBanGDream.cmd" in release_readme
    assert "coatcn1/MFAAvalonia" in release_readme
    assert "BUILD-INFO.json" in release_readme
    assert "Releases" in project_readme
    assert "feature/performance-visual-settings" in project_readme
