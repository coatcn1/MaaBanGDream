from collections import deque
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from agent.realtime import native_engine, native_play
from agent.realtime.native_observation_clock import ClockIdentity, ObservationClockMapper, ObservationProbe


pytestmark = pytest.mark.skipif(not native_engine.available(), reason='Native 未构建')


def fixture(tmp_path, monkeypatch, *, shift=0., residual=.002):
    chart = tmp_path / 'chart.json'
    chart.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]', encoding='utf-8')
    identity = ClockIdentity('domain-run', 1, 42, 100)
    mapper = ObservationClockMapper(identity)
    now = [200.5008]
    def probe(sequence, host):
        device = host + .0004 - 100 - shift
        return ObservationProbe(identity, sequence, host, host + .0008, device, device, device - 1, device - 1)
    for index in range(1, 11):
        mapper.observe(probe(index, 200 + index * .05))
    logs, events, accepted, applied = [], [], [], []
    device = SimpleNamespace(request_reset=lambda: True,
        log_records_since=lambda cursor: (len(logs), logs[cursor:]))
    backend = native_play.NativeMinitouchBackend(chart, adb_path='unused', serial='unused', device=device,
        life_feedback_enabled=True, observation_clock=mapper, observation_clock_trial_enabled=True,
        run_id=identity.run_id, clock=lambda: now[0])
    backend._device_clock_offset_s = 100
    backend._clock_basis, backend._clock_uncertainty_ms = 'probe-midpoint', .5
    backend._playback_observation_started = True
    backend._first_action_anchor_s = 200
    backend._state = backend._session_state = 'running'
    backend._session = SimpleNamespace(observe_minitouch_log=lambda _event: None,
        observe_execution=lambda *args: accepted.append(args) or True, report=lambda: dict(state='running'),
        apply_future_phase=lambda *args: applied.append(args) or 'applied', future_phase_offset_ms=15)
    monkeypatch.setattr(native_engine, 'parse_minitouch_log', lambda line: events[int(line[5:])])
    def commit(host, *, kind='tap', command='d', residual_s=residual):
        index = len(events)
        device_s = host - 100 - shift
        events.append(dict(command='c', start_ms=device_s * 1000, end_ms=device_s * 1000, cost_ms=0))
        logs.append(('jlog ' + str(index), host + .0005))
        receipt = dict(action_token=index + 1, planned_engine_s=device_s + 100 - residual_s,
                       action_kind=kind, command=command, feedback_single_tap=kind == 'tap')
        backend._expected_commands.append(native_play._ExpectedCommand('c', index + 1, receipts=(receipt,)))
        backend._observe_new_logs()
    return backend, mapper, now, probe, commit, accepted, applied


@pytest.mark.parametrize('shift', [-.103, .103])
@pytest.mark.parametrize('residual', [.002, .050])
def test_production_owner_uses_h_receipt_age_but_keeps_e_residual_gate(tmp_path, monkeypatch, shift, residual):
    backend, mapper, now, _, commit, accepted, applied = fixture(tmp_path, monkeypatch, shift=shift, residual=residual)
    for host in (200.2254, 200.3254, 200.4254):
        commit(host)
    backend._refresh_observation_views()
    rows = backend.mapped_execution_commit_snapshot()
    assert len(rows) == 3 and len(accepted) == 3
    assert all(row.host_lower_s <= host <= row.host_upper_s for row, host in zip(rows, (200.2254, 200.3254, 200.4254)))
    assert rows[-1].execution.actual_execution_s + shift == pytest.approx(200.4254)
    assert rows[-1].uncertainty_ms <= 1
    assert backend.request_future_phase(15, now[0])
    backend._consume_phase_candidate()
    diagnostic = backend._phase_receipt_diagnostics[-1]
    assert diagnostic['last_commit_age_s'] == pytest.approx(now[0] - rows[-1].host_lower_s)
    assert diagnostic['abs_execution_residual_median_ms'] == pytest.approx(residual * 1000)
    assert diagnostic['freshness_clock_domain'] == 'host-perf-counter-interval'
    assert bool(applied) == (residual < .04)
    if residual > .04:
        assert 'high-execution-residual' in diagnostic['failed_checks']
    assert backend._device_clock_offset_s == 100
    with pytest.raises(FrozenInstanceError):
        rows[-1].host_upper_s = 999


def test_latest_uncovered_down_does_not_permanently_hide_earlier_covered_effect_source(tmp_path, monkeypatch):
    backend, mapper, now, _, commit, _, _ = fixture(tmp_path, monkeypatch, shift=.103)
    commit(200.4254)
    commit(200.5005)
    backend._refresh_observation_views()
    assert len(backend.mapped_execution_commit_snapshot()) == 1
    status = backend.phase_status_snapshot()
    assert status.transient_action_token == 1
    assert status.transient_actual_s == pytest.approx(200.4254 - .103)
    assert status.transient_host_lower_s <= 200.4254 <= status.transient_host_upper_s
    assert status.transient_clock_identity == mapper.identity
    assert backend.feedback_input_snapshot(200.45, now[0]).action_token == 1


def test_h_snapshot_requires_complete_pre_request_interval_and_150ms_release_rx_upper(tmp_path, monkeypatch):
    backend, mapper, now, _, commit, _, _ = fixture(tmp_path, monkeypatch, shift=-.103)
    commit(200.3504, kind='up', command='u')
    commit(200.4254)
    backend._refresh_observation_views()
    now[0] = 200.5008
    assert not backend.feedback_input_eligible(now[0])
    now[0] = 200.5015
    source = backend.feedback_input_snapshot(200.45, now[0])
    assert source is not None
    assert backend.feedback_input_snapshot(source.host_upper_s - .000001, now[0]) is None


def test_view_is_frozen_once_and_keeps_sparse_1_5s_window_without_retroactive_offset(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    for host in (200.2254, 200.3254, 200.4254):
        commit(host)
    backend._refresh_observation_views()
    original = backend.mapped_execution_commit_snapshot()
    # 旧事件的右probe已超过500ms，但当时冻结的合法H区间仍可用于既有1.5s receipt窗口。
    for index in range(11, 28):
        now[0] = 200 + index * .05 + .0008
        mapper.observe(probe(index, 200 + index * .05))
        backend._refresh_observation_views()
    current = backend.mapped_execution_commit_snapshot()
    assert all(a is b for a, b in zip(original, current))
    recent, _age = backend._phase_receipt_window(now[0])
    assert len(recent) == 3
    mapper.invalidate('unqualified-probe')
    assert not backend.mapped_execution_commit_snapshot()
    assert backend.execution_commit_snapshot()


def test_quality_break_waits_pending_with_new_receipts_and_release_age_cannot_wait_forever(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _, applied = fixture(tmp_path, monkeypatch)
    commit(200.2254, kind='up', command='u')
    commit(200.3254)
    commit(200.4254)
    backend._refresh_observation_views()
    assert backend.request_future_phase(15, now[0])
    mapper.invalidate('high-rtt')
    backend._consume_phase_candidate()
    assert not applied and backend.phase_status_snapshot().state == 'waiting-execution-receipts'
    assert backend._phase_slot is not None
    for index in range(11, 15):
        now[0] = 200 + index * .05 + .0008
        mapper.observe(probe(index, 200 + index * .05))
    commit(200.6754)
    backend._refresh_observation_views()
    assert len(backend.mapped_execution_commit_snapshot()) == 1
    assert backend.feedback_input_eligible(now[0])
    backend._consume_phase_candidate()
    assert not applied and backend._phase_slot is not None


def test_missing_reader_and_same_generation_foreign_pid_cannot_reuse_old_views(tmp_path, monkeypatch):
    backend, mapper, now, _, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    backend._refresh_observation_views()
    assert backend.mapped_execution_commit_snapshot()
    backend._observation_clock = None
    assert not backend.mapped_execution_commit_snapshot()
    assert not backend.request_future_phase(15, now[0])
    other = replace(mapper.snapshot(), identity=replace(mapper.identity, pid=43))
    backend._observation_clock = SimpleNamespace(snapshot=lambda: other)
    assert not backend.mapped_execution_commit_snapshot()
    backend._refresh_observation_views()
    assert not backend.mapped_execution_commit_snapshot() and backend.execution_commit_snapshot()


def test_no_change_refresh_reuses_all_views_without_resolving_clock_history(tmp_path, monkeypatch):
    backend, mapper, _, _, commit, _, _ = fixture(tmp_path, monkeypatch)
    for index in range(24):
        commit(200.1754 + index * .01)
    backend._refresh_observation_views()
    original = backend._mapped_execution_commits
    def fail(*_args, **_kwargs):
        raise AssertionError('unchanged history must not remap')
    monkeypatch.setattr(type(mapper.snapshot()), 'commit_view', fail)
    for _ in range(5):
        backend._refresh_observation_views()
    assert backend._mapped_execution_commits is original


def test_optional_mapping_failure_clears_only_clock_source_and_preserves_accepted_input(tmp_path, monkeypatch, capsys):
    backend, mapper, _, _, commit, accepted, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    def fail(*_args, **_kwargs):
        raise OSError('private clock response must not leak')
    monkeypatch.setattr(type(mapper.snapshot()), 'commit_view', fail)
    backend._refresh_observation_views()
    backend._refresh_observation_views()
    assert len(accepted) == 1 and backend._observed_action_tokens == {1}
    assert backend.execution_commit_snapshot() and not backend.mapped_execution_commit_snapshot()
    assert backend.observation_clock_snapshot() is None and backend._observation_error is None
    assert backend._device_clock_offset_s == 100
    output = capsys.readouterr().out
    assert output.count('mapping_warning=OSError') == 1 and 'private clock response' not in output


@pytest.mark.parametrize('loss', ['none', 'exception'])
def test_observed_reader_loss_never_revives_cached_views_or_old_pending_and_recovers_new_inputs(tmp_path, monkeypatch, loss):
    backend, mapper, now, probe, commit, accepted, applied = fixture(tmp_path, monkeypatch)
    for host in (200.2254, 200.3254, 200.4254):
        commit(host)
    backend._refresh_observation_views()
    old = backend.mapped_execution_commit_snapshot()
    assert len(old) == 3 and backend.request_future_phase(15, now[0])
    if loss == 'none':
        backend._observation_clock = None
    else:
        def fail():
            raise OSError('reader fault')
        backend._observation_clock = SimpleNamespace(snapshot=fail)
    assert not backend.mapped_execution_commit_snapshot()
    backend._observation_clock = mapper
    assert not backend.mapped_execution_commit_snapshot()
    backend._refresh_observation_views()
    assert not backend.mapped_execution_commit_snapshot()
    for index in range(11, 14):
        now[0] = 200 + index * .05 + .0008
        mapper.observe(probe(index, 200 + index * .05))
        backend._refresh_observation_views()
        assert not backend.mapped_execution_commit_snapshot()
    backend._consume_phase_candidate()
    assert not applied and backend.phase_status_snapshot().terminal_reason == 'observation-hard-fence-changed'
    mapper.observe(probe(14, 200.7))
    now[0] = 200.7008
    for host in (200.6604, 200.6754, 200.6904):
        commit(host)
    backend._refresh_observation_views()
    recovered = backend.mapped_execution_commit_snapshot()
    assert len(recovered) == 3 and len(accepted) == 6
    assert recovered[0].trust_revision > old[0].trust_revision
    assert all(row.execution.command_cursor > 3 for row in recovered)
    assert backend.request_future_phase(15, now[0])
    backend._consume_phase_candidate()
    assert len(applied) == 1 and backend._device_clock_offset_s == 100


def test_stale_reader_revokes_old_cache_even_when_producer_continued_same_epoch(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    backend._refresh_observation_views()
    old = backend.mapped_execution_commit_snapshot()[0]
    frozen = mapper.snapshot()
    backend._observation_clock = SimpleNamespace(snapshot=lambda: frozen)
    now[0] = 201.0508
    assert not backend.mapped_execution_commit_snapshot()
    for index in range(11, 22):
        mapper.observe(probe(index, 200 + index * .05))
    assert mapper.snapshot().epoch == frozen.epoch
    backend._observation_clock = mapper
    backend._refresh_observation_views()
    assert not backend.mapped_execution_commit_snapshot()
    commit(200.9)
    backend._refresh_observation_views()
    new, = backend.mapped_execution_commit_snapshot()
    assert new.execution.command_cursor == 2 and new.trust_revision > old.trust_revision


def test_stale_quality_does_not_reuse_old_commits_and_pending_can_recover_with_new_receipts(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _, applied = fixture(tmp_path, monkeypatch)
    assert not backend._mapped_execution_commits
    assert backend.request_future_phase(15, now[0])
    frozen = mapper.snapshot()
    backend._observation_clock = SimpleNamespace(snapshot=lambda: frozen)
    now[0] = 201.0508
    assert not backend.mapped_execution_commit_snapshot()
    for index in range(11, 22):
        mapper.observe(probe(index, 200 + index * .05))
    backend._observation_clock = mapper
    for host in (200.9, 200.95, 201.):
        commit(host)
    backend._refresh_observation_views()
    assert len(backend.mapped_execution_commit_snapshot()) == 3
    backend._consume_phase_candidate()
    assert len(applied) == 1 and backend.phase_status_snapshot().request_sequence == 1
    backend._consume_phase_candidate()
    assert len(applied) == 1
