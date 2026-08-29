import json
from pathlib import Path

from scripts import check_runtime


ROOT = Path(__file__).parents[1]


def test_conda_environment_name_and_python_are_documented_consistently():
    environment = (ROOT / "environment.yml").read_text(encoding="utf-8")
    setup = (ROOT / "scripts/setup.ps1").read_text(encoding="utf-8")
    launch = (ROOT / "scripts/launch-mfa.ps1").read_text(encoding="utf-8")
    verify = (ROOT / "scripts/verify.ps1").read_text(encoding="utf-8")

    assert "name: maabangdream" in environment
    assert "python=3.12" in environment
    assert "nodefaults" in environment
    assert "maabangdream" in setup
    assert "maabangdream" in launch
    assert "maabangdream" in verify
    assert ".venv" not in setup
    assert ".venv" not in launch
    assert ".venv" not in verify


def test_source_interface_defers_machine_specific_python_path_to_launcher():
    interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))

    assert interface["agent"]["child_exec"] == "python"


def test_launcher_generates_machine_local_profile_manager_configuration():
    launch = (ROOT / "scripts/launch-mfa.ps1").read_text(encoding="utf-8")

    assert "profile-manager.json" in launch
    assert "artifact_paths" in launch
    for name in ("profiles", "realtime_recordings", "result_captures", "maafw_debug", "mfa_logs"):
        assert name in launch
    assert "agent\\profile_manager.py" in launch
    assert "child_exec = $python" in launch
    assert "resolution = @(1280, 720)" in launch


def test_launcher_propagates_framework_task_failures_to_mfa():
    launch = (ROOT / "scripts/launch-mfa.ps1").read_text(encoding="utf-8")

    assert "ContinueRunningWhenError" in launch
    assert "Value $false" in launch


def test_launcher_patches_mfa_user_stop_status_race():
    launch = (ROOT / "scripts/launch-mfa.ps1").read_text(encoding="utf-8")
    patcher = (ROOT / "scripts/patch-mfa-stop-status.ps1").read_text(encoding="utf-8")
    patch = (
        ROOT / "patches/mfaavalonia-v2.12.0-stop-status.patch"
    ).read_text(encoding="utf-8")

    assert "patch-mfa-stop-status.ps1" in launch
    assert "feature/performance-profile-settings" in patcher
    assert "PerformanceProfileSettingsUserControl" in patcher
    assert "SupportsSelectedResourceUpdateSource" in patcher
    assert "git clone" not in patcher
    assert ".maabangdream-backup" in patcher
    assert "MFAAvalonia.Core.dll" in patcher
    assert "git -c core.quotePath=false -C $SourceRoot ls-files" in patcher
    assert "SHA256]::Create()" in patcher
    assert "ComputeHash($fingerprintBytes)" in patcher
    assert "SHA256]::HashData" not in patcher
    assert "Convert]::ToHexString" not in patcher
    assert "when (token.IsCancellationRequested)" in patch


def test_launcher_scopes_process_cleanup_authorization_to_one_mfa_session():
    launch = (ROOT / "scripts/launch-mfa.ps1").read_text(encoding="utf-8")

    assert "MAABANGDREAM_MFA_SESSION_ID" in launch
    assert "[Guid]::NewGuid()" in launch
    assert "Remove-Item Env:MAABANGDREAM_MFA_SESSION_ID" in launch


def test_runtime_gate_requires_the_named_conda_environment():
    expected = json.loads(
        (ROOT / "runtime-compatibility.json").read_text(encoding="utf-8")
    )
    actual = check_runtime.static_versions()

    assert expected["environment_manager"] == "conda"
    assert expected["conda_environment"] == "maabangdream"
    assert actual["environment_manager"] == "conda"
    assert actual["conda_environment"] == "maabangdream"
