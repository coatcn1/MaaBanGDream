from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.native_game_clock import NativeGameClockController
from agent.realtime.native_game_clock_matcher import NativeGameClockMatcher, bootstrap, match
from test_native_life_feedback import _phase_backend


def test_visual_owner_does_not_stack_with_or_get_revoked_by_judgement_feedback():
    backend, calls = _phase_backend()
    backend.set_visual_phase_guard(True)
    assert not backend.request_future_phase(15, .95)
    assert backend.request_future_phase(15, .95, owner="game-clock")
    backend.revoke_future_phase("mixed-feedback")
    backend._consume_phase_candidate()
    assert calls == [(15, .5)]
    assert backend.phase_status_snapshot().owner == "game-clock"


def test_visual_invalidation_releases_guard_and_cancels_its_pending_request():
    backend, calls = _phase_backend()
    backend.set_visual_phase_guard(True)
    assert backend.request_future_phase(15, .95, owner="game-clock")
    backend.revoke_future_phase("invalid-scene", owner="game-clock")
    backend.set_visual_phase_guard(False)
    backend._consume_phase_candidate()
    assert calls == []
    assert backend.request_future_phase(-15, 1)


def test_visual_revoke_cannot_cancel_foreign_feedback_owner():
    backend, calls = _phase_backend()
    assert backend.request_future_phase(-15, .95)
    backend.revoke_future_phase("unknown", owner="game-clock")
    backend._consume_phase_candidate()
    assert calls == [(-15, .5)]


def test_claim_does_not_revoke_foreign_pending_request_or_extend_its_expiry():
    backend, calls = _phase_backend()
    assert backend.request_future_phase(15, .95)
    original = backend._phase_slot
    backend.set_visual_phase_guard(True)
    assert backend._phase_slot == original
    assert not backend.request_future_phase(15, .95, owner="game-clock")
    backend._consume_phase_candidate()
    assert calls == [(15, .5)]
    assert backend.phase_owner_cumulative("feedback") == 15
    assert backend.phase_owner_cumulative("game-clock") == 0


def test_foreign_feedback_effect_waits_for_real_execution_and_three_new_visual_frames():
    backend, calls = _phase_backend()
    backend._first_action_anchor_s = 0
    assert backend.request_future_phase(15, .75)
    original = backend._phase_slot
    controller = NativeGameClockController(backend, matcher=Matcher(30))
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert controller.pending_sequence is None and backend._phase_slot == original
    backend._consume_phase_candidate()
    assert calls == [(15, .5)]
    emit(controller, 1.1, 3)
    assert backend._phase_slot is None
    assert not controller.samples
    assert controller.summary["waiting-foreign-effect"] == 1
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, first_adjusted_due_s=1.2,
        first_affected_chunk_sequence=2, first_affected_action_token=8), "effect-published")
    backend._feedback_transient_snapshot = (1., 1., 7, 1)
    emit(controller, 1.15, 4)
    assert backend._phase_slot is None and not controller.samples
    backend._feedback_transient_snapshot = (1.25, 1.251, 8, 2)
    emit(controller, 1.3, 5)
    emit(controller, 1.4, 6)
    assert backend._phase_slot is None and len(controller.samples) == 2
    emit(controller, 1.5, 7)
    assert backend._phase_slot[3] == 15
    assert backend.phase_status_snapshot().owner == "game-clock"
    assert backend.phase_owner_cumulative("feedback") == 15
    assert backend.phase_owner_cumulative("game-clock") == 0
    assert [row for row in controller.events if row["reason"] == "trusted-target"][-1]["target_ms"] == 45


def test_foreign_applied_effect_cannot_hide_behind_later_expired_status():
    backend, calls = _phase_backend()
    backend._first_action_anchor_s = 0
    assert backend.request_future_phase(15, .75)
    controller = NativeGameClockController(backend, matcher=Matcher(30))
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    backend._consume_phase_candidate()
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, state="expired", request_sequence=99,
        terminal_reason="expired"), "later-request-expired")
    emit(controller, 1.1, 3)
    assert calls == [(15, .5)] and backend._phase_slot is None
    assert not controller.samples


class Matcher:
    def __init__(self, lag=130):
        self.lag = lag

    def observe(self, image, request_s, observed_s, anchor_s):
        return {"status": "matched", "relative_lag_ms": self.lag}


def emit(controller, now, sequence, **metadata):
    controller.observe(None, {"request_s": now - .01, "sequence": sequence,
                             "reused": False, **metadata}, now, visible=True)


def test_three_fresh_frames_request_once_and_wait_for_real_effect():
    backend, calls = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=Matcher())
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert backend._phase_slot[3] == 15
    backend._consume_phase_candidate()
    for sequence, now in enumerate((1, 1.05, 1.1), 3):
        emit(controller, now, sequence)
    assert len(calls) == 1
    assert backend._phase_slot is None


def test_input_only_stall_without_game_lag_does_not_request_positive_phase():
    backend, calls = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=Matcher(0))
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert backend._phase_slot is None
    assert calls == []
    assert not getattr(backend, "_visual_phase_guard", False)
    assert controller.baseline_phase_ms is None
    assert backend.request_future_phase(15, .95)
    backend._consume_phase_candidate()
    for sequence, now in enumerate((1.1, 1.2, 1.3), 3):
        emit(controller, now, sequence)
    assert backend._phase_slot is None
    assert backend.phase_status_snapshot().cumulative_phase_ms == 15
    assert not getattr(backend, "_visual_phase_guard", False)


def test_visual_episode_preserves_feedback_phase_and_only_returns_its_own_increment():
    backend, _ = _phase_backend()
    backend._first_action_anchor_s = 0
    assert backend.request_future_phase(15, .75)
    backend._consume_phase_candidate()
    controller = NativeGameClockController(backend, matcher=Matcher(30))
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert controller.baseline_phase_ms == 15
    backend._session.future_phase_offset_ms = 30
    backend._consume_phase_candidate()
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, first_adjusted_due_s=1.1,
        first_affected_chunk_sequence=2, first_affected_action_token=8), "effect-published")
    backend._feedback_transient_snapshot = (1.15, 1.151, 8, 2)
    controller.matcher.lag = 0
    for sequence, now in enumerate((1.3, 1.4, 1.5), 3):
        emit(controller, now, sequence)
    assert controller.visual_applied_ms == 15
    assert backend._phase_slot[3] == -15
    backend._clock = lambda: 1.5
    backend._phase_receipts = [(i, 1.4, 2, 2, 1.4, .5) for i in range(3)]
    backend._session.future_phase_offset_ms = 15
    backend._consume_phase_candidate()
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, first_adjusted_due_s=1.6,
        first_affected_chunk_sequence=3, first_affected_action_token=9), "effect-published")
    backend._feedback_transient_snapshot = (1.65, 1.651, 9, 3)
    for sequence, now in enumerate((1.8, 1.9, 2.0), 6):
        emit(controller, now, sequence)
    assert controller.visual_applied_ms == 0
    assert controller.baseline_phase_ms is None
    assert backend.phase_status_snapshot().cumulative_phase_ms == 15
    assert not backend._visual_phase_guard


def test_capture_uncertainty_covering_zero_does_not_move_phase():
    class UncertainMatcher(Matcher):
        def observe(self, *args):
            return {"status": "matched", "relative_lag_ms": 30,
                    "capture_bounds_ms": (-10, 70)}
    backend, calls = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=UncertainMatcher())
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert backend._phase_slot is None
    assert calls == []


def test_reused_or_scene_invalid_frame_revokes_pending_and_needs_three_new_frames():
    backend, calls = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=Matcher())
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    emit(controller, .95, 2, reused=True)
    backend._consume_phase_candidate()
    assert calls == []
    assert backend._phase_slot is None
    emit(controller, 1, 4)
    emit(controller, 1.05, 5)
    assert backend._phase_slot is None


def test_full_reset_cannot_be_downgraded_by_later_sampling_gap():
    class ResetMatcher(Matcher):
        def __init__(self):
            super().__init__()
            self.resets = []
        def reset(self, *, full):
            self.resets.append(full)
    backend, _ = _phase_backend()
    matcher = ResetMatcher()
    controller = NativeGameClockController(backend, matcher=matcher)
    controller.baseline_phase_ms = 30
    controller.invalidate(1, "invalid-scene")
    controller.invalidate(2, "sampling-gap")
    controller._reset_matcher_if_due()
    assert matcher.resets == [True]
    assert controller.baseline_phase_ms is None


def test_new_scene_baseline_absorbs_prior_visual_ledger_without_double_count():
    backend, _ = _phase_backend()
    backend._first_action_anchor_s = 0
    backend._phase_owner_totals = {"game-clock": 15, "feedback": 15}
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), cumulative_phase_ms=30), "test-state")
    controller = NativeGameClockController(backend, matcher=Matcher(0))
    controller.invalidate(.7, "invalid-scene")
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert controller.visual_origin_ms == 15
    assert controller.visual_applied_ms == 0
    assert controller.baseline_phase_ms is None
    controller.matcher.lag = 30
    for sequence, now in enumerate((1, 1.1, 1.2, 1.3), 3):
        emit(controller, now, sequence)
    assert controller.baseline_phase_ms == 30
    assert backend._phase_slot[3] == 15


def test_sampling_gap_and_reused_frames_only_clear_unfrozen_opening_seeds():
    matcher = NativeGameClockMatcher(SimpleNamespace())
    matcher.opening.append({"old": True})
    matcher.projection = {"a": 600, "h": -170}
    matcher.baseline_lag = .01
    backend, _ = _phase_backend()
    controller = NativeGameClockController(backend, matcher=matcher)
    controller.baseline_phase_ms = 30
    for reason in ("sampling-gap", "slow-or-reused-frames"):
        controller.invalidate(5, reason)
        controller._reset_matcher_if_due()
        assert not matcher.opening
        assert matcher.projection == {"a": 600, "h": -170}
        assert matcher.baseline_lag == .01
        assert controller.baseline_phase_ms == 30
    controller.invalidate(6, "invalid-scene")
    controller._reset_matcher_if_due()
    assert matcher.projection is None
    assert controller.baseline_phase_ms is None


def test_observer_failure_disables_once_and_releases_only_its_lease():
    class BrokenMatcher:
        calls = 0
        def observe(self, *args):
            self.calls += 1
            raise RuntimeError("test")
    backend, _ = _phase_backend()
    backend._first_action_anchor_s = 0
    matcher = BrokenMatcher()
    controller = NativeGameClockController(backend, matcher=matcher)
    emit(controller, .8, 1)
    emit(controller, .9, 2)
    assert matcher.calls == 1
    assert controller.closed
    assert not backend._visual_phase_guard


@pytest.mark.parametrize("problem", ["touch", "commits", "residual", "clock", "gap"])
def test_visual_request_keeps_existing_native_owner_safety_gates(problem):
    backend, calls = _phase_backend()
    if problem == "touch":
        backend._compiler.phase_boundary_safe = lambda: False
    elif problem == "commits":
        backend._phase_receipts.clear()
    elif problem == "residual":
        backend._phase_receipts = [(i, .9, 41, 41, .9, .5) for i in range(3)]
    elif problem == "clock":
        backend._clock_uncertainty_ms = 1.1
    elif problem == "gap":
        backend._session.apply_future_phase = lambda *args: "unsafe-gap"
    backend.set_visual_phase_guard(True)
    assert backend.request_future_phase(15, .95, owner="game-clock")
    backend._consume_phase_candidate()
    assert calls == []
    assert backend.phase_status_snapshot().state != "applied"


def test_late_effect_publication_refreshes_owned_applied_boundary():
    backend, _ = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=Matcher())
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    backend._consume_phase_candidate()
    controller.invalidate(1.01, "invalid-scene")
    status = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(status, first_adjusted_due_s=1.1,
        first_affected_chunk_sequence=2, first_affected_action_token=8), "effect-published")
    backend._feedback_transient_snapshot = (1.15, 1.151, 8, 2)
    assert controller._effect_ready(1.3, 1.29)
    assert controller.pending_sequence is None


def test_pending_valid_target_renews_evidence_without_extending_request_expiry():
    backend, _ = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=Matcher())
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    expiry = backend._phase_slot[-1]
    for sequence, now in enumerate((1.1, 1.3, 1.5), 3):
        emit(controller, now, sequence)
    assert controller.trusted_at == 1.5
    assert backend._phase_slot[-1] == expiry
    controller.matcher.lag = -130
    emit(controller, 1.6, 6)
    assert backend._phase_slot is None


def geometry(depths=(150, 250, 350, 470)):
    projection = {"a": 628.6, "h": -177}
    heads = [{"kind": "cyan", "lane": i, "y": y} for i, y in enumerate(depths)]
    notes = [SimpleNamespace(kind="tap", lane=i, note_index=i,
        time_s=2.3 + projection["a"] / (y - projection["h"])) for i, y in enumerate(depths)]
    return heads, notes, projection, 2.3 + projection["a"] / (590 - projection["h"])


def test_two_depth_multilane_projection_is_unknown_even_with_zero_fit_error():
    heads, notes, _, host = geometry((200, 200, 220, 220))
    assert bootstrap(heads, notes, host)["status"] == "unknown"
    heads.append({"kind": "cyan", "lane": 6, "y": 400})
    assert bootstrap(heads, notes, host)["status"] == "unknown"


def test_narrow_depth_projection_is_unknown_even_with_three_depth_groups():
    heads, notes, _, host = geometry((200, 210, 220, 230))
    assert bootstrap(heads, notes, host)["status"] == "unknown"


def test_effect_planned_time_uses_native_nanosecond_precision():
    backend, _ = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=Matcher())
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    backend._consume_phase_candidate()
    status = backend.phase_status_snapshot()
    due = 271884.46761599014
    backend._publish_phase_status(replace(status, first_adjusted_due_s=due,
        first_affected_chunk_sequence=293, first_affected_action_token=2944), "effect-published")
    backend._feedback_transient_snapshot = (271884.46761599, 271884.4757294655, 2944, 293)
    assert controller._effect_ready(271884.6, 271884.59)


@pytest.mark.parametrize("separation", [.020, .024, .100])
def test_wrong_chart_and_repeated_rhythm_competition_are_unknown(separation):
    heads, notes, projection, host = geometry()
    wrong = [SimpleNamespace(kind=n.kind, lane=6, note_index=n.note_index, time_s=n.time_s) for n in notes]
    assert match(heads, wrong, host, projection)["status"] == "unknown"
    repeated = [*notes, *[SimpleNamespace(kind=n.kind, lane=n.lane, note_index=n.note_index + 10,
        time_s=n.time_s + separation) for n in notes]]
    result = match(heads, repeated, host, projection)
    if separation > .025:
        assert result["status"] == "unknown"
        assert result["competition"]
    else:
        assert result["status"] == "matched"
        lo, hi = result["best"]["game_time_bounds_s"]
        assert hi - lo >= separation - 1e-9


@pytest.mark.parametrize("separation", [.020, .024])
def test_near_alias_bounds_reach_actual_controller_and_do_not_create_fake_lag(monkeypatch, separation):
    heads, notes, projection, host = geometry()
    repeated = [*notes, *[SimpleNamespace(kind=n.kind, lane=n.lane, note_index=n.note_index + 10,
        time_s=n.time_s + separation) for n in notes]]
    matcher = NativeGameClockMatcher(SimpleNamespace(start_time_s=0, judgements=repeated))
    matcher.projection = projection
    matcher.baseline_lag = -separation
    matcher.baseline_interval = (-separation, 0)
    monkeypatch.setattr("agent.realtime.native_game_clock_matcher.extract_heads", lambda image: heads)
    backend, calls = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=matcher)
    image = np.zeros((720, 1280, 3), np.uint8)
    for sequence, now in enumerate((host + .005, host + .006, host + .007)):
        controller.observe(image, {"sequence": sequence, "request_s": now - .01, "reused": False}, now, visible=True)
    assert backend._phase_slot is None
    assert controller.baseline_phase_ms is None
    assert calls == []


def test_async_queue_owns_one_readonly_copy_and_closed_worker_cannot_publish():
    backend, _ = _phase_backend()
    backend._first_action_anchor_s = 0
    controller = NativeGameClockController(backend, matcher=Matcher())
    controller._thread = SimpleNamespace()
    image = np.zeros((720, 1280, 3), np.uint8)
    controller.submit(image, {"request_s": .89, "sequence": 1, "reused": False}, .9, visible=True)
    queued = controller._frames.get_nowait()
    image[:] = 99
    assert not queued[1].flags.writeable
    assert not queued[1].any()
    controller.close(.91)
    controller._results.put((queued[0], {"status": "matched", "relative_lag_ms": 130}, queued[2], queued[3]))
    controller.submit(image, {"request_s": .99, "sequence": 2, "reused": False}, 1, visible=True)
    assert backend._phase_slot is None
