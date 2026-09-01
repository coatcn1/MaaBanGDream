from __future__ import annotations

from pathlib import Path


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


def test_release_launcher_writes_generated_json_without_utf8_bom():
    launcher = read("scripts/start-release.ps1")

    assert "function Write-JsonUtf8NoBom" in launcher
    assert "[System.Text.UTF8Encoding]::new($false)" in launcher
    assert "Set-Content -LiteralPath $interfacePath -Encoding utf8" not in launcher


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
        assert private_name in validator


def test_release_readme_documents_sources_and_first_run():
    release_readme = read("docs/release-package.md")
    project_readme = read("README.md")

    assert "启动 MaaBanGDream.cmd" in release_readme
    assert "coatcn1/MFAAvalonia" in release_readme
    assert "BUILD-INFO.json" in release_readme
    assert "Releases" in project_readme
    assert "feature/performance-visual-settings" in project_readme
