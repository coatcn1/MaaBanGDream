from types import SimpleNamespace
import json
from pathlib import Path
import pytest
from agent.realtime import cooperative_action, versus_action


@pytest.mark.parametrize('module,klass',[(cooperative_action,cooperative_action.CooperativeLiveFlow),(versus_action,versus_action.VersusLiveFlow)])
def test_internal_eight_round_loop_checks_allocated_stamina_before_each_attempt(monkeypatch,module,klass):
    flow=object.__new__(klass)
    flow.context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings={'count':8,'post_live_action':'exit','entry_method':'normal','play_failure_retry_count':0}
    flow.return_to_room_selection=lambda: None
    flow.run_attempt=lambda reuse_room=False: True
    flow.stopped=lambda: False
    calls=[]
    monkeypatch.setattr(module,'prepare_stamina_round',lambda owner,remaining: calls.append(remaining) or False,raising=False)
    assert flow.run()
    assert calls==[8,7,6,5,4,3,2,1]


def test_each_play_task_has_own_complete_refill_option_cases():
    interface=json.loads(Path('interface.json').read_text(encoding='utf-8'))
    for name in ['AutoLive','RealtimeLive','CooperativeLive','VersusLive','MedleyLive','RealtimeCalibration','ContinuousRealtimeLive']:
        task=next(t for t in interface['task'] if t['name']==name)
        option=interface['option'][name+'StaminaRefill']
        assert name+'StaminaRefill' in task['option']
        assert option['default_case']=='Off'
        assert [c['name'] for c in option['cases']]==['Off','SmallOnly','LargeOnly','Both']
        for case in option['cases']:
            params=next(iter(case['pipeline_override'].values()))['custom_action_param']
            assert set(params)=={'stamina_refill_enabled','stamina_refill_small_enabled','stamina_refill_large_enabled','task_mode'}


@pytest.fixture(autouse=True)
def isolated_task_stamina():
    from agent.realtime import stamina_task
    stamina_task._TASKS.clear()
    yield
    stamina_task._TASKS.clear()


@pytest.mark.parametrize('module,klass',[(cooperative_action,cooperative_action.CooperativeLiveFlow),(versus_action,versus_action.VersusLiveFlow)])
def test_real_loop_returns_home_only_after_settled_round_and_keeps_completed_count(module,klass):
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=401))
    state=stamina_task.TaskStamina({'stamina_refill_enabled':True},module.__name__,3,available=6)
    stamina_task._TASKS[401]=state
    flow=object.__new__(klass)
    flow.context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.stamina_argv=argv
    flow.settings={'count':8,'post_live_action':'exit','entry_method':'normal','play_failure_retry_count':0}
    events=[]
    flow.stopped=lambda: False
    flow.wait_for=lambda *a,**kw:('room_search',None)
    flow.run_attempt=lambda reuse_room=False: events.append(('played',reuse_room)) or True
    flow.return_to_room_selection=lambda:events.append(('settled',))
    def home(reason):
        assert events[-1]==('settled',)
        events.append(('home',flow._stamina_remaining))
        state.available=3*flow._stamina_remaining
    flow.recover_after_play_failure=home
    assert flow.run()
    assert sum(e[0]=='played' for e in events)==8
    assert [e for e in events if e[0]=='home']==[('home',6)]
    assert state.available==0


def test_unknown_or_insufficient_allocation_never_exits_a_live_scene():
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=402))
    stamina_task._TASKS[402]=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3)
    flow=SimpleNamespace(stamina_argv=argv,stopped=lambda:False,wait_for=lambda *a,**kw:(None,None),
                         recover_after_play_failure=lambda _:pytest.fail('不能退出进行中的演出'))
    assert not stamina_task.prepare_stamina_round(flow,2)
    assert stamina_task.task_state(argv).degraded


def test_failed_attempt_does_not_refund_or_keep_trusted_allocation():
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=403))
    state=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3,available=12)
    stamina_task._TASKS[403]=state
    stamina_task.reserve_attempt(argv)
    assert state.available==9
    stamina_task.mark_uncertain(argv)
    assert state.available is None
    stamina_task.reserve_attempt(argv)
    assert state.degraded


def test_task_configuration_is_idempotent_and_rejects_same_id_conflict(monkeypatch):
    from agent.realtime import stamina_task
    monkeypatch.setattr(stamina_task,'RealtimeProfileStore',lambda _:SimpleNamespace(runtime_options=lambda:{'live_boost_target':3}))
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=404),custom_action_param=json.dumps({'task_mode':'VersusLive','stamina_refill_enabled':True}))
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    assert stamina_task.StaminaRefillConfigure().run(context,argv)
    state=stamina_task.task_state(argv);state.available=17
    assert stamina_task.StaminaRefillConfigure().run(context,argv)
    assert stamina_task.task_state(argv).available==17
    argv.custom_action_param=json.dumps({'task_mode':'VersusLive','stamina_refill_enabled':False})
    assert stamina_task.StaminaRefillConfigure().run(context,argv)
    assert not stamina_task.enabled_state(argv)
    assert stamina_task.task_state(argv).available==17


def test_interface_cases_resolve_as_complete_blocks_in_real_maafw(tmp_path):
    import subprocess,sys
    interface=json.loads(Path('interface.json').read_text(encoding='utf-8'))
    pipelines={}
    for path in Path('resource/pipeline').glob('*.json'):pipelines.update(json.loads(path.read_text(encoding='utf-8')))
    entries=[]
    for task in interface['task']:
        name=task['name']+'StaminaRefill'
        if name in interface['option']:
            for case in interface['option'][name]['cases']:
                node=next(iter(case['pipeline_override']))
                entries.append({'base':{node:pipelines[node]},'override':case['pipeline_override'],'node':node})
    code='''
import json,sys
from maa.resource import Resource
from maa.toolkit import Toolkit
payload=json.load(sys.stdin);Toolkit.init_option(payload['logs']);resource=Resource();result=[]
for entry in payload['entries']:
 assert resource.override_pipeline(entry['base'])
 assert resource.override_pipeline(entry['override'])
 result.append(resource.get_node_data(entry['node'])['action']['param']['custom_action_param'])
print(json.dumps(result))
'''
    result=subprocess.run([sys.executable,'-c',code],input=json.dumps({'logs':str(tmp_path),'entries':entries}),text=True,capture_output=True,check=True)
    values=json.loads(result.stdout)
    assert len(values)==28
    for entry,value in zip(entries,values):assert value==entry['override'][entry['node']]['custom_action_param']


def test_enough_allocation_does_not_capture_or_read_room_ui():
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=405))
    state=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3,available=24)
    stamina_task._TASKS[405]=state
    flow=SimpleNamespace(stamina_argv=argv,stopped=lambda:False,
        wait_for=lambda *a,**kw:pytest.fail('体力足够不能截图或干扰房间'),
        recover_after_play_failure=lambda _:pytest.fail('体力足够不能离房'))
    assert not stamina_task.prepare_stamina_round(flow,8)
    assert state.available==21


def test_versus_safe_boundary_only_uses_owned_templates():
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=406))
    state=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3,available=0)
    stamina_task._TASKS[406]=state
    templates={p.stem:None for p in Path('resource/image/versus').glob('*.png')}
    assert 'room_wait' not in templates
    seen=[]
    def wait(names,**kw):
        assert set(names)<=set(templates)
        seen.extend(names)
        return 'room_together',None
    flow=SimpleNamespace(stamina_argv=argv,stopped=lambda:False,templates=templates,wait_for=wait,
        recover_after_play_failure=lambda _:setattr(state,'available',12))
    assert stamina_task.prepare_stamina_round(flow,4)
    assert 'room_together' in seen
    assert state.available==9


@pytest.mark.parametrize('module,klass',[(cooperative_action,cooperative_action.CooperativeLiveFlow),(versus_action,versus_action.VersusLiveFlow)])
def test_home_refill_failure_stops_before_attempt_and_does_not_retry_completed_round(monkeypatch,module,klass):
    from agent.realtime.stamina_task import StaminaNavigationError
    flow=object.__new__(klass)
    flow.context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings={'count':8,'play_failure_retry_count':9}
    flow.stopped=lambda:False
    flow.run_attempt=lambda **kw:pytest.fail('主页补充失败不能入房或准备')
    flow.recover_after_play_failure=lambda _:pytest.fail('补充失败不能盲重试')
    def fail(*a,**kw):raise StaminaNavigationError('无法确认主页安全导航')
    monkeypatch.setattr(module,'prepare_stamina_round',fail)
    assert flow.run() is False


def test_whole_medley_group_requires_three_songs_without_partial_refill():
    from agent.realtime.stamina_refill import plan_refill
    with pytest.raises(RuntimeError):plan_refill(0,3,5,0,30,minimum_required=9)
    assert plan_refill(0,3,9,0,30,minimum_required=9)==(9,0)


def test_formal_mode_uses_resolved_real_maafw_next_objects(tmp_path):
    import subprocess,sys
    from agent.realtime.stamina_task import formal_single
    code = """
import json,sys
from pathlib import Path
from maa.resource import Resource
from maa.toolkit import Toolkit
Toolkit.init_option(sys.argv[1]);r=Resource()
base=json.loads(Path('resource/pipeline/realtime_multi_live.json').read_text(encoding='utf-8'))
interface=json.loads(Path('interface.json').read_text(encoding='utf-8'))
assert r.override_pipeline({'RealtimeLiveFormalModeGate':base['RealtimeLiveFormalModeGate']})
before=r.get_node_data('RealtimeLiveFormalModeGate')
case=next(c for c in interface['option']['RealtimeMode']['cases'] if c['name']=='Formal')
assert r.override_pipeline(case['pipeline_override'])
print(json.dumps([before,r.get_node_data('RealtimeLiveFormalModeGate')]))
"""
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path)],capture_output=True,text=True,check=True)
    before,after=json.loads(result.stdout)
    assert not formal_single(SimpleNamespace(get_node_data=lambda _:before))
    assert formal_single(SimpleNamespace(get_node_data=lambda _:after))


def test_zero_cost_or_completed_task_never_reads_or_recovers():
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=407))
    stamina_task._TASKS[407]=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',0)
    flow=SimpleNamespace(stamina_argv=argv,wait_for=lambda *a,**kw:pytest.fail('零消耗不能读房间'),
        recover_after_play_failure=lambda _:pytest.fail('零消耗不能回主页'))
    assert not stamina_task.prepare_stamina_round(flow,None)
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    stamina_task.allocate_home(context,argv,0)


def test_home_entry_uses_current_controller_after_foreground_guard(monkeypatch):
    from agent.realtime import stamina_task,stamina_refill
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=408),box=SimpleNamespace(x=10,y=20,w=100,h=40))
    stamina_task._TASKS[408]=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3)
    clicks=[]
    old=SimpleNamespace(post_click=lambda *a:pytest.fail('不能使用前台保护前的旧代理'))
    fresh=SimpleNamespace(post_click=lambda x,y:clicks.append((x,y)) or SimpleNamespace(wait=lambda:SimpleNamespace(succeeded=True)))
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False,controller=old))
    def foreground(controller):
        assert controller is old
        context.tasker.controller=fresh
    monkeypatch.setattr(stamina_task,'allocate_home',lambda *a,**kw:None)
    monkeypatch.setattr(stamina_task,'capture_image',lambda _:None)
    detector=SimpleNamespace(page=lambda *a:True)
    monkeypatch.setattr(stamina_task,'detector',lambda:detector)
    monkeypatch.setattr(stamina_task,'StaminaRefill',lambda ctx:stamina_refill.StaminaRefill(ctx,detector=detector,foreground=foreground))
    assert stamina_task.StaminaHomeEntry().run(context,argv)
    assert clicks==[(60,40)]


def test_disabled_home_entry_preserves_existing_foreground_click(monkeypatch):
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=409))
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    seen=[]
    monkeypatch.setattr(stamina_task,'ForegroundClick',lambda:SimpleNamespace(run=lambda ctx,arg:seen.append((ctx,arg)) or True))
    assert stamina_task.StaminaHomeEntry().run(context,argv)
    assert seen==[(context,argv)]


@pytest.mark.parametrize("failure",["体力识别不稳定","道具不足","存在未决体力消费"])
def test_optional_home_failure_recovers_and_continues_in_degraded_mode(monkeypatch,failure):
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=501))
    state=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3)
    stamina_task._TASKS[501]=state
    events=[]
    class Runner:
        last_known_current=22
        def ensure(self,*a,**kw):raise RuntimeError(failure)
        def save_failure(self,error):events.append(('diagnostic',str(error)));return 'evidence'
        def return_home_safely(self):events.append(('cancel',));return True
    monkeypatch.setattr(stamina_task,'RefillJournal',lambda *a:object())
    monkeypatch.setattr(stamina_task,'StaminaRefill',lambda _:Runner())
    monkeypatch.setattr(stamina_task,'report_task_warning',lambda *a:events.append(('warn',a[-1])))
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    stamina_task.allocate_home(context,argv,8)
    assert state.degraded and state.available==22
    flow=SimpleNamespace(context=context,stamina_argv=argv,
        recover_after_play_failure=lambda _:pytest.fail('降级后不能无限强制回主页'),
        wait_for=lambda *a,**kw:pytest.fail('降级后不能检查房间'))
    for remaining in range(8,0,-1):assert not stamina_task.prepare_stamina_round(flow,remaining)
    assert state.available==0
    assert any(e[0]=='warn' for e in events)
    assert ('cancel',) in events


def test_degraded_home_uses_common_navigation_only_if_modal_cleanup_fails(monkeypatch):
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=502))
    state=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3)
    stamina_task._TASKS[502]=state
    class Runner:
        last_known_current=None
        def ensure(self,*a,**kw):raise RuntimeError('OCR')
        def save_failure(self,error):return None
        def return_home_safely(self):return False
    monkeypatch.setattr(stamina_task,'RefillJournal',lambda *a:object())
    monkeypatch.setattr(stamina_task,'StaminaRefill',lambda _:Runner())
    monkeypatch.setattr(stamina_task,'report_task_warning',lambda *a:None)
    calls=[]
    monkeypatch.setattr(stamina_task,'recover_stamina_home',lambda *a:calls.append('recover'))
    stamina_task.allocate_home(SimpleNamespace(tasker=SimpleNamespace(stopping=False)),argv,8)
    assert calls==['recover'] and state.degraded


@pytest.mark.parametrize("enabled,small,large,description",[(False,True,True,"已关闭"),(True,True,True,"大小火罐智能补充"),(True,True,False,"仅小火罐"),(True,False,True,"仅大火罐")])
def test_configure_off_and_item_modes_are_visible_in_task_log(monkeypatch,enabled,small,large,description):
    from agent.realtime import stamina_task
    monkeypatch.setattr(stamina_task,'RealtimeProfileStore',lambda _:SimpleNamespace(runtime_options=lambda:{'live_boost_target':3}))
    visible=[]
    def report(entry,override):
        assert entry=='TaskReportVisible'
        visible.append(override[entry]['focus']['Node.Action.Succeeded']['content'])
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False),run_task=report)
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=503),custom_action_param=json.dumps({'task_mode':'VersusLive','stamina_refill_enabled':enabled,'stamina_refill_small_enabled':small,'stamina_refill_large_enabled':large}))
    assert stamina_task.StaminaRefillConfigure().run(context,argv)
    assert len(visible)==1 and description in visible[0]
    assert '每局消耗 3 点' in visible[0] and 'task_id=503' in visible[0]


@pytest.mark.parametrize('module,klass,prefix',[(cooperative_action,cooperative_action.CooperativeLiveFlow,'Cooperative'),(versus_action,versus_action.VersusLiveFlow,'Versus')])
def test_navigation_excludes_slow_allocation_and_runs_it_once(monkeypatch,module,klass,prefix):
    clock=[0.0];events=[]
    flow=object.__new__(klass)
    flow.capture=lambda:'home' if clock[0]<43 else 'room'
    flow.visible=lambda image,name:image=='room' and name=='room_search'
    flow.pipeline_box=lambda image,name:SimpleNamespace(x=0,y=0,w=1,h=1) if image=='home' and name==prefix+'HomeMarker' else None
    flow.template_box=lambda *a:None
    flow.click=lambda point:events.append(('click',clock[0]))
    def allocate(owner):events.append(('allocate',clock[0]));clock[0]+=40
    monkeypatch.setattr(module,'allocate_flow_home',allocate)
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.time,'sleep',lambda delay:clock.__setitem__(0,clock[0]+delay))
    getattr(flow,'navigate_to_'+('cooperative' if prefix=='Cooperative' else 'versus')+'_room_selection')('home')
    assert [event for event in events if event[0]=='allocate']==[('allocate',0.0)]
    assert clock[0]>=43


@pytest.mark.parametrize('module,klass',[(cooperative_action,cooperative_action.CooperativeLiveFlow),(versus_action,versus_action.VersusLiveFlow)])
def test_real_loop_seventeen_points_covers_final_tail_after_settlement(module,klass):
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=990))
    state=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3,available=17)
    stamina_task._TASKS[990]=state
    flow=object.__new__(klass)
    flow.context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.stamina_argv=argv
    flow.settings={'count':8,'post_live_action':'exit','entry_method':'normal','play_failure_retry_count':0}
    events=[]
    flow.stopped=lambda:False
    flow.wait_for=lambda *a,**kw:('room_search',None)
    flow.run_attempt=lambda reuse_room=False:events.append(('played',state.available)) or True
    flow.return_to_room_selection=lambda:events.append(('settled',))
    def home(reason):
        from agent.realtime.stamina_refill import plan_refill
        assert events[-1]==('settled',)
        events.append(('home',state.available))
        small,large=plan_refill(state.available,3,100,100,flow._stamina_remaining)
        state.available+=small+10*large
    flow.recover_after_play_failure=home
    assert flow.run()
    assert sum(event[0]=='played' for event in events)==8
    assert [event for event in events if event[0]=='home']==[('home',2)]
    assert state.available==0


@pytest.mark.parametrize('remaining',[None,8])
def test_known_low_degraded_retries_only_once_after_a_completed_settlement(remaining):
    from agent.realtime import stamina_task
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=992))
    state=stamina_task.TaskStamina({'stamina_refill_enabled':True},'VersusLive',3,
        available=1,degraded=True,home_attempt_epoch=0)
    stamina_task._TASKS[992]=state
    homes=[]
    flow=SimpleNamespace(stamina_argv=argv,stopped=lambda:False,wait_for=lambda *a,**kw:('room_search',None),
        recover_after_play_failure=lambda _:homes.append(True))
    assert not stamina_task.prepare_stamina_round(flow,remaining)
    assert not homes
    stamina_task.mark_stamina_settled(argv,1)
    assert stamina_task.prepare_stamina_round(flow,remaining)
    assert len(homes)==1
    assert not stamina_task.prepare_stamina_round(flow,remaining)
    assert len(homes)==1
    state.available=None
    stamina_task.mark_stamina_settled(argv,2)
    assert not stamina_task.prepare_stamina_round(flow,remaining)
    assert len(homes)==1


@pytest.mark.parametrize("where", ["configure", "passive", "failure-evidence"])
def test_auxiliary_stamina_errors_continue_without_consumption(monkeypatch, where):
    from agent.realtime import stamina_task
    from agent import task_reporting
    task_reporting.clear_states()
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=930), custom_action_param=json.dumps({
        "task_mode": "VersusLive", "stamina_refill_enabled": True}))
    warnings = []
    monkeypatch.setattr(stamina_task, "report_task_warning", lambda *args: warnings.append(args[-1]))
    if where == "configure":
        monkeypatch.setattr(stamina_task, "RealtimeProfileStore", lambda _: (_ for _ in ()).throw(OSError("profile-read")))
        assert stamina_task.StaminaRefillConfigure().run(context, argv) is True
        assert stamina_task.enabled_state(argv) is None
    else:
        state = stamina_task.TaskStamina({"stamina_refill_enabled": True}, "VersusLive", 3)
        stamina_task._TASKS[930] = state
        if where == "passive":
            monkeypatch.setattr(stamina_task, "capture_image", lambda _: (_ for _ in ()).throw(OSError("auxiliary-capture")))
            assert stamina_task.StaminaPassiveHomeGate().run(context, argv) is True
        else:
            class Runner:
                last_known_current = None
                def ensure(self, *args, **kwargs):
                    raise RuntimeError("unresolved-consumption")
                def save_failure(self, error):
                    raise PermissionError("evidence-disk")
                def return_home_safely(self):
                    warnings.append("home-confirmed")
                    return True
            monkeypatch.setattr(stamina_task, "StaminaRefill", lambda _: Runner())
            monkeypatch.setattr(stamina_task, "RefillJournal", lambda *args: object())
            stamina_task.allocate_home(context, argv, 2)
            assert "home-confirmed" in warnings
            assert state.degraded is True
    assert warnings
    assert not task_reporting.latest_failure_reason()


def test_passive_gate_keeps_unsafe_home_recovery_exception_fatal(monkeypatch):
    from agent.realtime import stamina_task
    argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=940))
    state = stamina_task.TaskStamina({"stamina_refill_enabled": True}, "VersusLive", 3)
    stamina_task._TASKS[940] = state
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False),
        get_node_data=lambda _: (_ for _ in ()).throw(OSError("missing-recovery-node")))
    class Runner:
        last_known_current = None
        def ensure(self, *args, **kwargs): raise RuntimeError("modal-unknown")
        def save_failure(self, error): return None
        def return_home_safely(self): return False
    monkeypatch.setattr(stamina_task, "capture_image", lambda _: "home")
    monkeypatch.setattr(stamina_task, "detector", lambda: SimpleNamespace(page=lambda *args: True))
    monkeypatch.setattr(stamina_task, "RefillJournal", lambda *args: object())
    monkeypatch.setattr(stamina_task, "StaminaRefill", lambda _: Runner())
    assert stamina_task.StaminaPassiveHomeGate().run(context, argv) is False
