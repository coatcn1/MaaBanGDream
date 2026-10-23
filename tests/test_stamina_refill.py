from types import SimpleNamespace
import json

import numpy as np
import pytest

from agent.realtime.profile_store import RealtimeProfileStore
from agent.realtime.stamina_refill import (
    RefillJournal, StaminaDetector, StaminaRefill, TEMPLATE_REGIONS,
    ensure_stamina, parse_number, plan_refill, validate_refill_options,
)
from agent.screen_refresh import ScreenRefreshCancelled


@pytest.fixture(autouse=True)
def trusted_mfa_session(monkeypatch):
    monkeypatch.setenv("MAABANGDREAM_MFA_SESSION_ID", "test-session")


def options(**changes):
    return dict(RealtimeProfileStore.DEFAULT_RUNTIME_OPTIONS, stamina_refill_enabled=True, **changes)


@pytest.mark.parametrize("key", ["stamina_refill_enabled", "stamina_refill_small_enabled", "stamina_refill_large_enabled"])
@pytest.mark.parametrize("bad", [1, 0, "true", None])
def test_boolean_options_are_strict(key, bad):
    with pytest.raises(ValueError, match=key):
        validate_refill_options({key: bad})


def test_global_refill_options_are_ignored_by_profile_store(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    expected = store.update_runtime_options(options(stamina_refill_limit="ignored"))
    assert not any(key.startswith("stamina_refill") for key in expected)
    assert RealtimeProfileStore(tmp_path).runtime_options() == expected


@pytest.mark.parametrize("text", ["-1", "1/25", "1.0", "O", "1 2", "", "1x", "１２"])
def test_number_rejects_partial_or_guessed_digits(text):
    with pytest.raises(ValueError):
        parse_number(text, 99)


@pytest.mark.parametrize("current,rounds,expected", [(1,1,(2,0)),(1,2,(5,0)),
    (1,4,(1,1)),(2,4,(0,1)),(1,None,(0,2)),(2,None,(0,2)),
    (0,None,(0,2)),(3,4,(0,0)),(0,0,(0,0)),(17,8,(0,0)),
    (1,8,(3,2)),(0,8,(4,2)),(0,9,(0,2)),(0,16,(4,2))])
def test_plan_batches_long_tasks_and_covers_reachable_tail(current,rounds,expected):
    assert plan_refill(current,3,604,206,rounds)==expected


def test_plan_respects_inventory_switches_and_game_ceiling():
    assert plan_refill(1,3,1,206,1)==(0,1)
    assert plan_refill(0,3,10,206,1,small_enabled=False)==(0,1)
    assert plan_refill(0,3,10,206,None,large_enabled=False)==(10,0)
    assert plan_refill(0,11,1,206,1)==(1,1)
    with pytest.raises(RuntimeError):plan_refill(98,99,0,206,1)
    with pytest.raises(RuntimeError):plan_refill(0,3,10,206,1,small_enabled=False,large_enabled=False)
    with pytest.raises(RuntimeError):plan_refill(0,3,2,0,1)


@pytest.mark.parametrize("bad", [True,-1,1.0,"4"])
def test_remaining_rounds_rejects_invalid_values(bad):
    with pytest.raises(ValueError):plan_refill(0,3,10,10,bad)


class FakeContext:
    def __init__(self, *, current=0, stock=(604, 206), result_unknown=False):
        self.tasker = SimpleNamespace(stopping=False, controller=self)
        self.state = "menu"
        self.current = current
        self.stock = stock
        self.selected = [0, 0]
        self.clicks = []
        self.result_unknown = result_unknown
        self.bad_preview = False
        self.after_click = lambda: None

    def post_click(self, x, y):
        self.clicks.append((x, y))
        if (x, y) == (1149, 39):
            self.state = "choose"
        elif (x, y) == (497, 440):
            self.state = "items"
        elif (x, y) == (766, 228):
            self.selected[0] += 1
        elif (x, y) == (766, 376):
            self.selected[1] += 1
        elif (x, y) == (505, 603):
            self.state = "menu"
        elif (x, y) == (770, 603):
            self.state = "confirm"
        elif (x, y) == (770, 449):
            self.recovered = self.selected[0] + 10*self.selected[1]
            self.current += self.recovered
            self.stock = tuple(v-s for v,s in zip(self.stock,self.selected))
            self.selected = [0, 0]
            self.state = "unknown" if self.result_unknown else "success"
        elif (x,y) == (640,449):
            self.state = "menu"
        else:
            raise AssertionError((x,y))
        self.after_click()
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))

    def ocr(self, image, key):
        return {"success_points":getattr(self,"recovered",0), "top":self.current, "small_stock":self.stock[0], "large_stock":self.stock[1],
                "small_count":self.selected[0], "large_count":self.selected[1], "current":self.current,
                "preview":self.current+self.selected[0]+10*self.selected[1]+self.bad_preview}[key]


def runner(context):
    return StaminaRefill(context, detector=SimpleNamespace(page=lambda im,p: context.state==p),
        capture=lambda _:None, foreground=lambda _:None, sleep=lambda _:None, ocr=context.ocr)


def test_success_reserves_before_confirm_without_cumulative_limit(tmp_path):
    context = FakeContext()
    journal = RefillJournal(tmp_path, 101)
    def observe():
        if context.clicks[-1] == (770,449):
            assert journal.load()["status"] == "reserved"
            assert journal.load()["reserved_points"] == 3
    context.after_click = observe
    assert runner(context).ensure(3, options(), journal)
    assert journal.load()["reserved_points"] == 3
    assert journal.load()["status"] == "confirmed"
    count = len(context.clicks)
    assert runner(context).ensure(3, options(), RefillJournal(tmp_path,101))
    assert len(context.clicks) == count
    context.current = 0
    context.after_click = lambda: None
    for _ in range(10):
        context.current = 0
        assert runner(context).ensure(3, options(), RefillJournal(tmp_path,101))
    assert journal.load()["reserved_points"] == 33
    assert len(context.clicks) > count


def test_large_only_counts_ten_points(tmp_path):
    context = FakeContext()
    journal = RefillJournal(tmp_path, 102)
    assert runner(context).ensure(1, options(stamina_refill_small_enabled=False), journal)
    assert context.current == 10
    assert journal.load()["reserved_points"] == 10
    assert (766,228) not in context.clicks


def test_unknown_result_keeps_reservation_and_blocks_callbacks(tmp_path):
    context = FakeContext(result_unknown=True)
    journal = RefillJournal(tmp_path,103)
    with pytest.raises(RuntimeError):
        runner(context).ensure(3,options(),journal)
    assert journal.load()["status"] == "reserved"
    assert journal.load()["reserved_points"] == 3
    context.state = "menu"
    count = len(context.clicks)
    with pytest.raises(RuntimeError,match="未决|结果不明"):
        runner(context).ensure(3, options(), RefillJournal(tmp_path,103))
    assert len(context.clicks) == count


@pytest.mark.parametrize("initial", ["confirm", "items", "success", "unknown"])
def test_foreign_modal_never_clicked(tmp_path, initial):
    context = FakeContext()
    context.state = initial
    with pytest.raises(RuntimeError):
        runner(context).ensure(3, options(), RefillJournal(tmp_path,104))
    assert not context.clicks


def test_conflicting_preview_never_consumes(tmp_path):
    context = FakeContext()
    context.bad_preview = True
    journal = RefillJournal(tmp_path,105)
    with pytest.raises(RuntimeError,match="初始读数冲突"):
        runner(context).ensure(3,options(),journal)
    assert (770,449) not in context.clicks
    assert journal.load()["reserved_points"] == 0


def test_stopping_after_foreground_does_not_use_old_controller(tmp_path):
    context = FakeContext()
    refill = runner(context)
    refill.foreground = lambda _: setattr(context.tasker,"stopping",True)
    with pytest.raises(ScreenRefreshCancelled):
        refill.ensure(3,options(),RefillJournal(tmp_path,106))
    assert not context.clicks


def test_stopping_after_reserved_never_retries(tmp_path):
    context = FakeContext()
    journal = RefillJournal(tmp_path,107)
    original = journal.save
    def save(data):
        original(data)
        if data["status"] == "reserved": context.tasker.stopping=True
    journal.save=save
    with pytest.raises(ScreenRefreshCancelled):
        runner(context).ensure(3,options(),journal)
    assert (770,449) not in context.clicks
    assert journal.load()["reserved_points"] == 3


def test_missing_task_id_and_corrupt_or_locked_budget_fail_closed(tmp_path):
    for bad in [None, True, "1", -1, 0]:
        with pytest.raises(ValueError): RefillJournal(tmp_path,bad)
    context=FakeContext()
    with pytest.raises(ValueError, match="task_id"):
        ensure_stamina(context,SimpleNamespace(),required=3,options=options(),journal_root=tmp_path)
    journal=RefillJournal(tmp_path,108)
    journal.root.mkdir(exist_ok=True)
    journal.path.write_text('{"version": 1}',encoding='utf-8')
    with pytest.raises(RuntimeError): runner(context).ensure(3,options(),journal)
    assert not context.clicks
    with journal.locked():
        with pytest.raises(FileExistsError):
            with RefillJournal(tmp_path,108).locked(): pass


def test_disabled_and_target_zero_do_not_capture_or_require_task_id():
    context=FakeContext()
    assert ensure_stamina(context,None,options={})
    assert ensure_stamina(context,None,required=0,options=options())
    assert not context.clicks


def test_static_layout_requires_all_signatures_and_rejects_confirm_as_items():
    detector=StaminaDetector()
    image=np.zeros((720,1280,3),np.uint8)
    for name in ("items_title", "small_identity", "small_label", "large_identity", "large_label", "items_limit"):
        x,y,w,h=TEMPLATE_REGIONS[name]
        image[y:y+h,x:x+w]=detector.templates[name]
    assert detector.page(image,"items")
    x,y,w,h=TEMPLATE_REGIONS["large_label"]
    image[y:y+h,x:x+w]=0
    assert not detector.page(image,"items")
    assert not detector.page(image,"menu")
    assert not detector.page(np.zeros((1080,1920,3),np.uint8),"items")


def test_natural_recovery_updates_selection_baseline_without_consumption(tmp_path):
    context = FakeContext()
    def recover():
        if context.state == "items": context.current = 3
    context.after_click = recover
    journal = RefillJournal(tmp_path,109)
    assert runner(context).ensure(3,options(),journal)
    assert (505,603) in context.clicks
    assert (770,449) not in context.clicks
    assert journal.load()["reserved_points"] == 0


def test_natural_recovery_after_confirm_preserves_full_budget(tmp_path):
    context = FakeContext()
    def recover():
        if context.clicks[-1] == (770,449): context.current += 1
    context.after_click = recover
    journal=RefillJournal(tmp_path,110)
    assert runner(context).ensure(3,options(),journal)
    assert journal.load()["reserved_points"] == 3
    assert journal.load()["observed"] == 4


def test_dimmed_menu_background_is_not_a_supported_menu():
    detector=StaminaDetector()
    image=np.zeros((720,1280,3),np.uint8)
    for name in ("home_live", "home_band", "top_icon", "top_plus"):
        x,y,w,h=TEMPLATE_REGIONS[name]
        image[y:y+h,x:x+w]=detector.templates[name]
    assert detector.page(image,"menu")
    assert not detector.page((image*.6).astype(np.uint8),"menu")


@pytest.mark.parametrize("text,confidence,expected", [("'6''0''4'",.999,604), ('"1"',.99,1),
    ("O",.999,None), ("'12'",.999,None), ("1/25",.999,None), ("1",.89,None)])
def test_local_ocr_rejects_letters_low_confidence_and_non_dictionary_quotes(monkeypatch,text,confidence,expected):
    import agent.realtime.stamina_refill as module
    monkeypatch.setattr(module,"recognize_song_title",lambda *a,**kw:SimpleNamespace(text=text,confidence=confidence))
    refill=StaminaRefill(SimpleNamespace(),detector=SimpleNamespace())
    image=np.zeros((720,1280,3),np.uint8)
    if expected is None:
        with pytest.raises(ValueError): refill.read_number(image,"small_stock")
    else:
        assert refill.read_number(image,"small_stock")==expected


def test_success_amount_mismatch_retains_reservation_and_does_not_close(tmp_path):
    context=FakeContext()
    original=context.ocr
    context.ocr=lambda image,key: 10 if key=="success_points" else original(image,key)
    journal=RefillJournal(tmp_path,111)
    with pytest.raises(RuntimeError,match="成功提示"):
        runner(context).ensure(3,options(),journal)
    assert (640,449) not in context.clicks
    assert journal.load()["status"]=="reserved"
    assert journal.load()["reserved_points"]==3


def test_confirmed_budget_is_separated_by_client_session(tmp_path):
    context=FakeContext()
    first=RefillJournal(tmp_path,200000001,session_id="client-one")
    assert runner(context).ensure(1,options(),first)
    second=RefillJournal(tmp_path,200000001,session_id="client-two")
    assert first.path!=second.path
    assert second.load()["reserved_points"]==0
    assert not first.pending_path.exists()


def test_new_session_cannot_bypass_unresolved_consumption(tmp_path):
    context=FakeContext(result_unknown=True)
    first=RefillJournal(tmp_path,200000001,session_id="client-one")
    with pytest.raises(RuntimeError):runner(context).ensure(1,options(),first)
    context.state="menu"
    before=list(context.clicks)
    second=RefillJournal(tmp_path,200000001,session_id="client-two")
    with pytest.raises(RuntimeError,match="未决"):
        runner(context).ensure(2,options(),second)
    assert context.clicks==before
    assert first.load()["reserved_points"]==1


def test_missing_session_rejects_automatic_refill(monkeypatch,tmp_path):
    monkeypatch.delenv("MAABANGDREAM_MFA_SESSION_ID")
    with pytest.raises(ValueError,match="会话"):
        RefillJournal(tmp_path,200000001)


@pytest.mark.parametrize("text,expected", [("LIVE BOOST已回复'1''0'！",10),
    ("LIVEBOOST已回复1!",1), ("将要回复LIVE BOOST。确认吗？",None),
    ("LIVE BOOST已回复O！",None), ("LIVE BOOST已回复10/25！",None)])
def test_success_body_requires_exact_amount_and_completion_text(monkeypatch,text,expected):
    import agent.realtime.stamina_refill as module
    monkeypatch.setattr(module,"recognize_song_title",lambda *a,**kw:SimpleNamespace(text=text,confidence=.99))
    refill=StaminaRefill(SimpleNamespace(),detector=SimpleNamespace())
    image=np.zeros((720,1280,3),np.uint8)
    if expected is None:
        with pytest.raises(ValueError):refill.read_number(image,"success_points")
    else:
        assert refill.read_number(image,"success_points")==expected


def test_conflict_after_increment_does_not_open_confirmation(tmp_path):
    context=FakeContext()
    def conflict():
        if context.selected[0]:context.bad_preview=True
    context.after_click=conflict
    journal=RefillJournal(tmp_path,112)
    with pytest.raises(RuntimeError,match="预览"):
        runner(context).ensure(3,options(),journal)
    assert (770,603) not in context.clicks
    assert (770,449) not in context.clicks
    assert journal.load()["reserved_points"]==0


def test_stop_immediately_after_consumption_retains_unresolved_marker(tmp_path):
    context=FakeContext()
    def stop():
        if context.clicks[-1]==(770,449):context.tasker.stopping=True
    context.after_click=stop
    journal=RefillJournal(tmp_path,113)
    with pytest.raises(ScreenRefreshCancelled):
        runner(context).ensure(1,options(),journal)
    assert (640,449) not in context.clicks
    assert journal.pending_path.exists()
    assert journal.load()["reserved_points"]==1


def test_home_plan_keeps_minimum_gate_and_batches_only_when_needed():
    assert plan_refill(17,3,604,206,8)==(0,0)
    assert plan_refill(14,3,604,206,8)==(0,0)
    assert plan_refill(1,3,604,206,8)==(3,2)
    assert plan_refill(0,3,0,206,None)==(0,2)
    assert plan_refill(0,3,0,206,40)==(0,2)
    assert plan_refill(0,3,604,206,40)==(4,2)
    assert plan_refill(3,3,604,206,40,minimum_required=9)==(0,2)


@pytest.mark.parametrize("page,point",[("items",(505,603)),("confirm",(508,449)),("choose",(640,544)),("success",(640,449))])
def test_degrade_modal_cleanup_never_confirms_consumption(page,point):
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    current=[page]
    detector=SimpleNamespace(page=lambda im,name:name==current[0])
    refill=StaminaRefill(context,detector=detector,capture=lambda _:None,sleep=lambda _:None)
    clicks=[]
    def click(x,y):clicks.append((x,y));current[0]='menu'
    refill.click=click
    assert refill.return_home_safely()
    assert clicks==[point]
    assert (770,449) not in clicks


def test_failure_diagnostics_preserve_raw_ocr_and_confidence(monkeypatch,tmp_path):
    import agent.realtime.stamina_refill as module
    monkeypatch.setattr(module,'recognize_song_title',lambda *a,**kw:SimpleNamespace(text="'2'",confidence=.68526))
    image=np.full((720,1280,3),255,np.uint8)
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    refill=StaminaRefill(context,detector=SimpleNamespace(page=lambda *a:True),capture=lambda _:image,sleep=lambda _:None)
    with pytest.raises(RuntimeError):refill.stable('items',('current',),attempts=2)
    saved=refill.save_failure('items unstable',root=tmp_path)
    data=json.loads((__import__('pathlib').Path(saved)/'failure.json').read_text(encoding='utf-8'))
    assert data['stage']=='items'
    reading=data['history'][-1]['readings']['current']
    assert reading['text']=="'2'" and reading['confidence']==.68526
    assert 'error' in reading
    assert (__import__('pathlib').Path(saved)/'failure.png').exists()


@pytest.mark.parametrize("key", ["preview", "top"])
@pytest.mark.parametrize("answers,expected", [([("23",.998),("23",.50)],23),([("23",.998),("23",.999)],23),([("23",.998),("28",.999)],None)])
def test_preview_bounded_variants_require_high_confidence_consensus(monkeypatch,answers,expected,key):
    import agent.realtime.stamina_refill as module
    readings=iter(SimpleNamespace(text=text,confidence=conf) for text,conf in answers)
    monkeypatch.setattr(module,"recognize_song_title",lambda *a,**kw:next(readings))
    refill=StaminaRefill(SimpleNamespace(),detector=SimpleNamespace())
    image=np.zeros((720,1280,3),np.uint8)
    if expected is None:
        with pytest.raises(ValueError,match="冲突"):refill.read_number(image,key)
    else:assert refill.read_number(image,key)==expected
    assert len(refill.readings[key]['variants'])==2


def test_choose_to_items_retries_only_when_first_successful_click_has_not_delivered(tmp_path):
    context=FakeContext(current=1)
    original=context.post_click
    missed=[False]
    def click(x,y):
        if (x,y)==(497,440) and not missed[0]:
            missed[0]=True
            context.clicks.append((x,y))
            return SimpleNamespace(wait=lambda:SimpleNamespace(succeeded=True))
        return original(x,y)
    context.post_click=click
    assert runner(context).ensure(3,options(),RefillJournal(tmp_path,901),remaining_rounds=8)
    assert context.clicks.count((497,440))==2
    assert context.current==24 and context.clicks.count((766,228))==3
    assert context.clicks.count((766,376))==2


@pytest.mark.parametrize('state',['items','unknown','choose-stop'])
def test_items_delivery_never_blind_reclicks_or_clicks_after_stop(state):
    context=FakeContext()
    context.state='choose' if state=='choose-stop' else state
    refill=runner(context)
    if state=='items':
        refill.deliver_items_page()
    elif state=='unknown':
        with pytest.raises(RuntimeError):refill.deliver_items_page()
    else:
        context.tasker.stopping=True
        with pytest.raises(ScreenRefreshCancelled):refill.deliver_items_page()
    assert not context.clicks


def test_seventeen_points_eight_round_task_does_not_open_any_refill_window(tmp_path):
    context=FakeContext(current=17)
    assert runner(context).ensure(3,options(),RefillJournal(tmp_path,902),remaining_rounds=8)
    assert not context.clicks


def test_medley_home_gate_covers_next_full_group_even_if_one_song_is_already_affordable(tmp_path):
    context=FakeContext(current=3)
    assert runner(context).ensure(3,options(),RefillJournal(tmp_path,903),remaining_rounds=30,minimum_required=9)
    assert context.current==23
    assert context.clicks.count((766,228))==0
    assert context.clicks.count((766,376))==2


def test_completed_task_or_zero_cost_does_not_read_stamina(tmp_path):
    context=FakeContext()
    refill=runner(context)
    refill.capture=lambda _:pytest.fail('不应再读取体力')
    assert refill.ensure(3,options(),RefillJournal(tmp_path,904),remaining_rounds=0)
    assert refill.ensure(0,options(),RefillJournal(tmp_path,905),remaining_rounds=None)
    assert not context.clicks


@pytest.mark.parametrize("current,small,large,rounds,minimum,expected", [
    (0,7,0,None,None,(7,0)), (0,0,1,None,None,(0,1)),
    (0,5,0,8,None,(5,0)), (0,0,3,2,None,(0,1)),
    (0,8,1,6,9,(8,1)), (3,0,2,None,9,(0,2)),
    (0,9,0,30,9,(9,0)), (0,100,0,None,None,(18,0)),
])
def test_inventory_fallback_and_full_medley_units(current,small,large,rounds,minimum,expected):
    assert plan_refill(current,3,small,large,rounds,minimum_required=minimum)==expected


def test_all_small_inventory_combinations_obey_cap_and_tail_coverage():
    for cost in (1,2,3):
        for group in (1,3):
            minimum=cost*group
            for current in range(minimum):
                for small in range(6):
                    for large in range(3):
                        for rounds in (group,group*2,group*8,None):
                            feasible=[current+s+10*l for s in range(small+1) for l in range(large+1)
                                      if minimum<=current+s+10*l<=24]
                            if not feasible:
                                with pytest.raises(RuntimeError):
                                    plan_refill(current,cost,small,large,rounds,minimum_required=minimum)
                                continue
                            s,l=plan_refill(current,cost,small,large,rounds,minimum_required=minimum)
                            after=current+s+10*l
                            assert 0<=s<=small and 0<=l<=large
                            assert minimum<=after<=24
                            if rounds is not None and cost*rounds<=24:
                                covering=[value for value in feasible if value>=cost*rounds]
                                expected=min(covering) if covering else max(feasible)
                                assert after==expected


@pytest.mark.parametrize("rounds", [None,15])
def test_batch_consumption_unknown_result_cannot_be_repeated(tmp_path,rounds):
    context=FakeContext(current=1,result_unknown=True)
    journal=RefillJournal(tmp_path,906)
    with pytest.raises(RuntimeError):
        runner(context).ensure(3,options(),journal,remaining_rounds=rounds)
    assert journal.load()['reserved_points']==20
    assert journal.pending_path.exists()
    before=list(context.clicks)
    context.state='menu'
    with pytest.raises(RuntimeError,match='未决'):
        runner(context).ensure(3,options(),journal,remaining_rounds=rounds)
    assert context.clicks==before


def test_long_task_natural_recovery_still_cancels_at_minimum(tmp_path):
    context=FakeContext(current=1)
    context.after_click=lambda:setattr(context,'current',3) if context.state=='items' else None
    journal=RefillJournal(tmp_path,907)
    assert runner(context).ensure(3,options(),journal,remaining_rounds=None)
    assert context.current==3 and (770,449) not in context.clicks
    assert journal.load()['reserved_points']==0


def test_batch_plan_and_strict_readback_allow_natural_recovery_over_plan_cap(tmp_path,capsys):
    context=FakeContext(current=1)
    context.after_click=lambda:setattr(context,'current',context.current+1) if context.clicks[-1]==(770,449) else None
    journal=RefillJournal(tmp_path,908)
    assert runner(context).ensure(3,options(),journal,remaining_rounds=8)
    assert context.current==25
    data=journal.load()
    assert data['expected']==24 and data['observed']==25 and data['reserved_points']==23
    output=capsys.readouterr().out
    assert 'next_required=3' in output and 'planned_target=24' in output and 'remaining_required=24' in output


def test_fifteen_round_home_budget_uses_two_batches_without_final_excess(tmp_path):
    context=FakeContext(current=10)
    journal=RefillJournal(tmp_path,909)
    refills=[]
    for remaining in range(15,0,-1):
        before=context.current
        assert runner(context).ensure(3,options(),journal,remaining_rounds=remaining)
        if context.current!=before:refills.append((remaining,before,context.current))
        context.current-=3
    assert refills==[(12,1,21),(5,0,15)]
    assert context.current==0 and context.stock==(599,203)
    assert context.clicks.count((770,449))==2
    assert journal.load()['reserved_points']==35


@pytest.mark.parametrize('mode',['CooperativeLive','VersusLive'])
@pytest.mark.parametrize('total',[0,15])
def test_real_flow_uses_batch_home_readback_only_after_settlement(monkeypatch,tmp_path,mode,total):
    from agent.realtime import cooperative_action, stamina_task, versus_action
    klass=cooperative_action.CooperativeLiveFlow if mode=='CooperativeLive' else versus_action.VersusLiveFlow
    context=FakeContext(current=10)
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=910))
    state=stamina_task.TaskStamina(options(),mode,3)
    monkeypatch.setitem(stamina_task._TASKS,910,state)
    monkeypatch.setattr(stamina_task,'RefillJournal',lambda root,identity:RefillJournal(tmp_path,identity))
    monkeypatch.setattr(stamina_task,'StaminaRefill',runner)
    stamina_task.allocate_home(context,argv,total or None)
    assert state.available==10 and not context.clicks
    flow=object.__new__(klass)
    flow.context=context
    flow.stamina_argv=argv
    flow.settings={'count':total,'post_live_action':'exit','entry_method':'normal','play_failure_retry_count':0}
    played=[]
    homes=[]
    flow.stopped=lambda:context.tasker.stopping
    def safe_boundary(*a,**kw):
        assert context.state=='room'
        return 'room_search',None
    flow.wait_for=safe_boundary
    def home(reason):
        assert context.state=='room' and played
        context.state='menu'
        stamina_task.allocate_home(context,argv,flow._stamina_remaining)
        homes.append((len(played),state.available))
    flow.recover_after_play_failure=home
    def play(reuse_room=False):
        context.state='playing'
        context.current-=3
        assert context.current==state.available>=0
        played.append(True)
        return True
    flow.run_attempt=play
    def settle():
        assert context.state=='playing'
        context.state='room'
        if not total and len(played)==15:context.tasker.stopping=True
    flow.return_to_room_selection=settle
    assert flow.run()
    assert len(played)==15 and not state.degraded
    assert homes==([(3,21),(10,15)] if total else [(3,21),(10,20)])
    assert context.current==(0 if total else 5)
    assert context.clicks.count((770,449))==2
    assert not RefillJournal(tmp_path,910).pending_path.exists()


@pytest.mark.parametrize('total',[None,18])
def test_medley_home_allocation_spends_complete_groups_from_confirmed_batches(monkeypatch,tmp_path,total):
    from agent.realtime import stamina_task
    context=FakeContext(current=10)
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=911))
    state=stamina_task.TaskStamina(options(),'MedleyLive',3)
    monkeypatch.setitem(stamina_task._TASKS,911,state)
    monkeypatch.setattr(stamina_task,'RefillJournal',lambda root,identity:RefillJournal(tmp_path,identity))
    monkeypatch.setattr(stamina_task,'StaminaRefill',runner)
    batches=[]
    for completed in range(0,18,3):
        before=context.current
        stamina_task.allocate_home(context,argv,None if total is None else total-completed,minimum_rounds=3)
        if context.current!=before:batches.append((completed,context.current))
        assert context.current>=9 and state.available==context.current
        stamina_task.reserve_attempt(argv,rounds=3)
        context.current-=9
        assert state.available==context.current
    assert batches==([(3,21),(9,23),(15,9)] if total else [(3,21),(9,23),(15,18)])
    assert context.current==(0 if total else 9)
    assert not state.degraded


def test_practice_home_allocation_does_not_open_refill_and_calibration_has_one_formal_round(monkeypatch,tmp_path):
    from agent.realtime import stamina_task
    context=FakeContext()
    context.get_node_data=lambda _: {'next':[{'name':'RealtimeLiveRehearsal'}]}
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=912))
    state=stamina_task.TaskStamina(options(),'RealtimeLive',3)
    monkeypatch.setitem(stamina_task._TASKS,912,state)
    monkeypatch.setattr(stamina_task,'RefillJournal',lambda root,identity:RefillJournal(tmp_path,identity))
    monkeypatch.setattr(stamina_task,'StaminaRefill',runner)
    stamina_task.allocate_home(context,argv,15)
    assert not context.clicks
    for mode in ('RealtimeCalibration','ContinuousRealtimeLive'):
        state.mode=mode
        assert stamina_task.home_remaining(context,argv,state)==1
        stamina_task.allocate_home(context,argv,1)
        assert state.available==3
    assert context.clicks.count((770,449))==1
