"""启动时钟观察器只允许有界孤立宽点重试，保持原时间与停止边界。"""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from agent.realtime.native_clock_observer import NativeClockObserver
from test_native_observation_clock import IDENTITY, probe, warmed

def test_only_true_isolated_wide_skip_gets_two_bounded_short_waits(monkeypatch):
    from agent.realtime import native_clock_observer as module
    observer=NativeClockObserver('test',lease_current=lambda:True,adb=object(),wide_probe_retry_enabled=True)
    mapper=warmed();observer._mapper=mapper
    previous=mapper.snapshot()
    for index,host,expected in [(6,200.3,.005),(7,200.32,.005),(8,200.34,.05),(9,200.36,.05)]:
        sample=probe(index,host,rtt=.0035);before=observer._probe_schedule_before()
        assert not mapper.observe(sample) and mapper.snapshot() is previous
        monkeypatch.setattr(module.time,'perf_counter',lambda:sample.host_receive_s)
        assert observer._probe_schedule_after(before,sample,False)==expected
    sample=probe(10,200.38);before=observer._probe_schedule_before();assert mapper.observe(sample)
    monkeypatch.setattr(module.time,'perf_counter',lambda:sample.host_receive_s)
    assert observer._probe_schedule_after(before,sample,True)==.05
    sample=probe(11,200.4,rtt=.0035);before=observer._probe_schedule_before();assert not mapper.observe(sample)
    monkeypatch.setattr(module.time,'perf_counter',lambda:sample.host_receive_s)
    assert observer._probe_schedule_after(before,sample,False)==.005

def test_scheduler_fault_falls_back_to_original_wait_without_disabling_mapper(monkeypatch):
    from agent.realtime import native_clock_observer as module
    observer=NativeClockObserver('test',lease_current=lambda:True,adb=object(),wide_probe_retry_enabled=True)
    observer._mapper=mapper=warmed();before=observer._probe_schedule_before();sample=probe(6,200.3,rtt=.0035)
    assert not mapper.observe(sample)
    monkeypatch.setattr(module.time,'perf_counter',lambda:(_ for _ in ()).throw(RuntimeError('optional schedule')))
    assert observer._probe_schedule_after(before,sample,False)==.05
    assert not observer._wide_probe_retry_enabled and observer._probe_schedule_disabled_reason=='RuntimeError'
    assert mapper.snapshot() is before

def test_each_short_wait_iteration_rechecks_lease_and_stop_without_parallel_queries(monkeypatch):
    from agent.realtime import native_clock_observer as module
    observer=NativeClockObserver('test',lease_current=lambda:True,adb=object(),wide_probe_retry_enabled=True)
    observer._mapper=mapper=warmed();queries=[];guards=[];waits=[];active=[]
    class Stop:
        stopped=False
        def is_set(self):return self.stopped
        def set(self):self.stopped=True
        def wait(self,delay):waits.append(delay);return self.stopped
    observer._stop=stop=Stop()
    def query():
        assert not active;active.append(True)
        try:
            queries.append(None);return probe(5+len(queries),200.28+len(queries)*.01,rtt=.0035)
        finally:active.pop()
    def guard():
        guards.append(None)
        if len(guards)==3:raise RuntimeError('lease lost')
    observer._client=SimpleNamespace(query=query,close=lambda:None)
    monkeypatch.setattr(observer,'_setup',lambda:None)
    monkeypatch.setattr(observer,'_guard',guard)
    monkeypatch.setattr(observer,'_cleanup',lambda:None)
    monkeypatch.setattr(module.time,'perf_counter',lambda:200.32)
    observer._run()
    assert len(guards)==3 and len(queries)==2 and waits==[.005,.005]
    assert observer._closed.is_set() and observer._status.reason=='RuntimeError'

@pytest.mark.parametrize('waits_ms,rtts_ms,expected_gap_ms',[
    ([51.2953,51.8490,51.0857],[2.0485,3.0022,1.2534],159.2807),
    ([51.2014,52.2673,52.1745],[2.8127,4.4996,1.4962],162.9555),
])
def test_two_real_gap_shapes_reproduce_old_epoch_cut_and_bounded_retry_keeps_strict_mapper(monkeypatch,waits_ms,rtts_ms,expected_gap_ms):
    from agent.realtime import native_clock_observer as module
    outcomes=[]
    # 以4dd的实际等待/RTT形状构造稳定时钟；提前补采后的真实RTT未知，不能当真机反事实。
    for enabled in (False,True):
        observer=NativeClockObserver('test',lease_current=lambda:True,adb=object(),wide_probe_retry_enabled=enabled)
        observer._mapper=mapper=warmed();original=mapper.snapshot();previous_rx=original.probes[-1].host_receive_s
        delay=.05;now=[previous_rx];monkeypatch.setattr(module.time,'perf_counter',lambda:now[0])
        accepted=[];host_gaps=[]
        for index,(wait,rtt) in enumerate(zip(waits_ms,rtts_ms),6):
            host=previous_rx+delay+(wait/1000-.05)
            sample=probe(index,host,rtt=rtt/1000)
            before=observer._probe_schedule_before();ok=mapper.observe(sample)
            now[0]=sample.host_receive_s
            delay=observer._probe_schedule_after(before,sample,ok)
            accepted.append(ok);host_gaps.append(host-original.probes[-1].host_receive_s)
            previous_rx=sample.host_receive_s
        outcomes.append((mapper.snapshot(),host_gaps[-1],observer._probe_schedule_counts))
        assert accepted==[False,False,True]
    assert outcomes[0][0].epoch==1 and outcomes[0][0].reason=='warming'
    assert outcomes[0][1]*1000==pytest.approx(expected_gap_ms,abs=.0001)
    assert outcomes[1][0].epoch==0 and outcomes[1][0].reason is None
    assert outcomes[1][1]*1000==pytest.approx(expected_gap_ms-90,abs=.0001)
    assert outcomes[1][2]['short_waits']==2

@pytest.mark.parametrize('fault',['identity','source','sequence','processing','mono-step','offset-step','rtt','stale','stop','disabled'])
def test_short_retry_never_bypasses_mapper_invalidation_or_staleness(monkeypatch,fault):
    from agent.realtime import native_clock_observer as module
    observer=NativeClockObserver('test',lease_current=lambda:True,adb=object(),wide_probe_retry_enabled=fault!='disabled')
    observer._mapper=mapper=warmed();sample=probe(6,200.3,rtt=.0035)
    if fault=='identity':sample=replace(sample,identity=replace(IDENTITY,generation=4))
    elif fault=='source':sample=replace(sample,qualified_source=False)
    elif fault=='sequence':sample=replace(sample,sequence=5)
    elif fault=='processing':sample=replace(sample,device_send_s=sample.device_receive_s+.0021)
    elif fault=='mono-step':sample=replace(sample,monotonic_receive_s=sample.monotonic_receive_s+.01,monotonic_send_s=sample.monotonic_send_s+.01)
    elif fault=='offset-step':sample=probe(6,200.3,offset=100.01,rtt=.0035)
    elif fault=='rtt':sample=probe(6,200.3,rtt=.021)
    before=observer._probe_schedule_before();qualified=mapper.observe(sample)
    monkeypatch.setattr(module.time,'perf_counter',lambda:200.5 if fault=='stale' else sample.host_receive_s)
    if fault=='stop':observer._stop.set()
    assert observer._probe_schedule_after(before,sample,qualified)==.05
    assert observer._probe_schedule_counts['short_waits']==0
