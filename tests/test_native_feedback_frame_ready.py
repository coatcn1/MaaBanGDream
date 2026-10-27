from types import SimpleNamespace

import pytest

from agent.realtime import engine as engine_module
from agent.realtime.native_feedback_ticket import FeedbackTicketStream
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativePhaseStatus
from agent.realtime.timing_feedback import TimingFeedback
from test_native_life_feedback import _feedback_image


def _run_capture_crossing_due(monkeypatch, *, early_request=False, stop_in_capture=False,
                             capture_seconds=.020, target_blank=False):
    clock = [0.0]
    stopped = [False]
    calls = SimpleNamespace(captures=[], prefetch=[], fresh=[], observed=[],
                            monitor=[], polls=[], cleanup=[])
    monkeypatch.setattr(engine_module.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    stream = FeedbackTicketStream("frame-ready")
    receipt = dict(action_token=1, planned_engine_s=.16, action_kind="tap",
                   command="d", feedback_single_tap=True)
    stream.publish_receipts([receipt], published_until_s=2., final_chunk=True)
    committed = [False]

    def snapshot():
        if clock[0] >= .16 and not committed[0]:
            stream.observe_commit([receipt], .16, 1, .5)
            committed[0] = True
        return stream.snapshot()

    samplers = []

    class ObservedFeedback(NativeLifeFeedback):
        def __init__(self, backend):
            super().__init__(backend)
            samplers.append(self)

        def fresh(self, metadata, now):
            result = super().fresh(metadata, now)
            calls.fresh.append((dict(metadata), now, result))
            return result

        def observe_frame(self, image, now):
            calls.observed.append((self.frame_evidence["frame_sequence"], now))
            return super().observe_frame(image, now)

    monkeypatch.setattr(engine_module, "NativeLifeFeedback", ObservedFeedback)
    blank = _feedback_image(None)
    fast = _feedback_image(TimingFeedback.FAST)

    class Capture:
        frame_metadata = {}

        def enable_fresh_prefetch(self):
            pass

        def prefetch_fresh(self, now):
            calls.prefetch.append(now)
            return True

        def __call__(self):
            before = clock[0]
            # 基础监控先取走已完成的新图；默认后续只能返回同序号复用图。
            if calls.captures:
                clock[0] += capture_seconds
            now = clock[0]
            target_frame = bool(calls.captures)
            if len(calls.captures) == 1:
                calls.target_request = calls.prefetch[-1]
            if len(calls.captures) < 2:
                self.frame_metadata = dict(sequence=2 if target_frame else 1, reused=False,
                    request_s=(.18 if early_request else calls.target_request) if target_frame else 0.,
                    first_done_observed_s=now)
            elif target_blank and len(calls.captures) == 2:
                # 空白目标只提供既有的一次新图重采样，随后继续复用同图。
                self.frame_metadata = dict(sequence=3, reused=False,
                    request_s=calls.prefetch[-1], first_done_observed_s=now)
            else:
                self.frame_metadata = dict(self.frame_metadata, reused=True)
            calls.captures.append((before, now))
            if target_frame and stop_in_capture:
                stopped[0] = True
            return fast if target_frame and not target_blank else blank

    def monitor(_image, now):
        calls.monitor.append(now)
        return "active"

    backend = SimpleNamespace(exclusive=True, life_feedback_enabled=True,
        _run_id="frame-ready", arm=lambda: None,
        observe_start_frame=lambda _image, now: now, start=lambda _anchor: None,
        phase_status_snapshot=lambda: NativePhaseStatus("frame-ready"),
        feedback_observation_snapshot=snapshot,
        poll=lambda now: calls.polls.append(now),
        stop=lambda: calls.cleanup.append("native-stop"), report=lambda: {},
        revoke_future_phase=lambda _reason: None,
        request_future_phase=lambda *_args: pytest.fail("单票不应提交相位请求"))
    touch = SimpleNamespace(close=lambda: calls.cleanup.append("touch-close"),
        dispatch=lambda *_args: pytest.fail("Native 不应派发 Legacy 输入"))
    engine = engine_module.RealtimeEngine(SimpleNamespace(),
        SimpleNamespace(timing_offset_ms=13), touch, clock=lambda: clock[0],
        native_backend=backend,
        playfield_monitor=SimpleNamespace(active=True, mark_active=lambda _now: None,
                                         observe=monitor))
    stats = engine.run(Capture(), lambda: stopped[0], duration_seconds=1., target_fps=60)
    assert not stats.cleanup_failed
    assert samplers[0].disabled_reason is None
    return stats, calls, samplers[0], backend


def test_capture_crossing_target_due_observes_new_frame_exactly_once(monkeypatch):
    stats, calls, sampler, _backend = _run_capture_crossing_due(monkeypatch)
    before, after = calls.captures[1]
    due = calls.target_request + NativeLifeFeedback.SAMPLE_INTERVAL_SECONDS
    assert before < due < after
    target_fresh = [row for row in calls.fresh if row[0]["sequence"] == 2 and row[2]]
    assert len(target_fresh) == 1
    assert target_fresh[0][1] == after
    assert [row for row in calls.observed if row[0] == 2] == [(2, after)]
    assert sampler.summary["direction_votes"] == stats.timing_feedback_valid == 1
    assert sampler.summary["requests"] == 0
    assert any(row[0]["reused"] and not row[2] for row in calls.fresh)
    assert sampler.ignored_reasons["slow-or-reused-frames"] > 0


def test_completed_new_frame_before_consumer_due_observes_exactly_once(monkeypatch):
    stats, calls, sampler, _backend = _run_capture_crossing_due(monkeypatch, capture_seconds=.002)
    before, after = calls.captures[1]
    due = calls.target_request + NativeLifeFeedback.SAMPLE_INTERVAL_SECONDS
    assert before == pytest.approx(.200)
    assert after == pytest.approx(.202)
    assert .16 + .0005 + NativeLifeFeedback.RENDER_WAIT_SECONDS <= calls.target_request
    assert before < after < due
    target_fresh = [row for row in calls.fresh if row[0]["sequence"] == 2 and row[2]]
    assert len(target_fresh) == 1
    assert target_fresh[0][1] == after
    assert [row for row in calls.observed if row[0] == 2] == [(2, after)]
    assert sampler.summary["direction_votes"] == stats.timing_feedback_valid == 1
    assert sampler.summary["requests"] == 0
    assert any(row[0]["sequence"] == 2 and row[0]["reused"] and not row[2]
               for row in calls.fresh)


def test_blank_before_consumer_due_only_uses_existing_one_resample(monkeypatch):
    stats, calls, sampler, _backend = _run_capture_crossing_due(
        monkeypatch, capture_seconds=.002, target_blank=True)
    _before, after = calls.captures[1]
    assert after < calls.target_request + NativeLifeFeedback.SAMPLE_INTERVAL_SECONDS
    assert [row for row in calls.observed if row[0] == 2] == [(2, after)]
    decisions = [row["decision"] for row in sampler.sniff_records]
    assert decisions.count("target-blank-awaiting-render") == 1
    assert decisions.count("neutral-target") == 1
    assert len([row for row in calls.fresh if row[0]["sequence"] == 3 and row[2]]) == 1
    assert sampler._target is None
    assert sampler.summary["direction_votes"] == stats.timing_feedback_valid == 0
    assert sampler.summary["requests"] == 0


def test_capture_crossing_due_keeps_request_before_render_rejected(monkeypatch):
    stats, calls, sampler, _backend = _run_capture_crossing_due(monkeypatch, early_request=True)
    target_fresh = [row for row in calls.fresh if row[0]["sequence"] == 2 and row[2]]
    assert len(target_fresh) == 1
    metadata, _now, _fresh = target_fresh[0]
    assert metadata["request_s"] < .16 + .0005 + NativeLifeFeedback.RENDER_WAIT_SECONDS
    assert sampler.ignored_reasons["frame-before-target-render"] == 1
    assert sampler.summary["direction_votes"] == stats.timing_feedback_valid == 0
    assert sampler.summary["requests"] == 0


def test_stop_during_capture_precedes_frame_observation_and_input(monkeypatch):
    stats, calls, sampler, _backend = _run_capture_crossing_due(monkeypatch, stop_in_capture=True)
    assert stats.stopped
    assert len(calls.captures) == 2
    assert [row[0]["sequence"] for row in calls.fresh] == [1]
    assert [row[0] for row in calls.observed] == [1]
    assert calls.monitor == calls.polls == [0.]
    assert sampler.summary["direction_votes"] == 0
    assert calls.cleanup == ["native-stop", "touch-close"]
