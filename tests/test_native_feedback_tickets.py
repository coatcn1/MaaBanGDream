from dataclasses import FrozenInstanceError
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from agent.realtime.native_feedback_ticket import FeedbackTicketStream, SingleTapEligibility
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativeMinitouchBackend
from agent.realtime import native_engine
from agent.realtime.timing_feedback import TimingFeedback
from test_native_feedback_events import frame, sampler_fixture, two_events
from test_native_life_feedback import _phase_backend


def action(kind="tap", due=1.0, lane=2, contact=-1, note=1):
    return dict(kind=kind, due_s=due, lane=lane, contact=contact, note_index=note,
                target_x=510, flick_direction=None)


@pytest.mark.parametrize("interference", ["chord", "hold", "move", "unclosed", "flick", "tail"])
def test_single_tap_prefilter_rejects_interference(interference):
    target = action()
    extra = {
        "chord": [action(lane=3, note=2)],
        "hold": [action("down", .5, contact=0), action("up", 1.5, contact=0)],
        "move": [action("down", .5, contact=0), action("move", .8, contact=0), action("up", 1.5, contact=0)],
        "unclosed": [action("down", .5, contact=0)],
        "flick": [action("flick", 1.1)],
        "tail": [action("up", .9, contact=0)],
    }[interference]
    assert not SingleTapEligibility([target, *extra]).eligible(target)


def test_executed_tickets_are_sparse_immutable_and_overwritten_by_any_judgement():
    stream = FeedbackTicketStream("run")
    for index in range(9):
        receipt = dict(action_token=100 + index, planned_engine_s=1 + index,
                       action_kind="tap", command="d", feedback_single_tap=True)
        stream.publish_receipts([receipt])
        stream.observe_commit([receipt], 1 + index, index, .5)
        state = stream.snapshot()
        assert (state.ticket is not None) is (index in {0, 8})
    with pytest.raises(FrozenInstanceError):
        state.ticket.action_token = 3
    other = dict(action_token=999, planned_engine_s=10.1, action_kind="down", command="d")
    stream.publish_receipts([other])
    stream.observe_commit([other], 10.1, 10, .5)
    assert stream.snapshot().ticket is None


def test_hold_flick_tail_closes_contact_for_later_single_taps():
    target = action(due=2)
    assert SingleTapEligibility([action("down", .5, contact=0),
        action("flick", 1, contact=0), target]).eligible(target)


@pytest.mark.parametrize("reason", ["foreign", "covered", "overwritten", "expired", "before-render"])
def test_target_frame_attribution_rejects_unproven_samples(reason):
    sampler, backend, _ = sampler_fixture()
    frame(sampler, None, 0)
    frame(sampler, TimingFeedback.FAST, .1)
    assert sampler.summary["direction_votes"] == 1
    # 独立建立第二张目标，先绑定，再模拟截图期间的并发覆盖。
    frame(sampler, None, .2)
    state = backend._test_ticket_state
    ticket = replace(state.ticket, action_token=999, actual_s=.25, planned_s=.25, judgement_sequence=state.judgement_sequence + 1)
    if reason == "foreign":
        ticket = replace(ticket, run_id="other")
    backend._test_ticket_state = replace(state, ticket=ticket, judgement_sequence=ticket.judgement_sequence,
                                        last_actual_s=.25)
    sampler.capture_due(.26)
    if reason == "covered":
        backend._test_ticket_state = replace(backend._test_ticket_state, next_judgement_lower_s=.27)
    if reason == "overwritten":
        backend._test_ticket_state = replace(backend._test_ticket_state, judgement_sequence=99)
    now = .46 if reason == "expired" else .28
    request = .26 if reason == "before-render" else now - .001
    sampler.fresh(dict(sequence=100, request_s=request, reused=False), now)
    sampler.observe_frame(__import__("test_native_life_feedback")._feedback_image(TimingFeedback.FAST), now)
    assert sampler.summary["direction_votes"] == 1
    assert backend._phase_slot is None
    assert sampler.ignored_reasons


def test_continuous_same_colour_on_new_tickets_has_one_bounded_request():
    sampler, backend, _ = sampler_fixture()
    for index in range(60):
        # 新执行 token 只证明按键后观测，不能把持续字样计成独立视觉事件。
        backend._test_direction = None
        frame(sampler, TimingFeedback.FAST, index * .05)
    assert sampler.summary["direction_votes"] == 3
    assert sampler.summary["requests"] == 1
    assert sampler.summary["single_tap_observations"] == 60
    assert sampler.summary["distinct_events"] == 0


def test_visual_owner_applied_phase_cannot_cash_in_two_old_votes():
    sampler, backend, _ = sampler_fixture()
    for kind, now in ((None, 0), (TimingFeedback.FAST, .1), (None, .2), (TimingFeedback.FAST, .3)):
        frame(sampler, kind, now)
    assert len(sampler.votes) == 2
    backend.set_visual_phase_guard(True)
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), request_sequence=99,
        owner="game-clock", state="applied", cumulative_phase_ms=15, first_adjusted_due_s=.4,
        first_affected_chunk_sequence=1, first_affected_action_token=1), "effect-published")
    sampler.refresh_phase_status(.31)
    assert not sampler.votes
    assert sampler._effect_status.owner == "game-clock"
    backend.set_visual_phase_guard(False)
    frame(sampler, None, .4)
    frame(sampler, TimingFeedback.FAST, .5)
    assert sampler.summary["requests"] == 0


def test_hidden_guard_and_applied_then_expired_epoch_clears_old_votes_and_can_recover():
    sampler, backend, _ = sampler_fixture()
    for kind, now in ((None, 0), (TimingFeedback.FAST, .1), (None, .2), (TimingFeedback.FAST, .3)):
        frame(sampler, kind, now)
    assert len(sampler.votes) == 2
    backend.set_visual_phase_guard(True)
    applied = replace(backend.phase_status_snapshot(), request_sequence=99, owner="game-clock",
        state="applied", cumulative_phase_ms=15, first_adjusted_due_s=.4,
        first_affected_chunk_sequence=2, first_affected_action_token=1)
    backend._publish_phase_status(applied, "effect-published")
    backend._publish_phase_status(replace(applied, request_sequence=100, state="expired",
        terminal_reason="expired"), "expired")
    backend.set_visual_phase_guard(False)
    sampler.refresh_phase_status(.31)
    assert not sampler.votes and sampler._blank_baseline is None
    assert sampler._effect_status.request_sequence == 99
    frame(sampler, None, .4, chunk=1)
    frame(sampler, TimingFeedback.FAST, .5, chunk=1)
    assert sampler.summary["requests"] == 0
    # 新相位确实执行后的两空白，再接三个新目标，允许恢复而非永久封锁。
    frame(sampler, None, .6, chunk=2)
    frame(sampler, None, .65, chunk=2)
    for kind, now in ((TimingFeedback.FAST, .72), (None, .8), (TimingFeedback.FAST, .9),
                      (None, 1), (TimingFeedback.FAST, 1.1)):
        frame(sampler, kind, now, chunk=2)
    assert sampler.summary["requests"] == 1
    assert backend.phase_status_snapshot().owner == "feedback"


def test_hidden_guard_pulse_without_phase_change_still_invalidates_old_votes():
    sampler, backend, _ = sampler_fixture()
    for kind, now in ((None, 0), (TimingFeedback.FAST, .1), (None, .2), (TimingFeedback.FAST, .3)):
        frame(sampler, kind, now)
    backend.set_visual_phase_guard(True)
    backend.set_visual_phase_guard(False)
    sampler.refresh_phase_status(.31)
    assert not sampler.votes


def test_immediate_apply_before_requested_snapshot_is_counted_once(monkeypatch):
    sampler, backend, _ = sampler_fixture()
    request = backend.request_future_phase
    def immediate_apply(delta, observed_s):
        accepted = request(delta, observed_s)
        assert accepted
        backend._phase_slot = None
        backend._publish_phase_status(replace(backend.phase_status_snapshot(), state="applied",
            cumulative_phase_ms=15, first_adjusted_due_s=.4,
            first_affected_chunk_sequence=2, first_affected_action_token=1), "applied-before-return")
        return accepted
    monkeypatch.setattr(backend, "request_future_phase", immediate_apply)
    two_events(sampler)
    assert sampler.summary["accepted_requests"] == sampler.summary["applied_requests"] == 1
    assert list(sampler._owned_sequences) == [1]
    assert sampler._pending_observed_s is None
    sampler.refresh_phase_status(.3)
    sampler.refresh_phase_status(.31)
    assert sampler.summary["applied_requests"] == 1


def test_visual_guard_alone_clears_votes_and_never_revokes_other_owner_request():
    sampler, backend, _ = sampler_fixture()
    for kind, now in ((None, 0), (TimingFeedback.FAST, .1), (None, .2), (TimingFeedback.FAST, .3)):
        frame(sampler, kind, now)
    backend.set_visual_phase_guard(True)
    assert backend.request_future_phase(15, .31, owner="game-clock")
    sampler.capture_due(.32)
    assert not sampler.votes and sampler._blank_baseline is None
    assert backend._phase_slot is not None
    assert backend.phase_status_snapshot().owner == "game-clock"


@pytest.mark.parametrize("effect", ["unpublished", "not-executed", "republished"])
def test_any_owner_effect_epoch_blocks_unaffected_ticket_votes(effect):
    sampler, backend, _ = sampler_fixture()
    for kind, now in ((None, 0), (TimingFeedback.FAST, .1), (None, .2), (TimingFeedback.FAST, .3)):
        frame(sampler, kind, now)
    status = replace(backend.phase_status_snapshot(), request_sequence=99, owner="game-clock",
        state="applied", cumulative_phase_ms=15, first_adjusted_due_s=None if effect == "unpublished" else .6,
        first_affected_chunk_sequence=2, first_affected_action_token=1)
    backend._publish_phase_status(status, "applied")
    sampler.refresh_phase_status(.31)
    frame(sampler, None, .4, chunk=2)
    frame(sampler, None, .45, chunk=2)
    frame(sampler, TimingFeedback.FAST, .5, chunk=2)
    assert not sampler.votes
    if effect == "republished":
        backend._publish_phase_status(replace(status, first_adjusted_due_s=.9), "effect-published")
        sampler._blank_baseline = (1, .8)
        sampler.refresh_phase_status(.8)
        assert sampler._blank_baseline is None and sampler._target is None


@pytest.mark.parametrize("stage", ["requested", "touch", "receipts", "gap", "locked", "before-apply"])
def test_visual_guard_preserves_accepted_feedback_slot_owner_and_original_ttl(stage):
    backend, calls = _phase_backend()
    assert backend.request_future_phase(15, .95)
    original = backend._phase_slot
    receipts = tuple(backend._phase_receipts)
    if stage == "touch":
        backend._compiler.phase_boundary_safe = lambda: False
        backend._consume_phase_candidate()
        backend._compiler.phase_boundary_safe = lambda: True
    elif stage == "receipts":
        backend._phase_receipts.clear()
        backend._consume_phase_candidate()
    elif stage == "gap":
        backend._session.apply_future_phase = lambda *args: "unsafe-gap"
        backend._consume_phase_candidate()
        backend._session.apply_future_phase = lambda *args: calls.append(args) or "applied"
    if stage == "locked":
        backend._phase_lock.acquire()
        backend.set_visual_phase_guard(True)
        backend._phase_lock.release()
    elif stage == "before-apply":
        backend._compiler.contact_available_s = lambda: backend.set_visual_phase_guard(True) or .5
    else:
        backend.set_visual_phase_guard(True)
    assert backend._phase_slot == original
    assert not backend.request_future_phase(15, 1, owner="game-clock")
    assert backend._phase_slot == original and backend._phase_sequence == 1
    if stage == "receipts":
        backend._phase_receipts = list(receipts)
    backend._consume_phase_candidate()
    assert calls == [(15, .5)] and backend._phase_slot is None
    assert backend._visual_phase_guard
    assert backend.phase_status_snapshot().state == "applied"
    assert backend.phase_owner_cumulative("feedback") == 15
    assert backend.phase_owner_cumulative("game-clock") == 0
    backend._consume_phase_candidate()
    assert calls == [(15, .5)]


def test_apply_first_preserves_real_effect_then_linearizes_guard():
    backend, calls = _phase_backend()
    assert backend.request_future_phase(15, .95)
    def apply(delta, boundary):
        calls.append((delta, boundary))
        backend.set_visual_phase_guard(True)
        assert backend._visual_phase_guard_requested
        assert not backend._visual_phase_guard
        return "applied"
    backend._session.apply_future_phase = apply
    backend._consume_phase_candidate()
    assert calls == [(15, .5)]
    assert backend.phase_status_snapshot().state == "applied"
    assert backend._visual_phase_guard and backend.phase_owner_cumulative("feedback") == 15
    assert not backend.request_future_phase(15, 1)


def test_guard_observer_clears_votes_without_revoking_accepted_slot_then_needs_new_effect_sources():
    sampler, backend, calls = sampler_fixture()
    two_events(sampler)
    original = backend._phase_slot
    assert original is not None and sampler.summary["accepted_requests"] == 1
    backend.set_visual_phase_guard(True)
    sampler.capture_due(.3)
    assert backend._phase_slot == original
    assert not sampler.votes and sampler._blank_baseline is None
    assert not backend.request_future_phase(15, .3, owner="game-clock")
    assert backend._phase_slot == original
    backend._consume_phase_candidate()
    assert calls == [(15, .5)]
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, first_adjusted_due_s=.6,
        first_affected_chunk_sequence=2, first_affected_action_token=1), "effect-published")
    backend.set_visual_phase_guard(False)
    sampler.refresh_phase_status(.31)
    assert sampler.summary["applied_requests"] == 1
    # 已应用的旧请求不作废，但新趋势必须等本次相位影响到实际执行输入。
    for kind, now in ((None, .4), (None, .45), (TimingFeedback.FAST, .5)):
        frame(sampler, kind, now, chunk=1)
    assert not sampler.votes and sampler.summary["requests"] == 1
    for kind, now in ((None, .7), (None, .75), (TimingFeedback.FAST, .82),
                      (None, .9), (TimingFeedback.FAST, 1), (None, 1.1), (TimingFeedback.FAST, 1.2)):
        frame(sampler, kind, now, chunk=2)
    assert sampler.summary["requests"] == 2


def test_guard_claim_does_not_extend_expiring_accepted_feedback_slot():
    sampler, backend, calls = sampler_fixture()
    two_events(sampler)
    original = backend._phase_slot
    backend.set_visual_phase_guard(True)
    sampler.capture_due(.3)
    assert backend._phase_slot == original
    backend._clock = lambda: original[-1] + .001
    backend._consume_phase_candidate()
    assert not calls and backend._phase_slot is None
    assert backend.phase_status_snapshot().state == "expired"


def test_rejected_target_keeps_existing_roi_and_ticket_for_review():
    sampler, backend, _ = sampler_fixture()
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), state="applied",
        request_sequence=9, cumulative_phase_ms=15, first_adjusted_due_s=1,
        first_affected_chunk_sequence=2), "effect-published")
    sampler.refresh_phase_status(0)
    frame(sampler, TimingFeedback.FAST, .1)
    assert sampler.last_sniff["decision"] == "waiting-effect-execution"
    assert sampler.last_sniff["input"]["action_token"] is not None
    assert sampler.roi_worthy


def test_ticket_report_exception_is_isolated_from_native_report(tmp_path, monkeypatch):
    if not native_engine.available():
        pytest.skip("Native 未构建")
    chart = tmp_path / "single.json"
    chart.write_text(json.dumps([{"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 2, "lane": 2}]), encoding="utf-8")
    backend = NativeMinitouchBackend(chart, adb_path="unused", serial="unused",
        device=SimpleNamespace(request_reset=lambda: True), life_feedback_enabled=True)
    before = backend.report()
    def fail():
        raise OSError("ticket report unavailable")
    monkeypatch.setattr(backend._feedback_tickets, "report", fail)
    after = backend.report()
    assert after["state"] == before["state"]
    assert after["planned"] == before["planned"]
    assert after["release_confirmed"] == before["release_confirmed"]
    assert "ticket report unavailable" in after["life_feedback"]["tickets"]["report_error"]


@pytest.mark.parametrize("fault", [None, "eligibility", "publish_receipts", "observe_commit"])
@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_real_compiler_receipt_binds_ticket_and_observer_faults_preserve_input(tmp_path, monkeypatch, fault):
    chart = tmp_path / "single.json"
    chart.write_text(json.dumps([{"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 2, "lane": 2}, {"type": "Single", "beat": 4, "lane": 3}]), encoding="utf-8")
    sent = []
    device = SimpleNamespace(request_reset=lambda: True, publish=sent.append)
    backend = NativeMinitouchBackend(chart, adb_path="unused", serial="unused", device=device,
                                     life_feedback_enabled=True, clock=lambda: 102.)
    def fail(*args, **kwargs):
        raise RuntimeError("injected observer fault")
    if fault == "eligibility":
        monkeypatch.setattr(backend._feedback_single_taps, "eligible", fail)
    elif fault:
        monkeypatch.setattr(backend._feedback_tickets, fault, fail)
    backend._first_action_anchor_s = 100.
    for index, offset in enumerate((100., 100.015)):
        original = backend._actions[index]
        due = original["due_s"] + offset
        assert backend._publish_chunk(dict(sequence=index + 1, window_start_s=due - .3,
            window_end_s=due + .2, actions=[dict(action=original, engine_due_s=due)], final_chunk=False))
    receipts = [row for command in backend._expected_commands for row in command.receipts]
    assert len(receipts) == 2 and len(sent) == 2
    if fault is None:
        assert all(row["feedback_single_tap"] for row in receipts)
        assert receipts[1]["planned_engine_s"] == pytest.approx(102.015)
    records = list(backend._expected_commands)
    device.logs_since = lambda cursor: (len(records), ["jlog " + str(index) for index in range(len(records))])
    monkeypatch.setattr(native_engine, "parse_minitouch_log", lambda line: dict(
        command=records[int(line[5:])].command, start_ms=101000 + int(line[5:]),
        end_ms=101000 + int(line[5:]) + .1, cost_ms=.1))
    accepted = []
    backend._session = SimpleNamespace(observe_minitouch_log=lambda event: None,
        observe_execution=lambda *args: accepted.append(args) or True, report=lambda: {"state": "running"})
    backend._playback_observation_started = True
    backend._device_clock_offset_s = 0.
    backend._clock_basis = "probe-midpoint"
    backend._clock_uncertainty_ms = .5
    backend._complete_observed_chunk = lambda command: None
    backend._observe_new_logs()
    assert len(accepted) == 2 and backend._publish_error is None
    if fault:
        assert fault.replace("_", "-") in backend._feedback_ticket_disabled_reason
        assert backend.feedback_observation_snapshot() is None
    else:
        assert backend._feedback_tickets.summary["ticket-created"] == 1
        assert backend._feedback_tickets.summary["stride-skipped"] == 1
