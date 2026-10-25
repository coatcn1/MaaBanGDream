import json
import os
from pathlib import Path
from dataclasses import replace

import numpy as np
import pytest

from agent.realtime.native_feedback_ticket import FeedbackObservationState, FeedbackObservationTicket
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativePhaseStatus
from agent.realtime import native_engine
from agent.realtime.vision_io import imread_unicode
from test_native_life_feedback import _phase_backend
from test_native_feedback_events import sampler_fixture, frame
from test_native_feedback_continuation import publish_outcome
from agent.realtime.timing_feedback import TimingFeedback


def tap(sampler, now, direction=TimingFeedback.FAST, **kwargs):
    sampler.backend._test_direction = None
    frame(sampler, direction, now, **kwargs)


def rejected_request(monkeypatch):
    sampler, backend, _ = sampler_fixture()
    calls = []
    monkeypatch.setattr(backend, "request_future_phase", lambda delta, observed: calls.append((delta, observed)) or False)
    for now in (0, .1, .2):
        tap(sampler, now)
    assert len(calls) == 1
    return sampler, backend, calls


@pytest.mark.parametrize("invalid", ["mixed", "reverse", "scene", "reused", "gap", "guard", "phase"])
def test_rejected_request_recollects_new_tickets_after_invalidation(monkeypatch, invalid):
    sampler, backend, calls = rejected_request(monkeypatch)
    if invalid in {"mixed", "reverse"}:
        tap(sampler, .3, "mixed" if invalid == "mixed" else TimingFeedback.SLOW)
    elif invalid == "scene":
        sampler.close(.3, "invalid-scene")
    elif invalid == "reused":
        assert not sampler.fresh(dict(sequence=100, request_s=.29, reused=True), .3)
    elif invalid == "gap":
        tap(sampler, 1.)
    elif invalid == "guard":
        backend.set_visual_phase_guard(True)
        sampler.refresh_phase_status(.3)
        backend.set_visual_phase_guard(False)
        sampler.refresh_phase_status(.31)
    else:
        backend._publish_phase_status(replace(backend.phase_status_snapshot(), owner="game-clock",
            state="applied", request_sequence=99, cumulative_phase_ms=15), "applied")
        sampler.refresh_phase_status(.3)
    for now in (1.1, 1.2, 1.3):
        tap(sampler, now)
    assert len(calls) == (1 if invalid == "phase" else 2)


def test_blank_frames_do_not_create_requests_without_new_tickets(monkeypatch):
    sampler, backend, calls = rejected_request(monkeypatch)
    frame(sampler, None, .3)
    for now in (.4, .5, .6):
        tap(sampler, now)
    assert len(calls) == 2
    frame(sampler, None, .7)
    frame(sampler, None, .75)
    assert not sampler.votes
    for now in (.85, .95, 1.05):
        tap(sampler, now)
    assert len(calls) == 3


def test_request_exception_consumes_votes_before_call(monkeypatch):
    sampler, backend, _ = sampler_fixture()
    calls = []
    def fail(delta, observed):
        calls.append((delta, observed))
        raise OSError("request unavailable")
    monkeypatch.setattr(backend, "request_future_phase", fail)
    tap(sampler, 0)
    tap(sampler, .1)
    with pytest.raises(OSError, match="request unavailable"):
        tap(sampler, .2)
    assert not sampler.votes
    for now in (.3, .4, .5):
        sampler.capture_due(now)
    assert len(calls) == 1


def test_first_unsubmitted_episode_can_restart_three_votes_after_reused_frame():
    sampler, backend, _ = sampler_fixture()
    tap(sampler, 0)
    assert not sampler.fresh(dict(sequence=100, request_s=.04, reused=True), .05)
    assert not sampler.votes
    for now in (.1, .2, .3):
        tap(sampler, now)
    assert sampler.summary["requests"] == 1


def test_pending_cannot_vote_and_next_round_needs_affected_execution():
    sampler, backend, _ = sampler_fixture()
    for now in (0, .1, .2):
        tap(sampler, now)
    for now in (.3, .35):
        frame(sampler, None, now)
    assert sampler._pending_observed_s is not None and not sampler.votes
    publish_outcome(sampler, backend, "applied", due=.4, chunk=2)
    for now in (.5, .55):
        frame(sampler, None, now, chunk=1)
    assert not sampler.votes
    for now, token in ((.6, 2000), (.65, 2001)):
        frame(sampler, None, now, chunk=2, token=token)
    assert sampler._effect_status.request_sequence == 1
    for now, token in ((.75, 2002), (.85, 2003), (.95, 2004)):
        tap(sampler, now, chunk=2, token=token)
    assert sampler.summary["requests"] == 2


@pytest.mark.parametrize("reason,seam", [(reason, seam)
    for reason in ("overwritten", "covered", "expired") for seam in ("capture", "signal")]
    + [("before-render", "signal")])
@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_target_local_drop_keeps_waiting_slow_request_and_native_applies_once(reason, seam):
    sampler, backend, _ = sampler_fixture()
    session = native_engine.playback_session(publish=lambda chunk: True,
        request_reset=lambda: True, clock=lambda: 0.)
    assert session.arm([dict(kind="tap", lane=2, contact=-1, target_x=510,
        due_s=at, flick_direction=None) for at in (0., 5.)])
    assert session.start(0) and session.publish()
    backend._session = session
    for now in (0, .1, .2):
        tap(sampler, now, TimingFeedback.SLOW)
    original = backend._phase_slot
    assert original[3] == -15 and sampler.summary["accepted_requests"] == 1
    backend._compiler.phase_boundary_safe = lambda: False
    backend._consume_phase_candidate()
    assert backend.phase_status_snapshot().state == "waiting-touch-boundary"
    previous = backend._test_ticket_state
    target = replace(previous.ticket, action_token=999, judgement_sequence=previous.judgement_sequence + 1,
                     actual_s=.36, planned_s=.36)
    backend._test_ticket_state = FeedbackObservationState(target, target.judgement_sequence, .36, .5)
    sampler.capture_due(.4)
    now = .57 if reason == "expired" else .41
    if reason == "covered":
        backend._test_ticket_state = replace(backend._test_ticket_state, next_judgement_lower_s=.405)
    elif reason == "overwritten":
        backend._test_ticket_state = replace(backend._test_ticket_state, judgement_sequence=target.judgement_sequence + 1)
    if seam == "capture":
        sampler.capture_due(now)
    else:
        request_s = .37 if reason == "before-render" else now - .01
        assert sampler.fresh(dict(sequence=100, request_s=request_s, reused=False), now)
        from test_native_life_feedback import _feedback_image
        sampler.observe_frame(_feedback_image(TimingFeedback.SLOW), now)
    assert backend._phase_slot == original
    assert backend._phase_slot[-1] == original[2] + 2.
    assert not sampler.votes
    backend._compiler.phase_boundary_safe = lambda: True
    backend._consume_phase_candidate()
    sampler.refresh_phase_status(1.)
    assert session.future_phase_offset_ms == -15
    assert backend.phase_status_snapshot().state == "applied"
    assert sampler.summary["requests"] == sampler.summary["accepted_requests"] == sampler.summary["applied_requests"] == 1
    backend._consume_phase_candidate()
    assert session.future_phase_offset_ms == -15


@pytest.mark.parametrize("invalid", ["reverse", "mixed", "scene", "reused", "gap", "stop"])
def test_global_evidence_can_still_revoke_waiting_request_after_local_drop(invalid):
    sampler, backend, calls = sampler_fixture()
    for now in (0, .1, .2):
        tap(sampler, now, TimingFeedback.SLOW)
    backend._compiler.phase_boundary_safe = lambda: False
    backend._consume_phase_candidate()
    previous = backend._test_ticket_state
    target = replace(previous.ticket, action_token=999, judgement_sequence=previous.judgement_sequence + 1,
                     actual_s=.36, planned_s=.36)
    backend._test_ticket_state = FeedbackObservationState(target, target.judgement_sequence, .36, .5)
    sampler.capture_due(.4)
    backend._test_ticket_state = replace(backend._test_ticket_state, next_judgement_lower_s=.405)
    sampler.capture_due(.41)
    assert backend._phase_slot is not None and not sampler.votes and sampler.qualified_direction is None
    backend._test_ticket_state = replace(backend._test_ticket_state, ticket=None, next_judgement_lower_s=None)
    if invalid in {"reverse", "mixed"}:
        frame(sampler, TimingFeedback.FAST if invalid == "reverse" else "mixed", .5, eligible=False)
    elif invalid == "scene":
        sampler.observe_scene(.5, visible=False)
    elif invalid == "reused":
        assert not sampler.fresh(dict(sequence=101, request_s=.49, reused=True), .5)
    elif invalid == "gap":
        frame(sampler, TimingFeedback.SLOW, 1., eligible=False)
    else:
        sampler.close(.5, "stopped")
    backend._compiler.phase_boundary_safe = lambda: True
    backend._consume_phase_candidate()
    assert backend._phase_slot is None and not calls


def test_saved_fallen_target_rois_allow_one_bounded_first_request():
    manifest_path = os.environ.get("MBD_NATIVE_FEEDBACK_FIXTURE_MANIFEST")
    if not manifest_path:
        pytest.skip("未提供本机真实 Native 目标截图清单")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    root = Path(manifest["recording_root"])
    backend, _ = _phase_backend()
    backend._run_id = manifest["run_id"]
    backend._phase_status = NativePhaseStatus(backend._run_id)
    backend._test_ticket_state = FeedbackObservationState()
    backend.feedback_observation_snapshot = lambda: backend._test_ticket_state
    sampler = NativeLifeFeedback(backend)
    sampler.observe_scene(0, visible=True)
    rows = manifest["events"]
    assert len(rows) == 26
    for row in rows:
        metadata = row["frame_metadata"]
        sniff = metadata["feedback_sniff"]
        assert sniff["decision"] == "no-blank-baseline"
        assert metadata["reused"] is False and 0 <= row["timestamp"] - metadata["request_s"] <= .080
        ticket = FeedbackObservationTicket(**sniff["input"])
        backend._test_ticket_state = FeedbackObservationState(ticket, ticket.judgement_sequence,
            ticket.actual_s, ticket.clock_uncertainty_ms)
        now = row["timestamp"]
        sampler.capture_due(now)
        crop = imread_unicode(root / row["screenshot"])
        x1, y1, x2, y2 = metadata["roi"]
        assert crop.shape == (y2 - y1, x2 - x1, 3)
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        image[y1:y2, x1:x2] = crop
        # 保存的是已通过因果、新帧和覆盖门禁的 26 张目标 ROI。
        # 缺少中间基础帧时只重放此判定 seam，不假造连续画面或完整运行回放。
        sampler.frame_evidence = {key: sniff[key] for key in
            ("frame_sequence", "request_s", "reused", "observed_s", "request_age_ms")}
        sampler._pending_frame_at = now
        sampler.observe_frame(image, now)
    assert sampler.summary["requests"] == 1
    assert sampler.summary["accepted_requests"] == 1
    assert sampler.summary["distinct_events"] == 0
    assert sampler.summary["single_tap_observations"] == 26
    assert sampler.summary["direction_votes"] == 3
