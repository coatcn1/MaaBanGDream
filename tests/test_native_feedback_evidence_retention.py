from dataclasses import replace

import pytest

from agent.realtime.native_feedback_ticket import FeedbackObservationState, FeedbackObservationTicket
from agent.realtime.timing_feedback import TimingFeedback
from test_native_feedback_episode import tap
from test_native_feedback_events import frame, sampler_fixture, two_events
from test_native_life_feedback import _feedback_image


def blank_target(sampler, now, token):
    state = sampler.backend._test_ticket_state
    ticket = FeedbackObservationTicket("test", token, 1, now - .04, now - .04, .5,
                                       state.judgement_sequence + 1)
    sampler.backend._test_ticket_state = FeedbackObservationState(ticket, ticket.judgement_sequence,
                                                                 ticket.actual_s, .5)
    frame(sampler, None, now)


@pytest.mark.parametrize("inconclusive", ["blank", "covered", "short-reused", "short-old"])
def test_local_inconclusive_target_never_erases_two_valid_votes(inconclusive):
    sampler, backend, _ = sampler_fixture()
    tap(sampler, .1, token=1)
    tap(sampler, .2, token=2)
    if inconclusive == "blank":
        blank_target(sampler, .32, 99)
        frame(sampler, None, .34)
    elif inconclusive == "covered":
        state = backend._test_ticket_state
        ticket = replace(state.ticket, action_token=99, actual_s=.26, planned_s=.26,
                         judgement_sequence=state.judgement_sequence + 1)
        backend._test_ticket_state = FeedbackObservationState(ticket, ticket.judgement_sequence,
                                                             .26, .5, .27)
        sampler.capture_due(.3)
    else:
        assert not sampler.fresh(dict(sequence=100, request_s=.1 if inconclusive == "short-old" else .29,
                                      reused=inconclusive == "short-reused"), .3)
    assert len(sampler.votes) == 2
    tap(sampler, .4, token=3)
    assert sampler.summary["requests"] == 1


@pytest.mark.parametrize("inconclusive", ["first-blank", "short-reused"])
def test_waiting_touch_boundary_request_survives_inconclusive_target_with_original_ttl(inconclusive):
    sampler, backend, calls = sampler_fixture()
    two_events(sampler, TimingFeedback.SLOW)
    backend._compiler.phase_boundary_safe = lambda: False
    backend._consume_phase_candidate()
    original = backend._phase_slot
    assert backend.phase_status_snapshot().state == "waiting-touch-boundary"
    sampler.refresh_phase_status(.26)
    if inconclusive == "first-blank":
        blank_target(sampler, .32, 99)
        assert sampler.last_sniff["decision"] == "target-blank-awaiting-render"
    else:
        assert not sampler.fresh(dict(sequence=100, request_s=.29, reused=True), .3)
    assert backend._phase_slot == original and not calls
    backend._clock = lambda: original[-1] + .001
    backend._consume_phase_candidate()
    assert backend._phase_slot is None and backend.phase_status_snapshot().state == "expired"
    assert not calls


def test_real_c1_late_valid_vote_spacing_requires_ten_second_window():
    sampler, backend, _ = sampler_fixture()
    # C1 原始有效票 token102/147/148；只复现首次新请求，不外推改变输入后的画面。
    for token, at in ((102, 14.026819), (147, 22.428532), (148, 22.591564)):
        while sampler.last_fresh_at is not None and sampler.last_fresh_at + .2 < at:
            blank_target(sampler, sampler.last_fresh_at + .2, 10000 + sampler.last_sequence)
        tap(sampler, at, TimingFeedback.SLOW, token=token)
    assert sampler.VOTE_WINDOW_SECONDS == 10
    assert sampler.summary["requests"] == 1
    assert backend._phase_slot[3] == -15


def test_expired_votes_cannot_accumulate_across_continuous_fresh_but_directionless_frames():
    sampler, backend, _ = sampler_fixture()
    tap(sampler, .1, token=1)
    tap(sampler, .2, token=2)
    while sampler.last_fresh_at + .2 < 10.4:
        frame(sampler, None, sampler.last_fresh_at + .2)
    tap(sampler, 10.4, token=3)
    assert len(sampler.votes) == 1 and backend._phase_slot is None
    assert sampler.summary["expired_direction_votes"] == 2


@pytest.mark.parametrize("pending", [False, True])
def test_real_fresh_gap_still_clears_evidence_and_pending(pending):
    sampler, backend, _ = sampler_fixture()
    if pending:
        two_events(sampler)
    else:
        tap(sampler, .1)
        tap(sampler, .2)
    assert not sampler.fresh(dict(sequence=100, request_s=.99, reused=True), 1.)
    assert not sampler.votes and backend._phase_slot is None
