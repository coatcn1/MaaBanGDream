from types import SimpleNamespace
import math
from dataclasses import replace

import pytest

from agent.realtime.native_clocked_feedback import ClockedFeedbackEvidence
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.timing_feedback import TimingFeedback
from test_native_clock_domains import fixture
from test_native_life_feedback import _feedback_image


def advance(backend, mapper, now, probe, last, end):
    for sequence in range(last + 1, end + 1):
        mapper.observe(probe(sequence, 200 + sequence * .05))
    now[0] = 200 + end * .05 + .0008
    backend._refresh_observation_views()


@pytest.mark.parametrize('fault', [None, 'old-exposure', 'pending-boundary', 'pending-expired',
                                  'covered', 'missing-successor', 'epoch', 'stop'])
def test_sparse_real_successor_budget_starts_at_original_frame_observation(tmp_path, monkeypatch, fault):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    target = 200.4254
    commit(target)
    advance(backend, mapper, now, probe, 10, 12)
    stream = ClockedFeedbackEvidence(backend)
    request, upper, observed = target + .183074, target + .189344, target + .203882
    if fault == 'old-exposure':
        upper = target + .201
    now[0] = observed
    assert stream.enqueue(dict(frame_sequence=1051, request_s=request), upper, observed,
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100) == 'clock-attribution-pending'
    initial = stream.drain(now[0])
    assert not initial
    # 原图尚未获得右侧probe时，仍只等待自身的固定预算。
    now[0] = target + .584
    if fault != 'missing-successor':
        commit(target + (.18 if fault == 'covered' else .582847), kind='down', command='d')
    advance(backend, mapper, now, probe, 12, 23 if fault == 'pending-expired' else 22 if fault == 'pending-boundary' else 21)
    if fault == 'pending-boundary':
        now[0] = observed + .5
    if fault == 'epoch':
        mapper.invalidate('test-epoch')
    elif fault == 'stop':
        sampler = NativeLifeFeedback(backend)
        sampler._clocked = stream
        sampler.close(now[0], 'stopped')
    decisions = stream.drain(now[0])
    if fault == 'stop':
        assert not decisions and not stream.frames and not stream.consumed_tokens
    elif fault == 'missing-successor':
        assert not decisions and stream.frames and not stream.consumed_tokens
        assert stream._pending_deadline_s == pytest.approx(observed + .5)
    elif fault in {None, 'pending-boundary'}:
        decision, = decisions
        assert decision.reason == 'qualified'
        assert decision.frame.observed_s == observed and decision.frame.upper_s == upper
        assert now[0] - decision.target.host_lower_s > .5
        assert now[0] - observed <= .5
    elif fault == 'old-exposure':
        assert decisions[0].reason == 'ticket-expired'
    else:
        assert decisions and all(row.reason != 'qualified' for row in decisions)


@pytest.mark.parametrize('over', [0., .000001])
def test_original_exposure_200ms_boundary_is_unchanged(tmp_path, monkeypatch, over):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    backend._refresh_observation_views()
    now[0] = 201.01
    commit(201.008247, kind='down', command='d')
    advance(backend, mapper, now, probe, 10, 21)
    stream = ClockedFeedbackEvidence(backend)
    mapped = backend.mapped_execution_commit_snapshot(now[0])
    lower = mapped[0].host_lower_s
    stream.enqueue(dict(frame_sequence=1, request_s=lower + .18), lower + .2, lower + .22,
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    frame = replace(stream.frames[0], upper_s=lower + .2 + over)
    _, reason = stream._attribute(frame, backend.execution_commit_snapshot(), mapped, now[0])
    # 使用同一宿主浮点区间，边界以下仍合法，越过1us必须拒绝。
    if over:
        assert reason == 'ticket-expired'
    else:
        assert reason == 'qualified'


@pytest.mark.parametrize('shift', [-.103, .103])
def test_original_fresh_frame_waits_for_real_successor_without_future_p_translation(tmp_path, monkeypatch, shift):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch, shift=shift)
    commit(200.4254)
    backend._refresh_observation_views()
    stream = ClockedFeedbackEvidence(backend)
    assert stream.enqueue(dict(frame_sequence=1, request_s=200.455), 200.485, now[0],
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100) == 'clock-attribution-pending'
    # 未来P即使看似会覆盖或绝不覆盖，也不能代替真实H后继凭据。
    backend._feedback_tickets.publish_receipts([dict(action_token=999, planned_engine_s=200.456 - shift,
        action_kind='tap')], published_until_s=201.)
    assert not stream.drain(now[0])
    now[0] = 200.526
    commit(200.5254)
    backend._refresh_observation_views()
    assert not stream.drain(now[0])
    advance(backend, mapper, now, probe, 10, 11)
    decision, = stream.drain(now[0])
    assert decision.reason == 'qualified' and decision.target.action_token == 1
    assert decision.frame.observed_s == pytest.approx(200.5008)
    assert decision.frame.request_s == 200.455 and decision.frame.upper_s == 200.485
    assert decision.target.actual_s + shift == pytest.approx(200.4254)
    assert decision.target.host_lower_s <= 200.4254 <= decision.target.host_upper_s


@pytest.mark.parametrize('problem', ['covered', 'earliest-covered', 'earliest-unmapped', 'release',
                                    'residual', 'render', 'expired', 'clock-epoch'])
def test_full_interval_render_successor_release_and_ttl_boundaries_reject_unproven_direction(tmp_path, monkeypatch, problem):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch,
        residual=.05 if problem == 'residual' else .002)
    if problem == 'release':
        commit(200.4104, kind='up', command='u')
    commit(200.4254)
    backend._refresh_observation_views()
    stream = ClockedFeedbackEvidence(backend)
    request = 200.445 if problem == 'render' else 200.455
    stream.enqueue(dict(frame_sequence=1, request_s=request), 200.485, now[0],
                   TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    initial = stream.drain(now[0])
    if initial:
        assert all(row.reason != 'qualified' for row in initial)
        return
    if problem in {'covered', 'earliest-covered'}:
        now[0] = 200.501
        commit(200.4804, kind='down', command='d')
    elif problem == 'earliest-unmapped':
        # 位于probe自身设备采样时刻的±2us事件不能被双侧bracket，不能跳过它找更晚后继。
        now[0] = 200.501
        commit(200.4504, kind='down', command='d')
    now[0] = 200.526
    commit(200.5254)
    if problem == 'clock-epoch':
        mapper.invalidate('high-rtt')
    advance(backend, mapper, now, probe, 10, 21 if problem == 'expired' else 15 if problem == 'clock-epoch' else 11)
    decisions = stream.drain(now[0])
    assert all(row.reason != 'qualified' for row in decisions)
    if problem == 'earliest-unmapped':
        assert stream.frames and not stream.consumed_tokens


def test_release_after_original_frame_does_not_change_targets_frozen_release_guard(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    backend._refresh_observation_views()
    stream = ClockedFeedbackEvidence(backend)
    stream.enqueue(dict(frame_sequence=1, request_s=200.455), 200.485, now[0],
                   TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    assert not stream.drain(now[0])
    now[0] = 200.526
    commit(200.5254, kind='up', command='u')
    advance(backend, mapper, now, probe, 10, 11)
    decision, = stream.drain(now[0])
    assert decision.reason == 'qualified'
    assert backend._feedback_release_host_upper_s > decision.frame.upper_s


def test_80ms_acquisition_age_is_never_reset_and_finite_queue_discards_old_frames(tmp_path, monkeypatch):
    backend, _, now, _, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    backend._refresh_observation_views()
    stream = ClockedFeedbackEvidence(backend)
    assert stream.enqueue(dict(frame_sequence=1, request_s=now[0] - .081), now[0], now[0],
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100) == 'slow-or-reused-frames'
    assert not stream.frames
    for sequence in range(30):
        stream.enqueue(dict(frame_sequence=sequence, request_s=200.455), 200.485, now[0],
            TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    assert len(stream.frames) == stream.CAPACITY and stream.dropped == 14


def test_fresh_opposite_colour_discards_old_pending_colour_before_delayed_attribution(tmp_path, monkeypatch):
    backend, _, now, _, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    backend._refresh_observation_views()
    stream = ClockedFeedbackEvidence(backend)
    for sequence in (1,2,3):
        stream.enqueue(dict(frame_sequence=sequence, request_s=200.455), 200.485, now[0],
            TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    stream.enqueue(dict(frame_sequence=4, request_s=200.455), 200.485, now[0],
        TimingFeedback.SLOW, {TimingFeedback.SLOW}, 100, 0)
    assert len(stream.frames) == 1 and stream.contradicted_frames == 3


def test_capture_hint_uses_accepted_rx_upper_without_waiting_for_clock_view_or_scheduling_old_targets(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    sampler = NativeLifeFeedback(backend)
    assert sampler.capture_due(now[0]) is None
    now[0] = 200.501
    commit(200.5005)
    backend._refresh_observation_views()
    assert not backend.mapped_execution_commit_snapshot()
    due = sampler.capture_due(now[0])
    assert due == pytest.approx(200.5005 + .0005 + .027)
    assert not sampler.capture_request_due(now[0])
    now[0] = due
    assert sampler.capture_request_due(now[0])
    sampler.capture_requested(now[0])
    second = sampler.capture_due(now[0])
    assert second >= now[0] + .03
    now[0] = second
    sampler.capture_requested(now[0])
    assert sampler.capture_due(now[0]) is None
    # 不能为补旧TAP的次数，在较新UP已经执行后回头调度旧图。
    now[0] += .001
    commit(now[0] - .0005, kind='up', command='u')
    assert sampler.capture_due(now[0]) is None


@pytest.mark.parametrize('part', ['target', 'successor'])
@pytest.mark.parametrize('field', ['identity', 'epoch', 'trust'])
def test_target_and_earliest_successor_require_frames_complete_clock_key(tmp_path, monkeypatch, part, field):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    now[0] = 200.526
    commit(200.5254)
    advance(backend, mapper, now, probe, 10, 11)
    stream = ClockedFeedbackEvidence(backend)
    # 原图在首次到达时已经新鲜；延迟取证绝不重写这两个H时刻。
    stream.enqueue(dict(frame_sequence=1, request_s=200.455), 200.485, 200.5008,
                   TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    original = backend.mapped_execution_commit_snapshot
    def forged(_now=None):
        rows = list(original(_now))
        index = 0 if part == 'target' else 1
        changed = {'clock_identity': replace(rows[index].clock_identity, pid=43)} if field == 'identity' else {
            'observation_epoch' if field == 'epoch' else 'trust_revision': 123}
        rows[index] = replace(rows[index], **changed)
        return tuple(rows)
    monkeypatch.setattr(backend, 'mapped_execution_commit_snapshot', forged)
    decision, = stream.drain(now[0])
    assert decision.reason == 'clock-cache-key-mismatch' and decision.target is None


@pytest.mark.parametrize('shift', [-.103, .103])
@pytest.mark.parametrize('width', [.03, .073])
def test_real_sampler_three_same_colour_taps_use_original_h_observation_for_request_ttl(tmp_path, monkeypatch, shift, width):
    backend, mapper, now, probe, commit, _, applied = fixture(tmp_path, monkeypatch, shift=shift)
    sampler = NativeLifeFeedback(backend)
    sampler.observe_scene(now[0], visible=True)
    commit(200.4254)
    backend._refresh_observation_views()
    probe_cursor = 10
    observed = []
    interval = .1 if width == .03 else .15
    for sequence in (1,2,3):
        target = 200.4254 + (sequence - 1) * interval
        capture_observed = target + (.0754 if width == .03 else .104)
        end = math.floor((capture_observed - 200 - .0008) / .05 + 1e-6)
        if end > probe_cursor:
            advance(backend, mapper, now, probe, probe_cursor, end)
            probe_cursor = end
        now[0] = capture_observed
        observed.append(now[0])
        sampler.capture_due(now[0])
        assert sampler.fresh(dict(sequence=sequence, request_s=target + .03,
            first_done_observed_s=target + .03 + width, reused=False), now[0])
        sampler.observe_scene(now[0], visible=True)
        sampler.observe_frame(_feedback_image(TimingFeedback.FAST), now[0])
        now[0] = target + interval + .001
        commit(target + interval)
        end = math.ceil((target + interval - 200) / .05)
        advance(backend, mapper, now, probe, probe_cursor, end)
        probe_cursor = end
        sampler.capture_due(now[0])
    assert sampler.summary['direction_votes'] == 3 and sampler.summary['accepted_requests'] == 1
    assert backend._phase_slot[3] == 15
    assert backend._phase_slot[2] == observed[-1]
    assert backend._phase_slot[4] == observed[-1] + 2
    backend._consume_phase_candidate()
    assert applied
    qualified = [row for row in sampler.sniff_records if row['decision'] in {'eligible-single-tap','requested'}]
    assert len(qualified) == 3 and all(row['attribution_delay_ms'] > 0 for row in qualified)
    assert all(row['request_age_ms'] < 80 for row in qualified)
