from types import SimpleNamespace

import pytest

from agent.realtime import native_engine
from test_native_clock_domains import fixture


def prepared(tmp_path,monkeypatch,*,delta=15,trial=True,release=True,advance=.01,stop=False,final=False,during_publish=False):
    backend,mapper,now,probe,commit,_,applied=fixture(tmp_path,monkeypatch)
    for host in (200.2254,200.3254,200.4254):commit(host)
    backend._refresh_observation_views()
    backend._native_clock_trial=trial
    compiler=native_engine.touch_script_compiler()
    config=dict(judgement_y=590,press_bias_ms=0,max_wait_ms=250,lane_centers=list(native_engine.LANE_CENTERS))
    def action(kind,due):
        return dict(kind=kind,lane=3,contact=0,target_x=640.,due_s=due,note_index=0,flick_direction=None)
    compiler.compile([action('down',200.4)],config,200.3,False,200.45,[])
    assert not compiler.phase_boundary_safe()
    backend._compiler=compiler
    if not during_publish:
        assert backend.request_future_phase(delta,now[0],owner='feedback')
    original_expiry=backend._phase_slot[-1] if backend._phase_slot is not None else None
    publishes=[]
    def publish():
        publishes.append(now[0])
        if during_publish and len(publishes)==1:
            assert backend.request_future_phase(delta,now[0],owner='feedback')
        if release and not compiler.phase_boundary_safe():
            compiler.compile([action('up',200.6)],config,200.45,False,200.7,[])
            assert compiler.phase_boundary_safe()
        if len(publishes)==1:
            assert not applied
        now[0]+=advance
        if stop:backend._phase_closed=True
        return True
    backend._session.publish=publish
    backend._session.poll=lambda: 'running'
    if final:
        backend._session.apply_future_phase=lambda *args: 'no-future-actions'
    return backend,now,applied,publishes,original_expiry


@pytest.mark.parametrize('delta',[-15,15])
def test_real_drive_consumes_original_request_after_cpp_hold_boundary_is_released(tmp_path,monkeypatch,delta):
    backend,now,applied,publishes,expiry=prepared(tmp_path,monkeypatch,delta=delta)
    backend._drive_session()
    assert len(publishes)==1 and now[0]<expiry
    assert len(applied)==1 and applied[0][0]==delta
    assert backend.phase_status_snapshot().state=='applied'
    assert backend._phase_slot is None
    backend._drive_session()
    assert len(applied)==1


def test_unclocked_driver_retains_original_before_only_behavior(tmp_path,monkeypatch):
    backend,_,applied,_,_=prepared(tmp_path,monkeypatch,trial=False)
    backend._drive_session()
    assert not applied and backend.phase_status_snapshot().state=='waiting-touch-boundary'
    backend._drive_session()
    assert len(applied)==1


@pytest.mark.parametrize('fault',['expired','unsafe','stop','final'])
def test_post_publish_reuses_original_rejection_and_waiting_gates(tmp_path,monkeypatch,fault):
    backend,_,applied,_,_=prepared(tmp_path,monkeypatch,
        advance=2.01 if fault=='expired' else .01,release=fault!='unsafe',
        stop=fault=='stop',final=fault=='final')
    backend._drive_session()
    assert not applied
    status=backend.phase_status_snapshot()
    if fault=='expired':
        assert status.state=='expired' and backend._phase_slot is None
    elif fault=='unsafe':
        assert status.state=='waiting-touch-boundary' and backend._phase_slot is not None
    elif fault=='stop':
        assert status.state=='stopped' and backend._phase_slot is None
    else:
        assert status.state=='rejected' and status.terminal_reason=='no-future-actions'
        assert backend._phase_slot is None



def test_request_arriving_inside_publish_waits_until_callback_returns(tmp_path,monkeypatch):
    backend,_,applied,publishes,_=prepared(tmp_path,monkeypatch,during_publish=True)
    backend._drive_session()
    assert len(publishes)==1 and len(applied)==1
    assert backend.phase_status_snapshot().state=='applied'
