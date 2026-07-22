from __future__ import annotations

import json
import hashlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_interface_references_existing_entry_and_resource():
    interface = load(ROOT / "interface.json")
    assert interface["interface_version"] == 2
    assert interface["version"] == "0.2.0"
    assert interface["resource"][0]["path"] == ["./resource"]
    nodes = {}
    for path in (ROOT / "resource/pipeline").glob("*.json"):
        nodes.update(load(path))
    for task in interface["task"]:
        assert task["entry"] in nodes


def test_minimal_navigation_contract():
    common = load(ROOT / "resource/pipeline/common.json")
    nodes = load(ROOT / "resource/pipeline/minimal_navigation.json")
    merged = common | nodes
    for name in (
        "MinimalHomeMarker", "HomeLive", "FreeLive", "SongSelectMarker", "BackToHome"
    ):
        assert name in merged
    assert nodes["MinimalHomeMarker"]["next"] == ["HomeLive"]
    assert nodes["MinimalNavigation"]["action"] == "StartApp"
    assert "StartGame" not in nodes["MinimalNavigation"]["next"]
    assert nodes["BackToHome"]["custom_action"] == "CommonRecover"
    assert nodes["FreeLive"]["on_error"] == ["CommonRecover"]


def test_templates_exist_and_are_lossless_png():
    for pipeline in (ROOT / "resource/pipeline").glob("*.json"):
        for node in load(pipeline).values():
            template = node.get("template")
            if template:
                image = ROOT / "resource/image" / template
                assert image.is_file(), f"missing {image}"
                assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_recovery_is_bounded_and_shared():
    common = load(ROOT / "resource/pipeline/common.json")
    params = common["CommonRecover"]["custom_action_param"]
    assert params["escape_interval_ms"] == 1500
    assert params["escape_timeout_ms"] == 30000
    assert params["restart_limit"] == 2
    assert params["click_nodes"] == ["LoginTapToStart", "LoginNext", "CommonClose"]


def test_all_pipeline_references_exist_and_nodes_are_unique():
    merged = {}
    for path in (ROOT / "resource/pipeline").glob("*.json"):
        nodes = load(path)
        duplicates = set(merged) & set(nodes)
        assert not duplicates, f"duplicate nodes in {path}: {sorted(duplicates)}"
        merged.update(nodes)
    for name, node in merged.items():
        for field in ("next", "on_error"):
            refs = node.get(field, [])
            if isinstance(refs, str):
                refs = [refs]
            for ref in refs:
                if isinstance(ref, dict):
                    ref = ref["name"]
                assert ref in merged, f"{name}.{field} references missing {ref}"


def test_auto_live_safety_and_timeout_contract():
    nodes = load(ROOT / "resource/pipeline/auto_live.json")
    prepare_order = nodes["AutoLivePrepare"]["next"]
    assert prepare_order[:3] == [
        "AutoLiveQuotaExhausted",
        "AutoLiveEnabled",
        "AutoLiveDisabled",
    ]
    assert nodes["AutoLiveQuotaExhausted"]["action"] == "StopTask"
    assert nodes["AutoLiveDisabled"]["max_hit"] == 3
    assert nodes["AutoLivePrepare"]["target"] == [1040, 615, 100, 45]
    assert "AutoLiveStart" not in nodes["AutoLiveDisabled"]["next"]
    assert nodes["AutoLiveEnabled"]["next"] == ["AutoLiveStart"]
    incoming_to_start = [
        name
        for name, node in nodes.items()
        if "AutoLiveStart" in node.get("next", [])
    ]
    assert incoming_to_start == ["AutoLiveEnabled"]
    assert nodes["AutoLiveStart"]["timeout"] == 300000
    assert nodes["AutoLiveStart"]["next"] == ["AutoLiveResult"]
    assert nodes["AutoLiveResult"]["custom_action"] == "CommonRecover"
    assert nodes["AutoLiveResult"]["custom_action_param"]["home_node"] == (
        "AutoLiveHomeMarker"
    )


def test_imported_template_hashes_match_declared_sources():
    sources = load(ROOT / "docs/template-sources.json")
    assert sources["source"] == "BanGDreamAutoScript HEAD:assets/templates"
    for name, expected in sources["sha256"].items():
        image = ROOT / "resource/image" / name
        actual = hashlib.sha256(image.read_bytes()).hexdigest()
        assert actual == expected, f"source hash mismatch for {name}"
