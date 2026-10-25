"""竞演演出（VersusLive）的模式契约与设置校验。

竞演与协力高度同构（五人对战房、准备页、最终封面、结算），差异集中在：
- 房间选择页只有两张卡（活动歌曲房间 / 自由选歌房间），选卡后点右下「确定」入房；
- 由 `run_mode="versus"` 贯穿 profile_play / native 预武装，并复用协力的漏键抖动。
"""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import cv2
import numpy as np
import pytest

from agent.realtime.live_session import LiveRunContext
from agent.realtime.versus_action import (
    ROOM_TYPE_INDEX,
    VERSUS_DIFFICULTY_TARGETS,
    VERSUS_SONG_CHOICES,
    classify_room_type,
    configure_versus_settings,
    versus_play_params,
)


ROOT = Path(__file__).parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_versus_interface_exposes_room_types_difficulties_and_flow_options():
    interface = load(ROOT / "interface.json")
    task = next(t for t in interface["task"] if t["name"] == "VersusLive")
    assert task["entry"] == "VersusLive"
    assert task["label"] == "⚔️ 竞演演出"
    assert task["option"] == [
        "VersusRoomType",
        "VersusDifficulty",
        "VersusCount",
        "VersusSongChoice",
        "VersusDebug",
        "VersusDisconnectJump",
        "VersusLiveStaminaRefill",
    ]
    options = interface["option"]
    assert options["VersusRoomType"]["default_case"] == "Free"
    assert [c["name"] for c in options["VersusRoomType"]["cases"]] == ["Free", "Activity"]
    assert options["VersusRoomType"]["cases"][1]["pipeline_override"] == {
        "VersusRoomTypeConfigure": {"custom_action_param": {"room_type": "activity"}}
    }
    assert [c["name"] for c in options["VersusDifficulty"]["cases"]] == [
        "Easy", "Normal", "Hard", "Expert", "Special",
    ]
    for case in options["VersusDifficulty"]["cases"]:
        assert case["pipeline_override"]["VersusSpeedSettingsGate"][
            "custom_action_param"
        ] == {
            "entry_mode": "home",
            "difficulty": case["name"],
            "require_profile": True,
            "dpi": 240,
            "game_fps": 60,
            "render_quality": "standard",
        }
    assert [c["name"] for c in options["VersusSongChoice"]["cases"]] == [
        "Unspecified", "Random", "Current",
    ]
    debug = options["VersusDebug"]
    assert debug["default_case"] == "Light"
    assert {
        c["name"]: c["pipeline_override"]["VersusDebugConfigure"]["custom_action_param"]
        for c in debug["cases"]
    } == {
        "Light": {"debug_recording": False, "diagnostic_trace": True, "opening_recording": False},
        "Off": {"debug_recording": False, "diagnostic_trace": False, "opening_recording": False},
        "Full": {"debug_recording": True, "diagnostic_trace": True, "opening_recording": False},
        "Opening": {"debug_recording": False, "diagnostic_trace": True, "opening_recording": True},
    }
    count = options["VersusCount"]
    assert count["inputs"][0]["verify"] == r"^(?:0|[1-9][0-9]{0,2})$"
    assert count["pipeline_override"]["VersusCountConfigure"][
        "custom_action_param"
    ] == {"count": "{Count}"}
    assert [c["name"] for c in options["VersusDisconnectJump"]["cases"]] == ["Off", "On"]
    assert set(VERSUS_DIFFICULTY_TARGETS) == {
        "Easy", "Normal", "Hard", "Expert", "Special",
    }
    assert VERSUS_SONG_CHOICES == ("unspecified", "random", "current")
    assert ROOM_TYPE_INDEX == {"activity": 0, "free": 1}


def test_versus_pipeline_creates_room_and_returns_home():
    nodes = load(ROOT / "resource" / "pipeline" / "versus_live.json")
    assert nodes["VersusLive"]["next"] == ["VersusProcessConflictGuard"]
    assert nodes["VersusEntryConfigure"]["custom_action"] == "VersusLiveConfigure"
    assert nodes["VersusEntryConfigure"]["custom_action_param"] == {
        "reset": True, "entry_method": "confirm",
    }
    assert nodes["VersusRoomTypeConfigure"]["custom_action_param"] == {"room_type": "free"}
    assert nodes["VersusSpeedSettingsGate"]["custom_action"] == (
        "RealtimeGameSpeedSettingsGate"
    )
    assert nodes["VersusSpeedSettingsGate"]["next"] == ["VersusHomeLive"]
    assert nodes["VersusLiveEntryTemplate"]["template"] == "versus/live_entry.png"
    assert nodes["VersusEnterLive"]["custom_action"] == "LiveSelectFind"
    assert nodes["VersusEnterLive"]["custom_action_param"]["expected"] == "竞演演出"
    assert nodes["VersusRun"]["custom_action"] == "VersusLiveFlow"
    assert nodes["VersusRun"]["next"] == ["VersusReturnHome"]
    assert "max_hit" not in nodes["VersusRun"]
    assert nodes["VersusReturnHome"]["custom_action"] == "VersusLiveFinalize"
    assert nodes["VersusReturnHome"]["next"] == ["VersusComplete"]
    params = nodes["VersusReturnHome"]["custom_action_param"]
    assert params["back_only"] is True
    assert params["back_acceleration_click_point"] == [1279, 719]
    assert params["home_node"] == "VersusHomeMarker"
    assert nodes["VersusComplete"]["custom_action_param"]["task_name"] == "VersusLive"
    assert nodes["VersusFailure"]["custom_action_param"]["reason_source"] == "latest"


def test_versus_home_live_click_reuses_the_known_good_navigation_contract():
    versus = load(ROOT / "resource" / "pipeline" / "versus_live.json")
    realtime = load(ROOT / "resource" / "pipeline" / "realtime_multi_live.json")
    shared_fields = {
        "recognition", "template", "threshold", "action",
        "custom_action", "target", "post_delay",
    }
    assert {k: versus["VersusHomeLive"][k] for k in shared_fields} == {
        k: realtime["RealtimeLiveHomeLive"][k] for k in shared_fields
    }


def test_versus_templates_are_deployed_and_nonempty():
    image_dir = ROOT / "resource" / "image" / "versus"
    required = {
        "live_entry.png",
        "room_search.png",
        "create_room.png",
        "room_type_activity.png",
        "room_type_free.png",
        "room_together.png",
        "song_unspecified.png",
        "song_random.png",
        "ready_button.png",
        "member_exit_title.png",
        "connect_failed_body.png",
        "room_match_wait.png",
    }
    assert required == {path.name for path in image_dir.glob("*.png")}
    assert all(
        cv2.imread(str(image_dir / name), cv2.IMREAD_COLOR) is not None
        for name in required
    )


def test_agent_server_registers_versus_action():
    source = (ROOT / "agent" / "server.py").read_text(encoding="utf-8")
    assert "import realtime.versus_action" in source


def test_versus_play_params_carry_the_versus_run_mode():
    params = versus_play_params({"difficulty": "Expert", "debug_recording": False,
                                 "diagnostic_trace": True, "opening_recording": False})
    assert params["run_mode"] == "versus"
    assert params["difficulty"] == "Expert"
    assert params["confirm_final_cover"] is True


def test_versus_settings_validation_and_defaults():
    try:
        settings = configure_versus_settings({"reset": True})
        assert settings["entry_method"] == "confirm"
        assert settings["room_type"] == "free"
        assert settings["count"] == 1
        assert settings["song_choice"] == "unspecified"
        with pytest.raises(ValueError):
            configure_versus_settings({"reset": True, "room_type": "legend"})
        with pytest.raises(ValueError):
            configure_versus_settings({"reset": True, "count": 1000})
        with pytest.raises(ValueError):
            configure_versus_settings({"reset": True, "song_choice": "bogus"})
    finally:
        configure_versus_settings({"reset": True})


def test_classify_room_type_matches_the_centred_card():
    for name, expected in (("room_type_activity", "activity"), ("room_type_free", "free")):
        card = cv2.imread(str(ROOT / "resource" / "image" / "versus" / f"{name}.png"),
                          cv2.IMREAD_COLOR)
        assert card is not None
        frame = np.zeros((720, 1280, 3), dtype="uint8")
        frame[140:140 + card.shape[0], 490:490 + card.shape[1]] = card
        assert classify_room_type(frame) == expected


def test_versus_action_only_references_existing_live_run_fields():
    """防护机械改名遗留：`run.<attr>` 必须是 LiveRunContext 的真实字段。

    竞演 action 由协力 action 改写而来，共享对象上的字段若被一并加上 `versus_`
    前缀，就会在真机上抛 AttributeError（`versus_prestart_image` 已踩过一次）。
    这里静态核对 `run.` 属性访问集合，避免同类问题再进真机。
    """
    source = (ROOT / "agent" / "realtime" / "versus_action.py").read_text(
        encoding="utf-8"
    )
    fields = {field.name for field in dataclasses.fields(LiveRunContext)}
    referenced = set(re.findall(r"\brun\.([a-z_][a-z0-9_]*)", source))
    unknown = referenced - fields
    assert not unknown, (
        f"versus_action.py 引用了不存在的 LiveRunContext 字段：{sorted(unknown)}"
    )
