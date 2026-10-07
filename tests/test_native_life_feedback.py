from collections import deque
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativeMinitouchBackend
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
    backend = SimpleNamespace(feedback_input_eligible=lambda now: True,
                              request_future_phase=lambda delta, now: requests.append((delta, now)) or True)
    return NativeLifeFeedback(backend), requests


def _trigger(sampler, start=0):
    for index, value in enumerate([1000, 955, 910]):
        now = start + index * .2
        assert sampler.fresh({"sequence": index + int(start * 100), "request_s": now - .060,
                              "reused": False}, now)
        sampler.observe_life(value, now, visible=True)
    assert sampler.active(start + .4)


def test_window_source_frames_and_invalid_frame_close_keep_correct_metadata():
    sampler, _ = _sampler()
    _trigger(sampler)
    event = sampler.events[-1]
    assert event["event"] == "life-drop-window"
    assert [sample["frame_sequence"] for sample in event["life_samples"]] == [0, 1, 2]
    assert [sample["request_s"] for sample in event["life_samples"]] == pytest.approx([-.06, .14, .34])
    assert event["observed_s"] == .4
    for index in range(3):
        assert not sampler.fresh({"sequence": 3 + index, "request_s": .4,
                                  "reused": True}, .5 + index * .02)
    closed = sampler.events[-1]
    assert closed["event"] == "slow-or-reused-frames"
    assert closed["frame_sequence"] == 5
    assert closed["request_s"] == .4
    assert closed["reused"] is True
    assert closed["observed_s"] == pytest.approx(.54)


def test_burst_requires_repeated_losses_and_respects_cooldown_rate():
    sampler, _ = _sampler()
    sampler.observe_life(1000, 0, visible=True)
    sampler.observe_life(800, .2, visible=True)
    sampler.observe_life(900, .4, visible=True)
    assert not sampler.active(.4)
    sampler.life_samples.clear()
    _trigger(sampler, 1)
    assert not sampler.active(3.4)
    sampler.observe_life(800, 4, visible=True)
    assert not sampler.active(4)
    _trigger(sampler, 6)
    sampler.close(6.5, "test")
    for now, value in [(8.6, 1000), (8.8, 950), (9, 900)]:
        sampler.observe_life(value, now, visible=True)
    assert not sampler.active(9)
    assert sampler.summary["windows"] == 2


@pytest.mark.parametrize("direction,delta", [(TimingFeedback.FAST, 2), (TimingFeedback.SLOW, -2)])
def test_deduplicated_three_reports_request_once(direction, delta):
    sampler, requests = _sampler()
    _trigger(sampler)
    signals = iter([direction, None, direction, None, direction, direction])
    sampler.detector = SimpleNamespace(detect=lambda image: next(signals))
    for index in range(6):
        sampler.observe_frame(None, .5 + index / 60)
    assert requests == [(delta, pytest.approx(.5 + 4 / 60))]
    assert sampler.summary[direction.value + "_reports"] == 4


def test_mixed_ineligible_old_and_slow_frames_cannot_request():
    sampler, requests = _sampler()
    _trigger(sampler)
    signals = iter([TimingFeedback.FAST, TimingFeedback.SLOW] + [TimingFeedback.SLOW] * 3)
    sampler.detector = SimpleNamespace(detect=lambda image: next(signals))
    for index in range(5):
        sampler.observe_frame(None, .5 + index / 60)
    assert requests == []
    for index in range(3):
        assert not sampler.fresh({"sequence": 5 + index, "request_s": 0,
                                  "reused": False}, .6 + index / 60)
    assert not sampler.active(.7)
    assert sampler.summary["slow-or-reused-frames"] == 1


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
    assert sampler.fresh({"sequence": 4, "request_s": .49, "reused": False}, .5)
    sampler.observe_frame(orange, .5)
    assert not sampler.fresh({"sequence": 4, "request_s": .49, "reused": True}, .8)
    assert sampler.fresh({"sequence": 5, "request_s": .81, "reused": False}, .816)
    sampler.observe_frame(orange, .816)
    assert sampler.summary["slow_reports"] == 0
    sampler.observe_frame(orange, .832)
    for _ in range(10):
        sampler.observe_frame(orange, .848)
    assert sampler.summary["slow_reports"] == 1
    blank = _feedback_image(None)
    for report in range(2):
        for _ in range(3):
            sampler.observe_frame(blank, .9 + report * .1)
        for _ in range(2):
            sampler.observe_frame(orange, .95 + report * .1)
    assert len(requests) == 1
    assert requests[0][0] == -2


def test_real_detector_does_not_join_two_fresh_frames_across_unobserved_stall():
    sampler, _ = _sampler()
    _trigger(sampler)
    for sequence, now in [(4, .500), (5, .816)]:
        assert sampler.fresh({"sequence": sequence, "request_s": now - .01, "reused": False}, now)
        sampler.observe_frame(_feedback_image(TimingFeedback.SLOW), now)
    assert sampler.summary["slow_reports"] == 0
    assert sampler.summary["sampling_gaps"] == 2


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
    sampler.burst_until = 2.0
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


def test_engine_keeps_terminal_five_hz_during_sixty_hz_burst():
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
            return LifeReading(True, max(500, 1040 - len(counts["life"]) * 40))
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
    engine.run(Capture(), lambda: False, duration_seconds=1.1, target_fps=60)
    assert counts["captures"] > 25
    for samples in (counts["life"], counts["terminal"]):
        assert 4 <= len(samples) <= 7
        assert all(after - before >= .18 for before, after in zip(samples, samples[1:]))
    assert backend._life_feedback_summary["windows"] == 1
    assert counts["stop"] == counts["close"] == 1
