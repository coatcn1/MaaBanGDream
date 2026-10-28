from dataclasses import replace

import pytest

from agent.realtime.native_observation_clock import (
    ClockIdentity, ObservationClockMapper, ObservationProbe,
    JLOG_DEVICE_UNCERTAINTY_S, device_time_enclosure,
)


IDENTITY = ClockIdentity("test-run", 3, 42, 123)


def probe(sequence, host, *, offset=100.0, rtt=.0008, qualified=True):
    return ObservationProbe(IDENTITY, sequence, host, host + rtt,
        host + rtt / 2 - offset, host + rtt / 2 - offset,
        host + rtt / 2 - offset - 1, host + rtt / 2 - offset - 1, qualified)


def warmed(*, offset=100):
    mapper = ObservationClockMapper(IDENTITY)
    for n in range(1, 6):
        assert mapper.observe(probe(n, 200 + n * .05, offset=offset))
    return mapper


@pytest.mark.parametrize("jump", [-.103, .103])
def test_common_clock_jump_keeps_execution_domain_and_recovers(jump):
    mapper = warmed()
    old = mapper.snapshot()
    view = old.commit_view(100.1754, now_s=200.2508, execution_offset_s=100)
    assert view is not None
    assert view.device_to_host(100.1754)[0] <= 200.1754 <= view.device_to_host(100.1754)[1]
    for n in range(6, 10):
        assert mapper.observe(probe(n, 200 + n * .05, offset=100 + jump))
        if n < 8:
            assert mapper.snapshot().reason == "warming"
    new = mapper.snapshot()
    assert new.epoch == old.epoch + 1
    assert new.commit_view(100.1754, now_s=200.4508, execution_offset_s=100) is None
    device = 200.4254 - (100 + jump)
    mapped = new.commit_view(device, now_s=200.4508, execution_offset_s=100)
    assert mapped is not None
    lo, hi = mapped.device_to_host(device)
    assert lo <= 200.4254 <= hi
    elo, ehi = mapped.host_to_execution(200.425, 200.426)
    assert elo <= 200.425 - jump <= ehi
    assert mapped.execution_offset_s == 100


def test_frames_hold_own_view_and_interval_expands_both_sides():
    mapper = warmed()
    snapshot = mapper.snapshot()
    view = snapshot.frame_view(200.175, 200.176, now_s=200.2508, execution_offset_s=99.9)
    assert view is not None
    assert view.delta_bounds[0] < .1 < view.delta_bounds[1]
    lo, hi = view.host_to_execution(200.175, 200.176)
    assert lo < 200.075 and hi > 200.076
    mapper.invalidate("timeout")
    assert mapper.snapshot().frame_view(200.175, 200.176, now_s=200.251, execution_offset_s=99.9) is None
    assert view == snapshot.frame_view(200.175, 200.176, now_s=200.251, execution_offset_s=99.9)


@pytest.mark.parametrize("bad", ["rtt", "research", "rt-only", "reverse", "timeout", "gap"])
def test_unqualified_samples_cut_epoch_and_require_three_new_probes(bad):
    mapper = warmed()
    epoch = mapper.snapshot().epoch
    sample = probe(6, 200.3)
    if bad == "rtt":
        sample = probe(6, 200.3, rtt=.021)
    elif bad == "research":
        sample = replace(sample, qualified_source=False)
    elif bad == "rt-only":
        sample = replace(sample, device_receive_s=sample.device_receive_s + .001,
                         device_send_s=sample.device_send_s + .001)
    elif bad == "reverse":
        sample = replace(sample, sequence=5)
    elif bad == "gap":
        sample = probe(6, 201)
    if bad == "timeout":
        mapper.invalidate("timeout")
    else:
        mapper.observe(sample)
    assert mapper.snapshot().epoch > epoch
    for n in range(7, 11):
        mapper.observe(probe(n, 201 + n * .05))
    assert mapper.snapshot().reason is None
    assert mapper.snapshot().frame_view(200.175, 200.176, now_s=201.5508, execution_offset_s=100) is None


def test_no_extrapolation_or_stale_view_and_history_bounded():
    mapper = warmed()
    snapshot = mapper.snapshot()
    for lower, upper in [(200.2509, 200.26), (200.14, 200.15)]:
        assert snapshot.frame_view(lower, upper, now_s=200.251, execution_offset_s=100) is None
    assert snapshot.commit_view(100.1754, now_s=201, execution_offset_s=100) is None
    for n in range(6, 101):
        mapper.observe(probe(n, 200 + n * .05))
    assert len(mapper.snapshot().probes) == 64
    assert mapper.snapshot().frame_view(202.025, 202.026, now_s=205.0008, execution_offset_s=100) is None
    assert mapper.snapshot().commit_view(102.0254, now_s=205.0008, execution_offset_s=100) is None


def test_73ms_frame_crosses_multiple_20hz_segments_without_changing_original_age():
    mapper = warmed()
    for n in range(6, 11):
        mapper.observe(probe(n, 200 + n * .05))
    view = mapper.snapshot().frame_view(200.275, 200.348, now_s=200.5008, execution_offset_s=100)
    assert view is not None
    assert view.left_sequence == 5 and view.right_sequence == 7
    assert view.uncertainty_ms <= 1
    assert view.covered_host_upper_s - view.covered_host_lower_s < .15
    original_request, original_observed = 200.275, 200.348
    assert original_observed - original_request == pytest.approx(.073)
    mapper.invalidate("timeout")
    for n in range(11, 15):
        mapper.observe(probe(n, 200 + n * .05))
    assert mapper.snapshot().frame_view(original_request, original_observed,
        now_s=200.7008, execution_offset_s=100) is None


def test_view_contains_middle_probe_envelope_and_rejects_merged_uncertainty_over_1ms():
    mapper = warmed()
    # 相邻区间相交不表示整个 bracket 足够窄；中间点也必须进入最终包络。
    for sequence, host, offset in [(6, 200.3, 100.0005), (7, 200.35, 100.001),
                                    (8, 200.4, 100.0005), (9, 200.45, 100)]:
        mapper.observe(probe(sequence, host, offset=offset, rtt=.0018))
    snapshot = mapper.snapshot()
    assert snapshot.reason is None
    assert snapshot.frame_view(200.325, 200.423, now_s=200.4518, execution_offset_s=100) is None


@pytest.mark.parametrize("fields", [dict(run_id=""), dict(run_id="bad\nrun"), dict(generation=0),
    dict(pid=True), dict(pid=2**32), dict(start_ticks=-1), dict(realtime_id=1), dict(monotonic_id=True)])
def test_identity_constructor_rejects_invalid_production_seam_inputs(fields):
    with pytest.raises(ValueError):
        replace(IDENTITY, **fields)


@pytest.mark.parametrize("sequence", [True, 0, -1, 2**64, 2**80])
def test_invalid_wire_sequence_does_not_poison_next_valid_sequence(sequence):
    mapper = warmed()
    assert not mapper.observe(replace(probe(6, 200.3), sequence=sequence))
    assert mapper.observe(probe(6, 200.3))
    assert mapper.snapshot().probes[-1].sequence == 6


def test_commit_quantization_enclosure_requires_full_bracket_and_counts_toward_1ms():
    mapper = warmed()
    snapshot = mapper.snapshot()
    left = snapshot.probes[2]
    assert snapshot.commit_view(left.device_send_s + .000001, now_s=200.2508,
        execution_offset_s=100) is not None
    assert snapshot.commit_view(left.device_send_s + .000001, now_s=200.2508,
        execution_offset_s=100, device_uncertainty_s=JLOG_DEVICE_UNCERTAINTY_S) is None
    device = 100.1754
    point = snapshot.commit_view(device, now_s=200.2508, execution_offset_s=100)
    view = snapshot.commit_view(device, now_s=200.2508, execution_offset_s=100,
                                device_uncertainty_s=JLOG_DEVICE_UNCERTAINTY_S)
    assert view.uncertainty_ms >= point.uncertainty_ms + .002
    lower, upper = view.device_to_host(device)
    point_lower, point_upper = point.device_to_host(device)
    assert lower < point_lower and upper > point_upper
    for n in range(6, 10):
        mapper.observe(probe(n, 200 + n * .05, rtt=.001979))
    latest = mapper.snapshot()
    device = 100.4254
    assert latest.commit_view(device, now_s=200.452, execution_offset_s=100) is not None
    assert latest.commit_view(device, now_s=200.452, execution_offset_s=100,
                              device_uncertainty_s=JLOG_DEVICE_UNCERTAINTY_S) is None


def test_jlog_enclosure_at_real_epoch_never_rounds_inside_required_2us():
    device = 1792964107.123456
    lower, upper = device_time_enclosure(device, JLOG_DEVICE_UNCERTAINTY_S)
    assert device - lower >= .000002 and upper - device >= .000002
