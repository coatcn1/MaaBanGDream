from dataclasses import replace

import pytest

from agent.realtime.native_observation_clock import ObservationClockMapper
from test_native_observation_clock import IDENTITY, probe, warmed


def test_isolated_wide_probe_keeps_original_snapshot_and_same_historical_target():
    mapper = warmed()
    original = mapper.snapshot()
    target = original.commit_view(100.2254, now_s=200.2508, execution_offset_s=100)
    assert target is not None
    assert not mapper.observe(probe(6, 200.3, rtt=.0035))
    assert mapper.snapshot() is original
    assert mapper.snapshot().commit_view(100.2254, now_s=200.3035, execution_offset_s=100) == target
    assert mapper._last_sequence == 6
    assert mapper.observe(probe(7, 200.35))
    current = mapper.snapshot()
    assert current.epoch == original.epoch and current.recovery_sequence == original.recovery_sequence
    assert [row.sequence for row in current.probes] == [1, 2, 3, 4, 5, 7]
    view = current.frame_view(200.275, 200.298, now_s=200.3508, execution_offset_s=100)
    assert view is not None and (view.left_sequence, view.right_sequence) == (5, 7)
    assert view.uncertainty_ms <= 1


def test_wide_probe_cannot_supply_right_bracket_or_refresh_last_good_age():
    mapper = warmed()
    original = mapper.snapshot()
    assert not mapper.observe(probe(6, 200.3, rtt=.0035))
    assert mapper.snapshot() is original and original.probes[-1].sequence == 5
    assert original.frame_view(200.275, 200.29, now_s=200.3035, execution_offset_s=100) is None
    assert original.commit_view(100.2754, now_s=200.3035, execution_offset_s=100) is None
    assert original.commit_view(100.2254, now_s=200.7519, execution_offset_s=100) is None


@pytest.mark.parametrize('warm_count', [0, 1, 2])
def test_warming_never_uses_wide_skip_exception(warm_count):
    mapper = ObservationClockMapper(IDENTITY)
    for sequence in range(1, warm_count + 1):
        mapper.observe(probe(sequence, 200 + sequence*.05))
    previous = mapper.snapshot()
    assert not mapper.observe(probe(warm_count+1, 200.3, rtt=.0035))
    assert mapper.snapshot().epoch == previous.epoch + 1
    assert not mapper.snapshot().probes


def test_skipped_wire_sequence_is_consumed_and_replay_invalidates():
    mapper = warmed()
    original = mapper.snapshot()
    wide = probe(6, 200.3, rtt=.0035)
    assert not mapper.observe(wide) and mapper.snapshot() is original
    assert not mapper.observe(wide)
    assert mapper.snapshot().reason == 'identity-or-sequence'
    assert mapper.snapshot().epoch == original.epoch + 1


def test_consecutive_wide_samples_cannot_roll_forward_the_150ms_lease():
    mapper = warmed()
    original = mapper.snapshot()
    for sequence, host in [(6, 200.3), (7, 200.35), (8, 200.4)]:
        assert not mapper.observe(probe(sequence, host, rtt=.0035))
        assert mapper.snapshot() is original
    assert not mapper.observe(probe(9, 200.45, rtt=.0035))
    assert mapper.snapshot().epoch == original.epoch + 1 and not mapper.snapshot().probes


def test_good_after_skips_compares_offset_with_last_good_not_skipped_chain():
    mapper = warmed()
    original = mapper.snapshot()
    assert not mapper.observe(probe(6, 200.3, offset=100.001, rtt=.0035))
    assert not mapper.observe(probe(7, 200.35, offset=100.002, rtt=.0035))
    assert mapper.snapshot() is original
    assert mapper.observe(probe(8, 200.4, offset=100.003))
    current = mapper.snapshot()
    assert current.epoch == original.epoch + 1 and current.reason == 'warming'
    assert [row.sequence for row in current.probes] == [8]


@pytest.mark.parametrize('shift', [-.103, .103])
@pytest.mark.parametrize('common', [False, True])
def test_wide_probe_cannot_hide_rt_mono_step_or_common_offset_step(shift, common):
    mapper = warmed()
    previous = mapper.snapshot()
    wide = probe(6, 200.3, rtt=.0035)
    fields = dict(device_receive_s=wide.device_receive_s+shift, device_send_s=wide.device_send_s+shift)
    if common:
        fields.update(monotonic_receive_s=wide.monotonic_receive_s+shift,
                      monotonic_send_s=wide.monotonic_send_s+shift)
    assert not mapper.observe(replace(wide, **fields))
    assert mapper.snapshot().epoch == previous.epoch + 1


@pytest.mark.parametrize('fault', ['identity', 'qualified', 'rtt', 'processing', 'mono-processing',
                                 'processing-mismatch', 'nonfinite', 'nonoverlap', 'reverse-step'])
def test_only_width_failure_can_be_skipped(fault):
    mapper = warmed()
    previous = mapper.snapshot()
    wide = probe(6, 200.3, rtt=.0035)
    if fault == 'identity':
        wide = replace(wide, identity=replace(IDENTITY, generation=4))
    elif fault == 'qualified':
        wide = replace(wide, qualified_source=False)
    elif fault == 'rtt':
        wide = probe(6, 200.3, rtt=.021)
    elif fault == 'processing':
        wide = replace(wide, device_send_s=wide.device_receive_s+.0021)
    elif fault == 'mono-processing':
        wide = replace(wide, monotonic_send_s=wide.monotonic_receive_s+.0021)
    elif fault == 'processing-mismatch':
        wide = replace(wide, device_send_s=wide.device_receive_s+.0002)
    elif fault == 'nonfinite':
        wide = replace(wide, monotonic_send_s=float('inf'))
    elif fault == 'nonoverlap':
        wide = probe(6, 200.3, offset=100.01, rtt=.0035)
    else:
        last = previous.probes[-1]
        wide = replace(wide, device_receive_s=last.device_send_s-.001,
                       device_send_s=last.device_send_s-.001,
                       monotonic_receive_s=last.monotonic_send_s-.001,
                       monotonic_send_s=last.monotonic_send_s-.001)
    assert not mapper.observe(wide)
    assert mapper.snapshot().epoch == previous.epoch + 1 and not mapper.snapshot().probes
