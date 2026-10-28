from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from agent.realtime.engine import EngineStats
from agent.realtime import profile_play_action
from agent.realtime.profile_play_action import (
    RealtimeProfilePlay,
    ResultCollectionOutcome,
    ResultCollectionStatus,
    _dismiss_reward_popup,
    _effective_native_chart_selection,
    _result_report_payload,
    _write_calibration_report,
    finalize_deferred_result,
    _recording_kind,
    collect_result,
    resolve_life_monitor_enabled,
    resolve_life_policy,
)
from agent.realtime.result_parser import LiveResult
from agent.realtime.performance_settings_action import clear_verified_settings
from agent.realtime.live_session import (
    current_live_run,
    reset_live_run,
    update_live_run,
)


def test_recording_kind_distinguishes_play_types():
    assert _recording_kind("cooperative") == "coop"
    assert _recording_kind("formal") == "single-formal"
    assert _recording_kind("rehearsal") == "single-rehearsal"
    assert _recording_kind("challenge") == "challenge"
    assert _recording_kind("calibration-rehearsal") == "calibration-rehearsal"
    assert _recording_kind("continuous") == "continuous"
    assert _recording_kind("medley") == "medley"
    assert _recording_kind("unknown-mode") == "unknown-mode"


class Job:
    def wait(self):
        return self

    def get(self):
        return np.zeros((720, 1280, 3), dtype=np.uint8)


class Controller:
    def post_screencap(self):
        return Job()


def _chart_selection(
    path: str,
    *,
    song_id: int = 55,
    difficulty: str = "expert",
    level: int = 27,
) -> SimpleNamespace:
    return SimpleNamespace(
        path=Path(path),
        timeline=object(),
        bestdori_song_id=song_id,
        difficulty=difficulty,
        level=level,
    )


def test_effective_native_chart_accepts_jittered_variant_of_same_song():
    selected = _chart_selection("resource/charts/bestdori/55/expert.json")
    prepared = _chart_selection("debug/jittered-charts/run-1.json")

    effective = _effective_native_chart_selection(selected, prepared)

    assert effective is prepared


def test_effective_native_chart_keeps_selected_when_paths_match():
    selected = _chart_selection("resource/charts/bestdori/55/expert.json")
    prepared = _chart_selection("resource/charts/bestdori/55/expert.json")

    effective = _effective_native_chart_selection(selected, prepared)

    assert effective is selected


def test_effective_native_chart_rejects_different_song():
    selected = _chart_selection("resource/charts/bestdori/55/expert.json")
    prepared = _chart_selection(
        "debug/jittered-charts/run-1.json",
        song_id=56,
    )

    with pytest.raises(RuntimeError, match="预武装谱面不一致"):
        _effective_native_chart_selection(selected, prepared)


def test_effective_native_chart_rejects_missing_prepared():
    selected = _chart_selection("resource/charts/bestdori/55/expert.json")

    with pytest.raises(RuntimeError, match="预武装谱面不一致"):
        _effective_native_chart_selection(selected, None)


class Tasker:
    stopping = False

    def __init__(self):
        self.controller_reads = 0
        self._controller = Controller()

    @property
    def controller(self):
        self.controller_reads += 1
        return self._controller


def test_native_execution_gate_requires_complete_lossless_evidence():
    valid_report = {
        "planned": 637,
        "sent": 637,
        "executed": 637,
        "underflows": 0,
        "executed_observation_complete": True,
        "state": "finished",
        "session_state": "finished",
        "release_confirmed": True,
        "stop_latency_ms": 120.0,
    }
    assert profile_play_action._native_execution_gate_failures(valid_report) == []

    failures = profile_play_action._native_execution_gate_failures({
        **valid_report,
        "planned": 637,
        "sent": 637,
        "executed": 636,
        "underflows": 1,
        "executed_observation_complete": False,
        "executed_observation_reason": "pending=3",
    })
    assert any("637/637/636" in failure for failure in failures)
    assert any("pending=3" in failure for failure in failures)
    assert "queue_underflows=1" in failures

    failed_terminal = profile_play_action._native_execution_gate_failures({
        **valid_report,
        "state": "failed",
        "publisher_error": "late owner failure",
    })
    assert any("terminal_state=failed" in failure for failure in failed_terminal)
    assert "publisher_error=late owner failure" in failed_terminal

    missing_release = profile_play_action._native_execution_gate_failures({
        key: value
        for key, value in valid_report.items()
        if key != "release_confirmed"
    })
    assert "release_confirmed=false" in missing_release

    over_budget = profile_play_action._native_execution_gate_failures({
        **valid_report,
        "stop_latency_ms": float("nan"),
    })
    assert any("stop_latency_ms=nan" in failure for failure in over_budget)


def test_native_execution_gate_accepts_only_proven_jump_cancellation():
    cancelled = {
        "planned": 3739,
        "sent": 2636,
        "executed": 2618,
        "underflows": 0,
        "executed_observation_complete": False,
        "executed_observation_reason": "会话在完整设备回读前取消",
        "state": "cancelled",
        "session_state": "cancelled",
        "reset_executed": True,
        "release_confirmed": True,
        "stop_latency_ms": 902.0,
        "game_terminal_reason": "演出失败：生命值归零",
    }
    assert profile_play_action._native_execution_gate_failures(
        cancelled,
        expected_jump_cancel=True,
    ) == []

    # 同样的取消状态，但没有游戏终态理由时仍必须报技术失败。
    without_terminal = {
        key: value
        for key, value in cancelled.items()
        if key != "game_terminal_reason"
    }
    failures = profile_play_action._native_execution_gate_failures(
        without_terminal
    )
    assert any("terminal_state=cancelled" in item for item in failures)
    assert any("3739/2636/2618" in item for item in failures)
    assert any("device evidence incomplete" in item for item in failures)

    for field, value in (
        ("state", "cancelling"),
        ("session_state", "cancelling"),
        ("reset_executed", False),
        ("release_confirmed", False),
        ("underflows", 1),
        ("executed", 2640),
        ("sent", 3800),
        ("publish_error", "late publish"),
        ("stop_latency_ms", 1000.001),
    ):
        broken = {**cancelled, field: value}
        assert profile_play_action._native_execution_gate_failures(
            broken,
            expected_jump_cancel=True,
        ), field


def test_profile_play_refreshes_proxy_at_safety_boundaries(monkeypatch):
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    # 本测试只验证 Controller 代理生命周期，不能读取用户当前的 Native 开关。
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.runtime_options",
        lambda _self: {
            "chart_prediction_enabled": False,
            "chart_predict_presses": False,
            "native_realtime_enabled": False,
        },
    )
    foreground_checks = []
    dispatcher_options = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda controller: foreground_checks.append(controller),
    )

    class Dispatcher:
        def __init__(self, controller, stopping, **kwargs):
            dispatcher_options.append(kwargs)

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        Dispatcher,
    )

    engine_options = []
    engine_construction_options = []

    class Engine:
        def __init__(self, *args, **kwargs):
            engine_construction_options.append(kwargs)

        def run(self, capture, stopping, **kwargs):
            engine_options.append(kwargs)
            capture()
            return EngineStats(1, 0, False)

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)

    argv = SimpleNamespace(custom_action_param=json.dumps({"difficulty": "Easy"}))
    assert RealtimeProfilePlay()._run(context, argv)
    assert 3 <= tasker.controller_reads <= 5
    assert foreground_checks == [tasker._controller]
    assert dispatcher_options == [{}]
    assert engine_options[0]["startup_timeout_seconds"] == 60.0
    assert engine_construction_options[0]["life_detector"] is not None
    assert engine_construction_options[0]["life_guard"] is not None


def test_profile_play_uses_confirmed_expert_for_special_fallback(monkeypatch, tmp_path):
    # 故意中断会保存失败报告，测试证据必须与真实演出目录隔离。
    monkeypatch.setattr(profile_play_action, "PROJECT_ROOT", tmp_path)
    reset_live_run(
        mode="formal",
        difficulty="Expert",
        requested_difficulty="Special",
        prepared_for_play=True,
    )
    verified_calls = []
    resolved_params = []
    monkeypatch.setattr(
        profile_play_action,
        "verified_settings",
        lambda difficulty: verified_calls.append(difficulty),
    )

    def stop_after_resolution(context, params, *, controller=None):
        resolved_params.append(dict(params))
        raise RuntimeError("stop after effective difficulty resolution")

    monkeypatch.setattr(
        profile_play_action,
        "resolve_profile",
        stop_after_resolution,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=object()),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Special",
        "require_profile": True,
    }))

    with pytest.raises(
        RuntimeError,
        match="stop after effective difficulty resolution",
    ):
        RealtimeProfilePlay()._run(context, argv)

    assert verified_calls == ["Expert"]
    assert resolved_params[0]["difficulty"] == "Expert"
    run = current_live_run()
    assert run is not None
    assert run.requested_difficulty == "Special"
    assert run.difficulty == "Expert"


def test_explicit_native_initialization_failure_never_falls_back(
    monkeypatch,
):
    reset_live_run(
        mode="formal",
        difficulty="Expert",
        prepared_for_play=True,
    )
    tasker = Tasker()
    tasker._controller.info = {
        "adb_path": "C:/tools/adb.exe",
        "adb_serial": "test-device",
    }
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=17,
        note_speed=10.0,
        profile_path=SimpleNamespace(name="expert.json"),
    )
    selection = SimpleNamespace(
        path=Path("chart-48-expert.json"),
        timeline=object(),
        bestdori_song_id=48,
        difficulty="expert",
    )

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.runtime_options",
        lambda *args, **kwargs: {
            # Native 必须独立解析已确认谱面，不能依赖 Legacy 谱面预测开关。
            "chart_prediction_enabled": False,
            "chart_predict_presses": False,
            "native_realtime_enabled": True,
        },
    )
    resolution_calls = []

    def resolve_chart(*args, **kwargs):
        resolution_calls.append((args, kwargs))
        return SimpleNamespace(selection=selection, reason="matched")

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.resolve_local_chart_for_run",
        resolve_chart,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.debug_recording_enabled",
        lambda _argv: False,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.diagnostic_trace_enabled",
        lambda: False,
    )

    class Dispatcher:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    class ForbiddenLateNativeBackend:
        def __init__(self, *args, **kwargs):
            raise AssertionError("ProfilePlay 不得在 Start 后构造 Native")

    consume_calls = []

    def consume_prearmed(run_id, chart_path):
        consume_calls.append((run_id, chart_path))
        raise RuntimeError("simulated missing prearm")

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        Dispatcher,
    )
    monkeypatch.setattr(
        "agent.realtime.native_play.NativeMinitouchBackend",
        ForbiddenLateNativeBackend,
    )
    monkeypatch.setattr(
        profile_play_action,
        "consume_prearmed_backend",
        consume_prearmed,
        raising=False,
    )

    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
    }))
    with pytest.raises(RuntimeError, match="预武装.*禁止回退 Legacy"):
        RealtimeProfilePlay()._run(context, argv)
    assert len(resolution_calls) == 1
    assert len(consume_calls) == 1
    assert consume_calls[0][1] == selection.path


def test_profile_native_consumes_and_configures_prearmed_backend(monkeypatch):
    prepared_run = reset_live_run(
        mode="formal",
        difficulty="Expert",
        prepared_for_play=True,
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=17,
        note_speed=10.0,
        profile_path=SimpleNamespace(name="expert.json"),
    )
    selection = SimpleNamespace(
        path=Path("chart-48-expert.json"),
        timeline=object(),
        bestdori_song_id=48,
        difficulty="expert",
    )
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "runtime_options",
        lambda *args, **kwargs: {
            "chart_prediction_enabled": False,
            "chart_predict_presses": False,
            "native_realtime_enabled": True,
        },
    )
    monkeypatch.setattr(
        profile_play_action,
        "resolve_local_chart_for_run",
        lambda *args, **kwargs: SimpleNamespace(
            selection=selection,
            reason="matched",
        ),
    )
    monkeypatch.setattr(
        profile_play_action,
        "require_game_foreground",
        lambda controller: None,
    )
    monkeypatch.setattr(profile_play_action, "debug_recording_enabled", lambda _argv: False)
    monkeypatch.setattr(
        profile_play_action,
        "diagnostic_trace_enabled",
        lambda: False,
    )

    class Dispatcher:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    class PrearmedBackend:
        def __init__(self):
            self.offsets = []
            self.stop_calls = 0

        def configure_timing_offset(self, value):
            self.offsets.append(value)

        def stop(self):
            self.stop_calls += 1

    backend = PrearmedBackend()
    consume_calls = []

    def consume(run_id, chart_path):
        consume_calls.append((run_id, chart_path))
        return backend

    engine_backends = []

    class StopAfterSetupEngine:
        def __init__(self, *args, **kwargs):
            engine_backends.append(kwargs["native_backend"])
            raise RuntimeError("stop after native setup")

    monkeypatch.setattr(
        profile_play_action,
        "ControllerTouchDispatcher",
        Dispatcher,
    )
    monkeypatch.setattr(
        profile_play_action,
        "consume_prearmed_backend",
        consume,
    )
    monkeypatch.setattr(
        profile_play_action,
        "RealtimeEngine",
        StopAfterSetupEngine,
    )

    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
    }))
    with pytest.raises(RuntimeError, match="stop after native setup"):
        RealtimeProfilePlay()._run(context, argv)

    assert consume_calls == [(prepared_run.run_id, selection.path)]
    assert backend.offsets == [17]
    assert engine_backends == [backend]
    assert backend.stop_calls == 1


def test_profile_jump_cancellation_reaches_disconnect_branch(monkeypatch):
    prepared_run = reset_live_run(
        mode="cooperative",
        difficulty="Expert",
        prepared_for_play=True,
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=17,
        note_speed=10.0,
        profile_path=SimpleNamespace(name="expert.json"),
    )
    selection = SimpleNamespace(
        path=Path("chart-48-expert.json"),
        timeline=None,
        bestdori_song_id=48,
        difficulty="expert",
    )
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "runtime_options",
        lambda *args, **kwargs: {
            "chart_prediction_enabled": False,
            "chart_predict_presses": False,
            "native_realtime_enabled": True,
        },
    )
    monkeypatch.setattr(
        profile_play_action,
        "resolve_local_chart_for_run",
        lambda *args, **kwargs: SimpleNamespace(
            selection=selection,
            reason="matched",
        ),
    )
    monkeypatch.setattr(
        profile_play_action, "require_game_foreground", lambda _controller: None
    )
    monkeypatch.setattr(profile_play_action, "debug_recording_enabled", lambda _argv: False)
    monkeypatch.setattr(
        profile_play_action, "diagnostic_trace_enabled", lambda: False
    )

    class Dispatcher:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        profile_play_action, "ControllerTouchDispatcher", Dispatcher
    )

    class Backend:
        def configure_timing_offset(self, value):
            assert value == 17

    monkeypatch.setattr(
        profile_play_action,
        "consume_prearmed_backend",
        lambda run_id, chart_path: Backend(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            assert kwargs["native_backend"] is not None

        def run(self, _capture, _stopping, **kwargs):
            kwargs["on_life_depleted"](SimpleNamespace(value=0))
            return EngineStats(
                72,
                309,
                False,
                life_depleted=True,
                jump_requested=True,
                completed=False,
                terminal_reason="生命归零，请手动跳车",
                engine_mode="native",
                native_report={
                    "planned": 2764,
                    "sent": 309,
                    "executed": 282,
                    "underflows": 0,
                    "executed_observation_complete": False,
                    "executed_observation_reason": "会话在完整设备回读前取消",
                    "state": "cancelled",
                    "session_state": "cancelled",
                    "reset_executed": True,
                    "release_confirmed": True,
                    "stop_latency_ms": 902.0,
                },
            )

    monkeypatch.setattr(profile_play_action, "RealtimeEngine", Engine)

    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
        "run_mode": "cooperative",
        "life_depleted_jump_request": True,
        "confirm_final_cover": False,
    }))
    assert RealtimeProfilePlay()._run(context, argv) is True
    assert profile_play_action.current_live_run().disconnect_jump_requested is True
    assert profile_play_action.current_live_run().run_id == prepared_run.run_id


def test_profile_falls_back_to_legacy_without_reliable_native_chart(
    monkeypatch,
):
    prepared_run = reset_live_run(
        mode="formal",
        difficulty="Easy",
        prepared_for_play=True,
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=17,
        note_speed=2.0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "runtime_options",
        lambda *args, **kwargs: {
            "chart_prediction_enabled": True,
            "chart_predict_presses": True,
            "native_realtime_enabled": True,
        },
    )
    monkeypatch.setattr(
        profile_play_action,
        "resolve_local_chart_for_run",
        lambda *args, **kwargs: SimpleNamespace(
            selection=None,
            reason="selected song level does not match local chart metadata",
        ),
    )
    monkeypatch.setattr(
        profile_play_action,
        "require_game_foreground",
        lambda controller: None,
    )
    monkeypatch.setattr(profile_play_action, "debug_recording_enabled", lambda _argv: False)
    monkeypatch.setattr(
        profile_play_action,
        "diagnostic_trace_enabled",
        lambda: False,
    )

    class Dispatcher:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    consume_calls = []

    def consume(run_id, chart_path):
        consume_calls.append((run_id, chart_path))
        raise AssertionError("无可靠谱面时不得消费 Native 预武装")

    engine_backends = []

    class StopAfterSetupEngine:
        def __init__(self, *args, **kwargs):
            engine_backends.append(kwargs["native_backend"])
            raise RuntimeError("stop after legacy setup")

    monkeypatch.setattr(
        profile_play_action,
        "ControllerTouchDispatcher",
        Dispatcher,
    )
    monkeypatch.setattr(
        profile_play_action,
        "consume_prearmed_backend",
        consume,
    )
    monkeypatch.setattr(
        profile_play_action,
        "RealtimeEngine",
        StopAfterSetupEngine,
    )

    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
    }))
    with pytest.raises(RuntimeError, match="stop after legacy setup"):
        RealtimeProfilePlay()._run(context, argv)

    assert consume_calls == []
    assert engine_backends == [None]


def test_pending_preparation_identity_requires_independent_final_cover(
    monkeypatch, tmp_path,
):
    reset_live_run(
        mode="formal", difficulty="Expert", prepared_for_play=True,
    )
    update_live_run(
        song_id="selected-jacket",
        song_level=27,
        song_title="可信准备页标题",
        song_title_confidence=0.95,
        preparation_identity_pending_final_cover=True,
    )
    monkeypatch.setattr(profile_play_action, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "runtime_options",
        lambda *args, **kwargs: {
            "chart_prediction_enabled": False,
            "chart_predict_presses": False,
            "native_realtime_enabled": False,
        },
    )
    selected = _chart_selection("resource/charts/bestdori/55/expert.json")
    monkeypatch.setattr(
        profile_play_action,
        "resolve_local_chart_for_run",
        lambda *args, **kwargs: SimpleNamespace(
            selection=selected, reason="selected chart",
        ),
    )
    captured = {}

    def final_cover_failed(*args, **kwargs):
        captured["selection"] = args[2]
        captured["repository"] = kwargs["repository"]
        captured["require_title"] = kwargs["require_observed_title"]
        captured["ignore_level"] = kwargs["ignore_preparation_level"]
        return profile_play_action.FinalCoverWaitOutcome(
            status="timeout", resolution=None, reason="cover missing",
            frames=1, playfield_seen=True,
        )

    monkeypatch.setattr(
        profile_play_action, "wait_for_final_cover", final_cover_failed,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=Controller()),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert", "require_profile": False,
        "confirm_final_cover": False,
    }))

    with pytest.raises(RuntimeError, match="最终封面未确认准备页延迟的歌曲身份"):
        RealtimeProfilePlay()._run(context, argv)

    assert captured["selection"] is None
    assert captured["repository"] is not None
    assert captured["require_title"] is True
    assert captured["ignore_level"] is True


@pytest.mark.parametrize("replace_run", [False, True])
def test_final_cover_error_recorder_refreshes_only_same_run_metadata(monkeypatch, tmp_path, replace_run):
    reset_live_run(mode="formal", difficulty="Expert", prepared_for_play=True)
    original_run = update_live_run(song_level=27, preparation_identity_pending_final_cover=True)
    monkeypatch.setattr(profile_play_action, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(profile_play_action.RealtimeProfileStore, "runtime_options", lambda *_args, **_kwargs: {})
    selected = _chart_selection("resource/charts/bestdori/55/expert.json")
    monkeypatch.setattr(profile_play_action, "resolve_local_chart_for_run", lambda *_args, **_kwargs: SimpleNamespace(
        selection=selected, reason="selected chart",
    ))
    metadata = []
    class Recorder:
        output_dir = tmp_path / "debug" / "recordings" / "test-final-cover"
        def __init__(self, *_args, **_kwargs):
            self.output_dir.mkdir(parents=True)
            self.session = None
        def set_session_metadata(self, value):
            self.session = value
        def update_session_metadata(self, value):
            self.session = value
        def close(self):
            metadata.append(self.session)
            (self.output_dir / "summary.json").write_text(json.dumps({"session": self.session}), encoding="utf-8")
    monkeypatch.setattr(profile_play_action, "RealtimeDebugRecorder", Recorder)
    def failed(*_args, **_kwargs):
        if replace_run:
            reset_live_run(mode="formal", difficulty="Expert")
        update_live_run(final_cover_status="error", final_cover_reason="RuntimeError: 最终封面身份冲突")
        raise RuntimeError("最终封面身份冲突")
    monkeypatch.setattr(profile_play_action, "wait_for_final_cover", failed)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=Controller()))
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert", "require_profile": False,
        "confirm_final_cover": True, "diagnostic_trace": True, "debug_recording": False,
        "run_mode": "formal", "rehearsal_mode": False,
    }))
    with pytest.raises(RuntimeError, match="最终封面身份冲突"):
        RealtimeProfilePlay()._run(context, argv)
    summary = metadata[-1]
    assert summary["run_id"] == original_run.run_id
    if replace_run:
        assert summary["final_cover"]["status"] == "not-observed"
        assert summary["final_cover"]["reason"] is None
    else:
        report_path = next((tmp_path / "screencap").glob("realtime-result-*.json"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert summary["final_cover"]["status"] == "error"
        assert summary["final_cover"] == report["session"]["final_cover"]


def test_explicit_native_requires_controller_adb_endpoint():
    with pytest.raises(RuntimeError, match="adb_path.*adb_serial"):
        profile_play_action._native_adb_endpoint(Controller())

    controller = Controller()
    controller.info = {
        "adb_path": "C:/tools/adb.exe",
        "adb_serial": "test-device",
    }
    assert profile_play_action._native_adb_endpoint(controller) == (
        "C:/tools/adb.exe",
        "test-device",
    )


@pytest.mark.parametrize("saved", [True, False])
@pytest.mark.parametrize("snapshot", [None, True, False])
def test_cooperative_play_forwards_guard_snapshot_to_final_cover(monkeypatch, tmp_path, saved, snapshot):
    from agent.realtime.profile_store import RealtimeProfileStore
    reset_live_run(mode="cooperative", difficulty="Expert", prepared_for_play=True)
    update_live_run(preparation_identity_pending_final_cover=True)
    monkeypatch.setattr(profile_play_action, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(profile_play_action, "resolve_local_chart_for_run", lambda *args: SimpleNamespace(
        selection=_chart_selection("resource/charts/bestdori/55/expert.json"), reason="selected chart",
    ))
    RealtimeProfileStore(tmp_path / "profiles").update_runtime_options({
        "cooperative_member_loading_guard_enabled": saved,
        "chart_prediction_enabled": False,
        "chart_predict_presses": False,
        "native_realtime_enabled": False,
    })
    captured = []

    def stop_at_cover(*args, **kwargs):
        captured.append(kwargs["cooperative_member_loading_guard_enabled"])
        raise RuntimeError("stop at final cover")

    monkeypatch.setattr(profile_play_action, "wait_for_final_cover", stop_at_cover)
    params = {"difficulty": "Expert", "require_profile": False, "run_mode": "cooperative"}
    if snapshot is not None:
        params["cooperative_member_loading_guard_enabled"] = snapshot
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=Controller()))
    with pytest.raises(RuntimeError, match="stop at final cover"):
        RealtimeProfilePlay()._run(context, SimpleNamespace(custom_action_param=json.dumps(params)))
    assert captured == [saved if snapshot is None else snapshot]


def test_native_completion_guard_keeps_frame_threshold_time_equivalent():
    assert profile_play_action._completion_missing_frames(
        120,
        native=True,
        target_fps=60,
    ) == 10
    assert profile_play_action._completion_missing_frames(
        30,
        native=True,
        target_fps=60,
    ) == 3
    assert profile_play_action._completion_missing_frames(
        120,
        native=False,
        target_fps=60,
    ) == 120


def test_profile_play_refuses_pipeline_start_without_fresh_speed_gate(
    monkeypatch, tmp_path,
):
    clear_verified_settings()
    reset_live_run(
        mode="pending",
        difficulty="Easy",
        prepared_for_play=True,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=Controller()),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "require_profile": False,
        "settings_gate_required": True,
    }))

    with pytest.raises(RuntimeError, match="尚未实际验证游戏流速"):
        RealtimeProfilePlay()._run(context, argv)

    report = next((tmp_path / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "preflight_error"
    assert payload["terminal_stage"] == "profile_play_preflight"


def test_profile_play_stop_during_preflight_is_neutral_and_writes_nothing(
    monkeypatch, tmp_path,
):
    reset_live_run(
        mode="pending",
        difficulty="Easy",
        prepared_for_play=True,
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)

    def stop_while_reading_settings(_difficulty):
        tasker.stopping = True
        raise InterruptedError("settings read cancelled")

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.verified_settings",
        stop_while_reading_settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda root, **kwargs: recorder_constructions.append(root),
    )
    failure_reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        failure_reasons.append,
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "settings_gate_required": True,
        "debug_recording": True,
    }))

    assert RealtimeProfilePlay().run(context, argv) is True
    assert recorder_constructions == []
    assert failure_reasons == []
    assert not list(tmp_path.rglob("realtime-result-*.json"))


def test_rehearsal_life_policy_can_ignore_depletion():
    policy = resolve_life_policy(
        {"require_profile": False, "rehearsal_mode": True},
    )

    assert policy == (True, True)


def test_numeric_life_monitor_defaults_on_for_terminal_detection():
    assert resolve_life_monitor_enabled({}) is True
    assert resolve_life_monitor_enabled({"monitor_life": False}) is False


def test_rehearsal_life_policy_can_explicitly_stop_after_depletion():
    policy = resolve_life_policy(
        {
            "require_profile": False,
            "rehearsal_mode": True,
            "continue_after_life_depleted": False,
        },
    )

    assert policy == (True, False)


def test_formal_calibration_round_stops_only_after_life_is_depleted():
    policy = resolve_life_policy(
        {"require_profile": False, "rehearsal_mode": False},
    )

    assert policy == (False, False)


def test_calibration_report_contains_replay_diagnostics(tmp_path):
    report = tmp_path / "round.json"
    stats = EngineStats(
        100,
        20,
        False,
        completed=True,
        timing_feedback_fast=2,
        timing_feedback_slow=7,
        initial_timing_offset_ms=3,
        final_timing_offset_ms=5,
        timing_feedback_valid=9,
        timing_feedback_ignored=4,
        timing_feedback_ignored_reasons={"active_hold": 4},
        filtered_adjacent_artifacts=7,
        rejected_hold_candidates=2,
    )

    _write_calibration_report(
        report,
        result=LiveResult(90, 5, 2, 1, 2, 2, 7),
        stats=stats,
        timing_offset_ms=5,
        song_id="song-a",
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["initial_timing_offset_ms"] == 3
    assert payload["timing_offset_ms"] == 5
    assert payload["realtime_feedback_ignored_reasons"] == {"active_hold": 4}
    assert payload["filtered_adjacent_artifacts"] == 7
    assert payload["rejected_hold_candidates"] == 2


def test_result_report_contains_runtime_acceptance_metrics():
    stats = EngineStats(
        120,
        42,
        False,
        completed=True,
        action_counts={"tap": 31, "flick": 4, "down": 7},
        frame_interval_p50_ms=16.4,
        frame_interval_p95_ms=18.2,
        frame_interval_max_ms=24.0,
        effective_fps=59.1,
        terminal_reason="completed",
        initial_timing_offset_ms=-11,
        final_timing_offset_ms=-13,
        life_monitor_diagnostics={
            "enabled": True,
            "life_depleted_reason": "zero-streak-not-confirmed",
        },
    )

    payload = _result_report_payload(
        LiveResult(100, 10, 2, 1, 2, 3, 4),
        stats,
        timing_offset_ms=-11,
        suggested_timing_offset_ms=-14,
    )

    assert payload["miss"] == 2
    assert payload["processed_frames"] == 120
    assert payload["action_counts"] == {"tap": 31, "flick": 4, "down": 7}
    assert payload["frame_interval_p95_ms"] == pytest.approx(18.2)
    assert payload["effective_fps"] == pytest.approx(59.1)
    assert payload["terminal_reason"] == "completed"
    assert payload["life_monitor_diagnostics"] == {
        "enabled": True,
        "life_depleted_reason": "zero-streak-not-confirmed",
    }


@pytest.mark.parametrize(
    ("run_mode", "expected_success", "records_failure"),
    [
        ("formal", False, True),
        ("calibration-rehearsal", True, False),
    ],
)
def test_incomplete_round_records_structured_result_and_calibration_can_retry(
    monkeypatch,
    tmp_path,
    run_mode,
    expected_success,
    records_failure,
):
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="normal.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            return EngineStats(
                100,
                20,
                False,
                completed=False,
                terminal_reason="演奏超过安全时限 600 秒，仍未识别到结算画面",
            )

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)
    reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        reasons.append,
    )

    params = {
        "difficulty": "Normal",
        "duration_seconds": 600,
        "require_completion": True,
        "wait_for_completion": True,
        "save_result_frame": True,
        "run_mode": run_mode,
    }
    if run_mode.startswith("calibration-"):
        params["calibration_report"] = "screencap/calibration-retry.json"
    argv = SimpleNamespace(custom_action_param=json.dumps(params))

    assert RealtimeProfilePlay()._run(context, argv) is expected_success
    assert reasons == (
        ["演奏超过安全时限 600 秒，仍未识别到结算画面"]
        if records_failure else []
    )
    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "engine_incomplete"
    assert payload["terminal_reason"] == (
        "演奏超过安全时限 600 秒，仍未识别到结算画面"
    )
    assert payload["mode"] == run_mode
    if run_mode.startswith("calibration-"):
        calibration = json.loads(
            (tmp_path / "screencap" / "calibration-retry.json").read_text(
                encoding="utf-8"
            )
        )
        assert calibration["valid"] is False
        assert calibration["mode"] == run_mode


def test_life_depleted_calibration_formal_round_reports_death_without_technical_retry(monkeypatch, tmp_path):
    reset_live_run(mode="calibration", difficulty="Hard")
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="hard.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            return EngineStats(
                3438,
                391,
                False,
                aborted_for_life=True,
                life_depleted=True,
                completed=False,
                terminal_reason="生命值触发安全停止",
            )

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)
    reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        reasons.append,
    )
    params = {
        "difficulty": "Hard",
        "duration_seconds": 600,
        "require_completion": True,
        "wait_for_completion": True,
        "save_result_frame": True,
        "run_mode": "calibration-formal",
        "calibration_report": "screencap/calibration-life-retry.json",
    }
    argv = SimpleNamespace(custom_action_param=json.dumps(params))

    monkeypatch.setattr(profile_play_action, "exit_failed_live", lambda _context: True)
    assert RealtimeProfilePlay()._run(context, argv) is False
    assert reasons == ["演出失败：生命值归零"]
    calibration = json.loads(
        (tmp_path / "screencap" / "calibration-life-retry.json").read_text(
            encoding="utf-8"
        )
    )
    assert calibration["valid"] is False
    assert calibration["survived"] is False
    assert calibration["completed"] is False
    assert calibration["mode"] == "calibration-formal"
    assert calibration["result_status"] == "life_failed"


def test_engine_error_writes_invalid_result_with_partial_stats(monkeypatch, tmp_path):
    reset_live_run(mode="formal", difficulty="Normal")
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=-9,
        profile_path=SimpleNamespace(name="normal.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            error = RuntimeError("detector exploded")
            error.realtime_stats = EngineStats(
                17,
                6,
                False,
                terminal_reason=(
                    "实时演奏引擎异常: RuntimeError: detector exploded"
                ),
                action_counts={"tap": 6},
                frame_interval_p50_ms=16.7,
                frame_interval_p95_ms=22.5,
                frame_interval_max_ms=41.0,
                effective_fps=57.3,
            )
            raise error

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Normal",
        "duration_seconds": 600,
        "require_completion": True,
        "save_result_frame": True,
        "run_mode": "formal",
    }))

    assert not RealtimeProfilePlay().run(context, argv)
    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "engine_error"
    assert payload["processed_frames"] == 17
    assert payload["dispatched_actions"] == 6
    assert payload["action_counts"] == {"tap": 6}
    assert payload["frame_interval_p95_ms"] == pytest.approx(22.5)
    assert payload["reason"] == payload["terminal_reason"]
    assert payload["run_id"] == payload["session"]["run_id"]


def test_engine_interrupt_after_stop_writes_neutral_partial_result(
    monkeypatch, tmp_path,
):
    reset_live_run(mode="formal", difficulty="Normal")
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=-9,
        profile_path=SimpleNamespace(name="normal.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            tasker.stopping = True
            error = InterruptedError("stop observed during dispatch")
            error.realtime_stats = EngineStats(
                17,
                6,
                False,
                terminal_reason=(
                    "实时演奏引擎异常: InterruptedError: "
                    "stop observed during dispatch"
                ),
                action_counts={"tap": 6},
                frame_interval_p50_ms=16.7,
                frame_interval_p95_ms=22.5,
                frame_interval_max_ms=41.0,
                effective_fps=57.3,
            )
            raise error

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)
    failure_reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        failure_reasons.append,
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Normal",
        "duration_seconds": 600,
        "require_completion": True,
        "save_result_frame": True,
        "run_mode": "formal",
    }))

    assert RealtimeProfilePlay().run(context, argv) is True
    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "stopped"
    assert payload["processed_frames"] == 17
    assert payload["dispatched_actions"] == 6
    assert payload["terminal_reason"] == "用户已停止任务"
    assert payload["reason"] == "用户已停止任务"
    assert failure_reasons == []


def test_profile_resolution_failure_writes_correlated_preflight_result(
    monkeypatch, tmp_path,
):
    live_run = reset_live_run(
        mode="pending",
        difficulty="Normal",
        prepared_for_play=True,
    )
    update_live_run(
        song_id="song-phash-v1-profile-preflight",
        song_id_method="song-phash-v1",
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    verified = SimpleNamespace(
        difficulty="Normal",
        actual_note_speed=3.5,
        expected_note_speed=3.5,
        profile="normal.json",
        verified_at=1.0,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.verified_settings",
        lambda _difficulty: verified,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.resolve_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("profile mismatch")
        ),
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda root, **kwargs: recorder_constructions.append(root),
    )
    failure_reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        failure_reasons.append,
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Normal",
        "require_profile": True,
        "settings_gate_required": True,
        "debug_recording": True,
        "run_mode": "formal",
    }))

    assert RealtimeProfilePlay().run(context, argv) is False
    assert recorder_constructions == []
    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "preflight_error"
    assert payload["terminal_stage"] == "profile_play_preflight"
    assert payload["run_id"] == live_run.run_id
    assert payload["song_id"] == "song-phash-v1-profile-preflight"
    assert payload["profile"] == "normal.json"
    assert payload["settings"]["expected_note_speed"] == pytest.approx(3.5)
    assert payload["settings"]["actual_note_speed"] == pytest.approx(3.5)
    assert payload["reason"] == "ValueError: profile mismatch"
    assert failure_reasons == ["ValueError: profile mismatch"]


def test_late_preflight_failure_preserves_verified_speed(
    monkeypatch, tmp_path,
):
    reset_live_run(
        mode="pending",
        difficulty="Normal",
        prepared_for_play=True,
    )
    update_live_run(
        song_id="song-phash-v1-visual-preflight",
        song_id_method="song-phash-v1",
    )
    context = SimpleNamespace(tasker=Tasker())
    verified = SimpleNamespace(
        difficulty="Normal",
        actual_note_speed=3.5,
        expected_note_speed=3.5,
        profile="normal.json",
        verified_at=1.0,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.verified_settings",
        lambda _difficulty: verified,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.runtime_options",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.debug_recording_enabled",
        lambda _argv: (_ for _ in ()).throw(RuntimeError("debug option failed")),
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda root, **kwargs: recorder_constructions.append(root),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Normal",
        "require_profile": False,
        "settings_gate_required": True,
        "debug_recording": False,
        "run_mode": "formal",
    }))

    assert RealtimeProfilePlay().run(context, argv) is False
    assert recorder_constructions == []
    report = next((tmp_path / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["result_status"] == "preflight_error"
    assert payload["terminal_stage"] == "profile_play_preflight"
    assert payload["song_id"] == "song-phash-v1-visual-preflight"
    assert payload["profile"] == "normal.json"
    assert payload["settings"] == {
        "expected_note_speed": 3.5,
        "actual_note_speed": 3.5,
    }


def test_foreground_failure_still_starts_preflight_debug_recorder(monkeypatch, tmp_path):
    reset_live_run(mode="formal", difficulty="Easy")
    context = SimpleNamespace(tasker=Tasker())
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: (_ for _ in ()).throw(
            RuntimeError("game is not foreground")
        ),
    )
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda _root, **kwargs: recorder_constructions.append(_root),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "require_profile": True,
        "debug_recording": True,
    }))

    assert not RealtimeProfilePlay().run(context, argv)
    assert recorder_constructions == [tmp_path / "debug" / "recordings"]


def test_touch_construction_failure_still_starts_preflight_debug_recorder(
    monkeypatch, tmp_path,
):
    reset_live_run(mode="formal", difficulty="Easy")
    context = SimpleNamespace(tasker=Tasker())
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("touch construction failed")
        ),
    )
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda _root, **kwargs: recorder_constructions.append(_root),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "require_profile": True,
        "debug_recording": True,
    }))

    assert not RealtimeProfilePlay().run(context, argv)
    assert recorder_constructions == [tmp_path / "debug" / "recordings"]


@pytest.mark.parametrize("failure_point", ["construction", "run"])
def test_preflight_failure_closes_unowned_debug_recorder(
    monkeypatch, tmp_path, failure_point,
):
    reset_live_run(mode="formal", difficulty="Easy")
    context = SimpleNamespace(tasker=Tasker())
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )

    class Touch:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    touch = Touch()
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: touch,
    )

    class Recorder:
        def __init__(self):
            self.output_dir = tmp_path / "debug-rec"
            self.output_dir.mkdir()
            self.closed = 0
            self.session_metadata = None

        def set_session_metadata(self, metadata):
            self.session_metadata = metadata

        def close(self):
            self.closed += 1
            (self.output_dir / "summary.json").write_text(
                json.dumps({"session": self.session_metadata}),
                encoding="utf-8",
            )

    recorder = Recorder()
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda _root, **kwargs: recorder,
    )
    if failure_point == "construction":
        monkeypatch.setattr(
            "agent.realtime.profile_play_action.RealtimeEngine",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("engine construction failed")
            ),
        )
    else:
        class Engine:
            def __init__(self, *_args, **_kwargs):
                pass

            def run(self, *_args, **_kwargs):
                raise ValueError("duration_seconds must be in 1..600")

        monkeypatch.setattr(
            "agent.realtime.profile_play_action.RealtimeEngine", Engine,
        )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "require_profile": True,
        "debug_recording": True,
        "save_result_frame": True,
    }))

    assert not RealtimeProfilePlay().run(context, argv)
    assert recorder.closed == 1
    assert touch.closed == 1
    report = next((tmp_path / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert payload["valid"] is False
    assert payload["result_status"] == "preflight_error"
    assert payload["run_id"] == summary["session"]["run_id"]


def _completed_play_harness(
    monkeypatch,
    tmp_path,
    *,
    debug_recording,
    diagnostic_trace=True,
    collection_status=ResultCollectionStatus.STABLE,
    engine_stopped=False,
    calibration_report=False,
    prepared_for_play=True,
    collection_exception=None,
    screenshot_success=True,
    save_result_frame=True,
    expected_success=True,
    startup_timed_out=False,
    run_mode="formal",
    defer_result_collection=False,
    skip_result_check=False,
    life_failed=False,
    aborted_for_life=False,
    engine_cleanup_failed=False,
    native_report=None,
    collected_result=None,
    collect_callback=None,
    runtime_task_id=None,
):
    reset_live_run(
        mode="pending",
        difficulty="Easy",
        prepared_for_play=prepared_for_play,
    )
    update_live_run(
        song_id="song-phash-v1-0123456789abcdef",
        song_id_method="song-phash-v1",
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        note_speed=5.0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.PROJECT_ROOT",
        tmp_path,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.runtime_options",
        lambda *args, **kwargs: {
            "skip_result_check": skip_result_check,
            "native_realtime_enabled": native_report is not None,
        },
    )
    if native_report is not None:
        selection = SimpleNamespace(
            path=Path("chart-728-easy.json"),
            timeline=None,
            bestdori_song_id=728,
            difficulty="easy",
        )
        monkeypatch.setattr(
            profile_play_action,
            "resolve_local_chart_for_run",
            lambda *args, **kwargs: SimpleNamespace(
                selection=selection, reason="matched",
            ),
        )
        monkeypatch.setattr(
            profile_play_action,
            "consume_prearmed_backend",
            lambda *_args: SimpleNamespace(configure_timing_offset=lambda _: None),
        )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action._recover_completed_result",
        lambda context: True,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )
    if runtime_task_id is None:
        monkeypatch.setattr(
            "agent.realtime.profile_play_action.debug_recording_enabled",
            lambda _argv: False,
        )
    recorder_holder = {}
    class FakeRecorder:
        def __init__(self, *, video_enabled=True):
            self.output_dir = tmp_path / "debug-rec"
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.video_enabled = video_enabled
            self.session_metadata = None
            recorder_holder["value"] = self

        def set_session_metadata(self, metadata):
            self.session_metadata = metadata

        def close(self):
            (self.output_dir / "summary.json").write_text(json.dumps({
                "schema_version": 2,
                "recording_mode": (
                    "video" if self.video_enabled else "trace-only"
                ),
                "session": self.session_metadata,
            }), encoding="utf-8")

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda _root, **kwargs: FakeRecorder(
            video_enabled=kwargs.get("video_enabled", True)
        ),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            self.debug_recorder = kwargs.get("debug_recorder")

        def run(self, _capture, _stopping, **_kwargs):
            if startup_timed_out or life_failed or aborted_for_life:
                _capture()
            if self.debug_recorder is not None:
                self.debug_recorder.close()
            return EngineStats(
                120,
                42,
                engine_stopped,
                completed=(
                    not engine_stopped and not startup_timed_out
                    and not life_failed and not aborted_for_life
                ),
                life_failed=life_failed,
                aborted_for_life=aborted_for_life,
                life_depleted=life_failed or aborted_for_life,
                cleanup_failed=engine_cleanup_failed,
                engine_mode="native" if native_report is not None else "legacy",
                native_report=dict(native_report or {}),
                action_counts={"tap": 31, "flick": 4, "down": 7},
                frame_interval_p50_ms=16.4,
                frame_interval_p95_ms=18.2,
                frame_interval_max_ms=24.0,
                effective_fps=59.1,
                terminal_reason=(
                    "用户已停止任务"
                    if engine_stopped
                    else "开演后 20 秒仍未识别到生命条"
                    if startup_timed_out
                    else "演出失败：生命值归零"
                    if life_failed or aborted_for_life
                    else "已识别演奏结束并进入结算"
                ),
                initial_timing_offset_ms=-11,
                final_timing_offset_ms=-13,
                startup_timed_out=startup_timed_out,
            )

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)

    image = np.full((720, 1280, 3), 128, dtype=np.uint8)

    def fake_collect(*args, **kwargs):
        assert not (life_failed or aborted_for_life), "死亡局不能采集成功结算"
        assert kwargs["cooperative_mode"] is (run_mode in ("cooperative", "versus"))
        assert kwargs["robust_navigation"] is True
        assert kwargs["timeout_seconds"] == 180.0
        if collection_exception is not None:
            raise collection_exception
        if collect_callback is not None:
            return collect_callback(*args, **kwargs)
        return ResultCollectionOutcome(
            collection_status,
            result=(
                collected_result if collected_result is not None else
                LiveResult(100, 10, 2, 1, 2, 3, 4)
                if collection_status is ResultCollectionStatus.STABLE else None
            ),
            image=image,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.collect_result",
        fake_collect,
    )
    writes = []

    def fake_imwrite(path, _image):
        if Path(path).name.startswith('realtime-result-'):
            assert _image is image
        writes.append(str(path))
        if not screenshot_success:
            return False
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"png")
        return True

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.imwrite_unicode",
        fake_imwrite,
    )

    params = {
        "difficulty": "Easy",
        "require_profile": False,
        "settings_gate_required": False,
        "duration_seconds": 600,
        "wait_for_completion": True,
        "require_completion": True,
        "save_result_frame": save_result_frame,
        "debug_recording": debug_recording,
        "diagnostic_trace": diagnostic_trace,
        "run_mode": run_mode,
    }
    if calibration_report:
        params["calibration_report"] = "screencap/calibration-round.json"
    if native_report is not None:
        params["rehearsal_mode"] = False
    if defer_result_collection:
        params.update({
            "defer_result_collection": True,
            "deferred_result_report": "screencap/medley-session-song1.json",
        })
    argv = SimpleNamespace(custom_action_param=json.dumps(params))
    if runtime_task_id is not None:
        argv.task_detail = SimpleNamespace(task_id=runtime_task_id)
    assert RealtimeProfilePlay()._run(context, argv) is expected_success
    return tmp_path, writes, recorder_holder.get("value")


def _life_failed_native_report():
    return {
        "planned": 6287,
        "sent": 2218,
        "executed": 2195,
        "underflows": 0,
        "state": "cancelled",
        "session_state": "cancelled",
        "executed_observation_complete": False,
        "executed_observation_reason": "会话在完整设备回读前取消",
        "reset_executed": True,
        "release_confirmed": True,
        "stop_latency_ms": 21.0,
    }


@pytest.mark.parametrize("invalid", ["sequence", "sent", "execution", "contacts", "scope"])
def test_real_native_release_gate_requires_complete_protocol_evidence(invalid):
    report = _life_failed_native_report() | {
        "reset_requested": True, "reset_sent": True,
        "release_sequence_confirmed": True, "release_contacts": list(range(10)),
        "release_scope": "minitouch-protocol-and-cleanup",
        "release_proof": "current-release-sequence-jlog-and-cleanup",
    }
    assert profile_play_action._native_execution_gate_failures(report, expected_life_cancel=True) == []
    if invalid == "sequence":
        report["release_sequence_confirmed"] = False
    elif invalid == "sent":
        report["reset_sent"] = False
    elif invalid == "execution":
        report["reset_executed"] = False
    elif invalid == "contacts":
        report["release_contacts"] = [True, 1]
    else:
        report.pop("release_scope")
    assert profile_play_action._native_execution_gate_failures(report, expected_life_cancel=True)


@pytest.mark.parametrize("death_source", ["popup", "numeric"])
@pytest.mark.parametrize("run_mode", ["challenge", "medley"])
def test_native_life_failure_keeps_reason_and_exits_failed_live(
    tmp_path, monkeypatch, death_source, run_mode,
):
    from agent.task_reporting import record_failure_reason, latest_failure_reason

    record_failure_reason("")
    exits = []
    monkeypatch.setattr(
        profile_play_action, "exit_failed_live", lambda context: exits.append(context) or True,
    )
    root, writes, _ = _completed_play_harness(
        monkeypatch, tmp_path,
        debug_recording=False,
        diagnostic_trace=False,
        run_mode=run_mode,
        life_failed=death_source == "popup",
        aborted_for_life=death_source == "numeric",
        native_report=_life_failed_native_report(),
        expected_success=False,
    )
    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["result_status"] == "life_failed"
    assert payload["reason"] == "演出失败：生命值归零"
    assert payload["completed"] is False
    assert latest_failure_reason() == "演出失败：生命值归零"
    assert exits == []
    if run_mode == "challenge":
        assert len(writes) == 1
        assert "realtime-life-failed-" in writes[0]
    else:
        assert writes == []


@pytest.mark.parametrize("override", [
    {"release_confirmed": False},
    {"reset_executed": False},
    {"executed": 2219},
    {"underflows": 1},
    {"device_error": "connection lost"},
    {"stop_latency_ms": 1001.0},
])
def test_native_life_failure_does_not_hide_unsafe_cleanup(tmp_path, monkeypatch, override):
    exits = []
    monkeypatch.setattr(
        profile_play_action, "exit_failed_live", lambda context: exits.append(context) or True,
    )
    root, _, _ = _completed_play_harness(
            monkeypatch, tmp_path,
            debug_recording=False,
            diagnostic_trace=False,
            run_mode="challenge",
            life_failed=True,
            native_report=_life_failed_native_report() | override,
            expected_success=False,
        )
    payload = json.loads(next((root / "screencap").glob("realtime-result-*.json")).read_text(encoding="utf-8"))
    assert payload["reason"] == "演出失败：生命值归零"
    assert payload["native_cleanup_warnings"]
    assert exits == []


def test_native_life_failure_keeps_engine_cleanup_failure_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(
        profile_play_action, "exit_failed_live",
        lambda context: pytest.fail("触点清理失败时不能继续导航"),
    )
    root, _, _ = _completed_play_harness(
            monkeypatch, tmp_path,
            debug_recording=False,
            diagnostic_trace=False,
            run_mode="challenge",
            life_failed=True,
            engine_cleanup_failed=True,
            native_report=_life_failed_native_report(),
            expected_success=False,
        )
    payload = json.loads(next((root / "screencap").glob("realtime-result-*.json")).read_text(encoding="utf-8"))
    assert payload["reason"] == "演出失败：生命值归零"
    assert payload["cleanup_failed"] is True


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("stopped", [False, True])
def test_challenge_death_preserves_scene_and_stop_priority(tmp_path, monkeypatch, native, stopped):
    monkeypatch.setattr(profile_play_action, "exit_failed_live", lambda *args: pytest.fail("挑战死亡不得导航"))
    monkeypatch.setattr(profile_play_action, "collect_result", lambda *args, **kwargs: pytest.fail("挑战死亡不得结算"))
    root, writes, _ = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False, diagnostic_trace=False,
        run_mode="challenge", life_failed=True, engine_stopped=stopped,
        native_report=_life_failed_native_report() if native else None, expected_success=stopped)
    reports = list((root / "screencap").glob("realtime-result-*.json"))
    if stopped:
        assert reports == [] and writes == []
    else:
        assert len(reports) == 1 and len(writes) == 1


@pytest.mark.parametrize("failure", ["png", "json"])
def test_challenge_save_failure_retains_death_reason(tmp_path, monkeypatch, failure):
    from agent.task_reporting import latest_failure_reason
    monkeypatch.setattr(profile_play_action, "exit_failed_live", lambda *args: pytest.fail("保存失败不得导航"))
    if failure == "json":
        monkeypatch.setattr(profile_play_action, "_write_json_atomic",
                            lambda *args: (_ for _ in ()).throw(OSError("test report failure")))
    _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False, diagnostic_trace=False,
        run_mode="challenge", life_failed=True, screenshot_success=failure != "png", expected_success=False)
    assert latest_failure_reason() == "演出失败：生命值归零"


def test_completed_medley_play_defers_pggbm_collection(tmp_path, monkeypatch):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        run_mode="medley",
        defer_result_collection=True,
    )

    report = root / "screencap" / "medley-session-song1.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["result_status"] == "medley_result_pending"
    assert payload["valid"] is False
    assert payload["completed"] is True
    assert writes == []


def test_finalize_deferred_result_marks_report_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_play_action, "PROJECT_ROOT", tmp_path)
    report = tmp_path / "screencap" / "medley-session-song1.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({
        "result_status": "medley_result_pending",
        "valid": False,
        "eligible_for_profile_acceptance": False,
        "reason": "组曲判定详情将在第三曲后逐首读取",
        "current_timing_offset_ms": 8,
        "initial_timing_offset_ms": 5,
        "engine_mode": "native",
        "profile": "expert.json",
    }), encoding="utf-8")

    payload = finalize_deferred_result(
        "screencap/medley-session-song1.json",
        LiveResult(100, 2, 1, 0, 0, 3, 4),
        save_screenshot=False,
    )

    assert payload["result_status"] == "stable"
    assert payload["valid"] is True
    assert payload["eligible_for_profile_acceptance"] is True
    assert payload["suggested_timing_offset_ms"] == 8
    assert payload["perfect"] == 100
    assert "reason" not in payload

    repeated = finalize_deferred_result(
        "screencap/medley-session-song1.json",
        LiveResult(100, 2, 1, 0, 0, 3, 4),
        save_screenshot=False,
    )
    assert repeated == payload


def test_finalize_deferred_result_rejects_wrong_state_and_external_path(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(profile_play_action, "PROJECT_ROOT", tmp_path)
    report = tmp_path / "screencap" / "not-pending.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps({"result_status": "failed"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="状态不正确"):
        finalize_deferred_result(
            report,
            LiveResult(1, 0, 0, 0, 0, 0, 0),
            save_screenshot=False,
        )
    with pytest.raises(ValueError, match="screencap"):
        finalize_deferred_result(
            tmp_path / "outside.json",
            LiveResult(1, 0, 0, 0, 0, 0, 0),
            save_screenshot=False,
        )


def test_completed_trace_only_explicit_save_keeps_existing_stable_result_image(tmp_path, monkeypatch):
    root, writes, recorder = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    screenshots = list((root / "screencap").glob("realtime-result-*.png"))
    assert len(reports) == 1
    assert len(screenshots) == 1
    assert writes == [str(screenshots[0])]
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["perfect"] == 100
    assert payload["processed_frames"] == 120
    assert payload["run_id"]
    assert payload["song_id"] == "song-phash-v1-0123456789abcdef"
    assert payload["debug_recording_path"].endswith("debug-rec")
    assert recorder.video_enabled is False
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["recording_mode"] == "trace-only"


def test_trace_only_explicit_result_save_off_does_not_write_image(tmp_path,monkeypatch):
    root,writes,recorder=_completed_play_harness(monkeypatch,tmp_path,debug_recording=False,
        save_result_frame=False)
    assert writes==[] and not list((root/'screencap').glob('realtime-result-*.png'))
    assert recorder.video_enabled is False


def test_trace_only_result_image_write_failure_warns_without_losing_completion(tmp_path,monkeypatch):
    root,writes,recorder=_completed_play_harness(monkeypatch,tmp_path,debug_recording=False,
        screenshot_success=False)
    payload=json.loads(next((root/'screencap').glob('realtime-result-*.json')).read_text(encoding='utf8'))
    assert writes and recorder.video_enabled is False
    assert payload['completed'] and payload['valid'] and payload['result_status']=='stable'
    assert '无法保存结算截图' in payload['result_screenshot_error']


@pytest.mark.parametrize("run_mode", ["cooperative", "versus"])
def test_multiplayer_play_reads_its_own_layout_with_shared_navigation(tmp_path, monkeypatch, run_mode):
    import base64
    import zlib
    from agent.realtime.result_parser import CooperativeResultParser, ResultParser
    from agent.realtime.result_samples_v2 import RESULT_CROPS_V2_ZLIB_BASE64
    from agent.realtime.result_navigation import ResultNavigationOutcome, ResultNavigationStatus

    # 两种模式共用导航，但竞演的真实 PGGBM 数字位置与单人一致。
    parser_type = CooperativeResultParser if run_mode == "cooperative" else ResultParser
    crops = np.frombuffer(
        zlib.decompress(base64.b64decode(RESULT_CROPS_V2_ZLIB_BASE64)), dtype=np.uint8,
    ).reshape(7, 32, 60)
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    for crop, (x1, y1, x2, y2) in zip(crops, parser_type.FIELDS.values()):
        image[y1:y2, x1:x2] = (255 - crop[:, :x2-x1])[:, :, None]
    reading = LiveResult(117, 11, 0, 0, 8, 4, 7, confidence=1.0)
    now, advances = [0.0], []
    real_collect = profile_play_action.collect_result
    class Controller:
        def post_screencap(self):
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))
    monkeypatch.setattr(profile_play_action, "navigate_result_pages", lambda *a, **k: ResultNavigationOutcome(
        ResultNavigationStatus.IDENTIFIED, image=image, page_state="pggbm",
    ))
    monkeypatch.setattr(profile_play_action, "_template_click_point", lambda *a, **k: (100, 100))
    monkeypatch.setattr(profile_play_action, "accelerated_back", lambda *a, **k: advances.append(now[0]))
    def collect(_controller, stopping, **kwargs):
        assert kwargs["cooperative_mode"] is True
        return real_collect(
            Controller(), stopping, **dict(kwargs, expected_notes=reading.total),
            clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
    root, _, _ = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False,
        run_mode=run_mode, collect_callback=collect,
    )
    payload = json.loads(next((root / "screencap").glob("realtime-result-*.json")).read_text(encoding="utf-8"))
    assert payload["result_status"] == "cooperative_result_advanced"
    assert payload["cooperative_judgements_status"] == "stable"
    # 沿用多人结果不自动接受 Profile 的既有语义。
    assert payload["valid"] is False
    assert payload["eligible_for_profile_acceptance"] is False
    for name in ("perfect", "great", "good", "bad", "miss", "fast", "slow"):
        assert payload[name] == getattr(reading, name)
    assert sum(payload[name] for name in ("perfect", "great", "good", "bad", "miss")) == reading.total
    assert advances == [1.0]


def test_completed_cooperative_play_advances_when_judgements_are_unreadable(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_status=ResultCollectionStatus.ADVANCED,
        run_mode="cooperative",
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["result_status"] == "cooperative_result_advanced"
    assert payload["valid"] is False
    assert payload["cooperative_judgements_status"] == "unreadable"
    assert writes == []


def test_completed_cooperative_play_saves_stable_judgements(tmp_path, monkeypatch):
    reading = LiveResult(100, 10, 2, 1, 2, 3, 4)
    root, writes, _ = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False,
        collection_status=ResultCollectionStatus.ADVANCED,
        run_mode="cooperative", collected_result=reading,
    )
    payload = json.loads(next((root / "screencap").glob("realtime-result-*.json")).read_text(encoding="utf-8"))
    assert payload["cooperative_judgements_status"] == "stable"
    for name, value in reading.to_dict().items():
        assert payload[name] == value
    assert payload["eligible_for_profile_acceptance"] is False
    assert writes == []


def test_stable_multiplayer_read_uses_expected_total_resolver():
    now = [0.0]
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    original = LiveResult(568, 0, 0, 0, 1, 0, 0, confidence=.76)
    repaired = LiveResult(668, 0, 0, 0, 1, 0, 0, confidence=.76)
    calls = []

    class Parser:
        def parse(self, frame):
            return original

        def resolve_expected_total(self, frame, *, expected_notes, fallback):
            calls.append((expected_notes, fallback))
            return repaired

    result, _ = profile_play_action._read_stable_cooperative_judgements(
        Controller(), lambda: False, image, parser=Parser(), expected_notes=669,
        maximum_notes=9999, deadline=20, stability_interval_seconds=.2,
        is_result_page=lambda frame: True, clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert result == repaired
    assert calls == [(669, original), (669, original)]
    assert now[0] == pytest.approx(.2)


@pytest.mark.parametrize("failure,reason", [
    ("parse", "parse failed"),
    ("mismatch", "does not match chart"),
    ("confidence", "too low"),
    ("fast-slow", "FAST/SLOW counts are inconsistent"),
    ("unstable", "did not remain stable"),
    ("page-left", "result page left"),
])
def test_stable_multiplayer_read_keeps_rejection_budget_and_summary(failure, reason, capsys):
    now, reads = [0.0], [0]

    class Parser:
        def parse(self, image):
            reads[0] += 1
            if failure == "parse":
                raise ValueError("unreadable digit")
            if failure == "mismatch":
                return LiveResult(568, 0, 0, 0, 1, 0, 0)
            if failure == "confidence":
                return LiveResult(668, 0, 0, 0, 1, 0, 0, confidence=.29)
            if failure == "fast-slow":
                return LiveResult(668, 0, 0, 0, 1, 670, 0)
            if failure == "unstable" and reads[0] % 2:
                return LiveResult(667, 1, 0, 0, 1, 0, 0)
            return LiveResult(668, 0, 0, 0, 1, 0, 0)

        def resolve_expected_total(self, image, **kwargs):
            raise ValueError("no constrained candidate")

    result, _ = profile_play_action._read_stable_cooperative_judgements(
        Controller(), lambda: False, object(), parser=Parser(), expected_notes=669,
        maximum_notes=9999, deadline=20, stability_interval_seconds=.2,
        is_result_page=lambda image: failure != "page-left", clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert result is None
    assert now[0] <= 3.0
    output = capsys.readouterr().out
    assert reason in output
    assert output.count("cooperative_judgement_unreadable=") == 1


def test_stable_multiplayer_read_stop_precedes_capture_and_logging(monkeypatch):
    class Forbidden:
        def parse(self, image):
            raise AssertionError("停止后不能解析")

        def post_screencap(self):
            raise AssertionError("停止后不能截图")

    result, _ = profile_play_action._read_stable_cooperative_judgements(
        Forbidden(), lambda: True, object(), parser=Forbidden(), expected_notes=669,
        maximum_notes=9999, deadline=20, stability_interval_seconds=.2,
        is_result_page=lambda image: True, clock=lambda: 0, sleeper=lambda seconds: None,
    )
    assert result is None


def test_stable_multiplayer_read_logging_failure_remains_nonfatal(monkeypatch):
    now = [0.0]
    def parse(image):
        raise ValueError("unreadable digit")
    def print_failure(*args, **kwargs):
        raise OSError("output unavailable")
    monkeypatch.setattr("builtins.print", print_failure)
    result, _ = profile_play_action._read_stable_cooperative_judgements(
        Controller(), lambda: False, object(), parser=SimpleNamespace(parse=parse),
        expected_notes=669, maximum_notes=9999, deadline=20, stability_interval_seconds=.2,
        is_result_page=lambda image: True, clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert result is None
    assert now[0] == pytest.approx(3.0)


def test_completed_with_diagnostics_disabled_writes_result_only(
    tmp_path, monkeypatch,
):
    root, writes, recorder = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        diagnostic_trace=False,
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["debug_recording_path"] is None
    assert recorder is None
    assert writes == []


def test_direct_profile_play_does_not_reuse_unprepared_song_identity(
    tmp_path, monkeypatch,
):
    root, _, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        prepared_for_play=False,
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["song_id"] == "unknown"
    assert payload["song_id_method"] == "unknown"


def test_continuous_play_preserves_preconfirmed_opening_identity(
    tmp_path, monkeypatch,
):
    root, _, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        run_mode="continuous",
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["mode"] == "continuous"
    assert payload["song_id"] == "song-phash-v1-0123456789abcdef"


def test_completed_with_debug_recording_writes_json_and_screenshot(
    tmp_path, monkeypatch,
):
    root, writes, recorder = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=True,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    screenshots = list((root / "screencap").glob("realtime-result-*.png"))
    assert len(reports) == 1
    assert len(screenshots) == 1
    assert len(writes) == 1
    assert str(screenshots[0]) == writes[0]
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["debug_recording_path"].endswith("debug-rec")
    assert recorder.video_enabled is True


@pytest.mark.parametrize("run_mode,pipeline", [("formal", "realtime_multi_live.json"), ("challenge", "challenge_live.json")])
def test_current_task_full_gate_overrides_base_node_false_without_next_task_leak(monkeypatch, tmp_path, run_mode, pipeline):
    from agent.realtime import runtime_options
    monkeypatch.setattr(runtime_options, "_DEBUG_BY_TASK", {})
    monkeypatch.setattr(runtime_options, "_DEBUG_ENABLED", False)
    resource = json.loads((Path(__file__).parents[1] / "resource" / "pipeline" / pipeline).read_text(encoding="utf-8"))
    play_nodes = [node for node in resource.values() if node.get("custom_action") == "RealtimeProfilePlay"]
    assert play_nodes and all(node["custom_action_param"]["debug_recording"] is False for node in play_nodes)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    def options(task_id, enabled):
        argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=task_id),
            custom_action_param=json.dumps({"debug_recording": enabled}))
        assert runtime_options.RealtimeRuntimeOptions().run(context, argv)
    options(91001, True)
    _, _, recorder = _completed_play_harness(monkeypatch, tmp_path / "full", debug_recording=False,
        runtime_task_id=91001, run_mode=run_mode)
    assert recorder.video_enabled is True
    options(91002, False)
    for task_id in (91002, 91003):
        _, _, recorder = _completed_play_harness(monkeypatch, tmp_path / str(task_id), debug_recording=False,
            runtime_task_id=task_id, run_mode=run_mode)
        assert recorder.video_enabled is False


def test_result_collection_timeout_writes_invalid_correlated_json(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_status=ResultCollectionStatus.TIMED_OUT,
        expected_success=True,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    assert len(writes) == 1
    assert "realtime-result-timeout-" in writes[0]
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "timed_out"
    assert payload["run_id"]
    assert payload["song_id"] == "song-phash-v1-0123456789abcdef"
    assert payload["result_diagnostic_frame"].startswith(
        "screencap/realtime-result-timeout-"
    )
    assert "perfect" not in payload


def test_result_collection_timeout_stays_reportable_for_calibration_round(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_status=ResultCollectionStatus.TIMED_OUT,
        calibration_report=True,
        expected_success=True,
    )

    calibration = json.loads(
        (root / "screencap" / "calibration-round.json").read_text(
            encoding="utf-8"
        )
    )
    assert calibration["valid"] is False
    assert calibration["result_status"] == "timed_out"
    assert len(writes) == 1


def test_playfield_start_timeout_saves_frame_and_fails_ordinary_play(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        startup_timed_out=True,
        expected_success=False,
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "playfield_start_timeout"
    assert payload["startup_timed_out"] is True
    assert payload["startup_diagnostic_frame"].startswith(
        "screencap/realtime-startup-timeout-"
    )
    assert len(writes) == 1


def test_result_collection_exception_writes_invalid_correlated_json(
    tmp_path, monkeypatch,
):
    root, _, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_exception=RuntimeError("capture failed"),
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "result_collection_error"
    assert payload["reason"] == "结算读取异常: RuntimeError: capture failed"
    assert payload["processed_frames"] == 120
    assert payload["run_id"] == payload["session"]["run_id"]


@pytest.mark.parametrize("run_mode", ["formal", "cooperative", "challenge", "continuous", "calibration-formal"])
def test_completed_modes_continue_after_result_exception(tmp_path, monkeypatch, run_mode):
    _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False,
        run_mode=run_mode, collection_exception=RuntimeError("result OCR failed"),
    )


@pytest.mark.parametrize("status", [ResultCollectionStatus.TIMED_OUT, ResultCollectionStatus.BLOCKED])
def test_completed_modes_continue_after_unreadable_result(tmp_path, monkeypatch, status):
    _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False, collection_status=status,
    )


def test_skip_result_check_never_calls_numeric_collection(tmp_path, monkeypatch):
    root, writes, _ = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False, skip_result_check=True,
        collection_exception=AssertionError("must not collect result"),
    )
    assert writes == []
    assert not list((root / "screencap").glob("realtime-result-*.json"))


def test_skip_result_check_keeps_calibration_evidence(tmp_path, monkeypatch):
    root, _, _ = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False, skip_result_check=True,
        calibration_report=True, run_mode="calibration-formal",
    )
    payload = json.loads((root / "screencap/calibration-round.json").read_text(encoding="utf-8"))
    assert payload["valid"] is True


def test_completed_report_write_failure_is_nonfatal(tmp_path, monkeypatch):
    def fail_write(*_args, **_kwargs):
        raise OSError("result disk unavailable")
    monkeypatch.setattr("agent.realtime.profile_play_action._write_json_atomic", fail_write)
    _completed_play_harness(monkeypatch, tmp_path, debug_recording=False)


def test_skip_result_check_does_not_hide_life_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.realtime.profile_play_action.exit_failed_live", lambda context: True)
    root, _, _ = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False, skip_result_check=True,
        life_failed=True, expected_success=False,
    )
    payload = json.loads(next((root / "screencap").glob("realtime-result-*.json")).read_text(encoding="utf-8"))
    assert payload["result_status"] == "life_failed"
    assert payload["completed"] is False


def test_debug_screenshot_failure_keeps_stable_json_result(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=True,
        screenshot_success=False,
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert writes
    assert payload["valid"] is True
    assert payload["result_status"] == "stable"
    assert "无法保存结算截图" in payload["result_screenshot_error"]


def test_engine_stop_writes_neutral_structured_result_without_collecting_frame(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        engine_stopped=True,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    assert writes == []
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "stopped"
    assert payload["reason"] == "用户已停止任务"
    assert payload["eligible_for_profile_acceptance"] is False


def test_result_collection_stop_writes_neutral_structured_result(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_status=ResultCollectionStatus.STOPPED,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    assert writes == []
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "stopped"
    assert payload["reason"] == "用户在结算读取期间停止任务"


def test_one_run_links_result_calibration_and_recorder_summary(
    tmp_path, monkeypatch,
):
    root, _, recorder = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=True,
        calibration_report=True,
    )

    result_path = next((root / "screencap").glob("realtime-result-*.json"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    calibration = json.loads(
        (root / "screencap" / "calibration-round.json").read_text(
            encoding="utf-8"
        )
    )
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )

    assert result["run_id"] == calibration["run_id"]
    assert result["run_id"] == summary["session"]["run_id"]
    assert result["song_id"] == calibration["song_id"]
    assert result["song_id"] == summary["session"]["song_id"]


def test_dismiss_reward_popup_uses_safe_click_back_safe_click_cycle():
    template = cv2.imread(str(profile_play_action.REWARD_OK_TEMPLATE))
    assert template is not None
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    image[568:642, 562:716] = template
    actions = []
    foreground_checks = []

    class FakeController:
        def post_click(self, x, y):
            actions.append(("click", (x, y)))
            return SimpleNamespace(wait=lambda: None)

        def post_click_key(self, key):
            actions.append(("key", key))
            return SimpleNamespace(wait=lambda: None)

    assert _dismiss_reward_popup(
        FakeController(),
        image,
        before_input=lambda: foreground_checks.append(1),
        threshold=0.8,
    ) is True
    assert actions == [
        ("click", profile_play_action.RESULT_ANIMATION_SKIP_POINT),
        ("key", 4),
        ("click", profile_play_action.RESULT_ANIMATION_SKIP_POINT),
    ]
    assert foreground_checks == [1, 1, 1]


def test_dismiss_reward_popup_ignores_clean_result_screen():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    clicks = []

    class FakeController:
        def post_click(self, x, y):
            clicks.append((x, y))
            return SimpleNamespace(wait=lambda: None)

    assert _dismiss_reward_popup(FakeController(), image, threshold=0.8) is False
    assert clicks == []


def test_collect_result_dismisses_reward_popup_before_stabilizing():
    template = cv2.imread(str(profile_play_action.REWARD_OK_TEMPLATE))
    popup = np.zeros((720, 1280, 3), dtype=np.uint8)
    popup[568:642, 562:716] = template
    clean = np.zeros((720, 1280, 3), dtype=np.uint8)
    clean[0:10, 0:10] = 255
    images = [popup, popup, clean, clean]

    class FakeParser:
        def parse(self, image):
            if image is popup:
                raise ValueError("reward popup covers digits")
            return LiveResult(
                perfect=100, great=0, good=0, bad=0, miss=0,
                fast=0, slow=0, confidence=1.0,
            )

    clicks = []
    keys = []

    class FakeJob:
        def __init__(self, value=None):
            self.value = value

        def wait(self):
            return self

        def get(self):
            return self.value

    class FakeController:
        def post_screencap(self):
            return FakeJob(images.pop(0))

        def post_click(self, x, y):
            clicks.append((x, y))
            return FakeJob()

        def post_click_key(self, key):
            keys.append(key)
            return FakeJob()

    clock_state = [0.0]

    def clock():
        clock_state[0] += 1.0
        return clock_state[0]

    outcome = collect_result(
        FakeController(),
        lambda: False,
        parser=FakeParser(),
        sleeper=lambda _: None,
        clock=clock,
        reward_click_delay_seconds=0.0,
        reward_threshold=0.8,
        judgement_details_template=None,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.result is not None
    assert clicks == [profile_play_action.RESULT_ANIMATION_SKIP_POINT] * 4
    assert keys == [4, 4]


@pytest.mark.parametrize('mode',['full','trace','opening'])
def test_recorder_creation_failure_is_nonfatal_at_profile_play_callsite(monkeypatch,mode,capsys):
    monkeypatch.setattr(profile_play_action,'debug_recording_enabled',lambda _argv:mode=='full')
    monkeypatch.setattr(profile_play_action,'diagnostic_trace_enabled',lambda:mode=='trace')
    monkeypatch.setattr(profile_play_action,'opening_recording_enabled',lambda argv:mode=='opening')
    def broken(*args,**kwargs):raise OSError('diagnostic disk unavailable')
    monkeypatch.setattr(profile_play_action,'RealtimeDebugRecorder',broken)
    test_profile_play_refreshes_proxy_at_safety_boundaries(monkeypatch)
    assert 'create_warning' in capsys.readouterr().out


def test_recorder_metadata_checkpoint_and_lifecycle_failures_are_nonfatal_at_play_callsite(monkeypatch,tmp_path,capsys):
    monkeypatch.setattr(profile_play_action,'debug_recording_enabled',lambda _argv:False)
    monkeypatch.setattr(profile_play_action,'diagnostic_trace_enabled',lambda:True)
    monkeypatch.setattr(profile_play_action,'opening_recording_enabled',lambda argv:False)
    class Recorder:
        output_dir=tmp_path
        def __init__(self,*a,**kw):pass
        def set_session_metadata(self,*a):raise OSError('metadata disk error')
        def save_checkpoint(self,*a,**kw):raise OSError('checkpoint disk error')
        def close(self):pass
    def broken_lifecycle(*a,**kw):raise OSError('lifecycle disk error')
    monkeypatch.setattr(profile_play_action,'RealtimeDebugRecorder',Recorder)
    monkeypatch.setattr(profile_play_action,'_append_lifecycle_event',broken_lifecycle)
    test_profile_play_refreshes_proxy_at_safety_boundaries(monkeypatch)
    output=capsys.readouterr().out
    assert 'metadata_warning' in output and 'lifecycle_warning' in output and 'checkpoint_warning' in output


@pytest.mark.parametrize("enabled",[False,True])
def test_opening_only_native_wait_capture_uses_existing_refresh_and_return_time(monkeypatch,enabled):
    import agent.screen_refresh as refresh
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    frame=object()
    captured=[]
    recorder=SimpleNamespace(record_phase=lambda *a,**kw:captured.append((a,kw)))
    def capture(ctx):
        assert ctx is context
        return frame
    monkeypatch.setattr(refresh,'capture_image',capture)
    monkeypatch.setattr(profile_play_action.time,'monotonic',lambda:123.5)
    kwargs=profile_play_action._opening_native_wait_kwargs(context,recorder,enabled)
    if not enabled:
        assert kwargs=={} and not captured
        return
    kwargs['wait_observer']()
    assert captured[0][0]==(frame,123.5,'native-prearm')
    assert captured[0][1]['frame_metadata']['timestamp_kind']=='capture-return-upper-bound'
    context.tasker.stopping=True
    with pytest.raises(InterruptedError):kwargs['wait_observer']()
    assert len(captured)==1


def test_native_opening_observer_imports_in_agent_server_top_level_runtime(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/"agent"))
    script="""
from types import SimpleNamespace
import realtime.profile_play_action as play
import screen_refresh
screen_refresh.capture_image=lambda ctx:object()
context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
seen=[]
recorder=SimpleNamespace(record_phase=lambda *a,**kw:seen.append(a))
play._opening_native_wait_kwargs(context,recorder,True)['wait_observer']()
assert seen[0][2]=='native-prearm'
"""
    result=subprocess.run([sys.executable,'-c',script],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize("close_error",[False,True])
def test_stop_after_recorder_created_closes_it_without_failure_screenshot(monkeypatch,tmp_path,close_error):
    reset_live_run(mode="cooperative",difficulty="Expert",prepared_for_play=True)
    update_live_run(preparation_identity_pending_final_cover=True)
    monkeypatch.setattr(profile_play_action,'PROJECT_ROOT',tmp_path)
    monkeypatch.setattr(profile_play_action.RealtimeProfileStore,'runtime_options',lambda self:{
        'native_realtime_enabled':False,'chart_prediction_enabled':False,'chart_predict_presses':False})
    monkeypatch.setattr(profile_play_action,'resolve_local_chart_for_run',lambda *a:SimpleNamespace(
        selection=_chart_selection('resource/charts/bestdori/55/expert.json'),reason='test'))
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False,controller=Controller()))
    closed=[]
    class Recorder:
        output_dir=tmp_path
        recording_mode='opening-screenshots+trace'
        def __init__(self,*a,**kw):pass
        def set_session_metadata(self,*a,**kw):pass
        def save_checkpoint(self,*a,**kw):pass
        def record_phase(self,*a,**kw):pass
        def close(self):
            closed.append(True)
            if close_error:raise OSError('close unavailable')
    monkeypatch.setattr(profile_play_action,'RealtimeDebugRecorder',Recorder)
    def stop(*a,**kw):
        context.tasker.stopping=True
        raise InterruptedError('user stop')
    monkeypatch.setattr(profile_play_action,'wait_for_final_cover',stop)
    params={'difficulty':'Expert','require_profile':False,'run_mode':'cooperative','opening_recording':True}
    assert RealtimeProfilePlay()._run(context,SimpleNamespace(custom_action_param=json.dumps(params))) is True
    assert closed==[True]
    assert not list(tmp_path.glob('**/realtime-result-*.json'))


def test_nested_refresh_invalidates_old_proxy_and_foreground_can_replace_it_again(monkeypatch):
    class Proxy:
        valid=True
        @property
        def info(self):
            assert self.valid,'stale proxy used'
            return {'offline':True}
    class Context:
        def __init__(self):self.current=Proxy();self.tasker=SimpleNamespace(stopping=False)
        def run_task(self,node):
            self.current.valid=False;self.current=Proxy()
            self.current.cached_image=object()
            return SimpleNamespace(status=SimpleNamespace(succeeded=True))
    context=Context()
    class Tasker:
        stopping=False
        @property
        def controller(self):return context.current
    context.tasker=Tasker()
    old=context.tasker.controller
    recorder=SimpleNamespace(record_phase=lambda *a,**kw:None)
    profile_play_action._opening_native_wait_kwargs(context,recorder,True)['wait_observer']()
    def guard(controller):
        assert controller.info['offline']
        context.run_task('foreground-side-effect')
    monkeypatch.setattr(profile_play_action,'require_game_foreground',guard)
    current=profile_play_action._fresh_foreground_controller(context)
    assert not old.valid and current is context.current and current.info['offline']


def test_setup_stop_cleans_consumed_native_and_warns_if_cleanup_fails(monkeypatch,tmp_path,capsys):
    reset_live_run(mode="formal",difficulty="Expert",prepared_for_play=True)
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False,controller=Controller()))
    settings=SimpleNamespace(target_fps=60,timing_offset_ms=17,note_speed=10.0,profile_path=SimpleNamespace(name="expert.json"))
    selection=SimpleNamespace(path=Path('chart-48-expert.json'),timeline=object(),bestdori_song_id=48,difficulty='expert')
    monkeypatch.setattr(profile_play_action,'PROJECT_ROOT',tmp_path)
    monkeypatch.setattr(profile_play_action.RealtimeProfileStore,'resolve_latest',lambda *a,**kw:settings)
    monkeypatch.setattr(profile_play_action.RealtimeProfileStore,'runtime_options',lambda *a,**kw:{
        'chart_prediction_enabled':False,'chart_predict_presses':False,'native_realtime_enabled':True})
    monkeypatch.setattr(profile_play_action,'resolve_local_chart_for_run',lambda *a,**kw:SimpleNamespace(selection=selection,reason='matched'))
    monkeypatch.setattr(profile_play_action,'debug_recording_enabled',lambda _argv:False)
    monkeypatch.setattr(profile_play_action,'diagnostic_trace_enabled',lambda:False)
    monkeypatch.setattr(profile_play_action,'opening_recording_enabled',lambda argv:False)
    monkeypatch.setattr(profile_play_action,'require_game_foreground',lambda _:pytest.fail('stop优先，不能再查前台'))
    stopped=[]
    class Backend:
        def configure_timing_offset(self,value):context.tasker.stopping=True
        def stop(self):stopped.append(True);raise OSError('cleanup unavailable')
    monkeypatch.setattr(profile_play_action,'consume_prearmed_backend',lambda *a:Backend())
    assert RealtimeProfilePlay()._run(context,SimpleNamespace(custom_action_param=json.dumps({'difficulty':'Expert'}))) is True
    assert stopped==[True]
    assert 'stop_cleanup_warning' in capsys.readouterr().out
    assert not list(tmp_path.glob('**/realtime-result-*.json'))


@pytest.mark.parametrize("invalid", [{"release_confirmed": False}, {"executed": 1}, {"underflows": 1}, {"executed_observation_complete": False}])
def test_completed_visual_does_not_hide_native_integrity_failure(monkeypatch, tmp_path, invalid):
    report = {"planned": 637, "sent": 637, "executed": 637, "underflows": 0,
              "executed_observation_complete": True, "state": "finished",
              "session_state": "finished", "release_confirmed": True, "stop_latency_ms": 120.0}
    with pytest.raises(RuntimeError, match="完整性门禁"):
        _completed_play_harness(monkeypatch, tmp_path, debug_recording=False,
                                native_report=report | invalid)
    assert current_live_run().play_completed is False
