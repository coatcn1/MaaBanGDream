from collections import deque
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
from agent.realtime.timing_feedback import TimingFeedback


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
    return NativeLifeFeedback(backend), requests


def _trigger(sampler, start=0):
    sampler.observe_scene(start, visible=True)
    assert sampler.active(start)


def _sample(sampler, direction, now, *, sequence=None, reused=False, age=.01):
    sequence = sampler.last_sequence + 1 if sequence is None else sequence
    valid = sampler.fresh({"sequence": sequence, "request_s": now - age, "reused": reused}, now)
    if valid:
        sampler.observe_frame(_feedback_image(direction), now)
    return valid


def _window(sampler, direction, start, *, mixed=False):
    _trigger(sampler, start)
    for index in range(16):
        kind = TimingFeedback.SLOW if mixed and index >= 8 else direction
        assert _sample(sampler, kind, start + index * .05)
    sampler.observe_scene(start + .8, visible=True)


def test_window_source_frames_and_invalid_frame_close_keep_correct_metadata():
    sampler, _ = _sampler()
    _trigger(sampler)
    event = sampler.events[-1]
    assert event["event"] == "periodic-window"
    assert event["window_start_s"] == 0
    assert not sampler.fresh({"sequence": 3, "request_s": .4,
                              "reused": True}, .5)
    closed = sampler.events[-1]
    assert closed["event"] == "slow-or-reused-frames"
    assert closed["frame_sequence"] == 3
    assert closed["request_s"] == .4
    assert closed["reused"] is True
    assert closed["observed_s"] == pytest.approx(.5)


def test_periodic_windows_do_not_depend_on_life_and_never_catch_up_after_stall():
    sampler, _ = _sampler()
    sampler.observe_life(1000, 0, visible=True)
    sampler.observe_life(800, .2, visible=True)
    sampler.observe_life(900, .4, visible=True)
    assert not sampler.active(.4)
    _trigger(sampler, 1)
    assert not sampler.active(1.8)
    assert sampler.finish_due(1.8)
    sampler.observe_scene(1.8, visible=True)
    sampler.observe_scene(3.99, visible=True)
    assert not sampler.active(3.99)
    _trigger(sampler, 9)
    assert sampler.burst_started_at == 9
    assert sampler.next_window_at == 12
    assert sampler.summary["windows"] == 2


@pytest.mark.parametrize("direction,delta", [(TimingFeedback.FAST, 2), (TimingFeedback.SLOW, -2)])
@pytest.mark.parametrize("life_values", [[1000, 1000, 1000], [1000, 920, 920]])
def test_three_independent_windows_of_continuous_glyph_request_once(direction, delta, life_values):
    sampler, requests = _sampler()
    for start, value in zip([0, 3, 6], life_values):
        sampler.observe_life(value, start, visible=True)
        _window(sampler, direction, start)
        assert len(requests) == (1 if start == 6 else 0)
    assert requests == [(delta, pytest.approx(6.75))]
    assert sampler.summary[direction.value + "_votes"] == 3
    assert sampler.summary["sampling_frames"] == 48
    assert sampler.summary["requests"] == 1
    assert len(sampler.votes) == 3
    assert sampler.qualified_direction is direction


def test_mixed_ineligible_old_and_slow_frames_cannot_request():
    sampler, requests = _sampler()
    _window(sampler, TimingFeedback.FAST, 0)
    _window(sampler, TimingFeedback.FAST, 3)
    _window(sampler, TimingFeedback.FAST, 6, mixed=True)
    assert requests == []
    assert not sampler.votes
    assert sampler.summary["mixed-feedback"] == 1


def _feedback_image(direction):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if direction is TimingFeedback.SLOW:
        image[514:556, 570:710] = (0, 140, 255)
    elif direction is TimingFeedback.FAST:
        image[514:556, 570:710] = (255, 180, 0)
    return image


def test_real_detector_dedup_and_invalid_gap():
    sampler, requests = _sampler()
    _trigger(sampler)
    orange = _feedback_image(TimingFeedback.SLOW)
    assert _sample(sampler, TimingFeedback.SLOW, .05)
    assert _sample(sampler, TimingFeedback.SLOW, .10)
    for _ in range(10):
        sampler.observe_frame(orange, .10)
    assert requests == []
    assert sampler.summary["sampling_frames"] == 2
    assert sampler.summary["debounced_reports"] == 1
    assert not _sample(sampler, TimingFeedback.SLOW, .15, sequence=sampler.last_sequence, reused=True)
    assert not sampler.active(.15)
    assert not sampler.votes


def test_real_detector_does_not_join_two_fresh_frames_across_unobserved_stall():
    sampler, _ = _sampler()
    _trigger(sampler)
    for sequence, now in [(4, .05), (5, .366)]:
        assert sampler.fresh({"sequence": sequence, "request_s": now - .01, "reused": False}, now)
        sampler.observe_frame(_feedback_image(TimingFeedback.SLOW), now)
    assert sampler.summary["slow_reports"] == 0
    assert sampler.summary["sampling_gaps"] == 1
    assert not sampler.votes


def test_early_glyph_then_blank_is_one_valid_vote_with_actual_source_time():
    sampler, requests = _sampler()
    for start in (0, 3, 6):
        _trigger(sampler, start)
        for index in range(16):
            _sample(sampler, TimingFeedback.FAST if index < 2 else None, start + index * .05)
        sampler.observe_scene(start + .8, visible=True)
    assert requests == [(2, pytest.approx(6.05))]
    assert sampler.summary["direction_votes"] == sampler.summary["debounced_reports"] == 3


@pytest.mark.parametrize("invalid", ["reused", "old-sequence", "old-request", "gap", "bad-shape", "scene", "stop"])
def test_third_window_invalidity_clears_prior_votes_without_request(invalid):
    sampler, requests = _sampler()
    _window(sampler, TimingFeedback.FAST, 0)
    _window(sampler, TimingFeedback.FAST, 3)
    _trigger(sampler, 6)
    _sample(sampler, TimingFeedback.FAST, 6)
    _sample(sampler, TimingFeedback.FAST, 6.05)
    if invalid == "reused":
        _sample(sampler, TimingFeedback.FAST, 6.1, reused=True)
    elif invalid == "old-sequence":
        _sample(sampler, TimingFeedback.FAST, 6.1, sequence=sampler.last_sequence - 1)
    elif invalid == "old-request":
        _sample(sampler, TimingFeedback.FAST, 6.1, age=.081)
    elif invalid == "gap":
        _sample(sampler, TimingFeedback.FAST, 6.2)
    elif invalid == "bad-shape":
        assert sampler.fresh({"sequence": sampler.last_sequence + 1, "request_s": 6.09, "reused": False}, 6.1)
        sampler.observe_frame(np.zeros((100, 100, 3), dtype=np.uint8), 6.1)
    elif invalid == "scene":
        sampler.observe_scene(6.8, visible=False)
    else:
        sampler.close(6.8, "terminal")
    assert not sampler.votes
    assert requests == []


def test_no_evidence_window_keeps_live_trend_and_expired_votes_do_not_request():
    sampler, requests = _sampler()
    _window(sampler, TimingFeedback.FAST, 0)
    _window(sampler, TimingFeedback.FAST, 3)
    _window(sampler, None, 6)
    assert len(sampler.votes) == 2
    assert sampler.ignored_reasons["no-feedback"] == 1
    _window(sampler, None, 9)
    _window(sampler, None, 12)
    _window(sampler, TimingFeedback.FAST, 25)
    assert len(sampler.votes) == 1
    assert requests == []


def test_window_finish_queries_do_not_submit_and_close_rechecks_recent_input():
    sampler, requests = _sampler()
    for start in (0, 3):
        _window(sampler, TimingFeedback.FAST, start)
    _trigger(sampler, 6)
    for index in range(16):
        _sample(sampler, TimingFeedback.FAST, 6 + index * .05)
    assert not sampler.active(6.8)
    assert sampler.finish_due(6.8)
    assert requests == []
    sampler.backend.feedback_input_eligible = lambda _now: False
    sampler.observe_scene(6.8, visible=True)
    assert requests == []
    assert not sampler.votes


def test_same_frame_both_colours_rejects_window_even_after_detector_armed():
    sampler, requests = _sampler()
    _trigger(sampler)
    _sample(sampler, TimingFeedback.FAST, 0)
    _sample(sampler, TimingFeedback.FAST, .05)
    both = _feedback_image(TimingFeedback.FAST)
    both[514:535, 570:710] = (0, 140, 255)
    assert sampler.fresh({"sequence": 2, "request_s": .09, "reused": False}, .1)
    sampler.observe_frame(both, .1)
    sampler.observe_scene(.8, visible=True)
    assert requests == []
    assert sampler.ignored_reasons["mixed-feedback"] == 1


@pytest.mark.parametrize("reason", ["invalid-scene", "slow-or-reused-frames", "sampling-error"])
def test_invalid_window_revokes_pending_owner_candidate(reason):
    backend, calls = _phase_backend()
    sampler = NativeLifeFeedback(backend)
    assert backend.request_future_phase(2, .95)
    backend._compiler.phase_boundary_safe = lambda: False
    backend._consume_phase_candidate()
    sampler.close(1, reason)
    backend._compiler.phase_boundary_safe = lambda: True
    backend._consume_phase_candidate()
    assert calls == []


def _phase_backend():
    backend = object.__new__(NativeMinitouchBackend)
    backend.life_feedback_enabled = True
    backend._phase_lock = threading.Lock()
    backend._phase_closed = False
    backend._phase_slot = None
    backend._phase_sequence = 0
    backend._phase_events = deque(maxlen=128)
    backend._phase_receipts = deque([(1, .8, 2), (2, .85, 3), (3, .9, 4)])
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


def test_mailbox_is_nonblocking_single_slot_and_owner_only():
    backend, calls = _phase_backend()
    assert backend.request_future_phase(2, .95)
    assert not backend.request_future_phase(-2, .95)
    assert calls == []
    backend._consume_phase_candidate()
    assert calls == [(2, .5)]
    assert backend._phase_events[-1]["event"] == "applied"
    backend._phase_lock.acquire()
    assert not backend.request_future_phase(2, 1)
    backend._phase_lock.release()


def test_phase_snapshot_is_immutable_and_readable_while_owner_lock_is_held():
    backend, _ = _phase_backend()
    assert backend.request_future_phase(2, .95)
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
    assert backend.request_future_phase(2, .95)
    backend._compiler.phase_boundary_safe = lambda: False
    for _ in range(4):
        backend._consume_phase_candidate()
    assert [event["event"] for event in backend._phase_events] == ["requested", "waiting-touch-boundary"]
    backend.revoke_future_phase("mixed-feedback")
    revoked = [event for event in backend._phase_events if event["event"] == "revoked"]
    assert len(revoked) == 1
    assert revoked[0]["reason"] == "mixed-feedback"
    assert revoked[0]["sequence"] == 1 and revoked[0]["event_cursor"] == 3


@pytest.mark.parametrize("guard", ["hold", "expiry", "stop", "uncertain", "old", "few", "duplicate", "late"])
def test_phase_guards_preserve_dispatch(guard):
    backend, calls = _phase_backend()
    assert backend.request_future_phase(2, .95)
    if guard == "hold":
        backend._compiler.phase_boundary_safe = lambda: False
    elif guard == "expiry":
        backend._clock = lambda: 3
    elif guard == "stop":
        backend._phase_closed = True
    elif guard == "uncertain":
        backend._clock_uncertainty_ms = 2
    elif guard == "old":
        backend._phase_slot = ("old", 1, .95, 2, 2.95)
    elif guard == "few":
        backend._phase_receipts.pop()
    elif guard == "duplicate":
        backend._phase_receipts = deque([(1, .8, 2)] * 3)
    else:
        backend._phase_receipts = deque([(1, .1, 2), (2, .2, 3), (3, .3, 4)])
    backend._consume_phase_candidate()
    assert calls == []
    assert (backend._phase_slot is not None) is (guard == "hold")


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
    _trigger(sampler)
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


def test_engine_keeps_terminal_five_hz_during_twenty_hz_periodic_window():
    import time
    from agent.realtime.engine import RealtimeEngine
    from agent.realtime.life_monitor import LifeGuard, LifeReading
    counts = {"captures": 0, "life": [], "terminal": [], "close": 0, "stop": 0}
    class Capture:
        def __call__(self):
            counts["captures"] += 1
            now = time.perf_counter()
            self.frame_metadata = {"sequence": counts["captures"], "request_s": now - .01, "reused": False}
            return _feedback_image(TimingFeedback.FAST)
        def enable_fresh_prefetch(self):
            pass
        def prefetch_fresh(self, target):
            pass
    class Life:
        def detect(self, image):
            counts["life"].append(time.perf_counter())
            return LifeReading(True, 1000)
    class Backend:
        exclusive = takeover = life_feedback_enabled = True
        active = finished = False
        def arm(self):
            pass
        def observe_start_frame(self, image, now):
            return now
        def start(self, anchor):
            self.active = True
        def poll(self, now):
            pass
        def stop(self):
            counts["stop"] += 1
        def report(self):
            return {}
        def feedback_input_eligible(self, now):
            return True
        def request_future_phase(self, delta, now):
            return True
    backend = Backend()
    planner = SimpleNamespace(timing_offset_ms=0,
                              update=lambda *args: pytest.fail("Native 不应调用 Legacy planner"))
    touch = SimpleNamespace(close=lambda: counts.__setitem__("close", counts["close"] + 1))
    monitor = SimpleNamespace(active=True, mark_active=lambda now: None,
                              observe=lambda image, now: counts["terminal"].append(now) or "active")
    engine = RealtimeEngine(SimpleNamespace(), planner, touch, native_backend=backend,
                            life_detector=Life(), life_guard=LifeGuard(confirm_frames=1),
                            playfield_monitor=monitor)
    stats = engine.run(Capture(), lambda: False, duration_seconds=1.1, target_fps=60)
    assert 16 <= counts["captures"] <= 23
    for samples in (counts["life"], counts["terminal"]):
        assert 4 <= len(samples) <= 7
        # 每窗结束额外复核一次场景，其余生命与终点节拍保持约 5Hz。
        assert sum(after - before < .18 for before, after in zip(samples, samples[1:])) <= 1
    assert backend._life_feedback_summary["windows"] == 1
    assert stats.timing_feedback_fast == stats.timing_feedback_valid == 1
    assert stats.timing_feedback_sightings > 0
    assert stats.timing_feedback_reports == 1
    assert counts["stop"] == counts["close"] == 1


def _virtual_feedback_engine(monkeypatch, *, terminal=None, enabled=True, early_glyph=False):
    from agent.realtime import engine as engine_module
    from agent.realtime.life_monitor import LifeGuard, LifeReading
    clock = [.007]
    stopping = [False]
    data = {"captures": [], "life": [], "terminal": [], "requests": [], "windows": [], "finish_at": None}
    monkeypatch.setattr(engine_module.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    class Capture:
        def __call__(self):
            clock[0] += .004
            now = clock[0]
            data["captures"].append(now)
            self.frame_metadata = {"sequence": len(data["captures"]), "request_s": now - .01, "reused": False}
            if terminal == "stop" and data["finish_at"] is not None and now >= data["finish_at"]:
                stopping[0] = True
            direction = TimingFeedback.FAST
            if early_glyph and data["windows"] and now - data["windows"][-1] >= .15:
                direction = None
            return _feedback_image(direction)
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
            if terminal == "completed" and data["finish_at"] is not None and now >= data["finish_at"]:
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
        def record_native_feedback(self, image, now, metadata, window_start_s):
            if not data["windows"] or data["windows"][-1] != window_start_s:
                data["windows"].append(window_start_s)
                if len(data["windows"]) == 3:
                    data["finish_at"] = window_start_s + .8
    class Failed:
        def observe(self, image, now):
            return terminal == "life-failed" and data["finish_at"] is not None and now >= data["finish_at"]
    backend = Backend()
    planner = SimpleNamespace(timing_offset_ms=13, update=lambda *_args: pytest.fail("Native 不使用 Legacy planner"))
    engine = engine_module.RealtimeEngine(SimpleNamespace(), planner, SimpleNamespace(close=lambda: None),
        clock=lambda: clock[0], native_backend=backend, life_detector=Life(), life_guard=LifeGuard(confirm_frames=1),
        playfield_monitor=Monitor(), live_failed_detector=Failed(), debug_recorder=Recorder())
    stats = engine.run(Capture(), lambda: stopping[0], duration_seconds=7.3, target_fps=60)
    return stats, data, backend


@pytest.mark.parametrize("terminal", ["completed", "life-failed", "stop"])
def test_engine_third_window_end_checks_terminal_before_request(monkeypatch, terminal):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, terminal=terminal)
    assert len(data["windows"]) == 3
    assert all(start >= data["anchor"] for start in data["windows"])
    assert data["requests"] == []
    assert stats.timing_feedback_valid == 2
    assert stats.completed is (terminal == "completed")
    assert stats.life_failed is (terminal == "life-failed")
    assert stats.stopped is (terminal == "stop")
    assert backend._life_feedback_summary["direction_votes"] == 2


def test_engine_periodic_sampling_has_real_counts_and_frozen_profile_offset(monkeypatch):
    stats, data, backend = _virtual_feedback_engine(monkeypatch)
    assert len(data["requests"]) == 1
    assert data["requests"][0][0] == 2
    assert data["requests"][0][1] < data["requests"][0][2]
    assert stats.timing_feedback_fast == stats.timing_feedback_valid == 3
    assert stats.timing_feedback_sightings == backend._life_feedback_summary["sampling_frames"]
    assert stats.initial_timing_offset_ms == stats.final_timing_offset_ms == 13
    assert 32 <= len(data["life"]) <= 41
    assert 32 <= len(data["terminal"]) <= 41
    assert backend._life_feedback_summary["sampling_frames"] <= 48


def test_engine_disabled_feedback_retains_native_five_hz_baseline(monkeypatch):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, enabled=False)
    assert not data["windows"] and not data["requests"]
    assert not hasattr(backend, "_life_feedback_summary")
    assert stats.timing_feedback_fast == stats.timing_feedback_valid == 0
    assert len(data["captures"]) <= 43


def test_window_close_does_not_hide_sampling_gap_with_a_new_frame_after_deadline():
    sampler, requests = _sampler()
    _window(sampler, TimingFeedback.FAST, 0)
    _window(sampler, TimingFeedback.FAST, 3)
    _trigger(sampler, 6)
    for now in (6, 6.05, 6.1):
        _sample(sampler, TimingFeedback.FAST, now)
    assert sampler.fresh({"sequence": sampler.last_sequence + 1, "request_s": 6.99, "reused": False}, 7)
    sampler.observe_scene(7, visible=True)
    assert requests == []
    assert not sampler.votes
    assert sampler.ignored_reasons["sampling-gap"] == 1


def test_window_early_release_guard_then_eligible_fast_can_vote():
    sampler, requests = _sampler()
    sampler.backend.feedback_input_eligible = lambda now: now % 3 >= .15
    for start in (0, 3, 6):
        _window(sampler, TimingFeedback.FAST, start)
    assert len(requests) == 1
    assert sampler.summary["fast_votes"] == 3
    assert sampler.summary["ineligible_frames"] >= 9


@pytest.mark.parametrize("ineligible_direction", [TimingFeedback.FAST, None])
def test_ineligible_segment_cannot_join_isolated_fast_sightings(ineligible_direction):
    sampler, requests = _sampler()
    _trigger(sampler)
    _sample(sampler, TimingFeedback.FAST, 0)
    sampler.backend.feedback_input_eligible = lambda now: now != .05
    _sample(sampler, ineligible_direction, .05)
    _sample(sampler, TimingFeedback.FAST, .1)
    for index in range(3, 16):
        _sample(sampler, None, index * .05)
    sampler.observe_scene(.8, visible=True)
    assert sampler.summary["direction_votes"] == 0
    assert requests == []


def test_ineligible_opposite_colour_does_not_overwrite_prior_trusted_window_evidence():
    sampler, requests = _sampler()
    for start in (0, 3, 6):
        _trigger(sampler, start)
        sampler.backend.feedback_input_eligible = lambda now: not .1 <= round(now - start, 6) <= .25
        for index in range(16):
            kind = TimingFeedback.SLOW if 2 <= index <= 4 else TimingFeedback.FAST
            _sample(sampler, kind, start + index * .05)
        sampler.observe_scene(start + .8, visible=True)
    assert len(requests) == 1
    assert sampler.summary["direction_votes"] == 3
    assert not sampler.summary["mixed-feedback"]
