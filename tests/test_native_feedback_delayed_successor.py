import pytest

from agent.realtime.native_clocked_feedback import ClockedFeedbackEvidence
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.timing_feedback import TimingFeedback
from test_native_clock_domains import fixture
from test_native_clocked_feedback import advance
from test_native_life_feedback import _feedback_image


def pending(tmp_path, monkeypatch, *, budget=None, target=200.4254):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(target)
    backend._refresh_observation_views()
    stream = ClockedFeedbackEvidence(backend)
    if budget is not None:
        monkeypatch.setattr(stream, 'MAX_PENDING_S', budget)
    assert stream.enqueue(dict(frame_sequence=1, request_s=200.455), 200.485, now[0],
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100) == 'clock-attribution-pending'
    return backend, mapper, now, probe, commit, stream


@pytest.mark.parametrize('old_budget', [True, False])
def test_real_431ms_successor_closes_original_capture_only_with_500ms_budget(tmp_path, monkeypatch, old_budget):
    backend, mapper, now, probe, commit, stream = pending(
        tmp_path, monkeypatch, budget=.28 if old_budget else None)
    assert not stream.drain(now[0])
    original, = stream.frames
    now[0] = 200.856
    commit(200.8554)
    advance(backend, mapper, now, probe, 10, 18)
    decision, = stream.drain(now[0])
    assert decision.reason == ('attribution-expired' if old_budget else 'qualified')
    assert decision.frame is original
    assert (decision.frame.request_s, decision.frame.upper_s, decision.frame.observed_s) == (
        200.455, 200.485, 200.5008)
    if not old_budget:
        assert decision.target.action_token == 1
        assert decision.target.host_lower_s <= 200.4254 <= decision.target.host_upper_s
        assert list(stream.consumed_tokens) == [1]
        assert not stream.drain(now[0])


@pytest.mark.parametrize('probe_end, reason', [(19, 'qualified'), (21, 'attribution-expired')])
def test_500ms_receipt_wait_uses_original_frame_instead_of_target_age(tmp_path, monkeypatch, probe_end, reason):
    backend, mapper, now, probe, commit, stream = pending(tmp_path, monkeypatch)
    assert not stream.drain(now[0])
    original, = stream.frames
    now[0] = 200.906
    commit(200.9054)
    advance(backend, mapper, now, probe, 10, probe_end)
    decision, = stream.drain(now[0])
    assert decision.reason == reason
    assert decision.frame is original
    # 两分支距目标均超过500ms；450ms原图仍有效，550ms原图必须过期。
    assert now[0] - 200.4254 > .5
    assert now[0] - original.observed_s == pytest.approx(.45 if probe_end == 19 else .55)
    if reason == 'qualified':
        assert list(stream.consumed_tokens) == [1]
    else:
        assert not stream.consumed_tokens
    assert not stream.frames
    assert not stream.drain(now[0])


def test_longer_receipt_wait_does_not_expand_original_200ms_target_window(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, stream = pending(tmp_path, monkeypatch, target=200.2254)
    decision, = stream.drain(now[0])
    assert decision.reason == 'ticket-expired'
    assert not stream.frames and not stream.drain(now[0])


@pytest.mark.parametrize('fault', ['earliest-unmapped', 'epoch'])
def test_delayed_closure_never_skips_earliest_unknown_successor_or_rekeys_frame(tmp_path, monkeypatch, fault):
    backend, mapper, now, probe, commit, stream = pending(tmp_path, monkeypatch)
    assert not stream.drain(now[0])
    if fault == 'earliest-unmapped':
        now[0] = 200.501
        commit(200.4504, kind='down', command='d')
    else:
        mapper.invalidate('high-rtt')
    now[0] = 200.856
    commit(200.8554)
    advance(backend, mapper, now, probe, 10, 18)
    decisions = stream.drain(now[0])
    assert all(row.reason != 'qualified' for row in decisions)
    assert not stream.consumed_tokens
    if fault == 'earliest-unmapped':
        assert stream.frames
    else:
        assert decisions and not stream.frames


def test_opposite_colour_removes_delayed_old_colour_instead_of_reviving_it(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, stream = pending(tmp_path, monkeypatch)
    assert not stream.drain(now[0])
    stream.enqueue(dict(frame_sequence=2, request_s=200.455), 200.485, now[0],
        TimingFeedback.SLOW, {TimingFeedback.SLOW}, 100, 0)
    now[0] = 200.856
    commit(200.8554)
    advance(backend, mapper, now, probe, 10, 18)
    decision, = stream.drain(now[0])
    assert decision.reason == 'qualified' and decision.frame.kind is TimingFeedback.SLOW
    assert stream.contradicted_frames == 1 and list(stream.consumed_tokens) == [1]


def test_stopped_sampler_discards_pending_capture_before_delayed_successor(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _ = pending(tmp_path, monkeypatch)
    sampler = NativeLifeFeedback(backend)
    sampler.observe_scene(now[0], visible=True)
    assert sampler.fresh(dict(sequence=1, request_s=200.455,
        first_done_observed_s=200.485, reused=False), now[0])
    sampler.observe_frame(_feedback_image(TimingFeedback.FAST), now[0])
    assert sampler._clocked.frames
    sampler.close(now[0], 'stopped')
    now[0] = 200.856
    commit(200.8554)
    advance(backend, mapper, now, probe, 10, 18)
    sampler.capture_due(now[0])
    assert not sampler._clocked.frames and not sampler.votes
    assert sampler.summary['accepted_requests'] == 0


def test_later_expired_capture_cannot_consume_earlier_frame_waiting_for_real_successor(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, stream = pending(tmp_path, monkeypatch)
    assert not stream.drain(now[0])
    original, = stream.frames
    advance(backend, mapper, now, probe, 10, 14)
    stream.enqueue(dict(frame_sequence=2, request_s=200.655), 200.685, now[0],
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    rejected, = stream.drain(now[0])
    assert rejected.reason == 'ticket-expired'
    assert list(stream.frames) == [original] and not stream.consumed_tokens
    assert not stream.drain(now[0])
    now[0] = 200.856
    commit(200.8554)
    advance(backend, mapper, now, probe, 14, 18)
    qualified, = stream.drain(now[0])
    assert qualified.reason == 'qualified' and qualified.frame is original
    assert list(stream.consumed_tokens) == [1]


def test_overwritten_capture_cannot_reserve_target_against_earlier_valid_capture(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    commit(200.4254)
    now[0] = 200.526
    commit(200.5254, kind='down', command='d')
    advance(backend, mapper, now, probe, 10, 12)
    stream = ClockedFeedbackEvidence(backend)
    stream.enqueue(dict(frame_sequence=2, request_s=200.555), 200.585, now[0],
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    rejected, = stream.drain(now[0])
    assert rejected.reason == 'new-judgement-overwrote-ticket' and not stream.consumed_tokens
    # 两张图各自保留原采样时刻；拒绝较晚图不能取得该输入事件的消费权。
    stream.enqueue(dict(frame_sequence=1, request_s=200.455), 200.485, 200.5008,
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 100)
    qualified, = stream.drain(now[0])
    assert qualified.reason == 'qualified' and qualified.frame.sequence == 1
    assert list(stream.consumed_tokens) == [1]


@pytest.mark.parametrize('signals', [{TimingFeedback.FAST}, {TimingFeedback.FAST, TimingFeedback.SLOW}])
def test_only_first_qualified_signalled_capture_consumes_same_target_even_if_mixed(tmp_path, monkeypatch, signals):
    backend, mapper, now, probe, commit, stream = pending(tmp_path, monkeypatch)
    stream.frames.clear()
    for sequence in (1, 2):
        stream.enqueue(dict(frame_sequence=sequence, request_s=200.455), 200.485, now[0],
            TimingFeedback.FAST if len(signals) == 1 else None, signals, 100, 100)
    now[0] = 200.856
    commit(200.8554)
    advance(backend, mapper, now, probe, 10, 18)
    first, second = stream.drain(now[0])
    assert first.reason == 'qualified' and second.reason == 'no-rendered-target'
    assert list(stream.consumed_tokens) == [1]
