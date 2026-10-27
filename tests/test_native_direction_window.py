import json
import os
from pathlib import Path

import numpy as np

import pytest

from agent.realtime.native_visual_refresh import DirectionWindowDetector


def sample(seq,area,*,stamp=None,kind='fast',v=255):
    return dict(frame_sequence=seq,observed_s=seq*.05 if stamp is None else stamp,
        request_s=seq*.05-.015,capture_upper_s=seq*.05-.005,kind=kind,
        slow=area if kind=='slow' else 0,fast=area if kind=='fast' else 0,patch_v=[v,v])


def test_one_contraction_attack_and_three_real_direction_frames_form_one_window():
    detector=DirectionWindowDetector()
    rows=[sample(i,a) for i,a in enumerate((2400,1200,2400,2400,2400,2400),1)]
    results=[out for row in rows if (out:=detector.observe(row)) is not None]
    assert len(results)==1
    result=results[0]
    assert [r['frame_sequence'] for r in result['frames']]==[3,4,5]
    assert result['witness']['peak']['frame_sequence']==1
    assert result['witness']['valley']['frame_sequence']==2
    assert result['witness']['attack']['frame_sequence']==3
    assert result['unit']=='qualified-direction-window'
    json.dumps(result,allow_nan=False)


def test_local_rejection_never_updates_sealed_history_but_breaks_confirmation_chain():
    detector=DirectionWindowDetector()
    detector.observe(sample(1,2400))
    before=(detector.peak,detector.valley,detector.last_good)
    detector.reject(sample(2,0),'clock-or-execution-fence')
    assert (detector.peak,detector.valley,detector.last_good)==before
    detector.observe(sample(3,1200))
    assert detector.observe(sample(4,2400)) is None
    detector.observe(sample(5,2400))
    detector.reject(sample(6,2400),'clock-or-execution-fence')
    assert detector.observe(sample(7,2400)) is None
    assert detector.observe(sample(8,2400)) is None


@pytest.mark.parametrize('sequence',[9,10])
def test_duplicate_or_reversed_eligible_sequence_is_dropped_never_reseeds_peak(sequence):
    detector=DirectionWindowDetector()
    detector.observe(sample(10,2400))
    assert detector.observe(sample(sequence,1200)) is None
    assert detector.peak is None and detector.last_good is None
    assert detector.observe(sample(11,2400)) is None
    assert detector.observe(sample(12,2400)) is None
    assert detector.observe(sample(13,2400)) is None


def test_monotonic_growth_constant_and_fade_do_not_supply_new_attack():
    for areas in ((1000,1400,1800,2200),(2400,2400,2400,2400),(2400,2000,1600,1200)):
        detector=DirectionWindowDetector()
        assert not [out for i,a in enumerate(areas,1) if (out:=detector.observe(sample(i,a)))]


def test_local_eligible_gap_over_200ms_and_hard_reset_never_borrow_old_peak():
    for hard in (False,True):
        detector=DirectionWindowDetector()
        detector.observe(sample(1,2400))
        if hard:detector.reset()
        assert detector.observe(sample(2,1200,stamp=.251)) is None
        assert detector.observe(sample(3,2400,stamp=.3)) is None


def test_single_blank_or_reverse_only_seeds_cannot_invent_an_attack():
    for kind in (None,'slow'):
        detector=DirectionWindowDetector()
        detector.observe(sample(1,2400))
        detector.observe(sample(2,0,kind=kind))
        assert detector.observe(sample(3,2400)) is None
        assert detector.observe(sample(4,2400)) is None
        assert detector.observe(sample(5,2400)) is None


def test_constant_old_label_after_effect_reset_needs_a_new_witness():
    detector=DirectionWindowDetector()
    for i,a in enumerate((2400,1200,2400,2400,2400),1):detector.observe(sample(i,a))
    detector.reset()
    assert not [out for i in (6,7,8,9) if (out:=detector.observe(sample(i,2400)))]


def test_real_component_monotonic_single_animation_must_not_request_three_vote_correction(tmp_path,monkeypatch):
    import test_native_visual_refresh as helper
    from agent.realtime.native_visual_refresh import VisualRefreshDetector
    backend,_,_,sampler,emit,_=helper.setup(tmp_path,monkeypatch)
    def animation(value,step):
        image=np.zeros((720,1280,3),np.uint8)
        image[530:544,590:690]=(240,120,0)
        image[514:526,570:590+step*10]=(240,120,0)
        for x1,y1,x2,y2 in VisualRefreshDetector.PATCHES:image[y1:y2,x1:x2]=(value,value//2,0)
        return image
    pictures={value:animation(value,step) for step,value in enumerate((100,140,180,220))}
    monkeypatch.setattr(helper,'image',lambda value:pictures[value])
    for seq,value in enumerate((100,140,180,220),1):emit(seq,value)
    assert not backend._phase_slot
    assert sampler.summary['accepted_requests']==0


def test_optional_original_images_and_metadata_cross_local_rejects_through_real_components(tmp_path,monkeypatch):
    import cv2
    import test_native_visual_refresh as helper
    from agent.realtime.native_observation_clock import ObservationProbe
    fixture=os.environ.get('MBD_TEST_DIRECTION_WINDOW_FIXTURE')
    if not fixture:pytest.skip('未显式提供方向窗原图fixture')
    base=Path(fixture)
    source=base/'decomposition.json'
    recording=base/'recording'
    if not source.is_file():pytest.skip('外部方向窗fixture缺少元数据')
    # 外部fixture只提供一个已脱敏窗口，按相对帧号断言，不依赖本机运行身份或结果文件。
    rows=json.loads(source.read_text(encoding='utf8'))['rows']
    if len(rows)!=13:pytest.skip('外部方向窗fixture需要完整的十三帧窗口')
    origin=rows[0]['sequence']
    rows=[dict(row,sequence=row['sequence']-origin+1) for row in rows]
    if any(not r.get('png') or not (recording/r['png']).is_file() for r in rows if not r['category'].startswith('gate:')):
        pytest.skip('真实eligible ROI原件不完整')
    backend,mapper,now,sampler,_,_=helper.setup(tmp_path,monkeypatch)
    # 避开probe接收端点的浮点相消；统一平移保留全部原图年龄及帧间隔。
    offset=now[0]+.01-rows[0]['raw']['observed_s']
    rejected={round(r['raw']['request_s']+offset,9) for r in rows if r['category'].startswith('gate:')}
    original=sampler._visual_refresh._support
    monkeypatch.setattr(sampler._visual_refresh,'_support',lambda snap,effect,request,stamp:
        None if round(request,9) in rejected else original(snap,effect,request,stamp))
    identity=mapper.snapshot().identity
    events=[(200+i*.05+.0008,'probe',i) for i in range(11,25)]
    events += [(r['raw']['observed_s']+offset,'frame',r) for r in rows]
    for stamp,kind,payload in sorted(events,key=lambda value:value[0]):
        now[0]=stamp
        if kind=='probe':
            host=200+payload*.05;device=host+.0004-100
            mapper.observe(ObservationProbe(identity,payload,host,host+.0008,device,device,device-1,device-1))
            backend._refresh_observation_views()
            sampler.capture_due(stamp)
            continue
        row=payload;raw=row['raw']
        metadata=dict(sequence=row['sequence'],request_s=raw['request_s']+offset,
            first_done_observed_s=raw['capture_upper_s']+offset,reused=False)
        if row['category'].startswith('gate:'):
            picture=np.zeros((720,1280,3),np.uint8)
        else:
            roi=cv2.imdecode(np.fromfile(recording/row['png'],dtype=np.uint8),cv2.IMREAD_COLOR)
            picture=np.zeros((720,1280,3),np.uint8);picture[514:556,570:710]=roi
        sampler.refresh_phase_status(stamp)
        assert sampler.fresh(metadata,stamp)
        sampler.observe_scene(stamp,visible=True)
        sampler.observe_frame(picture,stamp)
    windows=[r for r in sampler._visual_refresh.metrics if r['event']=='qualified-direction-window']
    assert len(windows)==1,[(r.get('frame_sequence'),r.get('event'),r.get('reason'),r.get('slow'),r.get('fast'))
                          for r in sampler._visual_refresh.metrics]
    window=windows[0]
    assert [r['frame_sequence'] for r in window['frames']]==[11,12,13]
    assert window['witness']['peak']['frame_sequence']==1
    assert window['witness']['valley']['frame_sequence']==10
    assert sampler.summary['accepted_requests']==1 and backend._phase_slot[3]==-15.
    assert backend._phase_slot[4]==window['current']['observed_s']+2.


def test_roi_failure_disables_only_roi_and_does_not_prevent_direction_request(tmp_path,monkeypatch):
    import test_native_visual_refresh as helper
    backend,_,_,sampler,emit,_=helper.setup(tmp_path,monkeypatch)
    original=helper.image
    class CopyFailure(np.ndarray):
        def copy(self,*args,**kwargs):raise OSError('roi unavailable')
    monkeypatch.setattr(helper,'image',lambda value:original(value).view(CopyFailure))
    for seq,value in enumerate((240,170,240,240,240),1):emit(seq,value)
    assert sampler.summary['accepted_requests']==1 and backend._phase_slot[3]==15.
    assert sampler._visual_refresh.disabled_reason is None
    assert sampler._visual_refresh.roi_disabled_reason=='OSError'


def test_cached_roi_is_immutable_original_not_current_frame_and_cache_is_bounded(tmp_path,monkeypatch):
    import test_native_visual_refresh as helper
    _,_,_,sampler,emit,_=helper.setup(tmp_path,monkeypatch)
    pictures=[helper.image(value) for value in (240,170,240,240,240)]
    stream=iter(pictures)
    monkeypatch.setattr(helper,'image',lambda value:next(stream))
    emit(1,240)
    pictures[0][514:556,570:710]=0
    for seq,value in enumerate((170,240,240,240),2):emit(seq,value)
    evidence=sampler.take_roi_evidence()
    peak=next(row for row in evidence if row['metadata']['sequence']==1)
    assert peak['metadata']['evidence_roles']==['raw-direction-onset']
    assert peak['metadata']['sequence']==1 and peak['timestamp']<sampler.frame_evidence['observed_s']
    assert peak['image'][0,0,0]==240 and not peak['image'].flags.writeable
    observer=sampler._visual_refresh
    for seq in range(100,250):
        observer._cache_roi(pictures[1],sample(seq,1200))
    assert len(observer._roi_cache)==observer.ROI_CAPACITY
    assert observer.report()['roi_evidence']['total_capacity']==256


def test_new_witness_after_cooldown_is_not_stuck_by_previously_discarded_window(tmp_path,monkeypatch):
    from test_native_visual_refresh import setup
    backend,_,now,sampler,emit,_=setup(tmp_path,monkeypatch)
    for seq,value in enumerate([240,170,240,240],1):emit(seq,value)
    assert len(sampler._visual_refresh.detector.frames)==2
    backend._phase_lock.acquire()
    try:
        emit(5,240)
    finally:
        backend._phase_lock.release()
    assert sampler.summary['requests']==1
    assert sampler.summary['rejected_requests']==1
    assert backend._phase_slot is None
    assert sampler.ignored_reasons['request-rejected:owner-lock-busy']==1
    first_attempt=sampler._last_submit_s
    for seq,value in enumerate([240,170,240,240,240],6):emit(seq,value)
    assert now[0]-first_attempt==__import__('pytest').approx(.25)
    assert sampler.ignored_reasons['request-cooldown']==1
    assert backend._phase_slot is None
    assert not sampler._visual_refresh.detector.submitted
    for seq,value in enumerate([240,170,240,240,240],11):emit(seq,value)
    assert now[0]-first_attempt==__import__('pytest').approx(.5)
    print('REAL_LIFECYCLE',dict(sampler.summary),'ignored',dict(sampler.ignored_reasons),
          'submitted',sampler._visual_refresh.detector.submitted,'phase',backend.phase_status_snapshot())
    assert sampler.summary['requests']==2
    assert sampler.summary['accepted_requests']==1
    assert backend._phase_slot is not None
    assert backend._phase_slot[2]==now[0]
    assert sampler.summary['discarded_direction_windows']==1
    windows=[r for r in sampler._visual_refresh.metrics if r['event']=='qualified-direction-window']
    assert [r['window_id'] for r in windows]==[1,2,3]
    assert [f['frame_sequence'] for f in windows[-1]['frames']]==[13,14,15]

def test_periodic_audit_uses_only_eligible_original_sample_time_and_no_window(tmp_path,monkeypatch):
    from test_native_visual_refresh import setup
    _,_,now,sampler,emit,_=setup(tmp_path,monkeypatch)
    for seq in range(1,113):emit(seq,240)
    observer=sampler._visual_refresh
    rows=tuple(row for row in sampler.take_roi_evidence() if 'periodic-audit' in row['metadata']['evidence_roles'])
    assert len(rows)==1 and rows[0]['metadata']['evidence_roles']==['periodic-audit']
    row=rows[0]
    assert row['metadata']['control_window_id'] is None
    assert row['timestamp']==row['metadata']['observed_s']
    assert row['metadata']['capture_upper_s']<row['timestamp']
    assert observer.sequence==0 and sampler.summary['requests']==0
    assert row['timestamp']-observer.report()['roi_evidence']['periodic_first_observed_s']==0
    before=observer._periodic_anchor_s
    for _ in range(3):observer.drain(now[0]+.01)
    assert observer._periodic_anchor_s==before and not sampler.take_roi_evidence()
    monkeypatch.setattr(observer,'_support',lambda *args:None)
    for seq in range(113,230):emit(seq,240)
    assert observer._periodic_anchor_s==before and not sampler.take_roi_evidence()


def test_periodic_audit_caps_queue_without_recording_and_deduplicates_sequence(tmp_path,monkeypatch):
    from test_native_visual_refresh import setup,image
    _,_,_,sampler,_,_=setup(tmp_path,monkeypatch)
    observer=sampler._visual_refresh
    for seq in range(1,25):
        row=sample(seq,1000,stamp=seq*5.)
        observer._cache_roi(image(240),row)
        observer._queue_periodic_roi(row)
    assert len(observer._roi_ready)==16 and observer.periodic_roi_dropped==7
    assert observer.periodic_roi_queued==16
    assert len(observer._roi_cache)<=128
    row=sample(100,1000,stamp=125.)
    observer._cache_roi(image(240),row);observer._roi_seen.add(100)
    observer._queue_periodic_roi(row)
    assert observer.periodic_roi_queued==16
    assert observer.report()['roi_evidence']['periodic_last_observed_s']==85.
