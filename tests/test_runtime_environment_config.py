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
    assert "agent\\profile_manager.py" in launch
    assert "child_exec = $python" in launch
    assert "resolution = @(1280, 720)" in launch


def test_runtime_gate_requires_the_named_conda_environment():
    expected = json.loads(
        (ROOT / "runtime-compatibility.json").read_text(encoding="utf-8")
    )
    actual = check_runtime.static_versions()

    assert expected["environment_manager"] == "conda"
    assert expected["conda_environment"] == "maabangdream"
    assert actual["environment_manager"] == "conda"
    assert actual["conda_environment"] == "maabangdream"
