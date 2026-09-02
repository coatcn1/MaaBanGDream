"""Native Realtime Engine V2：binding、差分、调度与相位同步测试。

真实 trace 用例只在本机存在 `.local` 证据时运行；其他机器上自动跳过，
保证 `scripts/verify.ps1` 可移植。
"""

from __future__ import annotations

import json
import socket
import threading
import sys
from pathlib import Path

import pytest

from agent.realtime import native_engine
from agent.realtime.chart_timeline import ChartTimeline
from scripts import native_sync_offline as sync_front
from scripts.diff_native_timeline import compare_actions, diff_chart
from scripts.pure_chart_reference import compile_actions as reference_compile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_306 = PROJECT_ROOT / "resource" / "charts" / "bestdori" / "306" / "hard.json"
CHART_64 = PROJECT_ROOT / "resource" / "charts" / "bestdori" / "64" / "expert.json"
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
def test_pure_chart_actions_match_python_reference(chart_path: Path):
    if not native_engine.available():
        pytest.skip("native 未构建")
    python_timeline = ChartTimeline.from_json(chart_path)
    reference = reference_compile(python_timeline)
    native_timeline = native_engine.compile_chart(chart_path)
    native = native_timeline.compile_actions({})
    report = compare_actions(reference, native)
    assert report["mismatches"] == 0, json.dumps(report["details"], ensure_ascii=False)


@requires_native
def test_diff_tool_reports_zero_mismatches():
    report = diff_chart(CHART_64)
    assert report["mismatches"] == 0
    assert report["reference_actions"] == report["native_actions"] > 0


@requires_native
def test_touch_script_compiler_covers_full_song_duration():
    timeline = native_engine.compile_chart(CHART_64)
    actions = timeline.compile_actions({})
    script = native_engine.compile_touch_script(
        actions,
        start_engine_time=0.0,
    )
    waits = [int(line[2:]) for line in script if line.startswith("w ")]
    total_ms = sum(waits)
    # 补偿模型要求每个 w 前有 c 冲刷触点；脚本首行因此是 c。
    assert script and script[0] == "c\n"
    assert total_ms == pytest.approx(timeline.end_time_s * 1000.0, abs=3.0)


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


def test_engine_selection_defaults_to_legacy():
    # Native 默认关闭，即使模块可用也不接管真实演奏。
    assert native_engine.resolve_engine(
        {"native_realtime_enabled": False},
        chart_available=True,
    ) == "legacy"
    assert native_engine.resolve_engine(None, chart_available=True) == "legacy"
    # 显式开启但谱面不可用同样回退。
    assert native_engine.resolve_engine(
        {"native_realtime_enabled": True},
        chart_available=False,
    ) == "legacy"


def test_engine_selection_falls_back_when_import_fails(monkeypatch):
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
    assert native_engine.resolve_engine(
        {"native_realtime_enabled": True},
        chart_available=True,
    ) == "legacy"


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
