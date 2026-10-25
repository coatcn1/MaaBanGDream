from dataclasses import replace

import pytest

from agent.realtime.timing_feedback import TimingFeedback
from test_native_feedback_events import sampler_fixture, frame, two_events


def publish_outcome(sampler, backend, state, *, due=None, chunk=2, reason=None, cumulative=15):
    backend._phase_slot = None
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, state=state, terminal_reason=reason,
        cumulative_phase_ms=cumulative, first_adjusted_due_s=due,
        first_affected_chunk_sequence=chunk if due is not None else None,
        first_affected_action_token=1 if due is not None else None), "test-outcome")
    sampler.refresh_phase_status(.3)


def separated_pair(sampler, direction, start, *, chunk=2):
    for now in (start - .10, start - .05):
        frame(sampler, None, now, chunk=chunk)
    for now, kind in ((start, direction), (start + .05, None),
                      (start + .10, direction), (start + .15, None),
                      (start + .20, direction), (start + .25, direction)):
        frame(sampler, kind, now, chunk=chunk)


def test_expired_request_requires_two_new_events_and_does_not_extend_old_ttl():
    sampler, backend, _ = sampler_fixture()
    two_events(sampler)
    expiry = backend._phase_slot[-1]
    for now in (.3, .35, .4, .45):
        frame(sampler, None, now)
    assert backend._phase_slot[-1] == expiry
    publish_outcome(sampler, backend, "expired", reason="expired", cumulative=0)
    separated_pair(sampler, TimingFeedback.FAST, .6)
    assert sampler.summary["requests"] == 2


@pytest.mark.parametrize("problem", ["missing-effect", "old-chunk", "old-token", "old-planned", "prefetch"])
def test_post_effect_requires_causal_affected_down(problem):
    sampler, backend, _ = sampler_fixture()
    two_events(sampler)
    publish_outcome(sampler, backend, "applied", due=None if problem == "missing-effect" else .3)
    chunk = 1 if problem == "old-chunk" else 2
    if problem in {"old-token", "old-planned"}:
        status = backend.phase_status_snapshot()
        updated = replace(status, first_affected_action_token=1000) if problem == "old-token" else replace(status, first_adjusted_due_s=100)
        backend._publish_phase_status(updated, "changed-effect")
    def emit(kind, now):
        frame(sampler, kind, now, chunk=chunk,
              request=now - (.06 if problem == "prefetch" else .01),
              actual=now - .02 if problem == "prefetch" else None)
    emit(None, .4)
    emit(None, .45)
    for now, kind in ((.5, TimingFeedback.FAST), (.55, TimingFeedback.FAST),
                      (.6, None), (.65, None), (.7, TimingFeedback.FAST), (.75, TimingFeedback.FAST)):
        emit(kind, now)
    assert sampler.summary["requests"] == 1


def test_effect_published_later_requires_new_post_effect_blank_then_two_events():
    sampler, backend, _ = sampler_fixture()
    two_events(sampler)
    publish_outcome(sampler, backend, "applied")
    separated_pair(sampler, TimingFeedback.FAST, .6)
    assert sampler.summary["requests"] == 1
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, first_adjusted_due_s=.9,
        first_affected_chunk_sequence=2, first_affected_action_token=1), "effect-published")
    separated_pair(sampler, TimingFeedback.FAST, 1.1)
    assert sampler.summary["requests"] == 2


def test_revoke_owner_apply_interleaving_preserves_real_effect_requirement():
    sampler, backend, _ = sampler_fixture()
    two_events(sampler)
    status = backend.phase_status_snapshot()
    backend._phase_slot = None
    backend._publish_phase_status(replace(status, state="applied", cumulative_phase_ms=15,
        first_adjusted_due_s=.3, first_affected_chunk_sequence=2,
        first_affected_action_token=1), "applied-before-revoke")
    sampler.close(.3, "invalid-scene")
    sampler.refresh_phase_status(.35)
    assert sampler._effect_status.cumulative_phase_ms == 15
    for now in (.4, .45, .5):
        frame(sampler, TimingFeedback.SLOW, now, chunk=2)
    assert sampler.summary["requests"] == 1


def test_same_run_foreign_sequence_cannot_clear_our_pending():
    sampler, backend, _ = sampler_fixture()
    two_events(sampler)
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, request_sequence=99,
        state="expired", original_observed_s=status.original_observed_s), "foreign")
    assert not sampler.refresh_phase_status(.4)
    assert sampler._pending_sequence == 1
    separated_pair(sampler, TimingFeedback.FAST, .6)
    assert sampler.summary["requests"] == 1


def test_thirteen_production_steps_reach_195_and_reverse_application_releases_headroom():
    sampler, backend, _ = sampler_fixture()
    backend._session.future_phase_offset_ms = 0
    def apply(delta, _safe):
        candidate = backend._session.future_phase_offset_ms + delta
        if abs(candidate) > 200:
            return "phase-limit"
        backend._session.future_phase_offset_ms = candidate
        return "applied"
    backend._session.apply_future_phase = apply
    def consume(start, chunk):
        backend._clock = lambda: start + .30
        backend._phase_receipts.clear()
        for commit, age in enumerate((.10, .08, .04)):
            actual = start + .30 - age
            backend._phase_receipts.append((commit, actual, 2, 2, actual - .002, .5))
        backend._consume_phase_candidate()
        status = backend.phase_status_snapshot()
        if status.state == "applied":
            backend._publish_phase_status(replace(status, first_adjusted_due_s=start + .4,
                first_affected_chunk_sequence=chunk + 1, first_affected_action_token=1), "effect-published")
        sampler.refresh_phase_status(start + .3)
    for index in range(14):
        start = .1 + index * .8
        separated_pair(sampler, TimingFeedback.FAST, start, chunk=index + 1)
        consume(start, index + 1)
    assert backend._session.future_phase_offset_ms == 195
    assert sampler._blocked_direction is TimingFeedback.FAST
    separated_pair(sampler, TimingFeedback.SLOW, 11.3, chunk=15)
    assert sampler._blocked_direction is TimingFeedback.FAST
    consume(11.3, 15)
    assert backend._session.future_phase_offset_ms == 180
    assert sampler._blocked_direction is None
    separated_pair(sampler, TimingFeedback.FAST, 12.1, chunk=16)
    consume(12.1, 16)
    assert backend._session.future_phase_offset_ms == 195


def test_request_cooldown_never_retries_from_timer_or_old_blank_frames():
    sampler, backend, _ = sampler_fixture()
    two_events(sampler)
    publish_outcome(sampler, backend, "expired", reason="expired", cumulative=0)
    for now, kind in ((.28, None), (.31, None), (.36, TimingFeedback.FAST),
                      (.39, TimingFeedback.FAST), (.42, None), (.45, None),
                      (.50, TimingFeedback.FAST), (.53, TimingFeedback.FAST),
                      (.56, None), (.61, None)):
        frame(sampler, kind, now)
    assert sampler.summary["requests"] == 1
    assert backend._phase_slot is None
    frame(sampler, TimingFeedback.FAST, .66)
    frame(sampler, TimingFeedback.FAST, .71)
    assert sampler.summary["requests"] == 2
