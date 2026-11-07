from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from agent.realtime.native_feedback_ticket import FeedbackTicketStream
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.native_play import NativeMinitouchBackend, NativePhaseStatus
from agent.realtime.timing_feedback import TimingFeedback
from agent.realtime import native_engine
from test_native_life_feedback import _feedback_image
from test_native_random_great import ENABLED, write_chart


def fixture():
    stream = FeedbackTicketStream("simple")
    requests = []
    state = [NativePhaseStatus("simple", 0)]
    backend = SimpleNamespace(_simple_single_tap_feedback=True, _run_id="simple",
        _native_input_stop_requested=threading.Event(), _random_great_feedback_enabled=True,
        _random_great_feedback_disabled_reason=None,
        feedback_observation_snapshot=stream.snapshot, phase_status_snapshot=lambda: state[0],
        phase_effect_snapshot=lambda: state[0] if state[0].state == "applied" else None,
        revoke_future_phase=lambda *a, **k: None,
        request_future_phase=lambda delta, at, **kw: requests.append((delta, at, kw)) or True)
    sampler = NativeLifeFeedback(backend, detector=SimpleNamespace(scores=lambda _: dict(category="GREAT")))
    sampler.observe_scene(0., visible=True)
    return backend, stream, state, sampler, requests


def observe(stream, sampler, token, at, direction=TimingFeedback.SLOW, *, intentional=False, reused=False):
    receipt = dict(action_token=token, planned_engine_s=at, action_kind="tap", command="d",
        feedback_single_tap=not intentional)
    stream.publish_receipts([receipt], final_chunk=True)
    stream.observe_commit([receipt], at, 1, 0., execution_engine_s=at, execution_engine_uncertainty_ms=0.)
    due = sampler.capture_due(at + .001)
    now = at + .055
    if due is not None:
        sampler.capture_requested(due)
    fresh = sampler.fresh(dict(sequence=token, request_s=at + .03,
        first_done_observed_s=at + .05, reused=reused), now)
    if fresh:
        sampler.observe_frame(_feedback_image(direction), now)
    return due


@pytest.mark.parametrize("direction,delta", [(TimingFeedback.SLOW, -60.), (TimingFeedback.FAST, 60.)])
def test_three_executed_ordinary_keys_request_without_perfect_and_then_new_keys_30(direction, delta):
    backend, stream, state, sampler, requests = fixture()
    for token in range(1, 4):
        observe(stream, sampler, token, token * .1, direction)
    assert [r[0] for r in requests] == [delta]
    assert requests[0][2]["ordinary_action_tokens"] == (1, 2, 3)
    state[0] = NativePhaseStatus("simple", 1, state="applied", owner="feedback",
        delta_ms=delta, cumulative_phase_ms=delta, original_observed_s=requests[0][1],
        first_adjusted_due_s=.4, first_affected_chunk_sequence=1, first_affected_action_token=4,
        event_cursor=1)
    sampler.refresh_phase_status(.4)
    for token in range(4, 7):
        observe(stream, sampler, token, token * .1, direction)
    assert [r[0] for r in requests] == [delta, delta / 2]
    assert requests[1][2]["ordinary_action_tokens"] == (4, 5, 6)


def test_first_anomaly_increases_subsequent_key_sampling_not_repolling_same_token():
    _, stream, _, sampler, requests = fixture()
    observe(stream, sampler, 1, .1)
    assert sampler._single_tap_burst
    assert sampler.capture_due(.16) is None
    assert sampler.summary["direction_votes"] == 1
    assert observe(stream, sampler, 2, .2) is not None
    assert sampler.summary["direction_votes"] == 2
    assert not requests


def test_intentional_random_key_old_frame_stop_and_unexecuted_never_vote():
    backend, stream, _, sampler, requests = fixture()
    observe(stream, sampler, 1, .1, intentional=True)
    assert sampler.summary["direction_votes"] == 0
    observe(stream, sampler, 2, .2, reused=True)
    assert sampler.summary["direction_votes"] == 0
    backend._native_input_stop_requested.set()
    observe(stream, sampler, 3, .3)
    assert sampler.summary["direction_votes"] == 0
    assert not requests
    backend._native_input_stop_requested.clear()
    stream.publish_receipts([dict(action_token=4, planned_engine_s=.4, action_kind="tap")])
    assert sampler.capture_due(.4) is None


def test_mixed_direction_restarts_three_key_trend():
    _, stream, _, sampler, requests = fixture()
    for token, direction in enumerate([TimingFeedback.SLOW, TimingFeedback.SLOW,
        TimingFeedback.FAST, TimingFeedback.SLOW, TimingFeedback.SLOW], 1):
        observe(stream, sampler, token, token * .1, direction)
    assert not requests
    observe(stream, sampler, 6, .6)
    assert [r[0] for r in requests] == [-60.]
    assert requests[0][2]["ordinary_action_tokens"] == (4, 5, 6)


def test_not_great_does_not_vote_or_enable_burst():
    _, stream, _, sampler, requests = fixture()
    sampler._grade_extractor = SimpleNamespace(scores=lambda _: dict(category="PERFECT"))
    observe(stream, sampler, 1, .1)
    assert not sampler._single_tap_burst
    assert sampler.summary["direction_votes"] == 0
    assert not requests


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_normal_native_random_configuration_selects_simple_path_without_trial_flags(tmp_path, monkeypatch):
    for name in ("MAABANGDREAM_NATIVE_RANDOM_GREAT_FEEDBACK_TRIAL",
                 "MAABANGDREAM_NATIVE_RANDOM_GREAT_GAME_CLOCK_TRIAL",
                 "MAABANGDREAM_NATIVE_POST_RANDOM_DISPLAY_TRIAL"):
        monkeypatch.delenv(name, raising=False)
    backend = NativeMinitouchBackend(write_chart(tmp_path), adb_path="unused", serial="unused",
        device=SimpleNamespace(request_reset=lambda: True), life_feedback_enabled=True,
        random_great_options=ENABLED, start_gate_mode="formal", expected_notes=100,
        observation_clock_trial_enabled=True, visual_refresh_enabled=True)
    sampler = NativeLifeFeedback(backend)
    assert backend._random_great_feedback_enabled
    assert sampler._simple and sampler._clocked is None
    assert not hasattr(sampler, "_display")
    assert sampler._visual_refresh is None
    assert not hasattr(backend, "request_input_probe")
    assert not hasattr(backend, "configure_ordinary_proof")
    assert backend._session.supports_post_random_display_large_step()


def test_observer_real_backend_mailbox_requires_three_executed_tokens_and_large_step_capability():
    from test_native_life_feedback import _phase_backend
    backend, calls = _phase_backend()
    backend._simple_single_tap_feedback = True
    backend._feedback_tickets = FeedbackTicketStream("test")
    backend._session.supports_post_random_display_large_step = lambda: True
    backend._session.apply_future_phase = lambda *args, **kwargs: calls.append((args, kwargs)) or "applied"
    sampler = NativeLifeFeedback(backend, detector=SimpleNamespace(scores=lambda _: dict(category="GREAT")))
    sampler.observe_scene(0, visible=True)
    assert not backend.request_future_phase(-60, .1, ordinary_action_tokens=(1, 2, 3))
    assert backend._phase_request_rejection_reason == "ordinary-single-tap-not-executed"
    for token in range(1, 4):
        observe(backend._feedback_tickets, sampler, token, token * .1)
    assert backend._phase_slot[3] == -60
    assert backend.phase_status_snapshot().feedback_source == "ordinary-single-tap"
    assert not calls
    backend._consume_phase_candidate()
    assert calls == [((-60., .5), dict(post_random_display_large_step=True))]


def test_render_host_upper_bound_cannot_prove_engine_effect_execution():
    from agent.realtime.native_feedback_ticket import FeedbackObservationTicket
    _, _, _, sampler, _ = fixture()
    sampler._effect_status = NativePhaseStatus("simple", 1, state="applied", owner="feedback",
        first_adjusted_due_s=10., first_affected_chunk_sequence=2, first_affected_action_token=3)
    source = FeedbackObservationTicket("simple", 3, 2, 10.1, 200., 0., 3,
        execution_engine_s=9.9, execution_engine_uncertainty_ms=.5)
    assert not sampler._source_follows_effect(source)
    assert not sampler._source_follows_effect(replace(source, execution_engine_s=None))
    assert sampler._source_follows_effect(replace(source, execution_engine_s=10.1))


def test_phase_limit_at_180_keeps_fast_blocked_until_full_next_step_room():
    _, _, state, sampler, _ = fixture()
    sampler._single_tap_first_effect = True
    sampler._blocked_direction = TimingFeedback.FAST
    state[0] = NativePhaseStatus("simple", 5, state="applied", owner="feedback",
        cumulative_phase_ms=180., first_adjusted_due_s=1., first_affected_chunk_sequence=1,
        first_affected_action_token=1, event_cursor=1)
    sampler.refresh_phase_status(2.)
    assert sampler._blocked_direction is TimingFeedback.FAST
    state[0] = replace(state[0], request_sequence=6, cumulative_phase_ms=150., event_cursor=2)
    sampler.refresh_phase_status(2.1)
    assert sampler._blocked_direction is None


@pytest.mark.parametrize("direction, expected", [(TimingFeedback.SLOW, -60.), (TimingFeedback.FAST, 60.)])
def test_real_cpp_receipts_feed_three_votes_and_apply_once(direction, expected):
    from agent.realtime.native_feedback_ticket import FeedbackReceiptIdentityChain
    from agent.realtime.native_random_great import build_random_great_feedback_plan
    from test_native_life_feedback import _phase_backend
    backend, _ = _phase_backend()
    backend._simple_single_tap_feedback = True
    backend._feedback_tickets = FeedbackTicketStream("test")
    actions = [dict(kind="tap", due_s=at, lane=index, contact=-1, note_index=index,
        target_x=190 + 150 * index, flick_direction=None) for index, at in enumerate((.1, .2, .3))]
    compiler = native_engine.touch_script_compiler()
    compiler.compile(actions, dict(judgement_y=590, press_bias_ms=0, song_offset_s=0), 0., True, .5)
    receipts = list(compiler.execution_receipts())
    chain = FeedbackReceiptIdentityChain(build_random_great_feedback_plan(actions, actions), actions)
    chain.register_chunk(1, [dict(action=action, engine_due_s=action["due_s"]) for action in actions])
    session = native_engine.playback_session(publish=lambda chunk: True, clock=lambda: 1.)
    future = [dict(actions[0], due_s=at, note_index=index) for index, at in enumerate((0., 2., 4.))]
    assert session.arm(future, {}) and session.start(0.) and session.publish()
    backend._session = session
    sampler = NativeLifeFeedback(backend, detector=SimpleNamespace(scores=lambda _: dict(category="GREAT")))
    sampler.observe_scene(0., visible=True)
    # 回执来自真实C++编译器；执行时刻和图像明确为离线合成，不冒充设备回执。
    for receipt in receipts:
        receipt = dict(receipt, **chain.bind_receipt(receipt), feedback_single_tap=True)
        at = receipt["planned_engine_s"]
        backend._feedback_tickets.publish_receipts([receipt], final_chunk=True)
        backend._feedback_tickets.observe_commit([receipt], at, 1, 0.,
            execution_engine_s=at, execution_engine_uncertainty_ms=0.)
        sampler.capture_due(at + .001)
        now = at + .055
        assert sampler.fresh(dict(sequence=receipt["action_token"], request_s=at + .03,
            first_done_observed_s=at + .05, reused=False), now)
        sampler.observe_frame(_feedback_image(direction), now)
    assert sampler.summary["accepted_requests"] == 1
    backend._consume_phase_candidate()
    assert backend.phase_status_snapshot().state == "applied"
    assert session.future_phase_offset_ms == expected
    backend._consume_phase_candidate()
    assert session.future_phase_offset_ms == expected
