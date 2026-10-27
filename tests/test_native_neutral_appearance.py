import numpy as np
import pytest
from test_native_visual_refresh import setup,image


def test_actual_three_nostrong_three_fast_requests_one_typed_window_with_original_rois(tmp_path,monkeypatch):
    import test_native_visual_refresh as helper
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch)
    pictures={1:np.full((720,1280,3),11,np.uint8),2:np.full((720,1280,3),22,np.uint8),
        3:np.full((720,1280,3),33,np.uint8),4:image(240),5:image(240),6:image(240)}
    stream=iter(pictures.values());monkeypatch.setattr(helper,'image',lambda value:next(stream))
    for seq in range(1,7):
        emit(seq,240)
        if seq==1:pictures[1][514:556,570:710]=99
    assert s.summary['accepted_requests']==1
    windows=[r for r in s._visual_refresh.metrics if r['event']=='qualified-direction-window']
    assert len(windows)==1
    w=windows[0]
    assert w['witness']['type']=='neutral-appearance'
    assert [r['frame_sequence'] for r in w['witness']['baseline_frames']]==[1,2,3]
    assert [r['frame_sequence'] for r in w['frames']]==[4,5,6]
    rois=s.take_roi_evidence();assert len(rois)==6
    baseline=[r for r in rois if 'neutral-baseline' in r['metadata']['evidence_roles']]
    assert [r['image'][0,0,0] for r in baseline]==[11,22,33]
    for r in rois:
        assert r['timestamp']==r['metadata']['observed_s']
        assert not r['image'].flags.writeable
        assert r['metadata']['sequence']==r['metadata']['frame_sequence']
    import json
    json.dumps(b.report(),allow_nan=False)


def install_component(tmp_path,monkeypatch,candidate=True):
    import test_native_visual_refresh as helper
    assert candidate
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch)
    original=helper.image
    monkeypatch.setattr(helper,'image',lambda value:np.zeros((720,1280,3),np.uint8) if value==0 else original(value))
    windows=[];queue=s._visual_refresh._queue_window_rois
    def collect(window):
        queue(window)
        windows.append(window)
    monkeypatch.setattr(s._visual_refresh,'_queue_window_rois',collect)
    return b,s,emit,windows


from agent.realtime.native_observation_clock import ObservationProbe

def test_only_two_actual_neutral_plus_many_reused_cannot_supply_third(tmp_path,monkeypatch):
    b,s,emit,windows=install_component(tmp_path,monkeypatch,True)
    emit(1,0);emit(2,0)
    for seq in range(3,8):emit(seq,0,reused=True)
    for seq in range(8,12):emit(seq,240)
    assert not windows and s.summary['requests']==0


def test_reused_cannot_renew_lastgood_or_bridge_actual_eligible_gap(tmp_path,monkeypatch):
    b,s,emit,windows=install_component(tmp_path,monkeypatch,True)
    emit(1,0);emit(2,0)
    last=s._visual_refresh.detector.last
    for seq in range(3,10):emit(seq,0,reused=True)
    assert s._visual_refresh.detector.last is last
    emit(10,0)
    for seq in range(11,15):emit(seq,240)
    assert not windows


@pytest.mark.parametrize('early_seal',[False,True])
def test_same_valid_fresh_collection_sync_or_delayed_seal(tmp_path,monkeypatch,early_seal):
    import test_native_visual_refresh as helper
    import numpy as np
    b,m,n,s,_,_=helper.setup(tmp_path,monkeypatch)
    windows=[];queue=s._visual_refresh._queue_window_rois
    def collect(window):queue(window);windows.append(window)
    monkeypatch.setattr(s._visual_refresh,"_queue_window_rois",collect);original=helper.image
    def put(seq,observed,request,upper,reused=False,strong=False):
        n[0]=max(n[0],observed);s.refresh_phase_status(n[0])
        meta=dict(sequence=seq,request_s=request,first_done_observed_s=upper,reused=reused)
        if s.fresh(meta,observed):
            s.observe_scene(observed,visible=True)
            s.observe_frame(original(240) if strong else np.zeros((720,1280,3),np.uint8),observed)
    def probe(seq,host):
        dev=host+.0004-100
        assert m.observe(ObservationProbe(m.identity,seq,host,host+.0008,dev,dev,dev-1,dev-1))
        n[0]=host+.0008;b._refresh_observation_views()
    put(1,200.5008,200.5005,200.5007)
    put(2,200.5308,200.5305,200.5307)
    assert [sample['frame_sequence'] for _,sample in s._visual_refresh.pending]==[1,2]
    if early_seal:
        probe(11,200.54);s.capture_due(n[0])
        assert [q['frame_sequence'] for q in s._visual_refresh.detector.neutral]==[1,2]
    put(3,200.5508,200.5505,200.5507,reused=True)
    if not early_seal:probe(11,200.56);s.capture_due(n[0])
    probe(12,200.61);put(4,n[0],200.5808,200.5810)
    for seq,host in [(5,200.66),(6,200.71),(7,200.76)]:
        probe(seq+8,host);put(seq,n[0],host-.0142,host-.0042,strong=True)
    assert s.summary['accepted_requests']==1 and len(windows)==1
    assert [q['frame_sequence'] for q in windows[0]['witness']['baseline_frames']]==[1,2,4]
    assert [q['observed_s'] for q in windows[0]['witness']['baseline_frames']]==[200.5008,200.5308,200.6108]
    assert [q['frame_sequence'] for q in windows[0]['frames']]==[5,6,7]


def test_old_retreat_reused_fresh_images_remain_one_request(tmp_path,monkeypatch):
    import test_native_visual_refresh as helper
    b,m,n,s,emit,_=helper.setup(tmp_path,monkeypatch);windows=[];queue=s._visual_refresh._queue_window_rois
    def collect(window):queue(window);windows.append(window)
    monkeypatch.setattr(s._visual_refresh,"_queue_window_rois",collect)
    emit(1,240);emit(2,0,reused=True);emit(3,170)
    for seq in (4,5,6):emit(seq,240)
    assert s.summary['accepted_requests']==1
    assert windows[0]['witness']['type']=='retreat-reattack'

@pytest.mark.parametrize('change',['guard-aba','foreign-effect'])
def test_original_baseline_cannot_attach_to_new_hard_context(tmp_path,monkeypatch,change):
    from dataclasses import replace
    b,s,emit,windows=install_component(tmp_path,monkeypatch,True)
    for seq in range(1,4):emit(seq,0)
    assert len(s._visual_refresh.detector.neutral)==3
    if change=='guard-aba':
        b.set_visual_phase_guard(True);b.set_visual_phase_guard(False)
    else:
        b._phase_sequence=101
        foreign=replace(b.phase_status_snapshot(),state='applied',request_sequence=101,
            original_observed_s=200.60,delta_ms=-15.,cumulative_phase_ms=-15.,owner='feedback',
            owner_cumulative_phase_ms=-15.,applied_host_s=200.65,first_adjusted_due_s=201.,
            first_affected_chunk_sequence=999,first_affected_action_token=999)
        b._phase_owner_totals['feedback']=-15.;b._session.future_phase_offset_ms=-15.
        b._publish_phase_status(foreign,'foreign-unexecuted-effect')
    for seq in range(4,8):emit(seq,240)
    assert not windows and s.summary['accepted_requests']==0

@pytest.mark.parametrize('slow,fast',[(600,600),(600,599),(599,600),(900,800)])
def test_strong_mixed_or_ambiguous_never_arms_numeric_baseline(slow,fast):
    from agent.realtime.native_visual_refresh import DirectionWindowDetector
    detector=DirectionWindowDetector()
    def sample(seq,kind=None,slow=0,fast=0):
        return dict(frame_sequence=seq,observed_s=seq*.05,kind=kind,slow=slow,fast=fast,patch_v=[255,255])
    results=[]
    for seq in range(1,4):assert detector.observe(sample(seq,slow=slow,fast=fast)) is None
    for seq in range(4,8):
        result=detector.observe(sample(seq,'fast',fast=1800))
        if result:results.append(result)
    assert not results


def test_typed_roi_queue_failure_degrades_diagnostic_not_control(tmp_path,monkeypatch):
    b,s,emit,windows=install_component(tmp_path,monkeypatch)
    class BrokenReady:
        def __len__(self):return 0
        def append(self,row):raise RuntimeError('diagnostic queue unavailable')
    s._visual_refresh._roi_ready=BrokenReady()
    for seq,value in enumerate([0,0,0,240,240,240],1):emit(seq,value)
    assert s.summary['accepted_requests']==1
    assert s._visual_refresh.roi_disabled_reason=='RuntimeError'
    assert s._visual_refresh.disabled_reason is None


def test_typed_roi_capacity_and_dedup_remain_bounded_without_recorder(tmp_path,monkeypatch):
    from copy import deepcopy
    b,s,emit,windows=install_component(tmp_path,monkeypatch)
    for seq,value in enumerate([0,0,0,240,240,240],1):emit(seq,value)
    observer=s._visual_refresh;window=windows[0]
    assert len(observer._roi_ready)==6
    observer._queue_window_rois(window)
    assert len(observer._roi_ready)==6
    for batch in range(1,30):
        clone=deepcopy(window)
        for key in ('baseline_frames',):
            for sample in clone['witness'][key]:sample['frame_sequence']+=batch*10
        for sample in clone['frames']:sample['frame_sequence']+=batch*10
        clone['witness']['attack']=clone['frames'][0]
        for sample in clone['witness']['baseline_frames']+clone['frames']:
            observer._cache_roi(image(240),sample)
        observer._queue_window_rois(clone)
    report=observer.report()['roi_evidence']
    assert report['pending']==16 and report['dropped']>0
    assert report['queued']<=256 and len(observer._roi_cache)<=128


def test_stopping_never_adds_typed_window_or_new_roi(tmp_path,monkeypatch):
    b,s,emit,windows=install_component(tmp_path,monkeypatch)
    for seq in range(1,4):emit(seq,0)
    b._phase_closed=True
    for seq in range(4,8):emit(seq,240)
    assert not windows and not s.take_roi_evidence()

def test_recorded_typed_window_and_roi_metadata_survive_reset_guard_and_new_baseline(tmp_path,monkeypatch):
    import json
    from copy import deepcopy
    b,s,emit,windows=install_component(tmp_path,monkeypatch)
    for seq,value in enumerate([0,0,0,240,240,240],1):emit(seq,value)
    window=windows[0]
    snapshot=json.loads(json.dumps(window,allow_nan=False))
    rois=s.take_roi_evidence()
    metadata=json.loads(json.dumps([row['metadata'] for row in rois],allow_nan=False))
    pixels=[row['image'].copy() for row in rois]
    detector=s._visual_refresh.detector
    detector.reset()
    b.set_visual_phase_guard(True);b.set_visual_phase_guard(False)
    for index in range(3):
        sample=deepcopy(snapshot['witness']['baseline_frames'][index])
        sample['frame_sequence']+=100;sample['observed_s']+=2
        detector.observe(sample)
    assert len(detector.neutral)==3
    assert json.loads(json.dumps(window,allow_nan=False))==snapshot
    assert [q['frame_sequence'] for q in snapshot['witness']['baseline_frames']]==[1,2,3]
    assert [q['frame_sequence'] for q in snapshot['frames']]==[4,5,6]
    assert json.loads(json.dumps([row['metadata'] for row in rois],allow_nan=False))==metadata
    assert all(np.array_equal(before,row['image']) for before,row in zip(pixels,rois))
    assert len([m for m in metadata if 'neutral-baseline' in m['evidence_roles']])==3
    b._life_feedback_sniff=s.sniff_report()
    report=json.loads(json.dumps(b.report(),allow_nan=False))
    recorded=[r for r in report['life_feedback']['sniff']['visual_refresh']['records'] if r['event']=='qualified-direction-window']
    assert recorded[0]['witness']['baseline_frames']==snapshot['witness']['baseline_frames']
    assert recorded[0]['frames']==snapshot['frames']
