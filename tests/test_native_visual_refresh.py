import numpy as np
import pytest
import json
from dataclasses import replace

from agent.realtime.native_visual_refresh import VisualRefreshDetector, NativeVisualRefreshEvidence
from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.timing_feedback import TimingFeedback
from test_native_clock_domains import fixture
from test_native_clocked_feedback import advance


def image(value):
    result = np.zeros((720, 1280, 3), np.uint8)
    if value < 200:
        result[530:545, 590:690] = (value, value // 2, 0)
    else:
        result[514:556, 570:710] = (value, value // 2, 0)
    return result


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('MAABANGDREAM_NATIVE_VISUAL_REFRESH_TRIAL', '1')
    backend, mapper, now, probe, commit, _, applied = fixture(tmp_path, monkeypatch)
    for host in (200.2254, 200.3254, 200.4254):
        commit(host)
    backend._refresh_observation_views()
    sampler = NativeLifeFeedback(backend)
    cursor = [10]
    def emit(sequence, value, *, reused=False, age=.015):
        end = 10 + sequence
        if end > cursor[0]:
            advance(backend, mapper, now, probe, cursor[0], end)
            cursor[0] = end
        sampler.refresh_phase_status(now[0])
        metadata = dict(sequence=sequence, request_s=now[0] - age,
                        first_done_observed_s=now[0] - .005, reused=reused)
        if sampler.fresh(metadata, now[0]):
            sampler.observe_scene(now[0], visible=True)
            sampler.observe_frame(image(value), now[0])
        return metadata
    return backend, mapper, now, sampler, emit, applied


def test_fixed_detector_requires_same_colour_joint_rise_and_never_counts_constant_or_fade():
    detector = VisualRefreshDetector()
    def frame(sequence, value, pixels=1000, kind='fast', now=None):
        return dict(frame_sequence=sequence, observed_s=sequence * .05 if now is None else now,
                    kind=kind, slow=0, fast=pixels, patch_v=[value, value])
    assert detector.observe(frame(1, 180)) is None
    pulse = detector.observe(frame(2, 220, 1300))
    assert pulse['previous']['frame_sequence'] == 1
    assert pulse['current']['frame_sequence'] == 2
    assert detector.observe(frame(3, 220, 1300)) is None
    assert detector.observe(frame(4, 180)) is None
    assert detector.observe(frame(5, 240, 1400, kind='slow')) is None
    assert detector.observe(frame(6, 255, 1500, now=.7)) is None


def test_measurement_has_only_numbers_and_uses_fixed_bar_patches():
    image = np.zeros((720, 1280, 3), np.uint8)
    for x1, y1, x2, y2 in VisualRefreshDetector.PATCHES:
        image[y1:y2, x1:x2] = (180, 80, 0)
    measured = VisualRefreshDetector.measure(image)
    assert measured == [180., 180.]


def test_opt_in_one_attack_three_real_frames_use_original_arbiter_without_tap_double_count(tmp_path, monkeypatch):
    backend, _, now, sampler, emit, applied = setup(tmp_path, monkeypatch)
    for sequence, value in enumerate((170, 240, 170, 240, 170, 240), 1):
        # 改变色面积，模拟固定原型的条形放大，不从测试注入元数据授资格。
        emit(sequence, value)
    assert sampler.summary['requests'] == 1
    assert sampler.summary['qualified_direction_windows'] == 1
    assert sampler.summary['fresh_direction_frames'] == 3
    assert sampler.summary['visual_refresh_observations'] == 0
    assert not sampler._clocked.frames
    assert sampler.summary['single_tap_observations'] == 0
    assert backend._phase_slot[3] == 15 and backend._phase_slot[4] == now[0] + 2
    evidence = dict(sampler.frame_evidence)
    emit(7, 170)
    emit(8, 240)
    assert sampler.summary['requests'] == 1
    assert sampler.frame_evidence['frame_sequence'] == 8
    backend._consume_phase_candidate()
    assert len(applied) == 1
    events = [r for r in sampler.events if r['event'] == 'phase-request']
    assert events[0]['action_token'] is None and events[0]['direction_window_id'] == 1
    assert events[0]['frame_sequences'] == [4,5,6]
    report = sampler.sniff_report()['visual_refresh']
    assert report['independent_gr_claim'] is False
    observations = [r for r in report['records'] if r['event'] == 'qualified-direction-window']
    assert [r['window_id'] for r in observations] == [1]
    assert all(r['previous']['supporting_execution_cursor'] > 0 for r in observations)
    assert sampler._visual_refresh.fence is None or sampler._visual_refresh.fence['host_upper_s'] < 200.3
    json.dumps(report, allow_nan=False)


def test_detector_real_pixel_rise_requires_coloured_area_growth():
    detector = VisualRefreshDetector()
    sample = dict(frame_sequence=1, observed_s=1., kind='fast', slow=0, fast=1000, patch_v=[170.,170.])
    assert detector.observe(sample) is None
    assert detector.observe(dict(sample, frame_sequence=2, observed_s=1.05, patch_v=[240.,240.])) is None


@pytest.mark.parametrize('fault', ['no-execution', 'stop', 'visual-owner', 'stale-image', 'epoch', 'diagnostic'])
def test_refresh_rejection_does_not_issue_phase_or_interrupt_input(tmp_path, monkeypatch, fault):
    backend, mapper, now, sampler, emit, applied = setup(tmp_path, monkeypatch)
    if fault == 'no-execution':
        monkeypatch.setattr(backend, 'execution_commit_snapshot', lambda: ())
    elif fault == 'stop':
        backend._phase_closed = True
    elif fault == 'visual-owner':
        backend._visual_phase_guard_requested = True
    elif fault == 'epoch':
        mapper.invalidate('test-epoch')
    elif fault == 'diagnostic':
        monkeypatch.setattr(sampler._visual_refresh, '_record', lambda row: (_ for _ in ()).throw(OSError()))
    emit(1, 170, age=.081 if fault == 'stale-image' else .015)
    emit(2, 240, age=.081 if fault == 'stale-image' else .015)
    assert not backend._phase_slot and not applied and not sampler.votes
    if fault == 'diagnostic':
        assert sampler._visual_refresh.disabled_reason == 'OSError'


def test_new_mode_disabled_by_default_and_requires_all_three_gates(tmp_path, monkeypatch):
    backend, _, _, _, _, _, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.delenv('MAABANGDREAM_NATIVE_VISUAL_REFRESH_TRIAL', raising=False)
    assert NativeLifeFeedback(backend)._visual_refresh is None
    monkeypatch.setenv('MAABANGDREAM_NATIVE_VISUAL_REFRESH_TRIAL', '1')
    backend._native_clock_trial = False
    assert NativeLifeFeedback(backend)._visual_refresh is None
    backend._native_clock_trial = True
    backend.life_feedback_enabled = False
    assert NativeLifeFeedback(backend)._visual_refresh is None


def test_shared_capture_interest_keeps_original_30ms_budget_without_target_or_reused_spin(tmp_path, monkeypatch):
    backend, _, now, sampler, emit, _ = setup(tmp_path, monkeypatch)
    sampler.observe_scene(now[0], visible=True)
    assert sampler._target is None
    assert sampler.capture_request_due(now[0])
    sampler.capture_requested(now[0])
    assert sampler.capture_due(now[0]) == pytest.approx(now[0] + .03)
    assert not sampler.capture_request_due(now[0] + .029)
    assert sampler.capture_request_due(now[0] + .03)
    emit(1, 170, reused=True)
    assert not sampler.capture_request_due(now[0])
    sampler.observe_scene(now[0], visible=False)
    assert sampler.capture_due(now[0]) is None
    backend._phase_closed = True
    sampler.scene_valid = True
    assert sampler.capture_due(now[0]) is None


@pytest.mark.parametrize('executed', [False, True])
def test_effect_fence_needs_real_post_effect_receipt_and_keeps_earliest_boundary(tmp_path, monkeypatch, executed):
    backend, _, _, sampler, emit, _ = setup(tmp_path, monkeypatch)
    status = backend.phase_status_snapshot()
    effect = replace(status, state='applied', request_sequence=1, delta_ms=15., cumulative_phase_ms=15.,
        first_adjusted_due_s=200.2 if executed else 200.7,
        first_affected_chunk_sequence=1 if executed else 999,
        first_affected_action_token=1 if executed else 999)
    backend._publish_phase_status(effect, 'test-effect')
    for seq, value in enumerate((240,170,240,240,240),1):
        emit(seq,value)
    assert sampler.summary['qualified_direction_windows'] == (1 if executed else 0)
    if executed:
        window=next(row for row in sampler._visual_refresh.metrics if row['event']=='qualified-direction-window')
        assert window['frames'][0]['execution_fence']['command_cursor'] == 1
    else:
        assert sampler._visual_refresh.fence is None


def test_pending_original_frame_expires_after_500ms_and_never_changes_saved_current_metadata(tmp_path, monkeypatch):
    backend, mapper, now, sampler, emit, _ = setup(tmp_path, monkeypatch)
    # 原图upper晚于末probe，先有界等待右侧观察；不能把晚到处理时刻重写成原图时刻。
    original = dict(frame_sequence=1, request_s=200.5005)
    sampler._visual_refresh.enqueue(image(170), original, 200.5007, now[0],
        TimingFeedback.FAST, {TimingFeedback.FAST}, 0, 1500)
    sampler._visual_refresh.drain(now[0])
    assert sampler._visual_refresh.pending
    sample = sampler._visual_refresh.pending[0][1]
    now[0] += .501
    # 固定健康检查以隔离等待预算；原图自身已超过500ms，不能再进入映射或投票。
    monkeypatch.setattr(backend, '_observation_clock_ready', lambda s, t: True)
    assert not sampler._visual_refresh.drain(now[0])
    assert not sampler._visual_refresh.pending
    assert any(r.get('reason') == 'context-or-frame-expired' for r in sampler._visual_refresh.metrics)
    assert sample['observed_s'] == pytest.approx(200.5008)


def test_refresh_ring_is_bounded_and_current_roi_sniff_never_uses_previous_frame(tmp_path, monkeypatch):
    _, _, _, sampler, emit, _ = setup(tmp_path, monkeypatch)
    for seq,value in enumerate((240,170,240,240,240),1):emit(seq,value)
    assert not sampler.roi_worthy
    assert sampler.last_sniff['frame_sequence'] == sampler.frame_evidence['frame_sequence'] == 5
    evidence_rows=sampler.take_roi_evidence()
    assert [r['metadata']['sequence'] for r in evidence_rows]==[1,2,3,4,5]
    assert all(r['image'].shape==(42,140,3) for r in evidence_rows)
    evidence = sampler._visual_refresh
    for index in range(evidence.CAPACITY + 3):
        evidence._record(dict(event='bounded-test',index=index))
    report = evidence.report()
    assert len(report['records']) == evidence.CAPACITY and report['dropped'] >= 3
    json.dumps(report,allow_nan=False)


def test_clock_epoch_loss_clears_existing_refresh_votes(tmp_path,monkeypatch):
    _,mapper,_,sampler,emit,_=setup(tmp_path,monkeypatch)
    emit(1,240);emit(2,170);emit(3,240)
    assert len(sampler._visual_refresh.detector.frames)==1
    mapper.invalidate('test-hard-clock-loss')
    emit(4,240)
    assert not sampler.votes
    assert not sampler._visual_refresh.detector.frames


def test_mixed_colour_and_reused_frames_cannot_produce_refresh(tmp_path,monkeypatch):
    _,_,_,sampler,emit,_=setup(tmp_path,monkeypatch)
    emit(1,170)
    emit(2,240,reused=True)
    assert not sampler.votes
    detector=VisualRefreshDetector()
    sample=dict(frame_sequence=1,observed_s=1.,kind='fast',slow=0,fast=1500,patch_v=[170.,170.])
    detector.observe(sample)
    assert detector.observe(dict(sample,frame_sequence=2,observed_s=1.05,kind=None,
        slow=1000,fast=2000,patch_v=[240.,240.])) is None
    assert detector.observe(dict(sample,frame_sequence=3,observed_s=1.1,fast=2200,
        patch_v=[250.,250.])) is None


def test_both_refresh_frames_wait_full_200ms_after_cached_execution_fence(tmp_path,monkeypatch):
    backend,_,_,sampler,emit,_=setup(tmp_path,monkeypatch)
    raw=backend.execution_commit_snapshot()[-1:]
    mapped=backend.mapped_execution_commit_snapshot()[-1:]
    monkeypatch.setattr(backend,'execution_commit_snapshot',lambda:raw)
    monkeypatch.setattr(backend,'mapped_execution_commit_snapshot',lambda now=None:mapped)
    emit(1,170)
    emit(2,240)
    assert not sampler.votes
    for seq,value in enumerate((240,170,240,240,240),3):emit(seq,value)
    assert sampler.summary['qualified_direction_windows']==1
    window=next(row for row in sampler._visual_refresh.metrics if row['event']=='qualified-direction-window')
    assert window['frames'][0]['execution_fence']['host_upper_s']==mapped[0].host_upper_s


def test_stop_discards_original_frame_still_waiting_for_clock_bracket(tmp_path,monkeypatch):
    backend,_,now,sampler,_,_=setup(tmp_path,monkeypatch)
    evidence=sampler._visual_refresh
    evidence.enqueue(image(170),dict(frame_sequence=1,request_s=200.5005),200.5007,now[0],
        TimingFeedback.FAST,{TimingFeedback.FAST},0,1500)
    assert not evidence.drain(now[0]) and evidence.pending
    backend._phase_closed=True
    assert not evidence.drain(now[0]) and not evidence.pending


def test_real_backend_report_selects_refresh_model_only_from_actual_observer_report(tmp_path,monkeypatch):
    from agent.realtime import native_visual_refresh
    backend,_,_,sampler,_,_=setup(tmp_path,monkeypatch)
    assert backend.report()['life_feedback']['model']=='continuous-single-tap-observations-v5'
    original_units=backend.report()['life_feedback']['stat_units']
    assert original_units['direction_votes']=='eligible-single-tap-observations'
    assert original_units['debounced_reports']=='post-single-tap-direction-observations'
    backend._life_feedback_sniff=sampler.sniff_report()
    report=backend.report()['life_feedback']
    assert report['model']=='visual-direction-windows-v2'==native_visual_refresh.MODEL
    assert native_visual_refresh.SCHEMA_VERSION==3
    assert report['stat_units']['neutral_baseline_frames']=='qualified-fresh-frames-without-strong-direction'
    assert report['stat_units']['direction_votes']=='fresh-direction-frames'
    assert report['stat_units']['debounced_reports']=='qualified-direction-windows'
    assert report['stat_units']['single_tap_observations']=='disabled-in-direction-window-mode'
    assert report['sniff']['visual_refresh']['source_module']==native_visual_refresh.__file__
    backend._life_feedback_sniff={}
    assert backend.report()['life_feedback']['model']=='continuous-single-tap-observations-v5'
    assert backend.report()['life_feedback']['stat_units']==original_units
