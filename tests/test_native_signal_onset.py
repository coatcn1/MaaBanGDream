from test_native_visual_refresh import setup


def test_raw_single_direction_onset_survives_state_rejection_without_control(tmp_path,monkeypatch):
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch)
    b.set_visual_phase_guard(True)
    for seq in range(1,5):emit(seq,240)
    rows=s.take_roi_evidence()
    assert len(rows)==1
    assert rows[0]['metadata']['evidence_roles']==['raw-direction-onset']
    assert rows[0]['metadata']['qualification']=='raw-not-assessed'
    assert rows[0]['metadata']['control_window_id'] is None
    assert s.summary['accepted_requests']==0


def test_raw_onset_real_engine_recorder_png_metadata_and_boolean_ack(tmp_path,monkeypatch):
    import json,numpy as np
    import test_native_visual_refresh as helper
    from agent.realtime.engine import RealtimeEngine
    from agent.realtime.debug_recorder import RealtimeDebugRecorder
    from agent.realtime.vision_io import imread_unicode
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch)
    source=helper.image(240);original=source.copy()
    monkeypatch.setattr(helper,'image',lambda value:source)
    b.set_visual_phase_guard(True)
    emit(1,240);source[:]=0
    recorder=RealtimeDebugRecorder(tmp_path/'recorder',video_enabled=False)
    engine=RealtimeEngine.__new__(RealtimeEngine);engine.debug_recorder=recorder
    engine._handoff_native_feedback_rois(s)
    assert s._visual_refresh.roi_handoff_accepted==1
    assert s._visual_refresh.report()['roi_evidence']['saved_status']=='unknown-until-background-events'
    recorder.close()
    rows=[json.loads(line) for line in (recorder.output_dir/'events.jsonl').read_text(encoding='utf8').splitlines()]
    assert len(rows)==1
    meta=rows[0]['frame_metadata']
    assert meta['run_id']==b._run_id and meta['sequence']==1
    assert meta['qualification']=='raw-not-assessed' and meta['control_window_id'] is None
    assert not any(key in meta for key in ('clock_identity','effect_sequence','supporting_execution_cursor'))
    assert meta['request_s']<meta['capture_upper_s']<meta['observed_s']==rows[0]['timestamp']
    assert np.array_equal(imread_unicode(recorder.output_dir/rows[0]['screenshot']),original[514:556,570:710])
    assert s.summary['accepted_requests']==0


def test_raw_monotonic_mixed_reused_and_stop_are_not_new_onsets(tmp_path,monkeypatch):
    import cv2,numpy as np
    import test_native_visual_refresh as helper
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch)
    images={240:helper.image(240),0:np.zeros((720,1280,3),np.uint8)}
    hsv=np.zeros((42,140,3),np.uint8);flat=hsv.reshape(-1,3);flat[:600]=(10,240,240);flat[600:1200]=(105,240,240)
    mixed=np.zeros((720,1280,3),np.uint8);mixed[514:556,570:710]=cv2.cvtColor(hsv,cv2.COLOR_HSV2BGR);images[600]=mixed
    monkeypatch.setattr(helper,'image',lambda value:images[value])
    emit(1,600);emit(2,240);emit(3,240);emit(4,0,reused=True);emit(5,240)
    assert s._visual_refresh.onset_queued==1
    evidence=s._visual_refresh
    meta=dict(frame_sequence=1,request_s=n[0]-.015)
    evidence._observe_raw_onset(images[240],meta,n[0]-.005,n[0],None,set(),0,0)
    assert evidence._raw_sequence==5
    emit(6,0);b._native_input_stop_requested.set();emit(7,240)
    assert evidence.onset_queued==1


def test_original_rejection_detail_uses_only_checked_locals(tmp_path,monkeypatch):
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch)
    observer=s._visual_refresh
    monkeypatch.setattr(observer,'_support',lambda *args:None)
    emit(1,240)
    records=[r for r in observer.metrics if r.get('reason')=='clock-or-execution-fence']
    assert len(records)==1
    detail=records[0]['detail']
    assert detail['view_present'] and detail['clockkey_match']
    assert detail['support_checked'] is True and detail['has_support'] is False
    assert detail['fence_checked'] is False and detail['fence_margin_s'] is None
    assert s.summary['accepted_requests']==0


def test_ready_control_evicts_only_unhanded_low_priority_and_removes_pending_dedup(tmp_path,monkeypatch):
    import numpy as np
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch);observer=s._visual_refresh
    def item(seq,control=None):
        return dict(image=np.zeros((42,140,3),np.uint8),timestamp=seq,window_start_s=seq,
            metadata=dict(sequence=seq,control_window_id=control,evidence_roles=['periodic-audit'] if control is None else ['peak']))
    for seq in range(16):assert observer._queue_ready(item(seq),low=True)
    assert observer._queue_ready(item(100,1),low=False)
    assert observer.roi_evicted==1 and 0 not in observer._roi_seen
    assert len(observer._roi_ready)==16
    rows=observer.take_roi_evidence()
    assert 100 in [r['metadata']['sequence'] for r in rows]
    observer.acknowledge_roi_evidence(100,False)
    assert observer.roi_handoff_rejected==1 and 100 not in observer._roi_seen
    for row in rows:
        if row['metadata']['sequence']!=100:observer.acknowledge_roi_evidence(row['metadata']['sequence'],True)
    assert observer.report()['roi_evidence']['coverage_complete'] is False


def test_combined_low_priority_128_and_global_256_remain_bounded(tmp_path,monkeypatch):
    import numpy as np
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch);observer=s._visual_refresh
    def item(seq,low):return dict(image=np.zeros((42,140,3),np.uint8),timestamp=seq,window_start_s=seq,
        metadata=dict(sequence=seq,control_window_id=None if low else 1,evidence_roles=['raw-direction-onset' if seq%2 else 'periodic-audit']))
    for seq in range(128):
        assert observer._queue_ready(item(seq,True),low=True)
        for row in observer.take_roi_evidence():observer.acknowledge_roi_evidence(row['metadata']['sequence'],True)
    assert not observer._queue_ready(item(128,True),low=True)
    for seq in range(128,256):
        assert observer._queue_ready(item(seq,False),low=False)
        for row in observer.take_roi_evidence():observer.acknowledge_roi_evidence(row['metadata']['sequence'],True)
    assert not observer._queue_ready(item(256,False),low=False)
    assert observer.low_priority_queued==128 and observer.roi_queued==256


def test_raw_diagnostic_queue_failure_does_not_disable_control(tmp_path,monkeypatch):
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch);observer=s._visual_refresh
    original=observer._queue_ready
    def queue(row,*,low):
        if low:raise RuntimeError('raw diagnostic enqueue failure')
        return original(row,low=low)
    monkeypatch.setattr(observer,'_queue_ready',queue)
    for seq,value in enumerate([240,170,240,240,240],1):emit(seq,value)
    assert observer.onset_disabled_reason=='RuntimeError'
    assert observer.disabled_reason is None and s.summary['accepted_requests']==1


def test_engine_records_false_or_exception_handoff_without_disabling_observer(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from agent.realtime.engine import RealtimeEngine
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch);emit(1,240)
    def broken(*args):raise RuntimeError('recorder failed')
    engine=RealtimeEngine.__new__(RealtimeEngine);engine.debug_recorder=SimpleNamespace(record_native_feedback_roi=broken)
    engine._handoff_native_feedback_rois(s)
    assert s._visual_refresh.roi_handoff_rejected==1
    assert s._visual_refresh.disabled_reason is None
    for seq,value in enumerate([170,240,240,240],2):emit(seq,value)
    assert s.summary['accepted_requests']==1


def test_control_reference_keeps_original_raw_role_and_protects_pending_image(tmp_path,monkeypatch):
    import numpy as np
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch);observer=s._visual_refresh
    def sample(seq):return dict(frame_sequence=seq,observed_s=seq*.05,request_s=seq*.05-.015,
        capture_upper_s=seq*.05-.005,kind='fast',slow=0,fast=1800,patch_v=[255,255])
    for seq in range(16):
        assert observer._queue_ready(dict(image=np.full((42,140,3),seq,np.uint8),timestamp=seq,window_start_s=seq,
            metadata=dict(sequence=seq,control_window_id=None,evidence_roles=['raw-direction-onset'])),low=True)
    window=dict(window_id=1,witness=dict(type='retreat-reattack',peak=sample(0),valley=sample(100),attack=sample(101)),
        frames=[sample(101),sample(102),sample(103)])
    for seq in (100,101,102,103):observer._cache_roi(np.zeros((720,1280,3),np.uint8),sample(seq))
    observer._queue_window_rois(window)
    rows=observer.take_roi_evidence();peak=next(row for row in rows if row['metadata']['sequence']==0)
    assert peak['metadata']['evidence_roles']==['raw-direction-onset']
    assert peak['metadata']['control_window_id'] is None
    assert 0 in observer._roi_seen and observer.roi_evicted==4
    assert all(seq in observer._roi_seen for seq in (100,101,102,103))


def test_recorder_queue_full_or_closed_returns_false_and_none_ack_is_unknown(tmp_path,monkeypatch):
    import queue,numpy as np
    from agent.realtime.debug_recorder import RealtimeDebugRecorder
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch);emit(1,240)
    row=s.take_roi_evidence()[0]
    s.acknowledge_roi_evidence(row['metadata']['sequence'],None)
    assert s._visual_refresh.roi_handoff_unknown==1
    assert s._visual_refresh.report()['roi_evidence']['handoff_records'][0]['status']=='unknown'
    recorder=RealtimeDebugRecorder(tmp_path/'full',video_enabled=False);recorder.close()
    assert recorder.record_native_feedback_roi(row['image'],row['timestamp'],row['metadata'],row['window_start_s']) is False
    recorder._closed=False;recorder._record_queue=queue.Queue(maxsize=1);recorder._record_queue.put_nowait(object())
    assert recorder.record_native_feedback_roi(row['image'],row['timestamp'],row['metadata'],row['window_start_s']) is False
    recorder._closed=True


def test_stop_requested_during_raw_small_copy_prevents_admission(tmp_path,monkeypatch):
    import numpy as np
    import test_native_visual_refresh as helper
    b,m,n,s,emit,_=setup(tmp_path,monkeypatch)
    class StopOnCopy(np.ndarray):
        def copy(self,*args,**kwargs):
            result=np.asarray(self).copy(*args,**kwargs)
            b._native_input_stop_requested.set()
            return result
    picture=helper.image(240).view(StopOnCopy)
    monkeypatch.setattr(helper,'image',lambda value:picture)
    emit(1,240)
    assert s._visual_refresh.onset_queued==0 and not s.take_roi_evidence()
