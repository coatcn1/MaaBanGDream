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


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.parametrize("life", [False, True])
@pytest.mark.parametrize("wait", [False, True])
def test_settings_replace_roundtrip_and_frozen_run(tmp_path, life, wait, early):
    store = RealtimeProfileStore(tmp_path)
    options = store.runtime_options() | {"native_early_start_enabled": early, "native_life_feedback_enabled": life,
                                        "native_wait_jitter_filter_enabled": wait}
    assert store.update_runtime_options(options) == options
    assert store.runtime_options() == options
    stale_reference = reset_live_run(mode="formal", difficulty="Expert")
    assert frozen_native_runtime_options(stale_reference, options) == options
    changed = options | {"native_early_start_enabled": not early, "native_life_feedback_enabled": not life,
                         "native_wait_jitter_filter_enabled": not wait}
    assert frozen_native_runtime_options(stale_reference, changed) == options
    assert frozen_native_runtime_options(current_live_run(), changed) == options
    unrelated = changed | {"skip_result_check": True}
    assert frozen_native_runtime_options(stale_reference, unrelated)["skip_result_check"] is True
    next_run = reset_live_run(mode="formal", difficulty="Expert")
    assert frozen_native_runtime_options(next_run, changed) == changed


@pytest.mark.parametrize("key", ["native_life_feedback_enabled", "native_wait_jitter_filter_enabled", "native_early_start_enabled"])
@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_new_options_are_strict_booleans(tmp_path, key, value):
    store = RealtimeProfileStore(tmp_path)
    assert store.runtime_options()[key] is False
    with pytest.raises(ValueError, match=key):
        store.update_runtime_options({key: value})


def _feedback_image(direction):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if direction is TimingFeedback.SLOW:
        image[514:556, 570:710] = (0, 140, 255)
    elif direction is TimingFeedback.FAST:
        image[514:556, 570:710] = (255, 180, 0)
    return image


def _phase_backend():
    backend = object.__new__(NativeMinitouchBackend)
    backend.life_feedback_enabled = True
    backend._phase_lock = threading.Lock()
    backend._phase_closed = False
    backend._phase_slot = None
    backend._phase_sequence = 0
    backend._phase_events = deque(maxlen=128)
    backend._phase_receipts = deque([(1, .8, 2, 2, .8, .5), (2, .85, 3, 3, .85, .5), (3, .9, 4, 4, .9, .5)])
    backend._clock = lambda: 1.0
    backend._clock_basis = "probe-midpoint"
    backend._clock_uncertainty_ms = .5
    backend._run_id = "test"
    backend._state = backend._session_state = "running"
    backend._compiler = SimpleNamespace(phase_boundary_safe=lambda: True, contact_available_s=lambda: .5)
    calls = []
    backend._session = SimpleNamespace(apply_future_phase=lambda *args: calls.append(args) or "applied",
                                       future_phase_offset_ms=15)
    return backend, calls


def test_mailbox_is_nonblocking_single_slot_and_owner_only():
    backend, calls = _phase_backend()
    assert backend.request_future_phase(15, .95)
    assert not backend.request_future_phase(-15, .95)
    assert calls == []
    backend._consume_phase_candidate()
    assert calls == [(15, .5)]
    assert backend._phase_events[-1]["event"] == "applied"
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
        backend._clock_uncertainty_ms = 2
    elif guard == "old":
        backend._phase_slot = ("old", 1, .95, 15, 2.95)
    elif guard == "few":
        backend._phase_receipts.pop()
    elif guard == "duplicate":
        backend._phase_receipts = deque([(1, .8, 2, 2, .8, .5)] * 3)
    else:
        backend._phase_receipts = deque([(1, .1, 2, 2, .1, .5), (2, .2, 3, 3, .2, .5), (3, .3, 4, 4, .3, .5)])
    backend._consume_phase_candidate()
    assert calls == []
    assert (backend._phase_slot is not None) is (guard in {"hold", "few", "duplicate", "late"})


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
    sampler = NativeLifeFeedback(_phase_backend()[0])
    sampler.observe_scene(0, visible=True)
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
    assert sampler.scene_valid


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


def _virtual_feedback_engine(monkeypatch, *, terminal=None, enabled=True, early_glyph=False, glyph_mode="static", recording=True, async_capture=False, capture_fault=None, ticket_report_fault=False):
    from agent.realtime import engine as engine_module
    from agent.realtime.life_monitor import LifeGuard, LifeReading
    clock = [.007]
    stopping = [False]
    data = {"captures": [], "life": [], "terminal": [], "requests": [], "windows": [], "finish_at": .5 if terminal else None}
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
            if glyph_mode == "events":
                direction = TimingFeedback.FAST if any(at + .025 <= now < at + .18 for at in (.3, 1.1, 1.9)) else None
            if early_glyph and data["windows"] and now - data["windows"][-1] >= .15:
                direction = None
            return _feedback_image(direction)
        def enable_fresh_prefetch(self):
            if capture_fault == "enable":
                raise RuntimeError("enable prefetch unavailable")
        def prefetch_fresh(self, target):
            if capture_fault == "prefetch":
                raise RuntimeError("prefetch unavailable")
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
            if ticket_report_fault:
                return {"life_feedback": {"tickets": NativeMinitouchBackend._feedback_ticket_report(self)}}
            return {}
        def feedback_input_snapshot(self, request_s, now):
            from agent.realtime.native_play import NativeFeedbackInput
            return NativeFeedbackInput("test", request_s - .02, request_s - .02,
                                       len(data["captures"]), 1, .5)
        def feedback_observation_snapshot(self):
            from agent.realtime.native_feedback_ticket import FeedbackObservationState, FeedbackObservationTicket
            executed = [(index, at) for index, at in enumerate((.3, 1.1, 1.9), 1) if clock[0] >= at]
            if not executed:
                return FeedbackObservationState(next_judgement_lower_s=.26)
            index, at = executed[-1]
            ticket = FeedbackObservationTicket("test", index, 1, at, at, .5, index)
            return FeedbackObservationState(ticket, index, at, .5,
                (1.1, 1.9)[index - 1] - .04 if index < 3 else None)
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
    class Failed:
        def observe(self, image, now):
            return terminal == "life-failed" and data["finish_at"] is not None and now >= data["finish_at"]
    backend = Backend()
    if ticket_report_fault:
        def fail_report():
            raise OSError("ticket report unavailable")
        backend._feedback_tickets = SimpleNamespace(report=fail_report)
    planner = SimpleNamespace(timing_offset_ms=13, update=lambda *_args: pytest.fail("Native 不使用 Legacy planner"))
    engine = engine_module.RealtimeEngine(SimpleNamespace(), planner, SimpleNamespace(close=lambda: None),
        clock=lambda: clock[0], native_backend=backend, life_detector=Life(), life_guard=LifeGuard(confirm_frames=1),
        playfield_monitor=Monitor(), live_failed_detector=Failed(), debug_recorder=Recorder() if recording else None)
    capture = Capture()
    if async_capture:
        monkeypatch.setattr("agent.realtime.profile_play_action.time.perf_counter", lambda: clock[0])
        source_capture = capture
        class Job:
            def __init__(self):
                self.requested = clock[0]
                self.image = source_capture()
            @property
            def done(self):
                return clock[0] >= self.requested + .012
            def get(self):
                assert self.done
                return self.image
            def wait(self):
                clock[0] = max(clock[0], self.requested + .012)
                return self
        capture = StallSafeCapture(SimpleNamespace(post_screencap=Job))
    stats = engine.run(capture, lambda: stopping[0], duration_seconds=7.3, target_fps=60)
    return stats, data, backend



@pytest.mark.parametrize("terminal", ["completed", "life-failed", "stop"])
def test_engine_terminal_checks_precede_second_independent_event(monkeypatch, terminal):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, terminal=terminal, glyph_mode="events")
    assert data["requests"] == []
    assert stats.completed is (terminal == "completed")
    assert stats.life_failed is (terminal == "life-failed")
    assert stats.stopped is (terminal == "stop")
    assert backend._life_feedback_summary["single_tap_observations"] == 1
    assert backend._life_feedback_summary.get("distinct_events", 0) == 0
    assert backend._life_feedback_sniff["records"]


def test_actual_engine_sustained_glyph_can_request_once_without_claiming_visual_events(monkeypatch):
    stats, data, backend = _virtual_feedback_engine(monkeypatch)
    assert len(data["requests"]) == 1
    assert backend._life_feedback_summary.get("distinct_events", 0) == 0
    assert stats.timing_feedback_fast == stats.timing_feedback_valid == 3
    assert stats.timing_feedback_reports == 3
    assert backend._life_feedback_sniff["continuation"]["pending_sequence"] == 1
    assert stats.initial_timing_offset_ms == stats.final_timing_offset_ms == 13
    assert len(data["captures"]) < 60
    assert 32 <= len(data["life"]) <= 41
    assert 32 <= len(data["terminal"]) <= 41


def test_actual_engine_three_target_keys_request_and_sniff_survives_without_recorder(monkeypatch):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, glyph_mode="events", recording=False)
    assert len(data["requests"]) == 1
    assert data["requests"][0][0] == 15
    assert stats.timing_feedback_valid == stats.timing_feedback_fast == 3
    assert stats.timing_feedback_reports == backend._life_feedback_summary["single_tap_observations"] == 3
    assert backend._life_feedback_summary.get("distinct_events", 0) == 0
    sniff = backend._life_feedback_sniff
    assert sniff["capacity"] == 256 and sniff["dropped"] == 0
    assert any(record["decision"] == "requested" for record in sniff["records"])
    assert any(record["input"] is not None for record in sniff["records"])


def test_engine_disabled_feedback_retains_native_five_hz_baseline(monkeypatch):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, enabled=False)
    assert not data["windows"] and not data["requests"]
    assert not hasattr(backend, "_life_feedback_summary")
    assert stats.timing_feedback_fast == stats.timing_feedback_valid == 0
    assert len(data["captures"]) <= 43


def test_real_stall_safe_async_capture_preserves_blank_until_ticket_frame_is_ready(monkeypatch):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, glyph_mode="events", recording=False,
                                                   async_capture=True)
    assert len(data["requests"]) == 1
    assert stats.timing_feedback_valid == 3
    assert backend._life_feedback_sniff["ignored_reasons"].get("no-blank-baseline", 0) == 0
    eligible = [row for row in backend._life_feedback_sniff["records"] if row["decision"] in {"eligible-single-tap", "requested"}]
    assert len(eligible) == 3
    assert all(row["request_s"] + 1e-9 >= row["input"]["actual_s"] + .0255 for row in eligible)


@pytest.mark.parametrize("fault", ["enable", "prefetch"])
def test_optional_capture_prefetch_failure_disables_feedback_and_preserves_engine(monkeypatch, fault):
    stats, data, backend = _virtual_feedback_engine(monkeypatch, glyph_mode="events", capture_fault=fault)
    assert stats.processed_frames >= 30
    assert not stats.cleanup_failed and not stats.stopped
    assert not data["requests"]
    assert "prefetch unavailable" in backend._life_feedback_disabled_reason
    assert backend._life_feedback_sniff["disabled_reason"] == backend._life_feedback_disabled_reason
    assert backend._life_feedback_sniff["ignored_reasons"]["observer-disabled"] == 1


def test_ticket_report_failure_preserves_complete_engine_result_and_cleanup(monkeypatch):
    stats, _, backend = _virtual_feedback_engine(monkeypatch, terminal="completed", glyph_mode="events",
                                                 ticket_report_fault=True)
    assert stats.completed and not stats.cleanup_failed and not stats.cleanup_errors
    assert "ticket report unavailable" in stats.native_report["life_feedback"]["tickets"]["report_error"]
    assert backend._life_feedback_summary["direction_votes"] == 1


def test_real_eighth_round_receipt_staleness_waits_for_fresh_receipt_with_original_ttl():
    backend,calls=_phase_backend()
    now=[102931.3019289]
    backend._clock=lambda:now[0]
    backend._clock_uncertainty_ms=.5096
    backend._phase_receipts=deque((i,102930.68342733383,1.8043,1.8043,102930.681623,.5096) for i in range(22))
    assert backend.request_future_phase(15,102930.1637968)
    backend._consume_phase_candidate()
    assert backend.phase_status_snapshot().state=="waiting-execution-receipts"
    assert backend._phase_slot[-1]==pytest.approx(102932.1637968)
    assert backend._phase_receipt_diagnostics[-1]['failed_checks']==['stale-last-commit']
    for _ in range(100):backend._consume_phase_candidate()
    assert len(backend._phase_receipt_diagnostics)==1
    now[0]=102931.36
    backend._phase_receipts.append((23,102931.35887337,2,2,102931.3568,.5096))
    backend._session.apply_future_phase=lambda *args:"unsafe-gap"
    backend._consume_phase_candidate()
    assert not calls and backend.phase_status_snapshot().state=="waiting-gap"
    backend._session.apply_future_phase=lambda *args:calls.append(args) or "applied"
    backend._consume_phase_candidate()
    assert len(calls)==1 and backend.phase_status_snapshot().state=="applied"


def test_waiting_receipts_expires_without_applying_or_extending_ttl():
    backend,calls=_phase_backend()
    backend._phase_receipts.clear()
    assert backend.request_future_phase(15,.95)
    backend._consume_phase_candidate()
    backend._clock=lambda:2.951
    backend._consume_phase_candidate()
    assert not calls and backend.phase_status_snapshot().state=="expired"


@pytest.mark.parametrize("failure",["stop","invalidate","clock","residual"])
def test_waiting_receipt_candidate_revalidates_hard_guards(failure):
    backend,calls=_phase_backend()
    backend._phase_receipts.clear()
    assert backend.request_future_phase(15,.95)
    backend._consume_phase_candidate()
    assert backend.phase_status_snapshot().state=="waiting-execution-receipts"
    backend._phase_receipts=deque([(i,.9,2,2,.9,.5) for i in range(3)])
    if failure=="stop":backend._phase_closed=True
    elif failure=="invalidate":backend._phase_invalidated_sequence=1
    elif failure=="clock":backend._clock_uncertainty_ms=1.001
    else:backend._phase_receipts=deque([(i,.9,40.001,40.001,.9,.5) for i in range(3)])
    backend._consume_phase_candidate()
    assert not calls and backend._phase_slot is None


@pytest.mark.parametrize("probe",[False,True])
@pytest.mark.parametrize("behavior",["frames","failure","stop","timeout","cross-deadline-ready"])
def test_opening_ready_observer_preserves_deadline_ready_and_stop(monkeypatch,probe,behavior):
    import agent.realtime.native_play as module
    now=[0.0]
    monkeypatch.setattr(module.time,"monotonic",lambda:now[0])
    backend=object.__new__(NativeMinitouchBackend)
    backend._state="arming"
    backend._device_error=None
    backend._device=SimpleNamespace(connected=True)
    backend._require_probe=probe
    ready_at=[.3 if behavior!="timeout" else 20]
    class Thread:
        def is_alive(self):return not probe and now[0]<ready_at[0]
        def join(self,timeout):now[0]+=timeout
    class Event:
        def is_set(self):return now[0]>=ready_at[0]
        def wait(self,timeout):now[0]+=timeout;return self.is_set()
    backend._device_thread=Thread()
    backend._probe_complete=Event()
    cleanup=[]
    backend._cancel_device_start=lambda timeout:cleanup.append("cancel")
    backend._emergency_stop_device_with_budget=lambda timeout:cleanup.append("stop")
    calls=[]
    def observe():
        calls.append(now[0])
        if behavior=="failure":raise OSError("disk/capture unavailable")
        if behavior=="stop":raise InterruptedError("用户已停止任务")
        if behavior=="cross-deadline-ready":now[0]=2.0
    if behavior=="stop":
        with pytest.raises(InterruptedError):backend.wait_until_ready(1,wait_observer=observe,should_stop=lambda:False)
    elif behavior=="timeout":
        with pytest.raises(RuntimeError):backend.wait_until_ready(1,wait_observer=observe,should_stop=lambda:False)
        assert now[0]<=1.001 and cleanup
    else:
        assert backend.wait_until_ready(1,wait_observer=observe,should_stop=lambda:False)
        assert backend._state=="armed"
    assert len(calls)<=10
    if behavior=="failure":assert len(calls)==1


def test_opening_ready_already_complete_does_not_capture():
    backend=object.__new__(NativeMinitouchBackend)
    backend._state="arming"
    backend._device_error=None
    backend._device=SimpleNamespace(connected=True)
    backend._require_probe=False
    backend._device_thread=SimpleNamespace(is_alive=lambda:False)
    calls=[]
    assert backend.wait_until_ready(1,wait_observer=lambda:calls.append(1),should_stop=lambda:False)
    assert not calls


def test_opening_stop_race_after_ready_join_does_not_publish_armed():
    backend=object.__new__(NativeMinitouchBackend)
    backend._state="arming"
    backend._device_error=None
    backend._device=SimpleNamespace(connected=True)
    backend._require_probe=False
    state={'alive':True,'stop':False}
    def join(timeout):state.update(alive=False,stop=True)
    backend._device_thread=SimpleNamespace(is_alive=lambda:state['alive'],join=join)
    with pytest.raises(InterruptedError):backend.wait_until_ready(1,wait_observer=lambda:None,should_stop=lambda:state['stop'])
    assert backend._state=="arming"
