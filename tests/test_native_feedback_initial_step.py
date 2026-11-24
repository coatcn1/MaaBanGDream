from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.realtime.live_session import current_live_run, reset_live_run
from agent.realtime.native_feedback_ticket import FeedbackTicketStream
from agent.realtime.native_play import NativeMinitouchBackend
from agent.realtime.native_prearm import frozen_native_runtime_options
from agent.realtime.profile_store import RealtimeProfileStore
from agent.realtime.timing_feedback import TimingFeedback
from test_native_life_feedback import _phase_backend
from test_native_random_great import write_chart
from test_native_simple_single_tap import fixture


@pytest.mark.parametrize("value", [True, False, 29, 46, 37., "37", None, float("inf")])
def test_invalid_initial_step_is_rejected_before_atomic_save_or_backend_use(tmp_path, value):
    store = RealtimeProfileStore(tmp_path / "profiles")
    before = store.update_runtime_options({"native_life_feedback_initial_step_ms": 37})
    with pytest.raises(ValueError, match="30..45"):
        store.update_runtime_options(before | {"native_life_feedback_initial_step_ms": value})
    assert store.runtime_options() == before
    with pytest.raises(ValueError, match="30..45"):
        NativeMinitouchBackend(write_chart(tmp_path), adb_path="unused", serial="unused",
            life_feedback_initial_step_ms=value, device=SimpleNamespace(request_reset=lambda: True))


@pytest.mark.parametrize("initial_step", [30, 37, 45])
def test_initial_step_roundtrips_and_freezes_once_per_run(tmp_path, initial_step):
    store = RealtimeProfileStore(tmp_path)
    assert store.runtime_options()["native_life_feedback_initial_step_ms"] == 45
    options = store.update_runtime_options({"native_life_feedback_initial_step_ms": initial_step})
    run = reset_live_run(mode="formal", difficulty="Expert")
    assert frozen_native_runtime_options(run, options)["native_life_feedback_initial_step_ms"] == initial_step
    changed = options | {"native_life_feedback_initial_step_ms": 40}
    assert frozen_native_runtime_options(current_live_run(), changed)["native_life_feedback_initial_step_ms"] == initial_step
    assert frozen_native_runtime_options(reset_live_run(mode="formal", difficulty="Expert"), changed)[
        "native_life_feedback_initial_step_ms"] == 40


@pytest.mark.parametrize("old_choice", [True, False, "ignored", None])
def test_old_guard_field_is_ignored_and_not_written_back(tmp_path, old_choice):
    store = RealtimeProfileStore(tmp_path)
    options = store.update_runtime_options({"native_random_great_feedback_guard_enabled": old_choice})
    assert "native_random_great_feedback_guard_enabled" not in options
    assert "native_random_great_feedback_guard_enabled" not in store.runtime_options()
    assert options["native_life_feedback_initial_step_ms"] == 45


@pytest.mark.parametrize("initial_step", [30, 37, 45])
def test_phase_limit_waits_for_exact_initial_step_headroom(initial_step):
    _, _, state, sampler, _ = fixture(initial_step)
    sampler._blocked_direction = TimingFeedback.FAST
    state[0] = replace(state[0], state="applied", cumulative_phase_ms=201 - initial_step,
        first_adjusted_due_s=0., first_affected_chunk_sequence=1, first_affected_action_token=1, event_cursor=1)
    sampler.refresh_phase_status(1.)
    assert sampler._blocked_direction is TimingFeedback.FAST
    state[0] = replace(state[0], cumulative_phase_ms=200 - initial_step, event_cursor=2)
    sampler.refresh_phase_status(1.1)
    assert sampler._blocked_direction is None


def test_nondefault_initial_step_requires_new_native_range_capability():
    backend, _ = _phase_backend()
    backend._simple_single_tap_feedback = True
    backend._life_feedback_initial_step_ms = 37
    backend._feedback_tickets = FeedbackTicketStream("test")
    backend._session.supports_post_random_display_large_step = lambda: True
    backend._session.supports_ordinary_single_tap_step_range = None
    for token in range(1, 4):
        backend._feedback_tickets.observe_commit([dict(action_token=token, planned_engine_s=token * .1,
            action_kind="tap", command="d", feedback_single_tap=True)], token * .1, 1, 0.)
    assert not backend.request_future_phase(37, .4, ordinary_action_tokens=(1, 2, 3))
    assert backend._phase_request_rejection_reason == "ordinary-single-tap-step-range-unavailable"
    backend._session.supports_ordinary_single_tap_step_range = lambda: True
    assert not backend.request_future_phase(38, .4, ordinary_action_tokens=(1, 2, 3))
    assert backend._phase_request_rejection_reason == "invalid-request"
    assert backend.request_future_phase(37, .4, ordinary_action_tokens=(1, 2, 3))


def test_ordinary_requests_cannot_use_configured_step_without_three_owned_votes():
    backend, _ = _phase_backend()
    backend._life_feedback_initial_step_ms = 37
    assert not backend.request_future_phase(37, .4)
    assert backend._phase_request_rejection_reason == "invalid-request"
