from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime import difficulty_action, native_prearm, performance_settings_action, profile_play_action
from agent.realtime.calibration_action import calibration_round_plan
from agent.realtime.live_session import current_live_run, update_live_run
from agent.realtime.native_play import resolve_native_start_gate_policy


ROOT = Path(__file__).parents[1]


def test_single_mode_completion_preserves_native_start_gate_policy():
    assert resolve_native_start_gate_policy("formal") == resolve_native_start_gate_policy("realtime")


@pytest.fixture(scope="module")
def routes(tmp_path_factory):
    pipeline = json.loads((ROOT / "resource/pipeline/realtime_multi_live.json").read_text(encoding="utf-8"))
    interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))
    cases = interface["option"]["RealtimeLiveDifficulty"]["cases"]
    # 用真实 MaaFramework 验证整块替换后的参数，避免测试误把字典递归合并。
    code = """
import json, sys
from maa.resource import Resource
from maa.toolkit import Toolkit
data = json.load(sys.stdin)
Toolkit.init_option(data['log_dir'])
resource = Resource()
assert resource.override_pipeline(data['base'])
result = {}
for name, override in data['overrides'].items():
    assert resource.override_pipeline(override)
    result[name] = {node: resource.get_node_data(node)['action']['param']['custom_action_param']
                   for node in data['nodes']}
print(json.dumps(result))
"""
    nodes = ["RealtimeLiveDifficulty", "RealtimeLiveFormalSettingsGate", "RealtimeLiveRehearsalSettingsGate"]
    overrides = {case["name"]: case["pipeline_override"] for case in cases}
    for formal in (False, True):
        _, override = calibration_round_plan(
            difficulty="Expert", note_speed=5.0, calibration_debug=False, formal=formal,
            play_node="RealtimeLivePlayExpert", offset=0, report_path=Path("mock-report.json"),
        )
        overrides["calibration-formal" if formal else "calibration-rehearsal"] = override
    result = subprocess.run(
        [sys.executable, "-c", code], text=True, capture_output=True, check=True,
        input=json.dumps({"base": pipeline, "overrides": overrides,
                          "nodes": nodes, "log_dir": str(tmp_path_factory.mktemp("maafw-route"))}),
    )
    effective = json.loads(result.stdout)
    assert pipeline["RealtimeLiveFormalSettingsGate"]["custom_action_param"]["run_mode"] == "formal"
    for difficulty, route in effective.items():
        if difficulty.startswith("calibration-"):
            assert route["RealtimeLiveDifficulty"]["mode"] == difficulty
            assert "run_mode" not in route["RealtimeLiveFormalSettingsGate"]
            assert "run_mode" not in route["RealtimeLiveRehearsalSettingsGate"]
            continue
        assert route["RealtimeLiveFormalSettingsGate"]["run_mode"] == "formal"
        assert route["RealtimeLiveRehearsalSettingsGate"]["run_mode"] == "rehearsal"
        assert route["RealtimeLiveDifficulty"]["difficulty"] == difficulty
    return pipeline, effective


class Job:
    def wait(self):
        return self

    def get(self):
        return np.zeros((720, 1280, 3), dtype=np.uint8)


class Controller:
    info = {"adb_path": "mock-adb", "adb_serial": "mock-device"}

    def __init__(self):
        self.clicks = []

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return Job()

    def post_screencap(self):
        return Job()


class Backend:
    def __init__(self, options):
        self.options = options
        self.offsets = []

    def arm(self):
        pass

    def wait_until_ready(self, timeout):
        return True

    def configure_timing_offset(self, offset):
        self.offsets.append(offset)

    def stop(self):
        pass


def setup_route(monkeypatch, tmp_path, selection_params, options):
    performance_settings_action.clear_verified_settings()
    controller = Controller()
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=controller))
    difficulty = selection_params["difficulty"]
    selection = SimpleNamespace(path=tmp_path / "chart.json", timeline=None,
                                bestdori_song_id=48, difficulty=difficulty.casefold(), level=26)
    resolution = SimpleNamespace(selection=selection, reason="matched")
    settings = SimpleNamespace(target_fps=60, timing_offset_ms=17, note_speed=5.0,
                               profile_path=Path("accepted.json"))
    current_options = dict(options, note_speed_settings_enabled=False)
    backends = []
    manager = native_prearm.NativePrearmManager(timer_factory=lambda *_: SimpleNamespace(
        start=lambda: None, cancel=lambda: None))
    monkeypatch.setattr(difficulty_action, "require_game_foreground", lambda _: None)
    monkeypatch.setattr(difficulty_action.time, "sleep", lambda _: None)
    monkeypatch.setattr(difficulty_action, "selected_difficulty", lambda *_: difficulty)
    monkeypatch.setattr(difficulty_action, "identify_song", lambda _: SimpleNamespace(song_id="song-48", method="mock"))
    monkeypatch.setattr(difficulty_action, "read_song_level", lambda *_: 26)
    monkeypatch.setattr(difficulty_action, "recognize_song_title", lambda *_: SimpleNamespace(text="test song", confidence=.99))
    monkeypatch.setattr(difficulty_action, "resolve_chart_for_selected_song", lambda *_: resolution)
    monkeypatch.setattr("agent.realtime.preparation_identity.confirm_preparation_identity", lambda *_: None)
    monkeypatch.setattr(performance_settings_action, "_expected_speed", lambda *_: (5.0, "accepted.json"))
    monkeypatch.setattr(performance_settings_action, "require_special_chart_for_settings_gate", lambda _: selection)
    monkeypatch.setattr(performance_settings_action.RealtimeProfileStore, "runtime_options", lambda _: current_options)
    monkeypatch.setattr(performance_settings_action, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(profile_play_action, "PROJECT_ROOT", tmp_path)

    def prepare(**kwargs):
        return native_prearm.prepare_native_for_settings_gate(
            **kwargs, manager=manager, repository=SimpleNamespace(resolve=lambda *_args, **_kwargs: resolution),
            backend_factory=lambda *_args, **values: backends.append(Backend(values)) or backends[-1],
        )

    monkeypatch.setattr(performance_settings_action, "prepare_native_for_settings_gate", prepare)
    monkeypatch.setattr(profile_play_action, "consume_prearmed_backend", manager.consume)
    monkeypatch.setattr(profile_play_action, "resolve_local_chart_for_run", lambda *_args, **_kwargs: resolution)
    monkeypatch.setattr(profile_play_action, "wait_for_final_cover", lambda *_args, **_kwargs: SimpleNamespace(
        resolution=SimpleNamespace(selection=selection, confirmation=SimpleNamespace(
            song_id="song-48", song_id_method="mock"), observed_title=None)))
    monkeypatch.setattr(profile_play_action, "resolve_profile", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(profile_play_action, "require_game_foreground", lambda _: None)
    monkeypatch.setattr(profile_play_action, "debug_enabled", lambda: False)
    monkeypatch.setattr(profile_play_action, "diagnostic_trace_enabled", lambda: False)
    monkeypatch.setattr(profile_play_action, "ControllerTouchDispatcher", lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None))
    assert difficulty_action.RealtimeDifficultySelect().run(
        context, SimpleNamespace(custom_action_param=json.dumps(selection_params)))
    return SimpleNamespace(context=context, backends=backends, options=current_options,
                           prepare=prepare, controller=controller, selection=selection)


@pytest.mark.parametrize("difficulty", ["Easy", "Normal", "Hard", "Expert", "Special"])
@pytest.mark.parametrize("life,wait", [(False, False), (False, True), (True, False), (True, True)])
def test_formal_selection_prearm_play_uses_frozen_options(monkeypatch, tmp_path, routes, difficulty, life, wait):
    pipeline, effective = routes
    route = effective[difficulty]
    env = setup_route(monkeypatch, tmp_path, route["RealtimeLiveDifficulty"], {
        "native_realtime_enabled": True, "native_life_feedback_enabled": life,
        "native_wait_jitter_filter_enabled": wait,
    })
    selected_run = current_live_run()
    assert selected_run.mode == "realtime"
    assert performance_settings_action.RealtimePerformanceSettingsGate()._run(
        env.context, route["RealtimeLiveFormalSettingsGate"])
    assert current_live_run().run_id == selected_run.run_id
    assert current_live_run().mode == "formal"
    backend = env.backends[0]
    assert backend.options["life_feedback_enabled"] is life
    assert backend.options["wait_jitter_trial_enabled"] is wait
    env.options.update(native_realtime_enabled=False, native_life_feedback_enabled=not life,
                       native_wait_jitter_filter_enabled=not wait)
    engine_backends = []

    def engine(*_args, **kwargs):
        engine_backends.append(kwargs["native_backend"])
        raise RuntimeError("mock setup complete")

    monkeypatch.setattr(profile_play_action, "RealtimeEngine", engine)
    play_node = "RealtimeLiveFormalPlay" + ("" if difficulty == "Easy" else difficulty)
    with pytest.raises(RuntimeError, match="mock setup complete"):
        profile_play_action.RealtimeProfilePlay()._run(env.context, SimpleNamespace(
            custom_action_param=json.dumps(pipeline[play_node]["custom_action_param"])))
    assert engine_backends == [backend]
    assert backend.offsets == [17]
    assert env.controller.clicks == [difficulty_action.DIFFICULTY_TARGETS[difficulty]]


@pytest.mark.parametrize("options,expected", [
    ({"native_realtime_enabled": True}, False),
    ({"native_realtime_enabled": True, "native_life_feedback_enabled": False}, False),
    ({"native_realtime_enabled": False, "native_life_feedback_enabled": True}, None),
])
def test_old_disabled_and_legacy_formal_routes(monkeypatch, tmp_path, routes, options, expected):
    _, effective = routes
    route = effective["Expert"]
    env = setup_route(monkeypatch, tmp_path, route["RealtimeLiveDifficulty"], options)
    assert performance_settings_action.RealtimePerformanceSettingsGate()._run(env.context, route["RealtimeLiveFormalSettingsGate"])
    if expected is None:
        assert env.backends == []
    else:
        assert env.backends[0].options["life_feedback_enabled"] is expected
    assert env.controller.clicks == [difficulty_action.DIFFICULTY_TARGETS["Expert"]]


@pytest.mark.parametrize("mode", ["rehearsal", "calibration-rehearsal", "calibration-formal", "unknown"])
def test_nonformal_mode_is_not_upgraded_by_formal_gate(monkeypatch, tmp_path, routes, mode):
    _, effective = routes
    route = effective["Expert"]
    params = dict(route["RealtimeLiveDifficulty"])
    if mode != "rehearsal":
        params["mode"] = mode
    env = setup_route(monkeypatch, tmp_path, params, {
        "native_realtime_enabled": True, "native_life_feedback_enabled": True,
    })
    gate = route["RealtimeLiveRehearsalSettingsGate"] if mode == "rehearsal" else route["RealtimeLiveFormalSettingsGate"]
    assert performance_settings_action.RealtimePerformanceSettingsGate()._run(env.context, gate)
    assert current_live_run().mode == mode
    assert env.backends[0].options["life_feedback_enabled"] is False


@pytest.mark.parametrize("mode", ["calibration-rehearsal", "calibration-formal"])
def test_actual_calibration_override_keeps_feedback_disabled(monkeypatch, tmp_path, routes, mode):
    _, effective = routes
    route = effective[mode]
    env = setup_route(monkeypatch, tmp_path, route["RealtimeLiveDifficulty"], {
        "native_realtime_enabled": True, "native_life_feedback_enabled": True,
    })
    assert performance_settings_action.RealtimePerformanceSettingsGate()._run(
        env.context, route["RealtimeLiveFormalSettingsGate"])
    assert current_live_run().mode == mode
    assert env.backends[0].options["life_feedback_enabled"] is False


def test_invalid_explicit_mode_does_not_enable_feedback(monkeypatch, tmp_path, routes):
    _, effective = routes
    route = effective["Expert"]
    env = setup_route(monkeypatch, tmp_path, route["RealtimeLiveDifficulty"], {
        "native_realtime_enabled": True, "native_life_feedback_enabled": True,
    })
    assert performance_settings_action.RealtimePerformanceSettingsGate()._run(
        env.context, dict(route["RealtimeLiveFormalSettingsGate"], run_mode="unknown"))
    assert current_live_run().mode == "realtime"
    assert env.backends[0].options["life_feedback_enabled"] is False


@pytest.mark.parametrize("life", [False, True])
def test_deferred_and_rebuilt_prearm_keeps_formal_frozen_options(monkeypatch, tmp_path, routes, life):
    _, effective = routes
    route = effective["Expert"]
    env = setup_route(monkeypatch, tmp_path, route["RealtimeLiveDifficulty"], {
        "native_realtime_enabled": True, "native_life_feedback_enabled": life,
        "native_wait_jitter_filter_enabled": True,
    })
    update_live_run(preparation_identity_pending_final_cover=True)
    monkeypatch.setattr(performance_settings_action, "discard_prearmed_backend", lambda _: None)
    assert performance_settings_action.RealtimePerformanceSettingsGate()._run(env.context, route["RealtimeLiveFormalSettingsGate"])
    assert env.backends == []
    assert current_live_run().mode == "formal"
    env.options.update(native_life_feedback_enabled=not life, native_wait_jitter_filter_enabled=False)
    update_live_run(preparation_identity_pending_final_cover=False)
    for _ in range(2):
        env.prepare(controller=env.controller, live_run=current_live_run(), difficulty="Expert",
                    project_root=tmp_path, runtime_options=env.options)
    assert [backend.options["life_feedback_enabled"] for backend in env.backends] == [life, life]
    assert all(backend.options["wait_jitter_trial_enabled"] for backend in env.backends)
