from collections import Counter, deque
from dataclasses import FrozenInstanceError, replace
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativeMinitouchBackend, NativePhaseStatus
from agent.realtime import native_engine
from agent.realtime.native_prearm import frozen_native_runtime_options
from agent.realtime.live_session import current_live_run, reset_live_run
from agent.realtime.profile_play_action import StallSafeCapture
from agent.realtime.profile_store import RealtimeProfileStore
from agent.realtime.timing_feedback import TimingFeedback, probe_judgement_feedback


@pytest.mark.parametrize("life", [False, True])
@pytest.mark.parametrize("wait", [False, True])
def test_settings_replace_roundtrip_and_frozen_run(tmp_path, life, wait):
    store = RealtimeProfileStore(tmp_path)
    options = store.runtime_options() | {"native_life_feedback_enabled": life,
                                        "native_wait_jitter_filter_enabled": wait}
    assert store.update_runtime_options(options) == options
    assert store.runtime_options() == options
    stale_reference = reset_live_run(mode="formal", difficulty="Expert")
    assert frozen_native_runtime_options(stale_reference, options) == options
    changed = options | {"native_life_feedback_enabled": not life,
                         "native_wait_jitter_filter_enabled": not wait}
    assert frozen_native_runtime_options(stale_reference, changed) == options
    assert frozen_native_runtime_options(current_live_run(), changed) == options
    unrelated = changed | {"skip_result_check": True}
    assert frozen_native_runtime_options(stale_reference, unrelated)["skip_result_check"] is True
    next_run = reset_live_run(mode="formal", difficulty="Expert")
    assert frozen_native_runtime_options(next_run, changed) == changed


@pytest.mark.parametrize("key", ["native_life_feedback_enabled", "native_wait_jitter_filter_enabled"])
@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_new_options_are_strict_booleans(tmp_path, key, value):
    store = RealtimeProfileStore(tmp_path)
    assert store.runtime_options()[key] is False
    with pytest.raises(ValueError, match=key):
        store.update_runtime_options({key: value})


def _sampler():
    requests = []
    backend = SimpleNamespace(_run_id="test", feedback_input_eligible=lambda now: True,
                              status=NativePhaseStatus("test"))
    def request(delta, observed):
        requests.append((delta, observed))
        backend.status = replace(backend.status, state="requested", request_sequence=1,
                                 original_observed_s=observed, delta_ms=delta)
        return True
    backend.request_future_phase = request
    backend.phase_status_snapshot = lambda: backend.status
    backend.revoke_future_phase = lambda reason: None
    return NativeLifeFeedback(backend), requests


def _open(sampler, now=0):
    sampler.observe_scene(now, visible=True)
    assert sampler.active(now)


def _hit(sampler, direction, now, *, age=.01):
    # 与引擎同序：先确认新鲜帧（填 frame_evidence），再走常开计数。
    assert sampler.fresh({"sequence": sampler.last_sequence + 1, "request_s": now - age,
                          "reused": False}, now)
    return sampler.observe_judgement(direction, now, source="steady")


def _feedback_image(direction):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if direction is TimingFeedback.SLOW:
        image[514:556, 570:710] = (0, 140, 255)
    elif direction is TimingFeedback.FAST:
        image[514:556, 570:710] = (255, 180, 0)
    return image


def test_scene_open_marks_active_and_invalid_frame_closes_with_metadata():
    sampler, _ = _sampler()
    _open(sampler)
    assert sampler.active(0)
    assert not sampler.fresh({"sequence": 3, "request_s": .4, "reused": True}, .5)
    closed = sampler.events[-1]
    assert closed["event"] == "slow-or-reused-frames"
    assert closed["frame_sequence"] == 3
    assert closed["request_s"] == .4
    assert closed["reused"] is True
    assert closed["observed_s"] == pytest.approx(.5)
    assert not sampler.active(.5)


def test_two_same_direction_hits_request_one_step():
    sampler, requests = _sampler()
    _open(sampler)
    assert _hit(sampler, TimingFeedback.FAST, 0) == "counted"
    assert _hit(sampler, TimingFeedback.FAST, .05) == "requested"
    assert requests == [(15.0, .05)]
    assert sampler.summary["requests"] == 1
    # 请求后清零同向计数，需重新累积。
    assert sampler.same_reports == 0
    assert _hit(sampler, TimingFeedback.FAST, .10) == "counted"


def test_slow_direction_requests_negative_step():
    sampler, requests = _sampler()
    _open(sampler)
    _hit(sampler, TimingFeedback.SLOW, 0)
    _hit(sampler, TimingFeedback.SLOW, .05)
    assert requests == [(-15.0, .05)]


def test_reverse_hit_reanchors_instead_of_blocking_cluster():
    sampler, requests = _sampler()
    _open(sampler)
    # 单条反向（leading edge）不再整段否决，而是重锚到新方向。
    _hit(sampler, TimingFeedback.SLOW, 0)
    _hit(sampler, TimingFeedback.FAST, .05)
    assert requests == []
    assert sampler.mixed is True
    # 反向后的同向连续命中照样触发。
    _hit(sampler, TimingFeedback.FAST, .10)
    assert requests == [(15.0, .10)]


def test_streak_gap_resets_count():
    sampler, requests = _sampler()
    _open(sampler)
    _hit(sampler, TimingFeedback.FAST, 0)
    # 超过 STREAK_GAP_S(1.0s) 视为新一段，计数清零。
    _hit(sampler, TimingFeedback.FAST, 2.0)
    assert requests == []


def test_ineligible_hit_does_not_reset_streak():
    sampler, requests = _sampler()
    _open(sampler)
    sampler.backend.feedback_input_eligible = lambda now: now >= .02
    assert _hit(sampler, TimingFeedback.FAST, 0) == "ineligible"
    sampler.backend.feedback_input_eligible = lambda now: True
    assert _hit(sampler, TimingFeedback.FAST, .05) == "counted"
    # 不合格命中不清零：第二条合格命中即凑满阈值。
    assert _hit(sampler, TimingFeedback.FAST, .10) == "requested"
    assert len(requests) == 1


def test_request_cooldown_limits_rate():
    sampler, requests = _sampler()
    _open(sampler)
    for index in range(6):
        _hit(sampler, TimingFeedback.FAST, index * .05)
    # 冷却 0.3s：0.25s 内最多两次（0.05 与 0.20 附近的 0.25 不算）。
    assert len(requests) <= 1


def test_probe_judgement_feedback_reads_bar_direction_and_pixels():
    kind, slow_px, fast_px = probe_judgement_feedback(_feedback_image(TimingFeedback.FAST))
    assert kind is TimingFeedback.FAST and fast_px > 0 and slow_px == 0
    kind, slow_px, fast_px = probe_judgement_feedback(_feedback_image(TimingFeedback.SLOW))
    assert kind is TimingFeedback.SLOW and slow_px > 0 and fast_px == 0
    kind, slow_px, fast_px = probe_judgement_feedback(np.zeros((720, 1280, 3), dtype=np.uint8))
    assert kind is None and slow_px == 0 and fast_px == 0


def test_observe_frame_detects_bar_and_counts():
    sampler, requests = _sampler()
    _open(sampler)
    for index in range(3):
        now = index * .05
        assert sampler.fresh({"sequence": index, "request_s": now - .01, "reused": False}, now)
        sampler.observe_frame(_feedback_image(TimingFeedback.FAST), now)
    # 检测器每个判定条周期只上报一次（armed 去重）；常开计数因此只看到一次命中，
    # 引擎主路径改用无去重的 probe_judgement_feedback。
    assert sampler.summary["fast_reports"] == 1
    assert sampler.same_reports == 1
    assert requests == []


def _phase_backend():
    backend = object.__new__(NativeMinitouchBackend)
    backend.life_feedback_enabled = True
    backend._phase_lock = threading.Lock()
    backend._phase_closed = False
    backend._phase_slot = None
    backend._phase_sequence = 0
    backend._phase_events = deque(maxlen=128)
    backend._phase_receipts = deque([(1, .8, 2), (2, .85, 3), (3, .9, 4)])
    backend._phase_holds = Counter()
    backend._phase_slot_polls = 0
    backend._phase_slot_holds = Counter()
    backend._phase_receipt_medians = deque(maxlen=4096)
    backend._clock = lambda: 1.0
    backend._clock_basis = "probe-midpoint"
    backend._clock_uncertainty_ms = .5
    backend._run_id = "test"
    backend._state = backend._session_state = "running"
    backend._compiler = SimpleNamespace(phase_boundary_safe=lambda: True, contact_available_s=lambda: .5)
    calls = []
    backend._session = SimpleNamespace(apply_future_phase=lambda *args: calls.append(args) or "applied",
                                       future_phase_offset_ms=2)
    return backend, calls


def test_mailbox_replaces_single_slot_and_owner_only():
    backend, calls = _phase_backend()
    assert backend.request_future_phase(15, .95)
    # 信箱被占时新请求顶掉旧候选（不再直接丢弃）。
    assert backend.request_future_phase(-15, .95)
    assert calls == []
    backend._consume_phase_candidate()
    assert calls == [(-15, .5)]
    assert backend._phase_events[-1]["event"] == "applied"
    assert backend._phase_holds["replaced-mailbox"] == 1
    backend._phase_lock.acquire()
    assert not backend.request_future_phase(15, 1)
    backend._phase_lock.release()


def test_phase_snapshot_is_immutable_and_readable_while_owner_lock_is_held():
    backend, _ = _phase_backend()
    assert backend.request_future_phase(15, .95)
    original = backend.phase_status_snapshot()
    backend._phase_lock.acquire()
    try:
        assert backend.phase_status_snapshot() == original
        with pytest.raises(FrozenInstanceError):
            original.state = "applied"
    finally:
        backend._phase_lock.release()
    backend._consume_phase_candidate()
    applied = backend.phase_status_snapshot()
    assert original.state == "requested"
    assert applied.state == "applied" and applied.first_adjusted_due_s is None
    assert applied.event_cursor > original.event_cursor
    with backend._phase_lock:
        backend._publish_phase_status(replace(applied, first_adjusted_due_s=1.5,
            first_affected_chunk_sequence=4, first_affected_action_token=17), "effect-published")
    effect = backend.phase_status_snapshot()
    assert effect.event_cursor > applied.event_cursor
    assert applied.first_adjusted_due_s is None
    assert effect.first_adjusted_due_s == 1.5


def test_real_backend_revoke_records_one_state_transition_and_waiting_does_not_spam():
    backend, _ = _phase_backend()
    assert backend.request_future_phase(15, .95)
    backend._compiler.phase_boundary_safe = lambda: False
    for _ in range(4):
        backend._consume_phase_candidate()
    assert [event["event"] for event in backend._phase_events] == ["requested", "waiting-touch-boundary"]
    assert backend._phase_holds["boundary-unsafe"] == 4
    backend.revoke_future_phase("mixed-feedback")
    revoked = [event for event in backend._phase_events if event["event"] == "revoked"]
    assert len(revoked) == 1
    assert revoked[0]["reason"] == "mixed-feedback"
    assert revoked[0]["sequence"] == 1 and revoked[0]["event_cursor"] == 3


@pytest.mark.parametrize("guard", ["hold", "expiry", "stop", "uncertain", "old", "few", "duplicate", "late"])
def test_phase_guards_preserve_dispatch(guard):
    backend, calls = _phase_backend()
    assert backend.request_future_phase(15, .95)
    if guard == "hold":
        backend._compiler.phase_boundary_safe = lambda: False
    elif guard == "expiry":
        backend._clock = lambda: 3
    elif guard == "stop":
        backend._phase_closed = True
    elif guard == "uncertain":
        # 上界放宽到 20ms；25ms 仍视为不可信。
        backend._clock_uncertainty_ms = 25
    elif guard == "old":
        backend._phase_slot = ("old", 1, .95, 15, 2.95)
    elif guard == "few":
        backend._phase_receipts.pop()
    elif guard == "duplicate":
        backend._phase_receipts = deque([(1, .8, 2)] * 3)
    else:
        # 回执窗口 1.5s：超过 1.5s 之前的回执不再计入，样本不足被拒。
        backend._clock = lambda: 2.0
        backend._phase_receipts = deque([(1, .1, 2), (2, .2, 3), (3, .3, 4)])
    backend._consume_phase_candidate()
    assert calls == []
    assert (backend._phase_slot is not None) is (guard == "hold")


def test_phase_gate_diagnostics_carry_receipt_numbers():
    backend, calls = _phase_backend()
    backend._clock_uncertainty_ms = 25
    assert backend.request_future_phase(15, .95)
    backend._consume_phase_candidate()
    assert calls == []
    event = backend._phase_events[-1]
    assert event["event"] == "untrusted-execution-receipts"
    assert event["gate"].startswith("n=3,med=")
    assert "unc=25" in event["gate"]


def test_phase_receipt_stats_summarizes_run_median():
    backend, _ = _phase_backend()
    for error in (2.0, 3.0, 4.0, 40.0):
        backend._phase_receipt_medians.append(error)
    stats = backend._phase_receipt_stats()
    assert stats["samples"] == 4
    assert stats["max_ms"] == 40.0
    assert stats["last_ms"] == 40.0


def test_fresh_capture_prefetch_is_five_hz_without_wait(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("agent.realtime.profile_play_action.time.perf_counter", lambda: clock[0])
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    class Job:
        @property
        def done(self):
            return True
        def get(self):
            return image.copy()
        def wait(self):
            pytest.fail("已启动后的截图不得等待")
    posts = []
    capture = StallSafeCapture(SimpleNamespace(post_screencap=lambda: posts.append(clock[0]) or Job()))
    capture._last_image = image
    capture.enable_fresh_prefetch()
    for tick in range(1, 6):
        target = tick * .2
        clock[0] = target - .060
        capture.prefetch_fresh(target)
        capture.prefetch_fresh(target)
        clock[0] = target
        assert capture() is not None
        assert capture.frame_metadata["sequence"] == tick
        assert capture.frame_metadata["request_s"] == pytest.approx(target - .060)
        assert capture.frame_metadata["reused"] is False
        assert capture.frame_metadata["request_s"] == pytest.approx(posts[-1])
        capture()
        assert capture.frame_metadata["reused"] is True
    assert len(posts) == 5


def test_async_prefetch_accepts_new_frame_before_consumer_without_marking_reused(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("agent.realtime.profile_play_action.time.perf_counter", lambda: clock[0])
    posts = []
    class Job:
        def __init__(self):
            self.requested_s = clock[0]
            self.image = np.full((720, 1280, 3), len(posts) + 1, dtype=np.uint8)
        @property
        def done(self):
            return clock[0] >= self.requested_s + .020
        def get(self):
            assert self.done
            return self.image
        def wait(self):
            pytest.fail("运行中不得新增截图等待")
    def post():
        job = Job()
        posts.append(job)
        return job
    capture = StallSafeCapture(SimpleNamespace(post_screencap=post))
    capture._last_image = np.zeros((720, 1280, 3), dtype=np.uint8)
    capture.enable_fresh_prefetch()
    sampler, _ = _sampler()
    _open(sampler)
    sampler.last_sequence = 0
    capture.prefetch_fresh(.0167)
    clock[0] = .0167
    capture()
    for requested_s, target_s, marker in [(.020, .0334, 1), (.040, .0501, 2), (.060, .0668, 3)]:
        clock[0] = requested_s
        capture.prefetch_fresh(target_s)
        clock[0] = target_s
        image = capture()
        metadata = capture.frame_metadata
        assert int(image[0, 0, 0]) == marker
        assert metadata["sequence"] == marker
        assert metadata["request_s"] == posts[marker - 1].requested_s
        assert metadata["reused"] is False
        assert sampler.fresh(metadata, target_s)
        assert capture() is image
        assert capture.frame_metadata["reused"] is True
    assert sampler.active(.0668)


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_old_binary_api_fails_before_input_and_environment_does_not_override_ui(monkeypatch):
    from pathlib import Path
    chart = Path(__file__).resolve().parents[1] / "resource/charts/bestdori/306/hard.json"
    monkeypatch.setenv("MAABANGDREAM_NATIVE_WAIT_JITTER_TRIAL", "1")
    monkeypatch.setattr(native_engine, "playback_session", lambda **kwargs: SimpleNamespace())
    backend = NativeMinitouchBackend(chart, adb_path="adb", serial="test", wait_jitter_trial_enabled=False)
    assert backend._wait_cost_estimator is None
    with pytest.raises(RuntimeError, match="掉血反馈安全相位接口"):
        NativeMinitouchBackend(chart, adb_path="adb", serial="test", life_feedback_enabled=True)


def _virtual_feedback_engine(monkeypatch, *, terminal=None, enabled=True):
    from agent.realtime import engine as engine_module
    from agent.realtime.life_monitor import LifeGuard, LifeReading
    clock = [.007]
    stopping = [False]
    data = {"captures": [], "life": [], "terminal": [], "requests": [], "finish_at": 6.0}
    monkeypatch.setattr(engine_module.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    class Capture:
        def __call__(self):
            clock[0] += .004
            now = clock[0]
            data["captures"].append(now)
            self.frame_metadata = {"sequence": len(data["captures"]), "request_s": now - .01, "reused": False}
            if terminal == "stop" and now >= data["finish_at"]:
                stopping[0] = True
            return _feedback_image(TimingFeedback.FAST)
        def enable_fresh_prefetch(self):
            pass
        def prefetch_fresh(self, target):
            pass
    class Life:
        def detect(self, _image):
            data["life"].append(clock[0])
            return LifeReading(True, 1000)
    class Monitor:
        active = False
        def mark_active(self, now):
            self.active = True
        def observe(self, _image, now):
            data["terminal"].append(now)
            if terminal == "completed" and now >= data["finish_at"]:
                return "completed"
            return "active"
    class Backend:
        exclusive = takeover = True
        life_feedback_enabled = enabled
        active = finished = False
        _run_id = "test"
        phase = NativePhaseStatus("test")
        def phase_status_snapshot(self):
            return self.phase
        def arm(self):
            pass
        def observe_start_frame(self, _image, now):
            return now if now >= .1 else None
        def start(self, anchor):
            self.active = True
            data["anchor"] = anchor
        def poll(self, now):
            pass
        def stop(self):
            pass
        def report(self):
            return {}
        def feedback_input_eligible(self, now):
            return True
        def request_future_phase(self, delta, source):
            data["requests"].append((delta, source, clock[0]))
            self.phase = replace(self.phase, state="requested", request_sequence=1,
                                 original_observed_s=source, delta_ms=delta)
            return True
        def revoke_future_phase(self, reason):
            pass
    class Recorder:
        def record(self, *_args, **_kwargs):
            pass
        def close(self):
            pass
        def record_judgement_sniff(self, *_args, **_kwargs):
            pass
    class Failed:
        def observe(self, image, now):
            return terminal == "life-failed" and now >= data["finish_at"]
    backend = Backend()
    planner = SimpleNamespace(timing_offset_ms=13, update=lambda *_args: pytest.fail("Native 不使用 Legacy planner"))
    engine = engine_module.RealtimeEngine(SimpleNamespace(), planner, SimpleNamespace(close=lambda: None),
        clock=lambda: clock[0], native_backend=backend, life_detector=Life(), life_guard=LifeGuard(confirm_frames=1),
        playfield_monitor=Monitor(), live_failed_detector=Failed(), debug_recorder=Recorder())
    stats = engine.run(Capture(), lambda: stopping[0], duration_seconds=7.3, target_fps=60)
    return stats, data, backend


def test_engine_always_on_counts_judgement_and_freezes_profile_offset(monkeypatch):
    stats, data, backend = _virtual_feedback_engine(monkeypatch)
    # 常开 5Hz：整个演奏段都在采样，不再受 3 秒周期窗限制。
    assert stats.timing_feedback_fast >= 10
    assert stats.timing_feedback_valid >= 10
    assert len(data["requests"]) >= 1
    assert data["requests"][0][0] == 15
    assert stats.initial_timing_offset_ms == stats.final_timing_offset_ms == 13


@pytest.mark.parametrize("terminal", ["completed", "life-failed", "stop"])
def test_engine_terminal_paths_keep_always_on_sampling(monkeypatch, terminal):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, terminal=terminal)
    assert stats.completed is (terminal == "completed")
    assert stats.life_failed is (terminal == "life-failed")
    assert stats.stopped is (terminal == "stop")
    assert stats.timing_feedback_fast >= 10


def test_engine_disabled_feedback_retains_native_five_hz_baseline(monkeypatch):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, enabled=False)
    assert not data["requests"]
    assert not hasattr(backend, "_life_feedback_summary")
    assert stats.timing_feedback_fast == stats.timing_feedback_valid == 0
