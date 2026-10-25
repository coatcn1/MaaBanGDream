from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativeFeedbackInput, NativePhaseStatus
from agent.realtime.timing_feedback import TimingFeedback
from agent.realtime.native_feedback_ticket import FeedbackObservationState, FeedbackObservationTicket
from test_native_life_feedback import _phase_backend, _feedback_image


def sampler_fixture():
    backend, applied = _phase_backend()
    backend._feedback_input_snapshot = (-.03, -10)
    backend._feedback_input_history = ()
    sampler = NativeLifeFeedback(backend)
    backend._test_ticket_state = FeedbackObservationState()
    backend.feedback_observation_snapshot = lambda: backend._test_ticket_state
    backend._test_direction = None
    backend._test_tokens = set()
    sampler.observe_scene(0, visible=True)
    return sampler, backend, applied


def frame(sampler, direction, now, *, eligible=True, request=None, token=None, chunk=1, actual=None):
    backend = sampler.backend
    request = now - .01 if request is None else request
    token = sampler.last_sequence + 2 if token is None else token
    source = SimpleNamespace(run_id="test", planned_s=request - .02,
                             actual_s=now - .04 if actual is None else actual, action_token=token,
                             chunk_sequence=chunk, clock_uncertainty_ms=.5)
    backend._feedback_input_history = (*backend._feedback_input_history, source)[-64:]
    backend._feedback_input_snapshot = (source.actual_s, -10 if eligible else now)
    if direction is not None and direction != backend._test_direction:
        state = backend._test_ticket_state
        sequence = state.judgement_sequence + 1
        ticket = (FeedbackObservationTicket("test", token, chunk, source.planned_s, source.actual_s, .5, sequence)
                  if eligible and token not in backend._test_tokens else None)
        backend._test_ticket_state = FeedbackObservationState(ticket, sequence, source.actual_s, .5)
        backend._test_tokens.add(token)
    backend._test_direction = direction
    sampler.capture_due(now)
    assert sampler.fresh({"sequence": sampler.last_sequence + 1,
                          "request_s": request, "reused": False}, now)
    sampler.observe_scene(now, visible=True)
    image = _feedback_image(direction)
    if direction == "mixed":
        image[514:556, 570:630] = (255, 180, 0)
        image[514:556, 630:660] = (0, 140, 255)
    sampler.observe_frame(image, now)


def two_events(sampler, direction=TimingFeedback.FAST, start=0):
    # 沿用旧帮助函数名称以限制差异，实际已改为三个独立目标按键。
    frame(sampler, None, start - .05)
    frame(sampler, direction, start)
    frame(sampler, None, start + .05)
    frame(sampler, direction, start + .10)
    frame(sampler, None, start + .15)
    frame(sampler, direction, start + .20)
    frame(sampler, direction, start + .25)


@pytest.mark.parametrize("direction,delta", [(TimingFeedback.FAST, 15), (TimingFeedback.SLOW, -15)])
def test_three_single_tap_tickets_request_production_step(direction, delta):
    sampler, backend, _ = sampler_fixture()
    two_events(sampler, direction)
    assert backend._phase_slot is not None
    assert backend._phase_slot[3] == delta
    assert sampler.summary["single_tap_observations"] == 3
    assert sampler.summary["distinct_events"] == 0
    assert sampler.summary["eligible_events"] == 3


def test_one_sustained_glyph_with_new_down_tokens_never_requests():
    sampler, backend, _ = sampler_fixture()
    for index in range(60):
        frame(sampler, TimingFeedback.FAST, index * .05)
    assert backend._phase_slot is None
    assert sampler.summary["distinct_events"] == 0


@pytest.mark.parametrize("reason", ["reverse", "mixed", "scene", "reused", "terminal", "stop"])
def test_invalid_visual_evidence_revokes_pending_even_without_input_eligibility(reason):
    sampler, backend, applied = sampler_fixture()
    two_events(sampler)
    backend._compiler.phase_boundary_safe = lambda: False
    backend._clock = lambda: .3
    backend._consume_phase_candidate()
    if reason in {"reverse", "mixed"}:
        frame(sampler, TimingFeedback.SLOW if reason == "reverse" else "mixed", .3, eligible=False)
    elif reason == "scene":
        sampler.observe_scene(.3, visible=False)
    elif reason == "reused":
        assert not sampler.fresh({"sequence": 100, "request_s": .29, "reused": True}, .3)
    else:
        sampler.close(.3, reason)
    backend._compiler.phase_boundary_safe = lambda: True
    backend._consume_phase_candidate()
    assert backend._phase_slot is None
    assert applied == []


def test_scene_recovery_cannot_reuse_old_votes_or_visible_glyph():
    sampler, backend, _ = sampler_fixture()
    frame(sampler, TimingFeedback.FAST, 0)
    frame(sampler, TimingFeedback.FAST, .05)
    sampler.observe_scene(.1, visible=False)
    for now in (.2, .25, .3):
        frame(sampler, TimingFeedback.FAST, now)
    assert backend._phase_slot is None
    frame(sampler, None, .35)
    frame(sampler, None, .37)
    frame(sampler, TimingFeedback.FAST, .4)
    frame(sampler, TimingFeedback.FAST, .45)
    assert backend._phase_slot is None


def test_post_effect_needs_blank_after_affected_down_and_new_onset():
    sampler, backend, _ = sampler_fixture()
    two_events(sampler)
    backend._phase_slot = None
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, state="applied", cumulative_phase_ms=15,
        first_adjusted_due_s=.3, first_affected_chunk_sequence=2,
        first_affected_action_token=5), "effect-published")
    sampler.refresh_phase_status(.3)
    for now in (.4, .45, .5):
        frame(sampler, TimingFeedback.FAST, now, chunk=2)
    assert sampler.summary["requests"] == 1
    frame(sampler, None, .55, chunk=2)
    two_events(sampler, start=.6)
    assert sampler.summary["requests"] == 1


def test_duplicate_input_token_cannot_qualify_two_events():
    sampler, backend, _ = sampler_fixture()
    for now, direction in ((0, TimingFeedback.FAST), (.05, TimingFeedback.FAST),
                           (.1, None), (.2, TimingFeedback.FAST), (.25, TimingFeedback.FAST)):
        frame(sampler, direction, now, token=9)
    assert backend._phase_slot is None


def test_two_tickets_after_one_blank_cannot_reach_three_votes():
    sampler, backend, _ = sampler_fixture()
    for now, kind in ((0, TimingFeedback.FAST), (.05, TimingFeedback.FAST),
                      (.1, None), (.15, TimingFeedback.FAST), (.2, TimingFeedback.FAST)):
        frame(sampler, kind, now)
    assert sampler.summary["single_tap_observations"] == 2
    assert sampler.summary["distinct_events"] == 0
    assert backend._phase_slot is None


def test_native_entry_still_rejects_alpha_channel_input():
    sampler, backend, _ = sampler_fixture()
    assert sampler.fresh({"sequence": 1, "request_s": .01, "reused": False}, .02)
    sampler.observe_frame(np.zeros((720, 1280, 4), dtype=np.uint8), .02)
    assert not sampler.scene_valid
    assert backend._phase_slot is None
    assert sampler.ignored_reasons["sampling-error"] == 1


def test_numeric_sniff_has_explicit_capacity_and_dropped_without_file_io():
    sampler, _, _ = sampler_fixture()
    sampler.SNIFF_CAPACITY = 4
    from collections import deque
    sampler.sniff_records = deque(maxlen=4)
    for index in range(6):
        frame(sampler, TimingFeedback.FAST, index * .05)
    report = sampler.sniff_report()
    assert report["capacity"] == 4 and report["dropped"] == 2
    assert report["raw_roi_included"] is False
    assert len(report["records"]) == 4
    assert all("input" in row and "frame_sequence" in row and "fast_px" in row for row in report["records"])


@pytest.mark.parametrize("problem", ["future", "missing-unc", "high-unc", "foreign", "old", "relative-clock"])
def test_causal_input_snapshot_never_falls_back_to_future_or_untrusted_latest(problem):
    _, backend, _ = sampler_fixture()
    source = NativeFeedbackInput("test", .9, .9, 1, 1, .5)
    source = replace(source, actual_s=.96) if problem == "future" else source
    source = replace(source, clock_uncertainty_ms=None) if problem == "missing-unc" else source
    source = replace(source, clock_uncertainty_ms=1.001) if problem == "high-unc" else source
    source = replace(source, run_id="old") if problem == "foreign" else source
    source = replace(source, actual_s=.399) if problem == "old" else source
    if problem == "relative-clock":
        backend._clock_basis = "first-action-relative"
    backend._feedback_input_snapshot = (.98, -10)
    backend._feedback_transient_snapshot = (.98, .98, 99, 99)
    backend._feedback_input_history = (source,)
    assert backend.feedback_input_snapshot(.95, 1) is None


def test_input_history_selects_previous_causal_receipt_and_is_immutable_without_lock():
    _, backend, _ = sampler_fixture()
    past = NativeFeedbackInput("test", .90, .90, 1, 1, 1)
    future = NativeFeedbackInput("test", .98, .98, 2, 2, .5)
    backend._feedback_input_snapshot = (.98, -10)
    backend._feedback_input_history = (past, future)
    backend._phase_lock.acquire()
    try:
        assert backend.feedback_input_snapshot(past.actual_s + .001, 1) is past
        assert backend.feedback_input_snapshot(past.actual_s + .001 - 1e-6, 1) is None
        with pytest.raises(FrozenInstanceError):
            past.actual_s = .95
    finally:
        backend._phase_lock.release()


@pytest.mark.parametrize("offset,accepted", [(0, True), (-1e-6, False)])
def test_input_age_includes_uncertainty_lower_bound(offset, accepted):
    _, backend, _ = sampler_fixture()
    source = NativeFeedbackInput("test", .401, .401 + offset, 1, 1, 1)
    backend._feedback_input_snapshot = (.401, -10)
    backend._feedback_input_history = (source,)
    assert (backend.feedback_input_snapshot(.99, 1) is not None) is accepted


@pytest.mark.parametrize("problem,accepted", [("boundary", True), ("old-start", False),
    ("old-tail", False), ("high-unc", False), ("missing-unc", False),
    ("large-abs", False), ("duplicate", False), ("future", False)])
def test_receipt_window_keeps_precise_1500ms_500ms_1ms_and_40ms_guards(problem, accepted):
    _, backend, applied = sampler_fixture()
    backend._clock = lambda: 2.0
    backend._clock_uncertainty_ms = 1.0
    backend._phase_receipts.clear()
    actuals = [.501, 1.0, 1.501]
    if problem == "old-start":
        actuals[0] -= 1e-6
    if problem == "old-tail":
        actuals[2] -= 1e-6
    if problem == "future":
        actuals[2] = 2.0
    uncertainty = 1.001 if problem == "high-unc" else None if problem == "missing-unc" else 1.0
    error = 40.001 if problem == "large-abs" else 40.0
    for index, actual in enumerate(actuals):
        backend._phase_receipts.append((0 if problem == "duplicate" else index,
            actual, error, -error, actual + error / 1000.0, uncertainty))
    assert backend.request_future_phase(15, 1.95)
    backend._consume_phase_candidate()
    assert bool(applied) is accepted
    diagnostic = backend._phase_receipt_diagnostics[-1]
    if problem == "boundary":
        assert diagnostic["unique_commits"] == 3
        assert diagnostic["tail_age_s"] == .5
        assert diagnostic["abs_execution_residual_median_ms"] == 40
        assert diagnostic["signed_execution_residual_median_ms"] == -40


@pytest.mark.parametrize("gap", [1.0, 1.2, 4.1])
def test_vote_count_is_by_target_keys_without_time_window(gap):
    sampler, backend, _ = sampler_fixture()
    for index in range(3):
        at = index * gap
        for tick in range(int(gap / .2)):
            frame(sampler, None, at - gap + tick * .2)
        frame(sampler, None, at - .05)
        frame(sampler, TimingFeedback.FAST, at)
    assert backend._phase_slot is not None


def test_real_owner_publishes_only_executed_down_history_and_unique_commit_residuals(monkeypatch):
    from collections import deque
    from agent.realtime import native_engine, native_play
    _, backend, _ = sampler_fixture()
    backend._log_cursor = 0
    backend._playback_observation_started = True
    backend._observation_cancelled = False
    backend._last_device_start_ms = backend._last_device_end_ms = None
    backend._observed_commands = 0
    backend._device_clock_offset_s = 0
    backend._require_probe = True
    backend._observed_action_tokens = set()
    backend._first_action_anchor_s = 1
    backend._execution_timing = native_play._ExecutionTimingTrace()
    backend._drift_rate_estimator = None
    backend._final_chunk_published = False
    backend._phase_receipts = deque(maxlen=64)
    backend._phase_receipts_dropped = backend._feedback_input_history_dropped = 0
    backend._calibrator = SimpleNamespace(observe=lambda event: None)
    backend._session.observe_minitouch_log = lambda event: None
    backend._session.observe_execution = lambda *args: True
    backend._session.report = lambda: {"state": "running"}
    backend._device = SimpleNamespace(logs_since=lambda cursor: (70, [f"jlog {index}" for index in range(70)]))
    def parse(line):
        index = int(line.split()[-1])
        return {"start_ms": 1000 + index, "end_ms": 1000 + index, "cost_ms": 0, "command": "c"}
    monkeypatch.setattr(native_engine, "parse_minitouch_log", parse)
    commands = []
    for index in range(70):
        actual = 1 + index / 1000
        receipts = tuple(dict(planned_engine_s=actual + error, action_token=index * 2 + column,
                              action_kind="tap", command="d") for column, error in enumerate((-.002, .004)))
        commands.append(native_play._ExpectedCommand("c", 1, receipts=receipts))
    backend._expected_commands = deque(commands)
    backend._observe_new_logs()
    assert len(backend._feedback_input_history) == 64
    assert backend._feedback_input_history_dropped == 76
    assert backend._feedback_input_history[-1].action_token == 139
    assert len(backend._phase_receipts) == 64 and backend._phase_receipts_dropped == 6
    assert backend._phase_receipts[-1][2:4] == pytest.approx((3, -1))
    assert len(backend._observed_action_tokens) == 140


@pytest.mark.parametrize("direction", [1, -1])
def test_rebuilt_native_binding_enforces_production_step_cap_and_stop(direction):
    from agent.realtime import native_engine
    if not native_engine.available():
        pytest.skip("Native 未构建")
    session = native_engine.playback_session(publish=lambda chunk: True,
        request_reset=lambda: True, clock=lambda: 0)
    actions = [dict(kind="tap", lane=2, contact=0, target_x=510,
                    due_s=due, flick_direction=None) for due in (0, 5)]
    assert session.arm(actions)
    assert session.start(0) and session.publish()
    for _ in range(13):
        assert session.apply_future_phase(direction * 15, 0) == "applied"
    assert session.future_phase_offset_ms == pytest.approx(direction * 195)
    assert session.apply_future_phase(direction * 15, 0) == "phase-limit"
    assert session.apply_future_phase(-direction * 15, 0) == "applied"
    assert session.apply_future_phase(direction * 15, 0) == "applied"
    assert session.cancel("stop")
    assert session.apply_future_phase(15, 0) == "not-running"
