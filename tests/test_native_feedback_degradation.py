from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime import engine as engine_module
from agent.realtime import profile_play_action as play_module
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativePhaseStatus
from agent.realtime.profile_play_action import StallSafeCapture


def run_degraded_capture(monkeypatch, *, feedback_fault=True, terminal="completed",
                         delay=.01, peek_error=False, prefetch_fail_until=None,
                         stop_on_post=False, enable_fault=False):
    clock = [0.]
    stopped = [False]
    calls = SimpleNamespace(posts=[], gets=[], waits=[], monitors=[], life=[], polls=[],
                            cleanup=[], captures=[], prefetch=[], warnings=[], samplers=[])
    monkeypatch.setattr(engine_module.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(play_module.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(engine_module, "warning", calls.warnings.append)
    started = [False]

    class Job:
        def __init__(self):
            self.requested = clock[0]
            self.job_id = len(calls.posts) + 1

        @property
        def done(self):
            assert not stopped[0], "停止后不得查询截图作业"
            return clock[0] >= self.requested + delay

        def wait(self):
            assert not started[0], "Native启动后不得阻塞等待截图"
            calls.waits.append(self.requested)
            clock[0] = max(clock[0], self.requested + delay)
            return self

        def get(self):
            assert not stopped[0], "停止后不得取图"
            assert clock[0] >= self.requested + delay
            calls.gets.append((self.job_id, clock[0]))
            value = 2 if self.requested >= .6 else 1
            return np.full((720, 1280, 3), value, dtype=np.uint8)

    def post():
        assert not stopped[0], "停止后不得新建截图请求"
        job = Job()
        calls.posts.append(job.requested)
        if stop_on_post and started[0] and job.requested >= .3:
            stopped[0] = True
        return job

    class Capture(StallSafeCapture):
        def enable_fresh_prefetch(self):
            super().enable_fresh_prefetch()
            if enable_fault:
                raise OSError("optional prefetch enable unavailable")

        def has_ready_frame(self):
            if peek_error and started[0]:
                raise OSError("optional ready query unavailable")
            return super().has_ready_frame()

        def prefetch_fresh(self, target):
            calls.prefetch.append((target, clock[0]))
            if prefetch_fail_until is not None and target <= prefetch_fail_until:
                raise OSError("optional prefetch unavailable")
            return super().prefetch_fresh(target)

        def __call__(self):
            image = super().__call__()
            calls.captures.append((clock[0], dict(self.frame_metadata)))
            return image

    class Feedback(NativeLifeFeedback):
        def __init__(self, backend):
            super().__init__(backend)
            calls.samplers.append(self)

        def refresh_phase_status(self, now):
            if feedback_fault == "disabled":
                self.disable(now, "clock-observer-invalid")
                return None
            if feedback_fault is True:
                raise OSError("optional feedback unavailable")
            return super().refresh_phase_status(now)

        def fresh(self, metadata, now):
            if feedback_fault == "fresh":
                raise OSError("optional frame metadata unavailable")
            return super().fresh(metadata, now)

    monkeypatch.setattr(engine_module, "NativeLifeFeedback", Feedback)
    backend = SimpleNamespace(exclusive=True, life_feedback_enabled=True,
        _run_id="feedback-degradation", arm=lambda: None,
        observe_start_frame=lambda _image, now: now,
        start=lambda _anchor: started.__setitem__(0, True),
        phase_status_snapshot=lambda: NativePhaseStatus("feedback-degradation"),
        feedback_observation_snapshot=lambda: None,
        poll=lambda now: calls.polls.append(now),
        stop=lambda: calls.cleanup.append("native-stop"), report=lambda: {},
        revoke_future_phase=lambda *_args: None,
        request_future_phase=lambda *_args: pytest.fail("无方向证据不得申请纠偏"))

    def monitor(image, now):
        value = int(image[0, 0, 0])
        calls.monitors.append((now, value))
        return "completed" if terminal == "completed" and value == 2 else "active"

    def failed_popup(image, now):
        value = int(image[0, 0, 0])
        calls.life.append((now, value))
        return terminal == "life-failed" and value == 2

    engine = engine_module.RealtimeEngine(SimpleNamespace(),
        SimpleNamespace(timing_offset_ms=13),
        SimpleNamespace(close=lambda: calls.cleanup.append("touch-close"),
                        dispatch=lambda *_args: pytest.fail("Native不派发Legacy触控")),
        clock=lambda: clock[0], native_backend=backend,
        playfield_monitor=SimpleNamespace(active=True, mark_active=lambda _now: None,
                                         observe=monitor))
    engine.live_failed_detector = SimpleNamespace(observe=failed_popup)
    capture = Capture(SimpleNamespace(post_screencap=post))
    stats = engine.run(capture, lambda: stopped[0], duration_seconds=2., target_fps=60)
    return stats, calls, capture


@pytest.mark.parametrize("feedback_fault", [False, True])
@pytest.mark.parametrize("terminal", ["completed", "life-failed"])
@pytest.mark.parametrize("delay", [.01, .25])
def test_feedback_failure_keeps_real_capture_and_terminal_monitoring(monkeypatch,
                                                                    feedback_fault,
                                                                    terminal, delay):
    stats, calls, capture = run_degraded_capture(monkeypatch,
        feedback_fault=feedback_fault, terminal=terminal, delay=delay)
    assert stats.completed is (terminal == "completed")
    assert stats.life_failed is (terminal == "life-failed")
    assert not stats.cleanup_failed and not stats.stopped
    assert calls.cleanup == ["native-stop", "touch-close"]
    if feedback_fault:
        assert calls.samplers[0].disabled_reason is not None
    else:
        assert calls.samplers[0].disabled_reason is None
    assert len([row for row in calls.warnings if "NativeLifeFeedback" in row]) == int(feedback_fault)
    assert any(stamp >= .6 for stamp in calls.posts)
    assert calls.life[-1][1] == 2
    assert capture._sequence >= 3
    assert len(calls.waits) == 1
    assert len({job_id for job_id, _ in calls.gets}) == len(calls.gets)
    assert all(right[0] - left[0] >= .199
               for left, right in zip(calls.life, calls.life[1:]))
    assert calls.polls


def test_feedback_and_ready_query_failures_still_capture_at_base_cadence(monkeypatch):
    stats, calls, _ = run_degraded_capture(monkeypatch, peek_error=True)
    assert stats.completed and not stats.cleanup_failed
    assert len([row for row in calls.warnings if "NativeCaptureReady" in row]) == 1
    assert any(metadata["sequence"] > 2 for _, metadata in calls.captures)


@pytest.mark.parametrize("fault", ["fresh", "disabled"])
def test_frame_fault_or_explicit_clock_degradation_keeps_base_monitor(monkeypatch, fault):
    stats, calls, _ = run_degraded_capture(monkeypatch, feedback_fault=fault)
    assert stats.completed and not stats.cleanup_failed
    assert calls.samplers[0].disabled_reason
    assert calls.life[-1][1] == 2
    assert calls.cleanup == ["native-stop", "touch-close"]


@pytest.mark.parametrize("feedback_fault", [False, True])
def test_disabled_feedback_prefetch_failures_retry_only_next_base_target(monkeypatch, feedback_fault):
    stats, calls, _ = run_degraded_capture(monkeypatch,
        feedback_fault=feedback_fault, prefetch_fail_until=.65)
    assert stats.completed and not stats.cleanup_failed
    assert len([row for row in calls.warnings if "NativeCapturePrefetch" in row]) == 1
    failed = [(target, now) for target, now in calls.prefetch if target <= .65]
    assert len(failed) == 3
    assert len({target for target, _ in failed}) == 3
    assert all(right[1] - left[1] >= .199 for left, right in zip(failed, failed[1:]))


def test_prefetch_enable_failure_after_lazy_transition_still_updates_images(monkeypatch):
    stats, calls, _ = run_degraded_capture(monkeypatch,
        feedback_fault=False, enable_fault=True)
    assert stats.completed and not stats.cleanup_failed
    assert "prefetch enable unavailable" in calls.samplers[0].disabled_reason
    assert calls.life[-1][1] == 2
    assert calls.cleanup == ["native-stop", "touch-close"]


def test_stop_during_degraded_base_prefetch_precedes_more_capture_or_monitoring(monkeypatch):
    stats, calls, _ = run_degraded_capture(monkeypatch, stop_on_post=True)
    assert stats.stopped and not stats.completed and not stats.cleanup_failed
    assert calls.cleanup == ["native-stop", "touch-close"]
    stop_at = calls.posts[-1]
    assert all(when < stop_at for _, when in calls.gets)
    assert all(when < stop_at for when, _ in calls.life)
    assert calls.samplers[0].disabled_reason
