"""Native Realtime Engine V2：binding、差分、调度与相位同步测试。

真实 trace 用例只在本机存在 `.local` 证据时运行；其他机器上自动跳过，
保证 `scripts/verify.ps1` 可移植。
"""

from __future__ import annotations

import json
import socket
import threading
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from agent.realtime import native_engine
from agent.realtime import native_play as native_play_module
from agent.realtime.chart_timeline import ChartTimeline
from agent.realtime.engine import RealtimeEngine
from agent.realtime.life_monitor import LifeGuard, LifeReading
from agent.realtime.native_minitouch import NativeMinitouchDevice
from agent.realtime.native_play import (
    NativeMinitouchBackend,
    NativeStartPhotogate,
    resolve_native_start_gate_policy,
)
from agent.realtime.run_reporting import result_report_payload
from scripts import native_sync_offline as sync_front


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_306 = PROJECT_ROOT / "resource" / "charts" / "bestdori" / "306" / "hard.json"
CHART_64 = PROJECT_ROOT / "resource" / "charts" / "bestdori" / "64" / "expert.json"
CHART_48 = PROJECT_ROOT / "resource" / "charts" / "bestdori" / "48" / "expert.json"
CHART_165 = (
    PROJECT_ROOT / "resource" / "charts" / "bestdori" / "165" / "expert.json"
)
TRACE_64 = (
    PROJECT_ROOT / ".local" / "cooperative-regression-20260901-2228"
    / "realtime-20260901-222842" / "trace.jsonl"
)
TRACE_165 = (
    PROJECT_ROOT / ".local" / "local-regression-20260901-2237-2244"
    / "realtime-20260901-224424" / "trace.jsonl"
)


requires_native = pytest.mark.skipif(
    not native_engine.available(),
    reason="Native 模块未构建（运行 scripts/build_native_realtime.ps1）",
)


@pytest.mark.skipif(not native_engine.available(), reason="native 未构建")
def test_native_module_imports_and_has_version():
    assert native_engine.native_version()
    assert native_engine.unavailable_reason() is None


@requires_native
def test_native_chart_timeline_matches_python_counts():
    python = ChartTimeline.from_json(CHART_306)
    native = native_engine.compile_chart(CHART_306)
    assert native.judgement_count == len(python.judgements)
    assert native.hold_count == len(python.hold_paths)
    assert native.start_time_s == pytest.approx(python.start_time_s)
    assert native.end_time_s == pytest.approx(python.end_time_s)
    assert native.bestdori_song_id == 306
    assert native.difficulty == "hard"
    assert native.level == 20


@pytest.mark.parametrize("chart_path", [CHART_306, CHART_64, CHART_165])
def test_native_pure_chart_keeps_non_hold_judgements(chart_path: Path):
    if not native_engine.available():
        pytest.skip("native 未构建")
    python_timeline = ChartTimeline.from_json(chart_path)
    native = native_engine.compile_chart(chart_path).compile_actions({})
    transient = {
        int(action["note_index"]): action
        for action in native
        if action["kind"] in {"tap", "flick"}
        and int(action["contact"]) < 0
    }
    expected = [
        judgement
        for judgement in python_timeline.judgements
        if judgement.kind == "tap"
    ]
    assert len(transient) == len(expected)
    for judgement in expected:
        action = transient[judgement.note_index]
        assert action["due_s"] == pytest.approx(judgement.time_s)
        assert int(action["lane"]) == judgement.lane
        assert action["kind"] == ("flick" if judgement.flick else "tap")


def _compile_full_touch_script(actions, *, end_time_s: float) -> list[str]:
    compiler = native_engine.touch_script_compiler()
    return list(compiler.compile(
        list(actions),
        {
            "song_offset_s": 0.0,
            "press_bias_ms": 0,
            "judgement_y": 590.0,
            "lane_centers": [190, 340, 490, 640, 790, 940, 1090],
            "max_wait_ms": 250,
            "tap_duration_ms": 50,
            "flick_duration_ms": 80,
            "slide_step_s": 0.010,
        },
        0.0,
        True,
        float(end_time_s),
    ))


def _assert_protocol_lifecycle(script: list[str]) -> None:
    active: set[int] = set()
    for raw in script:
        parts = raw.strip().split()
        if not parts:
            continue
        command = parts[0]
        if command == "d":
            contact = int(parts[1])
            assert contact not in active, f"重复 DOWN: {raw!r}"
            active.add(contact)
        elif command == "m":
            assert int(parts[1]) in active, f"悬空 MOVE: {raw!r}"
        elif command == "u":
            contact = int(parts[1])
            assert contact in active, f"悬空 UP: {raw!r}"
            active.remove(contact)
        elif command == "r":
            active.clear()
    assert active == set(), f"脚本结束仍有触点未释放: {sorted(active)}"


@requires_native
def test_chart_48_hold_tails_and_protocol_are_complete():
    python_timeline = ChartTimeline.from_json(CHART_48)
    native_timeline = native_engine.compile_chart(CHART_48)
    actions = list(native_timeline.compile_actions({}))
    assert len(python_timeline.hold_paths) == native_timeline.hold_count == 87
    assert sum(path.tail.flick for path in python_timeline.hold_paths) == 12

    by_note: dict[int, list[dict[str, object]]] = {}
    for action in actions:
        by_note.setdefault(int(action["note_index"]), []).append(action)
    lane_centers = [190, 340, 490, 640, 790, 940, 1090]
    for path in python_timeline.hold_paths:
        hold_actions = by_note[path.note_index]
        down = next(action for action in hold_actions if action["kind"] == "down")
        terminal_kind = "flick" if path.tail.flick else "up"
        terminal = next(
            action for action in hold_actions
            if action["kind"] == terminal_kind
            and action["due_s"] == pytest.approx(path.tail.time_s)
        )
        assert terminal["contact"] == down["contact"]
        if path.tail.lane != path.points[-2].lane:
            tail_move = next(
                action for action in hold_actions
                if action["kind"] == "move"
                and action["due_s"] == pytest.approx(path.tail.time_s)
            )
            assert tail_move["contact"] == down["contact"]
            assert tail_move["target_x"] == pytest.approx(
                lane_centers[round(path.tail.lane)]
            )

    script = _compile_full_touch_script(
        actions,
        end_time_s=float(native_timeline.end_time_s) + 0.2,
    )
    _assert_protocol_lifecycle(script)


@requires_native
def test_same_timestamp_chord_downs_commit_without_serial_waits():
    actions = list(native_engine.compile_chart(CHART_48).compile_actions({}))
    groups: dict[float, list[dict[str, object]]] = {}
    for action in actions:
        if action["kind"] in {"tap", "flick"} and int(action["contact"]) < 0:
            groups.setdefault(round(float(action["due_s"]), 6), []).append(action)
    due_s, chord = next(
        (due, group) for due, group in groups.items() if len(group) >= 2
    )
    script = _compile_full_touch_script(chord, end_time_s=due_s + 0.2)
    commands = [line.strip() for line in script if line.strip()]
    down_indexes = [
        index for index, line in enumerate(commands) if line.startswith("d ")
    ]
    assert len(down_indexes) == len(chord)
    # 首拍前允许拆分长 wait；真正的和弦 DOWN 必须连续进入同一 commit。
    assert down_indexes == list(
        range(down_indexes[0], down_indexes[0] + len(chord))
    )
    assert commands[down_indexes[-1] + 1] == "c"
    _assert_protocol_lifecycle(script)


@requires_native
def test_native_minitouch_client_publishes_exact_bytes():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received: list[bytes] = []

    def accept_loop() -> None:
        connection, _ = listener.accept()
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            received.append(chunk)
        connection.close()

    worker = threading.Thread(target=accept_loop, daemon=True)
    worker.start()
    payload = "d 0 10 20 50\nc\nw 12\nu 0\nc\n"
    client = native_engine.minitouch_client()
    assert client.connect("127.0.0.1", port)
    assert client.publish(payload)
    client.close()
    worker.join(timeout=3)
    listener.close()
    assert b"".join(received).decode() == payload


@requires_native
def test_scheduler_deadline_conversion_and_lateness_metrics():
    timeline = native_engine.compile_chart(CHART_306)
    engine = native_engine.NativeRealtimeEngine(timeline)
    engine.start(song_offset_s=-6.0, press_bias_ms=30)
    # 全部动作在到期后 1000 秒一次性派发，lateness 指标必须有值。
    batch = engine.tick(1000.0)
    stats = engine.stats()
    assert len(batch) == len(engine.actions)
    assert stats["dispatched"] == len(engine.actions)
    assert stats["late_count"] == len(engine.actions)
    assert stats["late_max_ms"] > 0
    assert 0 < stats["late_p50_ms"] <= stats["late_p95_ms"]
    assert engine.stop() == []


@requires_native
def test_scheduler_stop_releases_active_hold():
    engine = native_engine.NativeRealtimeEngine(CHART_306)
    first_down = next(
        action for action in engine.actions if action["kind"] == "down"
    )
    first_up = next(
        action for action in engine.actions if action["kind"] == "up"
    )
    engine.start(song_offset_s=0.0)
    # 派发第一个 hold 头但不到尾：此刻该触点必须仍处于按下状态。
    dispatched = engine.tick(first_up["due_s"] - 0.01)
    releases = engine.stop()
    active_downs = [
        action for action in dispatched if action["kind"] == "down"
    ]
    assert active_downs
    assert first_down["due_s"] < first_up["due_s"] - 0.01
    assert releases
    assert all(release["kind"] == "up" for release in releases)
    assert {r["contact"] for r in releases} == {
        action["contact"] for action in active_downs
    }


@requires_native
def test_native_touch_script_uses_controller_touch_line_by_default():
    script = native_engine.compile_touch_script([
        {
            "kind": "tap",
            "lane": 3,
            "contact": -1,
            "target_x": 640.0,
            "due_s": 0.0,
            "note_index": 0,
            "flick_direction": None,
        }
    ])

    assert any(
        line.startswith("d ") and line.split()[2:4] == ["640", "590"]
        for line in script
    )


def test_engine_selection_defaults_to_legacy():
    # Native 默认关闭，即使模块可用也不接管真实演奏。
    assert native_engine.resolve_engine(
        {"native_realtime_enabled": False},
        chart_available=True,
    ) == "legacy"
    assert native_engine.resolve_engine(None, chart_available=True) == "legacy"
    # 显式开启后必须 fail-closed，不得以缺谱面为由静默回退。
    with pytest.raises(RuntimeError, match="谱面"):
        native_engine.resolve_engine(
            {"native_realtime_enabled": True},
            chart_available=False,
        )


def test_engine_selection_fails_closed_when_import_fails(monkeypatch):
    monkeypatch.delitem(
        sys.modules, "maabangdream_realtime", raising=False
    )
    real_native_dir = str(native_engine._NATIVE_DIR)
    monkeypatch.setattr(native_engine, "_module", None)
    monkeypatch.setattr(native_engine, "_import_error", "simulated failure")
    monkeypatch.setattr(
        native_engine,
        "_NATIVE_DIR",
        PROJECT_ROOT / ".local" / "missing-native",
    )
    # 之前测试可能已把真实 native 目录留在 sys.path 上，必须一并移除，
    # 否则 import 仍会成功。
    monkeypatch.setattr(
        sys,
        "path",
        [entry for entry in sys.path if entry != real_native_dir],
    )
    assert native_engine.available() is False
    with pytest.raises(RuntimeError, match="Native"):
        native_engine.resolve_engine(
            {"native_realtime_enabled": True},
            chart_available=True,
        )


def test_native_backend_owns_input_from_first_note_and_reports_session(
    monkeypatch,
):
    events: list[str] = []

    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class NativeBackend:
        exclusive = True
        active = False

        @property
        def takeover(self) -> bool:
            return True

        def arm(self) -> None:
            events.append("arm")

        def observe_start_frame(self, image, now: float) -> float:
            return now + 0.030

        def start(self, anchor_s: float) -> None:
            assert anchor_s > 0
            self.active = True
            events.append("start")

        def poll(self, now: float) -> None:
            assert self.active
            events.append("poll")

        def stop(self) -> None:
            events.append("stop")

        def report(self) -> dict[str, object]:
            return {
                "planned": 637,
                "sent": 637,
                "executed": 632,
                "action_counts": {"tap": 341, "down": 87},
                "chunks": 12,
                "underflows": 0,
                "drift_p50_ms": 0.8,
                "drift_p95_ms": 2.4,
                "drift_max_ms": 5.1,
                "stop_latency_ms": 80.0,
                "reason": "completed",
            }

    class ForbiddenDetector:
        def detect(self, image, now):
            raise AssertionError("Native 接管后不得运行视觉音符检测")

    class ForbiddenPlanner:
        timing_offset_ms = 17

        def update(self, notes, now):
            raise AssertionError("Native 接管后不得运行 Python planner")

        def reset(self, now):
            raise AssertionError("Native 接管后不得派发 Legacy cleanup")

    class ForbiddenTouch:
        def synchronize(self):
            raise AssertionError("Native 接管后不得操作 Legacy 触控")

        def dispatch(self, actions):
            raise AssertionError("Native 接管后不得派发 Legacy 动作")

        def close(self):
            events.append("legacy-close")

    class AliveDetector:
        def detect(self, image):
            return LifeReading(True, 1000)

    class ForbiddenFeedback:
        sightings = 0
        reports = 0

        def detect(self, image):
            raise AssertionError("Native 接管后不得检测 FAST/SLOW")

    class ForbiddenTimingController:
        current_offset_ms = 17
        fast_samples = 0
        slow_samples = 0
        valid_samples = 0
        ignored_samples = 0
        ignored_reasons: dict[str, int] = {}

        def update(self, feedback, now, *, eligible, ignored_reason):
            raise AssertionError("Native 单局偏移必须冻结")

    clock = Clock()
    monkeypatch.setattr(
        "agent.realtime.engine.time.sleep",
        lambda seconds: setattr(clock, "value", clock.value + seconds),
    )
    backend = NativeBackend()
    engine = RealtimeEngine(
        ForbiddenDetector(),
        ForbiddenPlanner(),
        ForbiddenTouch(),
        clock,
        life_detector=AliveDetector(),
        life_guard=LifeGuard(confirm_frames=1),
        timing_feedback_detector=ForbiddenFeedback(),
        timing_controller=ForbiddenTimingController(),
        native_backend=backend,
    )

    def capture() -> np.ndarray:
        events.append("capture")
        clock.value += 0.2
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    stats = engine.run(
        capture,
        lambda: False,
        duration_seconds=1,
        target_fps=60,
    )

    assert events[0] == "arm"
    assert events.count("start") == 1
    assert 4 <= events.count("capture") <= 5
    assert events[-2:] == ["stop", "legacy-close"]
    assert stats.engine_mode == "native"
    assert stats.dispatched_actions == 637
    assert stats.action_counts == {"tap": 341, "down": 87}
    assert stats.native_report["executed"] == 632
    assert stats.native_report["underflows"] == 0
    assert stats.initial_timing_offset_ms == stats.final_timing_offset_ms == 17


def test_native_start_photogate_maps_first_note_to_delayed_anchor():
    gate = NativeStartPhotogate(
        stable_duration_ms=250.0,
        grace_ms=0.0,
        change_threshold=3.0,
        playfield_detector=lambda _image: True,
    )
    stable = np.full((720, 1280, 3), 30, dtype=np.uint8)
    changed = stable.copy()
    changed[510:536, :, :] = 32

    # 60 FPS 下约 250ms 即可完成稳定门控，不能再固定等待 200 帧。
    for index in range(17):
        assert gate.observe(stable, index / 60.0) is None

    anchor = gate.observe(changed, 17 / 60.0)

    assert gate.frozen is True
    assert gate.waited_frames == 15
    assert gate.triggered is True
    assert anchor == pytest.approx(16.5 / 60.0 + 0.190)
    assert gate.report()["photogate_latency_ms"] == pytest.approx(190.0)


def test_native_start_photogate_requires_consecutive_stability_and_abs_change():
    gate = NativeStartPhotogate(
        stable_duration_ms=100.0,
        grace_ms=0.0,
        change_threshold=3.0,
        latency_ms=30.0,
        playfield_detector=lambda _image: True,
    )
    stable = np.full((720, 1280, 3), 30, dtype=np.uint8)
    brighter = stable.copy()
    brighter[510:536] = 32
    darker = stable.copy()
    darker[510:536] = 28

    assert gate.observe(stable, 0.00) is None
    assert gate.observe(stable, 0.05) is None
    # 开场闪光必须打断连续稳定计时，不能累计零散的安静帧。
    assert gate.observe(brighter, 0.08) is None
    assert gate.stable_since_s is None
    assert gate.observe(stable, 0.10) is None
    assert gate.observe(stable, 0.12) is None
    assert gate.observe(stable, 0.21) is None
    assert gate.frozen is True

    # 音符离开检测带造成的变暗同样是有效变化。
    anchor = gate.observe(darker, 0.23)
    assert anchor == pytest.approx(0.250)
    assert gate.trigger_score == pytest.approx(6.0)


def test_native_start_photogate_ignores_prelude_during_grace():
    gate = NativeStartPhotogate(
        stable_duration_ms=100.0,
        grace_ms=500.0,
        change_threshold=3.0,
        latency_ms=30.0,
        playfield_detector=lambda _image: True,
    )
    stable = np.full((720, 1280, 3), 30, dtype=np.uint8)
    prelude = stable.copy()
    prelude[510:536] = 32

    assert gate.observe(stable, 0.00) is None
    assert gate.observe(stable, 0.11) is None
    assert gate.frozen is True
    assert gate.observe(prelude, 0.20) is None
    assert gate.ignored_prelude_events == 1
    assert gate.observe(stable, 0.30) is None

    anchor = gate.observe(prelude, 0.62)
    assert anchor == pytest.approx(0.650)
    assert gate.triggered is True
    report = gate.report()
    assert report["photogate_wait_ms"] == pytest.approx(620.0)
    assert report["photogate_grace_ms"] == 500.0
    assert [event["event"] for event in report["photogate_events"]] == [
        "playfield-visible",
        "stable",
        "ignored-prelude",
        "ignored-prelude",
        "trigger",
    ]


def test_native_start_photogate_rejects_transition_before_playfield():
    gate = NativeStartPhotogate(
        stable_duration_ms=100.0,
        grace_ms=500.0,
        change_threshold=3.0,
        latency_ms=30.0,
    )
    loading = np.full((720, 1280, 3), 30, dtype=np.uint8)
    loading_transition = loading.copy()
    loading_transition[510:536] = 80

    assert gate.observe(loading, 0.00) is None
    assert gate.observe(loading, 0.11) is None
    assert gate.observe(loading, 0.30) is None
    # 加载页停稳后出现的全屏转场不能冒充首音。
    assert gate.observe(loading_transition, 0.70) is None
    assert gate.triggered is False

    playfield = loading.copy()
    cv2.rectangle(playfield, (942, 35), (964, 51), (80, 220, 40), -1)
    cv2.rectangle(playfield, (968, 29), (1184, 55), (210, 210, 210), 2)
    cv2.rectangle(playfield, (970, 32), (1181, 52), (80, 220, 40), -1)
    for center in (190, 340, 490, 640, 790, 940, 1090):
        cv2.circle(playfield, (center, 590), 10, (220, 220, 220), -1)
    first_note = playfield.copy()
    first_note[510:536] = 32

    assert gate.observe(playfield, 1.00) is None
    assert gate.observe(playfield, 1.11) is None
    assert gate.observe(playfield, 1.40) is None
    assert gate.observe(playfield, 1.65) is None
    anchor = gate.observe(first_note, 1.75)

    assert anchor is not None
    assert gate.triggered is True


def test_native_start_gate_uses_lifecycle_specific_stability_not_song_offset():
    single = resolve_native_start_gate_policy("calibration-rehearsal")
    cooperative = resolve_native_start_gate_policy("cooperative")

    assert single.mode == "single-playfield-first-note"
    assert single.stable_duration_ms == 250.0
    assert cooperative.mode == "cooperative-playfield-confirmed"
    assert cooperative.stable_duration_ms == 120.0
    assert single.grace_ms == cooperative.grace_ms == 500.0


def test_engine_waits_for_photogate_then_switches_to_5hz_monitor(monkeypatch):
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class NativeBackend:
        exclusive = True
        active = False
        observed_at: list[float] = []
        started_at: float | None = None

        @property
        def takeover(self) -> bool:
            return True

        def arm(self) -> None:
            pass

        def observe_start_frame(self, image, now: float) -> float | None:
            self.observed_at.append(now)
            return now + 0.030 if len(self.observed_at) == 3 else None

        def start(self, anchor_s: float) -> None:
            self.active = True
            self.started_at = anchor_s

        def poll(self, now: float) -> None:
            assert self.active

        def stop(self) -> None:
            pass

        def report(self) -> dict[str, object]:
            return {
                "planned": 1,
                "sent": 1,
                "executed": 1,
                "action_counts": {"tap": 1},
            }

    class ForbiddenDetector:
        def detect(self, image, now):
            raise AssertionError("photogate 前后均不得运行音符检测")

    class ForbiddenPlanner:
        timing_offset_ms = 0

        def update(self, notes, now):
            raise AssertionError("photogate 前后均不得运行 planner")

        def reset(self, now):
            raise AssertionError("Native 不得运行 Legacy cleanup")

    class Touch:
        def dispatch(self, actions):
            raise AssertionError("Native 等待首拍时也不得派发 Legacy 输入")

        def close(self):
            pass

    backend = NativeBackend()

    class PostStartLifeDetector:
        calls = 0

        def detect(self, image):
            assert backend.active, "photogate 触发前只允许截图与首拍检测"
            self.calls += 1
            return LifeReading(True, 1000)

    clock = Clock()
    capture_times: list[float] = []
    life_detector = PostStartLifeDetector()
    monkeypatch.setattr(
        "agent.realtime.engine.time.sleep",
        lambda seconds: setattr(clock, "value", clock.value + seconds),
    )
    engine = RealtimeEngine(
        ForbiddenDetector(),
        ForbiddenPlanner(),
        Touch(),
        clock,
        life_detector=life_detector,
        life_guard=LifeGuard(confirm_frames=1),
        native_backend=backend,
    )

    def capture() -> np.ndarray:
        capture_times.append(clock.value)
        clock.value += 0.002
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    engine.run(
        capture,
        lambda: False,
        duration_seconds=1,
        target_fps=60,
    )

    assert backend.started_at == pytest.approx(backend.observed_at[2] + 0.030)
    assert backend.observed_at[1] - backend.observed_at[0] < 0.030
    post_start_gaps = [
        current - previous
        for previous, current in zip(capture_times[2:], capture_times[3:])
    ]
    assert post_start_gaps
    assert min(post_start_gaps) >= 0.19
    assert life_detector.calls >= 1


def test_result_payload_exposes_native_session_counts():
    from agent.realtime.engine import EngineStats

    stats = EngineStats(
        processed_frames=7,
        dispatched_actions=637,
        stopped=False,
        engine_mode="native",
        native_report={
            "planned": 637,
            "sent": 637,
            "executed": 632,
            "chunks": 12,
            "underflows": 0,
        },
    )

    payload = result_report_payload(
        None,
        stats,
        timing_offset_ms=17,
        suggested_timing_offset_ms=None,
    )

    assert payload["engine_mode"] == "native"
    assert payload["native"]["planned"] == 637
    assert payload["native"]["sent"] == 637
    assert payload["native"]["executed"] == 632


def test_native_backend_publishes_first_chunk_from_photogate_anchor(monkeypatch):
    actions = [
        {
            "kind": "tap",
            "due_s": 2.0,
            "lane": 1,
            "contact": -1,
            "target_x": 340.0,
            "flick_direction": 0,
            "note_index": 0,
        },
        {
            "kind": "tap",
            "due_s": 2.5,
            "lane": 2,
            "contact": -1,
            "target_x": 490.0,
            "flick_direction": 0,
            "note_index": 1,
        },
    ]

    class Timeline:
        def compile_actions(self, config):
            return list(actions)

    class Offsets:
        def __init__(
            self,
            *,
            down_ms=0.0,
            up_ms=0.0,
            move_ms=0.0,
            wait_ms=0.0,
            interval_ms=0.0,
        ):
            self.down_ms = down_ms
            self.up_ms = up_ms
            self.move_ms = move_ms
            self.wait_ms = wait_ms
            self.interval_ms = interval_ms

    class Calibrator:
        def __init__(self):
            self.offsets = Offsets(down_ms=0.25)
            self.event_count = 0
            self.sample_counts = {
                "down": 1,
                "up": 0,
                "move": 0,
                "wait": 0,
                "interval": 0,
            }

        def observe(self, event):
            self.event_count += 1

        def correction_ms(self, previous):
            return 0.0

        def reset(self):
            self.event_count = 0
            self.offsets = Offsets(down_ms=0.25)

    compiler_calls: list[tuple] = []

    class Compiler:
        def __init__(self):
            self.offsets = Offsets(move_ms=7.0)
            self._receipts: list[dict[str, object]] = []

        def compile(self, *args):
            compiler_calls.append(args)
            compiled_actions = list(args[0])
            if compiled_actions:
                script = [
                    "d 7 340 590 50\n",
                    "d 8 490 590 50\n",
                    "c\n",
                ]
                self._receipts = [
                    {
                        "line_index": index,
                        "planned_engine_s": float(action["due_s"]),
                        "action_token": index + 1,
                        "command": "d",
                    }
                    for index, action in enumerate(compiled_actions)
                ]
                return script
            self._receipts = []
            return ["c\n"]

        def execution_receipts(self):
            return list(self._receipts)

        def add_residual_ms(self, value):
            assert value == pytest.approx(0.0)

        def set_offsets(self, offsets):
            self.offsets = offsets

    class Device:
        connected = False
        max_x = 1280
        max_y = 720
        max_contacts = 10
        recent_logs: list[str] = []

        def __init__(self):
            self.published: list[str] = []
            self.emergency_stops = 0
            self.logs: list[str] = []
            self.device_ms = 1000.0

        def start(self, *, cancel_event=None):
            assert cancel_event is not None
            self.connected = True

        def publish(self, text: str):
            self.published.append(text)
            for command in text.splitlines():
                command = command.strip()
                if not command:
                    continue
                event = {
                    "st": self.device_ms,
                    "et": self.device_ms + 0.1,
                    "c": 0.1,
                    "cmd": command,
                }
                self.logs.append("jlog " + json.dumps(event))
                self.device_ms += 0.1

        def logs_since(self, cursor: int):
            return len(self.logs), self.logs[cursor:]

        def request_reset(self) -> bool:
            return False

        def emergency_stop(self):
            self.emergency_stops += 1
            self.connected = False
            return True

        def stop(self):
            self.connected = False
            return True

    class Session:
        def __init__(self, publish):
            self._publish = publish
            self.state = "idle"
            self.sent = 0
            self.chunks = 0
            self.anchor = None
            self.executed = 0
            self.execution_observations: list[tuple[float, float]] = []
            self.owner_threads: list[int] = []
            self.finished_event = threading.Event()

        def _record_owner(self):
            self.owner_threads.append(threading.get_ident())

        def arm(self, session_actions, config):
            self._record_owner()
            assert session_actions == actions
            self.state = "armed"
            return True

        def start(self, anchor):
            self._record_owner()
            self.anchor = anchor
            self.state = "running"
            return True

        def publish(self):
            self._record_owner()
            if self.chunks >= 2:
                return False
            first = self.chunks == 0
            ok = self._publish({
                "sequence": self.chunks + 1,
                "window_start_s": 10.0 if first else 10.7,
                "window_end_s": 10.7 if first else 10.8,
                "actions": (
                    [
                        {"action": actions[0], "engine_due_s": self.anchor},
                        {
                            "action": actions[1],
                            "engine_due_s": self.anchor + 0.5,
                        },
                    ]
                    if first else []
                ),
                # 所有高层动作已 sent 后仍需空的尾事件切片。
                "final_chunk": not first,
            })
            if ok:
                self.chunks += 1
                if first:
                    self.sent = len(actions)
            return ok

        def poll(self):
            self._record_owner()
            return self.state

        def cancel(self, reason):
            self._record_owner()
            self.state = "cancelled"
            return True

        def finish(self, reason):
            self._record_owner()
            if self.sent != len(actions):
                return False
            self.state = "finished"
            self.finished_event.set()
            return True

        def observe_minitouch_log(self, event):
            self._record_owner()

        def observe_execution(self, planned, actual, count):
            self._record_owner()
            if self.executed + count > self.sent:
                return False
            self.execution_observations.append((planned, actual))
            self.executed += count
            return True

        def reset_calibration(self):
            self._record_owner()

        def report(self):
            self._record_owner()
            return {
                "planned": len(actions),
                "sent": self.sent,
                "executed": self.executed,
                "chunks": self.chunks,
                "underflows": 0,
                "reason": self.state,
            }

    device = Device()
    sessions: list[Session] = []

    def session_factory(**kwargs):
        session = Session(kwargs["publish"])
        sessions.append(session)
        return session

    monkeypatch.setattr(native_engine, "available", lambda: True)
    monkeypatch.setattr(native_engine, "compile_chart", lambda path: Timeline())
    monkeypatch.setattr(
        native_engine, "touch_script_compiler", lambda offsets=None: Compiler()
    )
    monkeypatch.setattr(native_engine, "latency_calibrator", Calibrator)
    monkeypatch.setattr(
        native_engine,
        "parse_minitouch_log",
        lambda line: {
            "start_ms": json.loads(line[5:])["st"],
            "end_ms": json.loads(line[5:])["et"],
            "cost_ms": json.loads(line[5:])["c"],
            "command": json.loads(line[5:])["cmd"],
        },
    )
    backend = NativeMinitouchBackend(
        "chart.json",
        adb_path="adb",
        serial="serial",
        clock=lambda: 10.0,
        press_bias_ms=0,
        device=device,
        session_factory=session_factory,
        photogate=NativeStartPhotogate(
            stable_duration_ms=1.0,
            grace_ms=0.0,
            playfield_detector=lambda _image: True,
        ),
        publisher_poll_ms=5,
        require_probe=True,
    )

    backend.arm()
    backend.arm()
    assert backend.wait_until_ready(1.0) is True
    assert len(sessions) == 1
    backend.configure_timing_offset(17)
    stable = np.full((720, 1280, 3), 30, dtype=np.uint8)
    changed = stable.copy()
    changed[510:536] = 32
    assert backend.observe_start_frame(stable, 9.8) is None
    assert backend.observe_start_frame(stable, 9.9) is None
    anchor = backend.observe_start_frame(changed, 10.0)
    # Profile 正偏移沿用既有语义（提前输入），且本局启动后保持冻结。
    assert anchor == pytest.approx(10.122)

    backend.start(anchor)
    with pytest.raises(RuntimeError, match="启动前"):
        backend.configure_timing_offset(33)
    assert sessions[0].finished_event.wait(timeout=1)
    backend.stop()

    assert sessions[0].anchor == pytest.approx(10.122)
    assert len(compiler_calls) == 2
    (
        compiled_actions,
        config,
        start_s,
        final_chunk,
        end_s,
        future_down_reservations,
    ) = compiler_calls[0]
    assert [item["note_index"] for item in compiled_actions] == [0, 1]
    assert [item["due_s"] for item in compiled_actions] == pytest.approx(
        [10.122, 10.622]
    )
    assert config["song_offset_s"] == config["press_bias_ms"] == 0
    assert start_s == 10.0
    assert final_chunk is False
    assert end_s == 10.7
    assert future_down_reservations == []
    assert compiler_calls[1][0] == []
    assert compiler_calls[1][3] is True
    assert compiler_calls[1][4] == 10.8
    assert device.published[-1].startswith("c\n")
    assert backend.report()["frozen_timing_offset_ms"] == 17
    assert backend.report()["touch_y"] == 590.0
    assert backend.report()["executed_observation_supported"] is True
    assert backend.report()["executed_observation_complete"] is True
    assert backend.report()["executed"] == 2
    assert backend.report()["published_commands"] == 4
    assert backend.report()["observed_commands"] == 4
    assert backend.report()["calibration_chunks"] == 2
    assert backend.report()["clock_offset_ms"] is not None
    assert backend.report()["device_offsets"]["move_ms"] == pytest.approx(7.0)
    assert backend.report()["absolute_drift_valid"] is False
    assert backend.report()["timing_gate_passed"] is False
    assert backend.report()["release_confirmed"] is True
    assert len(sessions[0].execution_observations) == 2
    # 两条同相位 DOWN 只有一次 commit，设备可见时刻必须完全相同。
    assert (
        sessions[0].execution_observations[0][1]
        == sessions[0].execution_observations[1][1]
    )
    assert sessions[0].state == "finished"
    assert set(sessions[0].owner_threads) == {
        backend._publisher_thread.ident
    }
    assert threading.get_ident() not in set(sessions[0].owner_threads)

    prearmed_device = Device()
    prearmed = NativeMinitouchBackend(
        "chart.json",
        adb_path="adb",
        serial="serial",
        clock=lambda: 20.0,
        device=prearmed_device,
        session_factory=session_factory,
        photogate=NativeStartPhotogate(
            stable_duration_ms=1.0, grace_ms=0.0
        ),
        require_probe=True,
    )
    prearmed.arm()
    assert prearmed.wait_until_ready(1.0) is True
    prearmed.stop()
    assert prearmed_device.connected is False
    assert sessions[-1].state == "cancelled"
    assert float(prearmed.report()["stop_latency_ms"]) <= 500.0

    class LateDevice(Device):
        def __init__(self):
            super().__init__()
            self.start_entered = threading.Event()
            self.allow_start_return = threading.Event()

        def start(self, *, cancel_event=None):
            assert cancel_event is not None
            self.start_entered.set()
            self.allow_start_return.wait(timeout=2.0)
            self.connected = True

    late_device = LateDevice()
    late = NativeMinitouchBackend(
        "chart.json",
        adb_path="adb",
        serial="serial",
        clock=lambda: 30.0,
        device=late_device,
        session_factory=session_factory,
        photogate=NativeStartPhotogate(
            stable_duration_ms=1.0, grace_ms=0.0
        ),
        require_probe=True,
    )
    late.arm()
    assert late_device.start_entered.wait(timeout=1.0)
    with pytest.raises(RuntimeError, match="未 ready"):
        late.wait_until_ready(0.01)
    late.stop()

    # stop 返回时仍卡住的准备线程必须让释放门禁失败；线程稍后恢复时只能
    # 自清理，禁止重新连上并发布启动 probe。
    assert late.report()["release_confirmed"] is False
    assert late.report()["state"] == "failed"
    late_device.allow_start_return.set()
    late._device_thread.join(timeout=1.0)
    assert late._device_thread.is_alive() is False
    assert late_device.connected is False
    assert late_device.published == []

    class ProbeRaceDevice(Device):
        def __init__(self):
            super().__init__()
            self.publish_entered = threading.Event()
            self.allow_publish = threading.Event()

        def publish(self, text: str):
            self.publish_entered.set()
            self.allow_publish.wait(timeout=2.0)
            if not self.connected:
                raise RuntimeError("device closed before probe commit")
            super().publish(text)

    race_device = ProbeRaceDevice()
    race = NativeMinitouchBackend(
        "chart.json",
        adb_path="adb",
        serial="serial",
        clock=lambda: 40.0,
        device=race_device,
        session_factory=session_factory,
        photogate=NativeStartPhotogate(
            stable_duration_ms=1.0, grace_ms=0.0
        ),
        require_probe=True,
    )
    race.arm()
    assert race_device.publish_entered.wait(timeout=1.0)

    # worker 已通过 cancel 检查但卡在 publish 入口时，取消拿不到提交锁，
    # 必须先关闭设备边界；恢复后的 probe 不能落入传输。
    assert race._cancel_device_start(0.01) is False
    assert race._device_start_cancel.is_set()
    race_device.allow_publish.set()
    race._device_thread.join(timeout=1.0)
    assert race._device_thread.is_alive() is False
    assert race_device.published == []
    race.stop()


def test_native_backend_waits_for_delayed_final_jlog_before_finish():
    class Session:
        def __init__(self):
            self.executed = 0
            self.finish_calls = 0

        def report(self):
            return {
                "planned": 1,
                "sent": 1,
                "executed": self.executed,
            }

        def finish(self, reason):
            assert reason == "all-actions-executed"
            self.finish_calls += 1
            return True

        def poll(self):
            return "finished"

    session = Session()
    backend = object.__new__(NativeMinitouchBackend)
    backend._session = session
    backend._session_report = {}
    backend._session_state = "running"
    backend._session_terminal = threading.Event()
    backend._final_chunk_published = True
    backend._expected_commands = native_play_module.deque([
        native_play_module._ExpectedCommand(
            command="c",
            chunk_sequence=2,
        )
    ])
    backend._observation_error = None

    assert backend._finish_when_fully_published() is False
    assert session.finish_calls == 0
    assert backend._session_state == "running"

    # 模拟最终 commit 的 jlog/动作回执稍后才到达。
    backend._expected_commands.clear()
    session.executed = 1

    assert backend._finish_when_fully_published() is True
    assert session.finish_calls == 1
    assert backend._session_state == "finished"


def test_native_report_rejects_absolute_drift_when_clock_uncertainty_exceeds_1ms():
    backend = object.__new__(NativeMinitouchBackend)
    backend._actions = [{}]
    backend._session_report = {
        "planned": 1,
        "sent": 1,
        "executed": 1,
        "chunks": 1,
        "underflows": 0,
        "drift_p50_ms": 0.2,
        "drift_p95_ms": 0.3,
        "drift_max_ms": 0.4,
    }
    backend._publisher_error = None
    backend._publish_error = None
    backend._observation_error = None
    backend._state = "finished"
    backend._session_state = "finished"
    backend._release_latency_ms = None
    backend._release_confirmed = None
    backend._release_error = None
    backend._device_error = None
    backend._final_chunk_published = True
    backend._expected_commands = native_play_module.deque()
    backend._observation_cancelled = False
    backend._playback_observation_started = True
    backend._clock_basis = "probe-midpoint"
    backend._clock_uncertainty_ms = 1.001
    backend._run_id = "uncertainty-regression"
    backend._first_action_anchor_s = 1.0
    backend._jlog_path = None
    backend._frozen_offsets = {}
    backend._frozen_timing_offset_ms = 0
    backend._published_commands = 2
    backend._observed_commands = 2
    backend._calibration_chunks = 1
    backend._calibration_correction_ms = 0.0
    backend._device_clock_offset_s = 0.0
    backend._last_observed_offsets = {}
    backend._game_terminal_reason = "completed"
    backend._cancelled_pending_commands = 0
    backend._cancelled_pending_actions = 0

    report = backend.report()

    assert report["executed_observation_complete"] is True
    assert report["clock_uncertainty_ms"] == pytest.approx(1.001)
    assert report["absolute_drift_valid"] is False
    assert report["conservative_drift_p95_ms"] is None
    assert report["conservative_drift_max_ms"] is None
    assert report["timing_gate_passed"] is False


def test_native_device_emergency_stop_avoids_adb_cleanup(monkeypatch):
    events: list[str] = []

    class Client:
        connected = True

        def publish(self, text):
            events.append(text)
            return True

        def close(self):
            events.append("close")

    class Process:
        def kill(self):
            events.append("kill")

    device = NativeMinitouchDevice("adb", "serial")
    device._client = Client()
    device._process = Process()
    device._closed = False
    monkeypatch.setattr(
        device,
        "_run_adb",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("emergency_stop 不得等待 adb")
        ),
    )

    assert device.request_reset() is False
    assert device._reset_thread is not None
    device._reset_thread.join(timeout=1.0)
    assert device.emergency_stop() is True

    assert events == ["r\n", "close", "kill"]
    assert device.connected is False


def test_native_device_log_cursor_preserves_identical_jlog_rows():
    device = NativeMinitouchDevice("adb", "serial")
    repeated = 'jlog {"st":1,"et":2,"c":1,"cmd": "c"}'

    device._record_log_line(repeated)
    device._record_log_line(repeated)

    cursor, rows = device.logs_since(0)
    assert cursor == 2
    assert rows == [repeated, repeated]
    assert device.logs_since(cursor) == (cursor, [])


def test_native_device_log_records_keep_receive_clock_for_probe():
    device = NativeMinitouchDevice("adb", "serial")
    row = 'jlog {"st":1,"et":2,"c":1,"cmd": "w 0"}'
    device._record_log_line(row, received_s=123.456)

    cursor, records = device.log_records_since(0)

    assert cursor == 1
    assert records == [(row, 123.456)]


def test_native_backend_fails_closed_on_jlog_command_mismatch(monkeypatch):
    class Device:
        def logs_since(self, cursor):
            assert cursor == 0
            return 1, ['jlog {"cmd": "u 9"}']

    backend = object.__new__(NativeMinitouchBackend)
    backend._device = Device()
    backend._log_cursor = 0
    backend._clock = lambda: 10.0
    backend._playback_observation_started = True
    backend._observation_cancelled = False
    backend._observation_error = None
    backend._last_device_start_ms = None
    backend._last_device_end_ms = None
    backend._expected_commands = native_play_module.deque([
        native_play_module._ExpectedCommand(
            command="c",
            chunk_sequence=7,
        )
    ])
    monkeypatch.setattr(
        native_engine,
        "parse_minitouch_log",
        lambda line: {
            "start_ms": 1.0,
            "end_ms": 1.1,
            "cost_ms": 0.1,
            "command": "u 9",
        },
    )

    with pytest.raises(RuntimeError, match="命令失配"):
        backend._observe_new_logs()
    assert backend._observation_error is not None


@pytest.mark.parametrize(
    ("event", "last_start_ms", "last_end_ms", "reason"),
    [
        (
            {
                "start_ms": float("nan"),
                "end_ms": 1.0,
                "cost_ms": 0.1,
                "command": "c",
            },
            None,
            None,
            "非有限",
        ),
        (
            {
                "start_ms": 2.0,
                "end_ms": 1.0,
                "cost_ms": 0.1,
                "command": "c",
            },
            None,
            None,
            "时间范围无效",
        ),
        (
            {
                "start_ms": 1.0,
                "end_ms": 1.1,
                "cost_ms": -0.1,
                "command": "c",
            },
            None,
            None,
            "时间范围无效",
        ),
        (
            {
                "start_ms": 1.0,
                "end_ms": 1.1,
                "cost_ms": 0.1,
                "command": "c",
            },
            2.0,
            None,
            "时钟倒退",
        ),
        (
            {
                "start_ms": 1.5,
                "end_ms": 1.6,
                "cost_ms": 0.1,
                "command": "c",
            },
            1.0,
            2.0,
            "命令发生重叠",
        ),
    ],
)
def test_native_backend_rejects_invalid_jlog_timing(
    monkeypatch, event, last_start_ms, last_end_ms, reason
):
    class Device:
        def logs_since(self, cursor):
            return 1, ["jlog invalid"]

    backend = object.__new__(NativeMinitouchBackend)
    backend._device = Device()
    backend._log_cursor = 0
    backend._clock = lambda: 10.0
    backend._playback_observation_started = True
    backend._observation_cancelled = False
    backend._observation_error = None
    backend._last_device_start_ms = last_start_ms
    backend._last_device_end_ms = last_end_ms
    backend._expected_commands = native_play_module.deque([
        native_play_module._ExpectedCommand(
            command="c",
            chunk_sequence=1,
        )
    ])
    monkeypatch.setattr(
        native_engine, "parse_minitouch_log", lambda line: dict(event)
    )

    with pytest.raises(RuntimeError, match=reason):
        backend._observe_new_logs()


def test_native_device_log_cursor_detects_ring_buffer_overflow():
    device = NativeMinitouchDevice("adb", "serial")
    for index in range(4097):
        device._record_log_line(
            'jlog {"st":0,"et":0,"c":0,"cmd": "c"}'
        )

    with pytest.raises(RuntimeError, match="队列已溢出"):
        device.logs_since(0)


def test_native_device_emergency_stop_reports_local_close_failure():
    class BrokenClient:
        connected = True

        def close(self):
            raise OSError("simulated close failure")

    device = NativeMinitouchDevice("adb", "serial")
    device._client = BrokenClient()

    assert device.emergency_stop() is False
    assert device.connected is False
    assert device._client is not None


def test_native_device_stop_requires_bounded_remote_pid_evidence(monkeypatch):
    class Client:
        connected = True

        def publish(self, text):
            return text == "r\n"

        def close(self):
            return None

    class Process:
        def kill(self):
            return None

        def wait(self, timeout):
            assert timeout <= 0.05
            return 0

    device = NativeMinitouchDevice("adb", "serial")
    device._closed = False
    device._spawned = True
    device._pid = 2468
    device._port = 13579
    device._client = Client()
    device._process = Process()
    cleanup_calls: list[tuple[str, ...]] = []

    def cleanup(*args, timeout_s):
        assert timeout_s > 0
        cleanup_calls.append(tuple(args))
        return True

    monkeypatch.setattr(device, "_run_adb_cleanup", cleanup)

    assert device.stop_with_deadline(0.5) is True
    assert device.last_reset_sent is True
    assert device._pid is None
    assert device._client is None
    assert device._process is None
    assert cleanup_calls[0][0] == "shell"
    assert cleanup_calls[1] == ("forward", "--remove", "tcp:13579")

    failed = NativeMinitouchDevice("adb", "serial")
    failed._spawned = True
    failed._pid = 9753
    monkeypatch.setattr(
        failed,
        "_run_adb_cleanup",
        lambda *args, timeout_s: False,
    )

    assert failed.stop_with_deadline(0.01) is False
    assert failed._pid == 9753
    assert "未确认退出" in str(failed.last_release_error)

    no_pid = NativeMinitouchDevice("adb", "serial")
    no_pid._spawned = True
    no_pid_calls: list[tuple[str, ...]] = []

    def no_pid_cleanup(*args, timeout_s):
        assert timeout_s > 0
        no_pid_calls.append(tuple(args))
        return True

    monkeypatch.setattr(no_pid, "_run_adb_cleanup", no_pid_cleanup)
    assert no_pid.stop_with_deadline(0.1) is True
    assert no_pid._spawned is False
    assert no_pid._socket_name in no_pid_calls[0][1]

    forward_warning = NativeMinitouchDevice("adb", "serial")
    forward_warning._spawned = True
    forward_warning._pid = 8642
    forward_warning._port = 24680
    monkeypatch.setattr(
        forward_warning,
        "_run_adb_cleanup",
        lambda *args, timeout_s: args[0] == "shell",
    )
    assert forward_warning.stop_with_deadline(0.1) is True
    assert "ADB forward" in str(forward_warning.last_release_error)


def test_native_device_stop_deadline_survives_blocked_reset_and_log_lock(
    monkeypatch,
):
    class BlockingClient:
        connected = True

        def __init__(self):
            self.publish_entered = threading.Event()
            self.release_publish = threading.Event()

        def publish(self, text):
            self.publish_entered.set()
            self.release_publish.wait(timeout=2.0)
            return text == "r\n"

        def close(self):
            self.connected = False

    client = BlockingClient()
    device = NativeMinitouchDevice("adb", "serial")
    device._closed = False
    device._spawned = True
    device._pid = 1234
    device._client = client
    monkeypatch.setattr(
        device,
        "_run_adb_cleanup",
        lambda *args, timeout_s: True,
    )

    started = time.monotonic()
    assert device.stop_with_deadline(0.05) is True
    elapsed = time.monotonic() - started
    assert elapsed < 0.15
    assert client.publish_entered.is_set()
    client.release_publish.set()

    locked = NativeMinitouchDevice("adb", "serial")
    locked._log_lock.acquire()
    try:
        started = time.monotonic()
        assert locked.stop_with_deadline(0.05) is False
        elapsed = time.monotonic() - started
    finally:
        locked._log_lock.release()
    assert elapsed < 0.15


def test_profile_store_defaults_native_realtime_off(tmp_path):
    from agent.realtime.profile_store import RealtimeProfileStore

    store = RealtimeProfileStore(tmp_path)
    options = store.runtime_options()
    assert options["native_realtime_enabled"] is False


@requires_native
def test_sync_sparse_hold_opening_locks_with_anchor():
    # 模拟 Bestdori 165 开场：只有两个 hold head，靠 GO 锚点 + 序列验证锁定。
    chart_json = json.dumps([
        {"type": "BPM", "bpm": 120, "beat": 0},
        {"type": "Long", "connections": [
            {"lane": 0, "beat": 8.0}, {"lane": 0, "beat": 10.333},
        ]},
        {"type": "Long", "connections": [
            {"lane": 4, "beat": 8.0}, {"lane": 4, "beat": 10.333},
        ]},
        {"type": "Single", "lane": 1, "beat": 12.0},
    ])
    module = native_engine._module
    timeline = module.ChartTimeline.from_json(chart_json)
    sync = module.SongClockSynchronizer(timeline, {
        "min_samples_with_anchor": 2,
        "max_mad_s": 0.10,
    })
    sync.set_anchor(5.30, 0.5)
    sync.observe(0, "hold", 9.30)
    assert sync.state()["status"] == "pending"  # 只有一条证据不够。
    sync.observe(4, "hold", 9.44)
    state = sync.state()
    assert state["status"] == "locked"
    assert abs(state["offset_s"] - (-5.30)) < 0.25
    assert state["samples"] == 2
    assert state["lanes"] == 2


@requires_native
def test_sync_wrong_chart_and_prelude_junk_are_rejected():
    module = native_engine._module
    chart_json = json.dumps([
        {"type": "BPM", "bpm": 120, "beat": 0},
        {"type": "Single", "lane": 0, "beat": 8.0},
        {"type": "Single", "lane": 4, "beat": 8.0},
        {"type": "Single", "lane": 6, "beat": 9.0},
    ])
    timeline = module.ChartTimeline.from_json(chart_json)
    sync = module.SongClockSynchronizer(timeline, {})
    sync.set_anchor(5.30, 0.5)
    # 错误的谱面：hold 观测与 tap 判定语义不兼容。
    sync.observe(0, "hold", 9.30)
    sync.observe(4, "hold", 9.44)
    sync.observe(6, "tap", 10.31)
    state = sync.state()
    assert state["status"] != "locked"
    # GO/前奏静态误检：过早的证据落在保护窗内。
    sync2 = module.SongClockSynchronizer(timeline, {})
    sync2.set_anchor(5.30, 0.5)
    sync2.observe(0, "tap", 0.5)
    sync2.observe(4, "tap", 0.7)
    sync2.observe(6, "tap", 0.9)
    assert sync2.state()["status"] == "pending"


@pytest.mark.skipif(not TRACE_64.exists(), reason="失败证据不在本机")
def test_cooperative_64_trace_locks_before_first_hp_loss():
    if not native_engine.available():
        pytest.skip("native 未构建")
    observations = sync_front.extract_observations(TRACE_64, until_s=25.0)
    hp_loss_ms = sync_front.first_hp_loss_ms(TRACE_64)
    timeline = native_engine.compile_chart(CHART_64)
    anchor = sync_front.derive_go_anchor(observations, timeline.start_time_s)
    assert anchor is not None
    sync = native_engine.NativeRealtimeEngine(timeline).synchronizer(
        sync_config={"min_samples_with_anchor": 4},
    )
    sync.set_anchor(*anchor)
    locked_at: float | None = None
    for observation in observations:
        if hp_loss_ms and observation.time_s * 1000 > hp_loss_ms:
            break
        sync.observe(observation.lane, observation.kind, observation.time_s)
        if sync.state()["status"] == "locked":
            locked_at = observation.time_s
            break
    state = sync.state()
    assert state["status"] == "locked"
    assert state["samples"] >= 4
    assert state["lanes"] >= 2
    assert abs(state["offset_s"] - (-anchor[0])) <= anchor[1] + 0.15
    assert locked_at is not None and hp_loss_ms is not None
    assert locked_at * 1000 < hp_loss_ms


@pytest.mark.skipif(not TRACE_165.exists(), reason="失败证据不在本机")
def test_cooperative_165_trace_locks_before_first_hp_loss():
    if not native_engine.available():
        pytest.skip("native 未构建")
    observations = sync_front.extract_observations(TRACE_165, until_s=25.0)
    hp_loss_ms = sync_front.first_hp_loss_ms(TRACE_165)
    timeline = native_engine.compile_chart(CHART_165)
    anchor = sync_front.derive_go_anchor(observations, timeline.start_time_s)
    assert anchor is not None
    sync = native_engine.NativeRealtimeEngine(timeline).synchronizer(
        sync_config={"min_samples_with_anchor": 2, "max_mad_s": 0.10},
    )
    sync.set_anchor(*anchor)
    locked_at: float | None = None
    for observation in observations:
        if hp_loss_ms and observation.time_s * 1000 > hp_loss_ms:
            break
        sync.observe(observation.lane, observation.kind, observation.time_s)
        if sync.state()["status"] == "locked":
            locked_at = observation.time_s
            break
    state = sync.state()
    assert state["status"] == "locked"
    assert state["samples"] == 2
    assert state["lanes"] == 2
    assert abs(state["offset_s"] - (-anchor[0])) <= anchor[1] + 0.15
    assert locked_at is not None and hp_loss_ms is not None
    assert locked_at * 1000 < hp_loss_ms


@pytest.mark.skipif(not TRACE_64.exists(), reason="失败证据不在本机")
@pytest.mark.parametrize(
    ("trace_path", "chart_path"),
    [(TRACE_64, CHART_165), (TRACE_165, CHART_64)],
)
def test_failed_traces_reject_wrong_chart(trace_path: Path, chart_path: Path):
    if not native_engine.available():
        pytest.skip("native 未构建")
    if not trace_path.exists():
        pytest.skip("失败证据不在本机")
    observations = sync_front.extract_observations(trace_path, until_s=25.0)
    hp_loss_ms = sync_front.first_hp_loss_ms(trace_path)
    timeline = native_engine.compile_chart(chart_path)
    anchor = sync_front.derive_go_anchor(observations, timeline.start_time_s)
    sync = native_engine.NativeRealtimeEngine(timeline).synchronizer(
        sync_config={"min_samples_with_anchor": 2, "max_mad_s": 0.10},
    )
    if anchor is not None:
        sync.set_anchor(*anchor)
    for observation in observations:
        if hp_loss_ms and observation.time_s * 1000 > hp_loss_ms:
            break
        sync.observe(observation.lane, observation.kind, observation.time_s)
        if sync.state()["status"] == "locked":
            break
    assert sync.state()["status"] != "locked"


@pytest.mark.skipif(not TRACE_64.exists(), reason="失败证据不在本机")
def test_static_go_prelude_junk_produces_no_observations():
    # 开场 0~5 秒只有 GO/前奏静态残影，运动门禁必须全部排除。
    observations = sync_front.extract_observations(TRACE_64, until_s=5.0)
    assert observations == []
