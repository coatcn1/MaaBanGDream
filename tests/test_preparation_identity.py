from pathlib import Path
from types import SimpleNamespace
import json

import numpy as np
import pytest

from agent.realtime import preparation_identity as identity
from agent.realtime.live_session import reset_live_run, update_live_run, current_live_run


@pytest.fixture
def ready(monkeypatch):
    reset_live_run(mode="formal", difficulty="Expert")
    update_live_run(song_id="shared-cover", song_level=28, song_title="ERER")
    state = SimpleNamespace(title="[FULL]FIRE BIRD", level=28, difficulty="EXPERT", resolved=True)
    monkeypatch.setattr(identity, "read_preparation_title", lambda image: SimpleNamespace(text=state.title, confidence=.98))
    monkeypatch.setattr(identity, "read_song_level", lambda image, **kwargs: state.level)
    monkeypatch.setattr(identity, "recognize_song_title", lambda image, **kwargs: SimpleNamespace(text=state.difficulty, confidence=.99))
    def resolve(song_id, difficulty, level, title):
        assert (song_id, difficulty, level, title) == ("shared-cover", "Expert", 28, "[FULL]FIRE BIRD")
        return SimpleNamespace(selection=SimpleNamespace(bestdori_song_id=243) if state.resolved else None, reason="ambiguous")
    monkeypatch.setattr(identity, "resolve_chart_for_selected_song", resolve)
    return state


def test_preparation_replaces_list_title_and_keeps_evidence(ready):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    run = identity.confirm_preparation_identity(image, "Expert")
    assert run.song_title == "[FULL]FIRE BIRD"
    assert run.song_level == 28
    assert run.preparation_identity_image is not image
    assert "preparation_identity_image" not in run.to_mapping()


@pytest.mark.parametrize("field,value,reason", [("level", None, "读取失败"), ("level", 27, "等级冲突"), ("difficulty", "HARD", "难度冲突"), ("resolved", False, "未确认")])
def test_preparation_conflicts_do_not_publish_identity(ready, field, value, reason):
    setattr(ready, field, value)
    with pytest.raises(RuntimeError, match=reason):
        identity.confirm_preparation_identity(np.zeros((720, 1280, 3), dtype=np.uint8), "Expert")
    assert current_live_run().song_title == "ERER"


def test_solo_pipeline_and_calibration_require_preparation_identity():
    from agent.realtime.calibration_action import calibration_round_plan
    pipeline = json.loads((Path(__file__).parents[1] / "resource/pipeline/realtime_multi_live.json").read_text(encoding="utf-8"))
    _, override = calibration_round_plan(difficulty="Expert", note_speed=5, calibration_debug=False, formal=True, play_node="play", offset=0, report_path=Path("report.json"))
    for source in (pipeline, override):
        assert source["RealtimeLiveDifficulty"]["custom_action_param"]["defer_song_title_to_preparation"]
        for node in ("RealtimeLiveFormalSettingsGate", "RealtimeLiveRehearsalSettingsGate"):
            assert source[node]["custom_action_param"]["confirm_preparation_identity"]
    assert pipeline["RealtimeLiveVisualEvaluationSettingsGate"]["custom_action_param"]["confirm_preparation_identity"]


@pytest.mark.parametrize("difficulty", ["Easy", "Normal", "Hard", "Expert", "Special"])
def test_mfa_difficulty_override_keeps_identity_gates(tmp_path, difficulty):
    import subprocess
    import sys
    root = Path(__file__).parents[1]
    pipeline = json.loads((root / "resource/pipeline/realtime_multi_live.json").read_text(encoding="utf-8"))
    interface = json.loads((root / "interface.json").read_text(encoding="utf-8"))
    case = next(item for item in interface["option"]["RealtimeLiveDifficulty"]["cases"] if item["name"] == difficulty)
    flags = {
        "RealtimeLiveDifficulty": "defer_song_title_to_preparation",
        "RealtimeLiveFormalSettingsGate": "confirm_preparation_identity",
        "RealtimeLiveRehearsalSettingsGate": "confirm_preparation_identity",
        "RealtimeLiveVisualEvaluationSettingsGate": "confirm_preparation_identity",
    }
    # AgentServer 导入会替换当前进程的绑定，使用独立进程加载实际 MaaFramework。
    code = """
import json, sys
from maa.resource import Resource
from maa.toolkit import Toolkit
data = json.load(sys.stdin)
Toolkit.init_option(data['log_dir'])
resource = Resource()
assert resource.override_pipeline(data['base'])
assert resource.override_pipeline(data['override'])
print(json.dumps({node: resource.get_node_data(node)['action']['param']['custom_action_param'] for node in data['base']}))
"""
    result = subprocess.run([sys.executable, "-c", code], input=json.dumps({
        "log_dir": str(tmp_path), "base": {node: pipeline[node] for node in flags},
        "override": {node: case["pipeline_override"][node] for node in flags},
    }), text=True, capture_output=True, check=True)
    effective = json.loads(result.stdout)
    for node, flag in flags.items():
        params = effective[node]
        assert params["difficulty"] == difficulty
        assert params.get(flag) is True, f"{difficulty}: {node} lost {flag}"
