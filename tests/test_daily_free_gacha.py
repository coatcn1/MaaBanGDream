from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from agent.realtime.daily_free_gacha import _images_identical


ROOT = Path(__file__).resolve().parents[1]


def test_gacha_pipeline_contract_and_templates_exist():
    pipeline = json.loads(
        (ROOT / "resource" / "pipeline" / "daily_free_gacha.json").read_text(
            encoding="utf-8"
        )
    )
    assert pipeline["DailyFreeGacha"]["next"] == [
        "DailyFreeGachaProcessConflictGuard",
    ]
    run = pipeline["DailyFreeGachaRun"]
    assert run["custom_action"] == "DailyFreeGachaRun"
    assert run["on_error"] == ["DailyFreeGachaFailure"]
    failure = pipeline["DailyFreeGachaFailure"]
    assert failure["custom_action"] == "TaskOutcome"
    assert failure["custom_action_param"]["status"] == "failure"

    for name, node in pipeline.items():
        template = node.get("template")
        if template:
            path = ROOT / "resource" / "image" / template
            assert path.exists(), f"missing gacha template: {template}"


def test_images_identical_compares_mean_absolute_difference():
    frame = np.full((720, 1280, 3), 40, dtype=np.uint8)
    assert _images_identical(frame, frame.copy())
    changed = frame.copy()
    changed[:, :640, :] = 200
    assert not _images_identical(frame, changed)
    assert not _images_identical(frame, None)


def test_agent_server_registers_gacha_action():
    source = (ROOT / "agent" / "server.py").read_text(encoding="utf-8")
    assert "import realtime.daily_free_gacha" in source
