from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def test_v140_uses_polyform_noncommercial_for_project_owned_material():
    interface = json.loads(read("interface.json"))
    license_text = read("LICENSE")
    licensing = read("LICENSING.md")
    readme = read("README.md")

    assert interface["version"] == "1.4.2"
    assert interface["license"] == "PolyForm-Noncommercial-1.0.0"
    assert license_text.startswith("Required Notice: Copyright (c) 2026")
    assert "# PolyForm Noncommercial License 1.0.0" in license_text
    assert "## Noncommercial Purposes" in license_text
    assert "v1.3.9" in licensing
    assert "source-available" in licensing
    assert "收费部署或维护" in readme


def test_brand_policy_requires_public_derivatives_to_use_distinct_identity():
    policy = read("TRADEMARKS.md")

    assert "明显不同的名称和 Logo" in policy
    assert "基于 MaaBanGDream" in policy
    assert "事实性的" in policy
    assert "收费服务" in policy


def test_third_party_licenses_are_not_overridden():
    notices = read("THIRD-PARTY-NOTICES.md")
    maafw_license = read("licenses/LICENSE-MaaFramework-LGPL-3.0.md")

    for component in (
        "MFAAvalonia",
        "MaaFramework",
        "minitouch",
        "nlohmann/json",
        "OCR",
        "Bestdori",
    ):
        assert component in notices
    assert "GNU Lesser General Public License" in maafw_license
    assert "Version 3, 29 June 2007" in maafw_license
