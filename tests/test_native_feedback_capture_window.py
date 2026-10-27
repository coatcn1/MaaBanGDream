from types import SimpleNamespace
from dataclasses import replace
import json

import pytest

from agent.realtime.native_feedback_ticket import FeedbackTicketStream, SingleTapEligibility
from agent.realtime.timing_feedback import TimingFeedback
from agent.realtime.profile_play_action import StallSafeCapture
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativeMinitouchBackend
from agent.realtime import native_engine
from test_native_feedback_events import sampler_fixture
from test_native_feedback_tickets import action
from test_native_life_feedback import _feedback_image


def receipt(token, due, *, eligible=True, kind="tap"):
    return dict(action_token=token, planned_engine_s=due, action_kind=kind,
                command="d", feedback_single_tap=eligible)


def test_each_executed_homogeneous_tap_group_supplies_one_ticket():
    actions = [action(due=1, lane=2), action(due=1, lane=3, note=2)]
    eligibility = SingleTapEligibility(actions, allow_groups=True)
    assert all(eligibility.eligible(row) for row in actions)
    stream = FeedbackTicketStream("test")
    for index in range(3):
        rows = [receipt(2 * index + 1, 1 + index), receipt(2 * index + 2, 1 + index)]
        stream.publish_receipts(rows, published_until_s=2 + index)
        stream.observe_commit(rows, 1 + index, index + 1, .5)
        ticket = stream.snapshot().ticket
        assert ticket is not None
        assert ticket.action_tokens == (2 * index + 1, 2 * index + 2)
        assert ticket.judgement_sequence == 2 * index + 2
    assert stream.summary["ticket-created"] == 3


@pytest.mark.parametrize("problem", [None, "early-successor", "late-completion", "missing-bound"])
def test_completed_prefetch_survives_processing_delay_only_before_first_successor(problem):
    sampler, backend, _ = sampler_fixture()
    stream = FeedbackTicketStream("test")
    backend.feedback_observation_snapshot = stream.snapshot
    target = receipt(1, 1.)
    stream.publish_receipts([target], published_until_s=2.)
    stream.observe_commit([target], 1., 1, .5)
    sampler.capture_due(1.026)
    sampler.capture_requested(1.026)
    # 截图已在后继判定前完成；消费者延迟至两次后继回执以后才读图。
    for token, actual in ((2, 1.04 if problem == "early-successor" else 1.10), (3, 1.12)):
        row = receipt(token, actual, eligible=False)
        stream.publish_receipts([row], published_until_s=2.)
        stream.observe_commit([row], actual, 2, .5)
    sampler.capture_due(1.13)
    assert sampler._target is not None, "预取图须先消费，不能由处理时刻提前丢票"
    metadata = dict(sequence=1, request_s=1.06, reused=False,
                    first_done_observed_s=1.11 if problem == "late-completion" else 1.08)
    if problem == "missing-bound":
        metadata.pop("first_done_observed_s")
    assert sampler.fresh(metadata, 1.13)
    sampler.observe_frame(_feedback_image(TimingFeedback.FAST), 1.13)
    assert sampler.summary["direction_votes"] == (1 if problem is None else 0)
    assert backend._phase_slot is None


def test_latest_successor_cannot_hide_an_earlier_covering_judgement():
    stream = FeedbackTicketStream("test")
    for token, actual in ((1, 1.), (2, 1.04), (3, 1.12)):
        row = receipt(token, actual)
        stream.publish_receipts([row], published_until_s=2.)
        stream.observe_commit([row], actual, 1, .5)
    history = stream.snapshot().judgement_history
    assert history[1][2] == 1.04
    assert history[2][2] == 1.12


@pytest.mark.parametrize("kind", ["down", "flick", "up"])
def test_mixed_commit_cannot_supply_homogeneous_tap_group_ticket(kind):
    stream = FeedbackTicketStream("test")
    rows = [receipt(1, 1.), receipt(2, 1., kind=kind)]
    stream.publish_receipts(rows, published_until_s=2.)
    stream.observe_commit(rows, 1., 1, .5)
    assert stream.snapshot().ticket is None


def test_stall_safe_prefetch_reports_old_inflight_without_relabelling_it(monkeypatch):
    clock = [1.]
    monkeypatch.setattr("agent.realtime.profile_play_action.time.perf_counter", lambda: clock[0])
    jobs = []
    def post():
        job = SimpleNamespace(done=False, job_id=len(jobs), get=lambda: object())
        jobs.append(job)
        return job
    capture = StallSafeCapture(SimpleNamespace(post_screencap=post))
    capture.enable_fresh_prefetch()
    capture._last_image = object()
    assert capture.prefetch_fresh(1.) is True
    clock[0] = 1.04
    assert capture.prefetch_fresh(1.04) is False
    assert len(jobs) == 1
    assert capture._pending_metadata["request_s"] == 1.
    jobs[0].done = True
    assert capture.prefetch_fresh(1.04) is True
    assert len(jobs) == 2
    assert capture.frame_metadata["request_s"] == 1.


def test_engine_does_not_acknowledge_declined_prefetch(monkeypatch):
    from test_native_life_feedback import _virtual_feedback_engine
    acknowledgements = []
    original = NativeLifeFeedback.capture_requested
    def acknowledged(self, now):
        acknowledgements.append(now)
        return original(self, now)
    monkeypatch.setattr(NativeLifeFeedback, "capture_requested", acknowledged)
    _virtual_feedback_engine(monkeypatch, capture_fault="declined")
    assert acknowledgements == []


@pytest.mark.parametrize("problem", ["history-gap", "untrusted-clock", "expired", "old-image", "future-upper"])
def test_delayed_frame_recovery_keeps_attribution_and_freshness_guards(problem):
    sampler, backend, _ = sampler_fixture()
    stream = FeedbackTicketStream("test")
    backend.feedback_observation_snapshot = stream.snapshot
    row = receipt(1, 1.)
    stream.publish_receipts([row], published_until_s=2.)
    stream.observe_commit([row], 1., 1, .5)
    sampler.capture_due(1.026)
    sampler.capture_requested(1.026)
    for token, actual in ((2, 1.25 if problem == "expired" else 1.10),
                          (3, 1.26 if problem == "expired" else 1.12)):
        row = receipt(token, actual, eligible=False)
        stream.publish_receipts([row], published_until_s=2.)
        stream.observe_commit([row], actual, 2, None if problem == "untrusted-clock" else .5)
    state = stream.snapshot()
    if problem == "history-gap":
        state = replace(state, judgement_history=state.judgement_history[2:])
    backend.feedback_observation_snapshot = lambda: state
    now = 1.27 if problem == "expired" else 1.13
    sampler.capture_due(now)
    metadata = dict(sequence=1, reused=False,
        request_s=1.01 if problem == "old-image" else 1.21 if problem == "expired" else 1.06,
        first_done_observed_s=now + .01 if problem == "future-upper" else 1.22 if problem == "expired" else 1.08)
    fresh = sampler.fresh(metadata, now)
    if fresh:
        sampler.observe_frame(_feedback_image(TimingFeedback.FAST), now)
    assert not sampler.votes and backend._phase_slot is None


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_production_compiler_homogeneous_chord_binds_one_commit_ticket(tmp_path):
    chart = tmp_path / "chord.json"
    chart.write_text(json.dumps([{"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 2, "lane": 2},
        {"type": "Single", "beat": 2, "lane": 3}]), encoding="utf-8")
    sent = []
    backend = NativeMinitouchBackend(chart, adb_path="unused", serial="unused",
        device=SimpleNamespace(request_reset=lambda: True, publish=sent.append), life_feedback_enabled=True)
    backend._first_action_anchor_s = 100.
    assert backend._publish_chunk(dict(sequence=1, window_start_s=100., window_end_s=102.,
        actions=[dict(action=row, engine_due_s=101.) for row in backend._actions], final_chunk=True))
    commands = [row for row in backend._expected_commands if row.receipts]
    assert len(commands) == 1 and len(commands[0].receipts) == 2
    assert all(row["feedback_single_tap"] for row in commands[0].receipts)
    backend._feedback_tickets.observe_commit(commands[0].receipts, 101.001, 1, .5)
    state = backend.feedback_observation_snapshot()
    assert state.ticket.action_tokens == (1, 2)
    assert backend._feedback_tickets.summary["ticket-created"] == 1
