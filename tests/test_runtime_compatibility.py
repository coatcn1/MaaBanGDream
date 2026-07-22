from __future__ import annotations

import pytest

from scripts import check_runtime


def test_requirement_must_be_exactly_pinned():
    assert check_runtime.required_maafw_version("MaaFw==5.10.2\n") == "5.10.2"
    with pytest.raises(ValueError):
        check_runtime.required_maafw_version("MaaFw>=5.10.2\n")


def test_reads_mfa_and_binding_versions_from_deps():
    deps = {
        "libraries": {
            "MFAAvalonia.Core.Reference/2.12.0.0": {},
            "Maa.Framework.Binding/5.8.0": {},
        }
    }
    assert check_runtime.deps_versions(deps) == ("2.12.0", "5.8.0")


def test_rejects_unvalidated_version_tuple():
    with pytest.raises(RuntimeError, match="maafw_core"):
        check_runtime.verify(
            {"maafw_core": "5.11.0"},
            {"maafw_core": "5.10.2"},
        )


def test_repository_lock_matches_project_files():
    expected = check_runtime.load_json(check_runtime.LOCK_PATH)
    requirements = check_runtime.REQUIREMENTS_PATH.read_text(encoding="utf-8")
    interface = check_runtime.load_json(check_runtime.INTERFACE_PATH)
    assert check_runtime.required_maafw_version(requirements) == expected["maafw_python"]
    assert interface["interface_version"] == expected["project_interface"]
