from dataclasses import replace

import pytest

from agent.realtime import native_engine
from agent.realtime.native_feedback_ticket import FeedbackObservationState, FeedbackObservationTicket
from agent.realtime.timing_feedback import TimingFeedback
from test_native_feedback_episode import tap
from test_native_feedback_events import sampler_fixture, frame


@pytest.mark.parametrize("direction,step", [(TimingFeedback.FAST, 15), (TimingFeedback.SLOW, -15)])
@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_three_applied_steps_need_new_effect_sources_but_no_blank(direction, step):
    sampler, backend, _ = sampler_fixture()
    session = native_engine.playback_session(publish=lambda chunk: True,
        request_reset=lambda: True, clock=lambda: 0.)
    assert session.arm([dict(kind="tap", lane=2, contact=-1, target_x=510,
        due_s=at, flick_direction=None) for at in (0., 5., 10.)])
    assert session.start(0) and session.publish()
    backend._session = session
    for cycle in range(3):
        for offset in (.1, .2, .3):
            tap(sampler, cycle + offset, direction, chunk=cycle + 1)
        assert sampler.summary["requests"] == cycle + 1
        now = cycle + .35
        backend._clock = lambda: now
        backend._phase_receipts = [(index, now - age, 2, 2, now - age - .002, .5)
                                   for index, age in enumerate((.1, .08, .04))]
        backend._consume_phase_candidate()
        assert backend.phase_status_snapshot().state == "applied"
        sampler.refresh_phase_status(now)
        # 此测试显式发布效果边界；完整编译/执行/jlog 闭环由独立 runner 验证。
        backend._publish_phase_status(replace(backend.phase_status_snapshot(),
            first_adjusted_due_s=cycle + .4, first_affected_chunk_sequence=cycle + 2,
            first_affected_action_token=1), "effect-published")
        sampler.refresh_phase_status(cycle + .4)
        assert session.future_phase_offset_ms == step * (cycle + 1)
    assert sampler.summary["accepted_requests"] == sampler.summary["applied_requests"] == 3


def test_pending_observations_never_carry_votes_past_expiry():
    sampler, backend, _ = sampler_fixture()
    for now in (.1, .2, .3):
        tap(sampler, now)
    original = backend._phase_slot
    for index in range(8):
        tap(sampler, .4 + index * .1)
    assert backend._phase_slot == original and not sampler.votes
    backend._phase_slot = None
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), state="expired",
        terminal_reason="expired"), "expired")
    sampler.refresh_phase_status(1.2)
    for now in (1.3, 1.4):
        tap(sampler, now)
    assert sampler.summary["requests"] == 1
    tap(sampler, 1.5)
    assert sampler.summary["requests"] == 2


def test_rejected_request_retries_only_after_three_new_tickets(monkeypatch):
    sampler, backend, _ = sampler_fixture()
    calls = []
    monkeypatch.setattr(backend, "request_future_phase", lambda delta, at: calls.append((delta, at)) or False)
    for now in (.1, .2, .3):
        tap(sampler, now)
    for now in (.5, .6):
        tap(sampler, now)
    assert len(calls) == 1
    tap(sampler, .7)
    assert len(calls) == 2
    for _ in range(3):
        sampler.capture_due(.71)
    assert len(calls) == 2


def test_foreign_pending_then_terminal_without_phase_change_clears_old_votes():
    sampler, backend, _ = sampler_fixture()
    for now in (.1, .2):
        tap(sampler, now)
    assert len(sampler.votes) == 2
    foreign = replace(backend.phase_status_snapshot(), owner="game-clock", request_sequence=99,
        original_observed_s=.25, state="requested")
    backend._publish_phase_status(foreign, "foreign-request")
    sampler.refresh_phase_status(.25)
    assert not sampler.votes
    backend._publish_phase_status(replace(foreign, state="expired", terminal_reason="expired"), "foreign-expiry")
    sampler.refresh_phase_status(.3)
    tap(sampler, .4)
    tap(sampler, .5)
    assert sampler.summary["requests"] == 0
    tap(sampler, .6)
    assert sampler.summary["requests"] == 1


@pytest.mark.parametrize("problem", ["missing-due", "missing-chunk", "missing-token", "old-chunk",
    "old-token", "old-planned", "old-actual-bound", "foreign-run"])
def test_effect_boundary_and_every_new_source_component_are_required(problem):
    sampler, backend, _ = sampler_fixture()
    for now in (.1, .2, .3):
        tap(sampler, now)
    backend._phase_slot = None
    effect = replace(backend.phase_status_snapshot(), state="applied", cumulative_phase_ms=15,
        first_adjusted_due_s=None if problem == "missing-due" else 1.,
        first_affected_chunk_sequence=None if problem == "missing-chunk" else 2,
        first_affected_action_token=None if problem == "missing-token" else 100)
    backend._publish_phase_status(effect, "effect-published")
    sampler.refresh_phase_status(.4)
    for index, now in enumerate((1.2, 1.3, 1.4)):
        sequence = 20 + index
        ticket = FeedbackObservationTicket("other" if problem == "foreign-run" else "test",
            99 if problem == "old-token" else 100 + index,
            1 if problem == "old-chunk" else 2,
            .9 if problem == "old-planned" else now - .04,
            1.0004 if problem == "old-actual-bound" else now - .04, .5, sequence)
        backend._test_ticket_state = FeedbackObservationState(ticket, sequence, ticket.actual_s, .5)
        sampler.capture_due(now)
        assert sampler.fresh(dict(sequence=100 + index, request_s=now - .01, reused=False), now)
        from test_native_life_feedback import _feedback_image
        sampler.observe_frame(_feedback_image(TimingFeedback.FAST), now)
    assert sampler.summary["requests"] == 1 and not sampler.votes


def test_new_waiting_epoch_drops_old_target_without_revoking_accepted_request():
    sampler, backend, _ = sampler_fixture()
    for now in (0, .1, .2):
        tap(sampler, now)
    original = backend._phase_slot
    backend._compiler.phase_boundary_safe = lambda: False
    backend._consume_phase_candidate()
    target = FeedbackObservationTicket("test", 999, 1, .3, .3, .5, 99)
    backend._test_ticket_state = FeedbackObservationState(target, 99, .3, .5)
    sampler.capture_due(.4)
    assert backend._phase_slot == original
    assert sampler.ignored_reasons["before-observation-epoch"] == 1


@pytest.mark.parametrize("untrusted", [None, "foreign"])
def test_untrusted_phase_status_cannot_carry_two_votes_into_recovery(monkeypatch, untrusted):
    sampler, backend, _ = sampler_fixture()
    for now in (.1, .2):
        tap(sampler, now)
    original_reader = backend.phase_status_snapshot
    status = None if untrusted is None else replace(original_reader(), run_id="other")
    monkeypatch.setattr(backend, "phase_status_snapshot", lambda: status)
    tap(sampler, .3)
    assert not sampler.votes
    monkeypatch.setattr(backend, "phase_status_snapshot", original_reader)
    sampler.refresh_phase_status(.4)
    tap(sampler, .5)
    tap(sampler, .6)
    assert sampler.summary["requests"] == 0
    tap(sampler, .7)
    assert sampler.summary["requests"] == 1


def test_phase_limit_uses_request_delta_after_local_drop_and_reverse_restores_headroom():
    sampler, backend, _ = sampler_fixture()
    backend._session.future_phase_offset_ms = 195
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), state="applied",
        cumulative_phase_ms=195, first_adjusted_due_s=0, first_affected_chunk_sequence=1,
        first_affected_action_token=1), "seed-phase")
    sampler.refresh_phase_status(0)
    for now in (.1, .2, .3):
        tap(sampler, now)
    sampler._clear_trend("ticket-expired", revoke=False)
    assert sampler.qualified_direction is None
    backend._phase_slot = None
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), state="rejected",
        terminal_reason="phase-limit"), "phase-limit")
    sampler.refresh_phase_status(.4)
    assert sampler._blocked_direction is TimingFeedback.FAST
    for now in (.5, .6, .7):
        tap(sampler, now)
    assert sampler.summary["requests"] == 1
    for now in (.8, .9, 1.):
        tap(sampler, now, TimingFeedback.SLOW)
    assert sampler.summary["requests"] == 2
    def apply(delta, safe):
        backend._session.future_phase_offset_ms += delta
        return "applied"
    backend._session.apply_future_phase = apply
    backend._clock = lambda: 1.05
    backend._consume_phase_candidate()
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), first_adjusted_due_s=1.1,
        first_affected_chunk_sequence=2, first_affected_action_token=1), "effect-published")
    sampler.refresh_phase_status(1.1)
    assert backend._session.future_phase_offset_ms == 180 and sampler._blocked_direction is None
    for now in (1.2, 1.3, 1.4):
        tap(sampler, now, chunk=2)
    assert sampler.summary["requests"] == 3


def test_neutral_target_clears_two_votes_and_reverse_needs_three_new_observations():
    sampler, backend, _ = sampler_fixture()
    tap(sampler, .1)
    tap(sampler, .2)
    previous = backend._test_ticket_state
    target = replace(previous.ticket, action_token=99, judgement_sequence=previous.judgement_sequence + 1,
                     actual_s=.26, planned_s=.26)
    backend._test_ticket_state = FeedbackObservationState(target, target.judgement_sequence, .26, .5)
    frame(sampler, None, .3)
    assert not sampler.votes and sampler.summary["requests"] == 0
    tap(sampler, .4)
    tap(sampler, .5)
    tap(sampler, .6, TimingFeedback.SLOW)
    assert len(sampler.votes) == 1 and sampler.summary["requests"] == 0
    tap(sampler, .7, TimingFeedback.SLOW)
    assert sampler.summary["requests"] == 0
    tap(sampler, .8, TimingFeedback.SLOW)
    assert backend._phase_slot[3] == -15


def test_duplicate_ticket_cannot_supply_third_continuous_vote():
    sampler, backend, _ = sampler_fixture()
    tap(sampler, .1, token=9)
    tap(sampler, .2, token=10)
    tap(sampler, .3, token=9)
    assert sampler.summary["requests"] == 0 and len(sampler.votes) == 2
    tap(sampler, .4, token=11)
    assert sampler.summary["requests"] == 1
