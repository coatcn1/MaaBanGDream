from collections import deque
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime import native_engine, native_play
from agent.realtime.native_game_clock import NativeGameClockController
from test_native_game_clock import emit
from test_native_life_feedback import _phase_backend


class BoundedMatcher:
    def __init__(self, lag=0., bounds=None):
        self.lag = lag
        self.bounds = bounds or (lag, lag)

    def observe(self, *_args):
        return dict(status='matched', relative_lag_ms=self.lag, capture_bounds_ms=self.bounds)


def _accepted_backend(monkeypatch):
    """走真实 owner 的 accepted-commit 路径，设备和会话仅提供有界回执。"""
    backend, _calls = _phase_backend()
    backend._clock_relation_required = True
    backend._log_cursor = 0
    backend._playback_observation_started = True
    backend._observation_cancelled = False
    backend._last_device_start_ms = backend._last_device_end_ms = None
    backend._observed_commands = 0
    backend._device_clock_offset_s = 0.
    backend._require_probe = True
    backend._observed_action_tokens = set()
    backend._first_action_anchor_s = 0.
    backend._execution_timing = native_play._ExecutionTimingTrace()
    backend._drift_rate_estimator = None
    backend._final_chunk_published = False
    backend._feedback_input_history = ()
    backend._feedback_input_history_dropped = 0
    backend._feedback_input_snapshot = (float('-inf'), float('-inf'))
    backend._calibrator = SimpleNamespace(observe=lambda _event: None)
    backend._session.observe_minitouch_log = lambda _event: None
    accepted = []
    backend._session.observe_execution = lambda *args: accepted.append(args) or True
    backend._session.report = lambda: dict(state='running')
    logs, events = [], []
    backend._device = SimpleNamespace(log_records_since=lambda cursor: (len(logs), logs[cursor:]))
    backend._expected_commands = deque()
    monkeypatch.setattr(native_engine, 'parse_minitouch_log', lambda line: events[int(line.split()[-1])])

    def commit(actual, shift_ms=0.):
        index = len(events)
        events.append(dict(start_ms=actual * 1000, end_ms=actual * 1000, cost_ms=0., command='c'))
        logs.append((f'jlog {index}', actual + .0005 + shift_ms / 1000))
        receipts = tuple(dict(action_token=index * 2 + column, planned_engine_s=actual,
                              action_kind='tap', command='d') for column in (1, 2))
        backend._expected_commands.append(native_play._ExpectedCommand('c', 1, receipts=receipts))
        backend._observe_new_logs()

    return backend, commit, accepted


def _relation_fixture(monkeypatch):
    backend, commit, accepted = _accepted_backend(monkeypatch)
    for actual in (.65, .7, .75):
        commit(actual)
    matcher = BoundedMatcher()
    controller = NativeGameClockController(backend, matcher=matcher)
    for sequence, now in enumerate((.8, .85, .9), 1):
        commit(now - .02)
        emit(controller, now, sequence)
    controller.samples.clear()
    return controller, backend, commit, accepted


def test_owner_snapshot_is_immutable_one_per_accepted_commit_and_bounded(monkeypatch):
    backend, commit, accepted = _accepted_backend(monkeypatch)
    for index in range(70):
        commit(.1 + index * .02)
    snapshot = backend.clock_relation_snapshot()
    assert isinstance(snapshot, tuple) and len(snapshot) == 64
    assert len(accepted) == 140
    assert [row.command_cursor for row in snapshot] == list(range(7, 71))
    assert snapshot[-1].received_host_s - snapshot[-1].actual_mapped_s == pytest.approx(.0005)
    with pytest.raises(FrozenInstanceError):
        snapshot[-1].command_cursor = 99
    backend._phase_lock.acquire()
    try:
        assert backend.clock_relation_snapshot() is snapshot
    finally:
        backend._phase_lock.release()


def test_rejected_commit_does_not_publish_relation(monkeypatch):
    backend, commit, _accepted = _accepted_backend(monkeypatch)
    backend._session.observe_execution = lambda *_args: False
    with pytest.raises(RuntimeError):
        commit(.7)
    assert backend.clock_relation_snapshot() == ()


def test_optional_publisher_failure_preserves_input_and_warns_once(monkeypatch, capsys):
    backend, commit, accepted = _accepted_backend(monkeypatch)
    def fail(*_args):
        raise OSError('relation observer unavailable')
    monkeypatch.setattr(backend, '_publish_clock_relation', fail)
    commit(.7)
    commit(.8)
    assert len(accepted) == 4 and not backend._expected_commands
    assert backend.clock_relation_snapshot() is None
    assert capsys.readouterr().out.count('NativeClockRelation') == 1


@pytest.mark.parametrize('shift_ms', [0., 103.])
def test_vm_geometry_requires_stable_frozen_host_native_relation(monkeypatch, shift_ms):
    controller, backend, commit, _accepted = _relation_fixture(monkeypatch)
    controller.matcher.lag, controller.matcher.bounds = 113.876, (40.818, 192.606)
    for sequence, now in enumerate((1.1, 1.2, 1.3), 4):
        commit(now - .02 - shift_ms / 1000, shift_ms)
        emit(controller, now, sequence)
    if shift_ms:
        assert backend._phase_slot is None
        assert not backend._visual_phase_guard
        assert backend.request_future_phase(-15, 1.31)
    else:
        assert backend._phase_slot[3] == 15
    assert controller.report()['clock_relation']['baseline_delta_ms'] == pytest.approx(.5)


def test_relation_loss_on_short_reused_drops_epoch_but_preserves_applied_debt(monkeypatch):
    controller, backend, commit, _accepted = _relation_fixture(monkeypatch)
    controller.matcher.lag, controller.matcher.bounds = 30., (30., 30.)
    for sequence, now in enumerate((1., 1.05, 1.1), 4):
        commit(now - .02)
        emit(controller, now, sequence)
    backend._clock = lambda: 1.1
    backend._consume_phase_candidate()
    assert backend.phase_owner_cumulative('game-clock') == 15
    pending = controller.pending_sequence
    epoch = controller.epoch
    commit(1.25 - .02 - .103, 103.)
    controller.observe(None, dict(sequence=6, request_s=1.09, reused=True), 1.25, visible=True)
    assert controller.epoch > epoch
    assert not backend._visual_phase_guard
    assert controller.pending_sequence == pending
    assert controller.applied_effect is not None
    assert controller.visual_applied_ms == 15
    assert controller.visual_origin_ms == 0


def test_recovery_needs_three_new_cursors_and_never_rebases_relation(monkeypatch):
    controller, backend, commit, _accepted = _relation_fixture(monkeypatch)
    commit(1.08 - .103, 103.)
    emit(controller, 1.1, 4)
    baseline = controller.report()['clock_relation']['baseline_delta_ms']
    for sequence, now in enumerate((1.2, 1.3), 5):
        commit(now - .02)
        emit(controller, now, sequence)
        assert not controller.report()['clock_relation']['trusted']
    commit(1.38)
    emit(controller, 1.4, 7)
    assert controller.report()['clock_relation']['trusted']
    controller.invalidate(1.41, 'invalid-scene')
    for sequence, now in enumerate((1.6, 1.7, 1.8), 8):
        commit(now - .02 - .103, 103.)
        emit(controller, now, sequence)
    assert controller.report()['clock_relation']['baseline_delta_ms'] == baseline
    assert not controller.report()['clock_relation']['trusted']
    assert backend._phase_slot is None


@pytest.mark.parametrize('problem', ['missing', 'error', 'empty', 'duplicate', 'stale', 'old-tail',
                                     'uncertain', 'foreign', 'future', 'rollback'])
def test_missing_invalid_or_old_relation_fails_closed(monkeypatch, problem):
    controller, backend, _commit, _accepted = _relation_fixture(monkeypatch)
    rows = backend.clock_relation_snapshot()
    if problem == 'missing':
        backend.clock_relation_snapshot = None
    elif problem == 'error':
        def fail():
            raise RuntimeError('snapshot unavailable')
        backend.clock_relation_snapshot = fail
    else:
        if problem == 'empty':
            rows = ()
        elif problem == 'duplicate':
            rows = (rows[-1],) * 3
        elif problem == 'stale':
            rows = tuple(replace(row, actual_mapped_s=row.actual_mapped_s - 2,
                                 received_host_s=row.received_host_s - 2) for row in rows)
        elif problem == 'old-tail':
            rows = tuple(replace(row, actual_mapped_s=row.actual_mapped_s - .8,
                                 received_host_s=row.received_host_s - .8) for row in rows)
        elif problem == 'uncertain':
            rows = tuple(replace(row, probe_uncertainty_ms=1.001) for row in rows)
        elif problem == 'foreign':
            rows = tuple(replace(row, run_id='another-run') for row in rows)
        elif problem == 'future':
            rows = tuple(replace(row, received_host_s=2.) for row in rows)
        elif problem == 'rollback':
            rows = rows[:3]
        backend.clock_relation_snapshot = lambda: rows
    controller.matcher.lag, controller.matcher.bounds = 113.876, (40.818, 192.606)
    for sequence, now in enumerate((1., 1.1, 1.2), 4):
        emit(controller, now, sequence)
    assert backend._phase_slot is None and not backend._visual_phase_guard
    assert not controller.report()['clock_relation']['trusted']


def test_production_capability_defaults_fail_closed(monkeypatch, tmp_path):
    if not native_engine.available():
        pytest.skip('Native 未构建')
    chart = tmp_path / 'chart.json'
    chart.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]',
                     encoding='utf-8')
    backend = native_play.NativeMinitouchBackend(chart, adb_path='unused', serial='unused',
        device=SimpleNamespace(request_reset=lambda: True, publish=lambda _packet: True),
        life_feedback_enabled=True)
    backend._first_action_anchor_s = 0.
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(30.))
    assert backend._clock_relation_required and controller.report()['clock_relation']['required']
    pipeline = backend.report()['first_chunk_pipeline']
    assert pipeline['host_clock_domain'] == 'host-perf-counter-seconds'
    assert pipeline['device_clock_domain'] == 'minitouch-CLOCK_REALTIME-milliseconds'
    monkeypatch.setattr(backend, 'clock_relation_snapshot', None)
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert not controller.report()['clock_relation']['trusted']
    assert backend._phase_slot is None


def test_relation_recovery_rejects_old_worker_epoch(monkeypatch):
    controller, backend, commit, _accepted = _relation_fixture(monkeypatch)
    old_epoch = controller.epoch
    controller._results.put_nowait((old_epoch, dict(status='matched', relative_lag_ms=110),
                                    dict(request_s=.89), .9))
    commit(1.08 - .103, 103.)
    emit(controller, 1.1, 4)
    controller._thread = SimpleNamespace()
    for sequence, now in enumerate((1.2, 1.3, 1.4), 5):
        commit(now - .02)
        controller.submit(np.zeros((720, 1280, 3), np.uint8),
                          dict(sequence=sequence, request_s=now - .01, reused=False), now, visible=True)
    assert controller.report()['clock_relation']['trusted']
    assert controller.epoch > old_epoch
    assert not controller.samples
    assert controller._results.empty()
    assert backend._phase_slot is None


def test_relation_loss_revokes_only_unapplied_visual_pending(monkeypatch):
    controller, backend, commit, _accepted = _relation_fixture(monkeypatch)
    controller.matcher.lag, controller.matcher.bounds = 30., (30., 30.)
    for sequence, now in enumerate((1., 1.05, 1.1), 4):
        commit(now - .02)
        emit(controller, now, sequence)
    assert backend._phase_slot is not None
    commit(1.23 - .103, 103.)
    controller.observe(None, dict(sequence=6, request_s=1.09, reused=True), 1.25, visible=True)
    assert backend._phase_slot is None and controller.pending_sequence is None
    assert controller.visual_applied_ms == 0
    assert not backend._visual_phase_guard


def test_no_relation_baseline_can_be_learned_after_opening(monkeypatch):
    backend, commit, _accepted = _accepted_backend(monkeypatch)
    for actual in (9.7, 9.8, 9.9):
        commit(actual, 103.)
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(110.))
    for sequence, now in enumerate((10.1, 10.2, 10.3)):
        emit(controller, now, sequence)
    assert controller.report()['clock_relation']['baseline_delta_ms'] is None
    assert backend._phase_slot is None


def test_passive_target_reclaims_only_after_three_new_supported_frames():
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    backend._phase_owner_totals = {'game-clock': 30.}
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), cumulative_phase_ms=30.), 'own-executed')
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(110., (20., 160.)))
    controller.visual_applied_ms = 30.
    controller.baseline_phase_ms = 0.
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert not getattr(backend, '_visual_phase_guard', False)
    controller.matcher.bounds = (80., 160.)
    emit(controller, 1., 3)
    emit(controller, 1.05, 4)
    assert backend._phase_slot is None
    emit(controller, 1.1, 5)
    assert backend._phase_slot[3] == 15
    assert backend.phase_owner_cumulative('game-clock') == 30


def test_relation_loss_preserves_foreign_pending_and_blocks_old_epoch(monkeypatch):
    controller, backend, commit, _accepted = _relation_fixture(monkeypatch)
    assert backend.request_future_phase(-15, .91)
    original = backend._phase_slot
    epoch = controller.epoch
    controller._results.put_nowait((epoch, dict(status='matched', relative_lag_ms=110),
                                    dict(request_s=.89), .9))
    commit(1.08 - .103, 103.)
    emit(controller, 1.1, 4)
    assert backend._phase_slot == original
    assert controller.epoch > epoch
    assert not controller.samples
    controller.close(1.11, 'stopped')
    assert controller.closed and backend._phase_slot == original


@pytest.mark.parametrize('lag,bounds', [(110., (20., 160.)), (20., (15., 25.))])
def test_twelve_seconds_without_safe_next_step_returns_guard_and_keeps_ledger(lag, bounds):
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    backend._phase_owner_totals = {'feedback': 15., 'game-clock': 30.}
    backend._publish_phase_status(replace(backend.phase_status_snapshot(), cumulative_phase_ms=45.), 'fixture-ledger')
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(lag, bounds))
    controller.visual_applied_ms = 30.
    controller.baseline_phase_ms = 15.
    backend.set_visual_phase_guard(True)
    for sequence in range(61):
        emit(controller, .8 + sequence * .2, sequence)
    assert not backend._visual_phase_guard
    assert backend._visual_phase_guard_epoch == 2
    assert controller.visual_applied_ms == 30.
    assert controller.baseline_phase_ms == 15.
    assert backend.phase_owner_cumulative('feedback') == 15.
    assert backend._phase_slot is None
    assert backend.request_future_phase(-15, 13.)


def test_busy_foreign_owner_never_claims_guard_or_overwrites_slot():
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    assert backend.request_future_phase(-15, .75)
    original = backend._phase_slot
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(30.))
    for sequence, now in enumerate((.8, .85, .9, .95)):
        emit(controller, now, sequence)
    assert backend._phase_slot == original
    assert not backend._visual_phase_guard
    assert getattr(backend, '_visual_phase_guard_epoch', 0) == 0


def test_rejected_visual_request_does_not_hold_guard(monkeypatch):
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(30.))
    monkeypatch.setattr(backend, 'request_future_phase', lambda *_args, **_kwargs: False)
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    assert controller.pending_sequence is None
    assert not backend._visual_phase_guard


@pytest.mark.parametrize('foreign_cover', [False, True])
def test_owner_apply_between_invalidation_read_and_revoke_keeps_effect_debt(monkeypatch, foreign_cover):
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(30.))
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    pending = controller.pending_sequence
    original = backend.revoke_future_phase

    def raced(reason, *, owner='feedback'):
        backend._consume_phase_candidate()
        if foreign_cover:
            backend._publish_phase_status(replace(backend.phase_status_snapshot(), owner='feedback',
                request_sequence=99, state='requested'), 'foreign-covered')
        original(reason, owner=owner)

    monkeypatch.setattr(backend, 'revoke_future_phase', raced)
    controller.invalidate(1., 'clock-relation-changed')
    assert backend.phase_owner_cumulative('game-clock') == 15
    assert controller.visual_applied_ms == 15
    assert controller.pending_sequence == pending
    assert controller.applied_effect.request_sequence == pending
    assert controller.visual_origin_ms == 0
    assert not controller._effect_ready(1.1, 1.09)
    assert controller.pending_sequence == pending
    assert not backend._visual_phase_guard


def test_owner_lock_busy_revoke_keeps_identity_until_confirmed_terminal():
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(30.))
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    pending = controller.pending_sequence
    backend._phase_lock.acquire()
    try:
        controller.invalidate(1., 'clock-relation-changed')
        assert controller.pending_sequence == pending
        assert backend._phase_slot is not None
    finally:
        backend._phase_lock.release()
    backend._consume_phase_candidate()
    assert backend.phase_status_snapshot().state == 'rejected'
    assert controller._effect_ready(1.1, 1.09)
    assert controller.pending_sequence is None
    assert controller.visual_applied_ms == 0


def test_foreign_cover_without_owned_effect_boundary_degrades_visual_only(monkeypatch):
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(30.))
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    pending = controller.pending_sequence
    original = backend.revoke_future_phase

    def covered(reason, *, owner='feedback'):
        original(reason, owner=owner)
        backend.set_visual_phase_guard(False)
        assert backend.request_future_phase(-15, 1.)

    monkeypatch.setattr(backend, 'revoke_future_phase', covered)
    controller.invalidate(1., 'clock-relation-changed')
    foreign = backend._phase_slot
    assert controller.pending_sequence == pending
    assert not controller._effect_ready(1.1, 1.09)
    assert controller.pending_sequence == pending
    assert backend._phase_slot == foreign
    assert controller.visual_origin_ms == 0
    assert not backend._visual_phase_guard


def test_foreign_applied_effect_cannot_absorb_uncached_owned_application(monkeypatch):
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    controller = NativeGameClockController(backend, matcher=BoundedMatcher(30.))
    for sequence, now in enumerate((.8, .85, .9)):
        emit(controller, now, sequence)
    pending = controller.pending_sequence
    original = backend.revoke_future_phase

    def covered(reason, *, owner='feedback'):
        backend._consume_phase_candidate()
        backend.set_visual_phase_guard(False)
        assert backend.request_future_phase(-15, 1.)
        backend._session.future_phase_offset_ms = 0.
        backend._consume_phase_candidate()
        original(reason, owner=owner)

    monkeypatch.setattr(backend, 'revoke_future_phase', covered)
    controller.invalidate(1., 'clock-relation-changed')
    assert backend.phase_effect_snapshot().owner == 'feedback'
    assert backend.phase_owner_cumulative('feedback') == -15
    assert controller.visual_applied_ms == 15
    assert controller.pending_sequence == pending
    assert controller.applied_effect is None
    assert not controller._effect_ready(1.1, 1.09)
    assert controller.visual_origin_ms == 0
    assert not backend._visual_phase_guard
