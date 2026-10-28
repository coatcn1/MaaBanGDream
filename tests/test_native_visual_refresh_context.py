from dataclasses import replace
import pytest
from test_native_visual_refresh import setup

@pytest.mark.parametrize('boundary', ['after-drain','before-request-lock'])
@pytest.mark.parametrize('change', ['foreign-effect','clock-epoch','hard-provenance','foreign-pending','visual-guard-epoch'])
def test_visual_original_context_survives_to_atomic_request_gate(tmp_path,monkeypatch,boundary,change):
    backend,mapper,now,sampler,emit,_=setup(tmp_path,monkeypatch)
    for sequence,value in enumerate((240,170,240,240),1):emit(sequence,value)
    assert len(sampler._visual_refresh.detector.frames)==2
    changed=[]
    original_request=backend.request_future_phase
    def switch_context():
        if changed:return
        changed.append(change)
        if change=='foreign-effect':
            backend._phase_sequence=101
            foreign=replace(backend.phase_status_snapshot(),state='applied',request_sequence=101,
                original_observed_s=now[0]-.05,delta_ms=-15.,cumulative_phase_ms=-15.,owner='feedback',
                owner_cumulative_phase_ms=-15.,applied_host_s=now[0],first_adjusted_due_s=now[0]+.3,
                first_affected_chunk_sequence=999,first_affected_action_token=999)
            backend._phase_owner_totals['feedback']=-15.
            backend._session.future_phase_offset_ms=-15.
            backend._publish_phase_status(foreign,'foreign-unexecuted-effect')
        elif change=='hard-provenance':
            backend._observation_hard_revision+=1
        elif change=='foreign-pending':
            assert original_request(-15.,now[0]-.05,owner='feedback')
        elif change=='visual-guard-epoch':
            backend.set_visual_phase_guard(True)
            backend.set_visual_phase_guard(False)
        else:
            source=mapper.snapshot().probes[-1]
            mapper.invalidate('independent-epoch-change')
            for offset in (1,2,3):
                host=source.host_send_s+offset*.05
                device=host+.0004-100
                mapper.observe(type(source)(source.identity,source.sequence+offset,host,host+.0008,
                    device,device,device-1,device-1))
            now[0]=host+.0008
            backend._refresh_observation_views()
            assert backend._observation_clock_ready(backend.observation_clock_snapshot(),now[0])
    if boundary=='after-drain':
        original=sampler._visual_refresh.drain
        def drain(stamp):
            pulses=original(stamp)
            if pulses:switch_context()
            return pulses
        monkeypatch.setattr(sampler._visual_refresh,'drain',drain)
    else:
        original=backend.request_future_phase
        def request(*args,**kwargs):
            switch_context()
            return original(*args,**kwargs)
        monkeypatch.setattr(backend,'request_future_phase',request)
    emit(5,240)
    assert changed
    if change=='foreign-pending':
        assert backend._phase_slot and backend._phase_slot[3]==-15.
    else:
        assert not backend._phase_slot
    assert sampler.summary['accepted_requests']==0


def test_legacy_feedback_request_without_new_context_remains_compatible(tmp_path,monkeypatch):
    backend,_,now,_,_,_=setup(tmp_path,monkeypatch)
    assert backend.request_future_phase(15.,now[0],owner='feedback')
