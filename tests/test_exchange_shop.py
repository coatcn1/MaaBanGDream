from dataclasses import replace
import json
import os
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

from agent.realtime.exchange_shop import (
    ExchangeFlow, ExchangeJournal, ExchangeReader, ExchangeShopConfigure, ExchangeShopRun,
    ExchangeStopped, ExchangeUI, ExchangeBudget, validate_options, _OPTIONS,
)
from agent.realtime.exchange_shop_policy import BoxState, THRESHOLDS, decide, validate_draw, validate_reset


def state(**changes):
    return replace(BoxState(19, 180, 180, 10, 1, 30822), **changes)


@pytest.mark.parametrize("total", (180, 230))
@pytest.mark.parametrize("y", (1, 2, 3, 4, 5, 6, 7, 8))
def test_long_run_threshold_continues_through_maximum(total, y):
    threshold = THRESHOLDS[total][y-1]
    base = state(total=total, remaining=threshold, cans=y, special=0)
    assert decide(replace(base, remaining=threshold-1)).action == "draw"
    assert decide(base).action == "draw"
    assert decide(replace(base, remaining=threshold+1)).action == "reset"


@pytest.mark.parametrize("y", (9, 10))
def test_many_cans_only_requests_next_ten(y):
    assert decide(state(cans=y)).amount == 10
    assert decide(state(cans=y)).max_steps is None


def test_zero_cans_requires_special_unlock_and_empty_box_waits():
    assert decide(state(cans=0, special=0)).action == "reset"
    assert decide(state(cans=0, special=1)).action == "draw"
    assert decide(state(remaining=0, cans=0, special=0)).action == "wait"
    assert decide(state(remaining=5, cans=1, special=1)).amount == 5


def test_no_obsolete_n_countdown():
    assert decide(state(remaining=50,cans=1,special=0),budget=20).max_steps is None


@pytest.mark.parametrize("changes", ({"remaining":181}, {"cans":11}, {"special":2},
    {"currency":-1}, {"box":0}, {"remaining":5,"cans":6}, {"total":200}, {"cans":True}))
def test_bad_state_rejected(changes):
    with pytest.raises(ValueError):
        decide(state(**changes))


class UI:
    consumption_verified = True
    reset_verified = True

    def __init__(self, states, fail_draw=False):
        self.initial_currency=states[0].currency
        self.states = iter(states)
        self.actions = []
        self.fail_draw = fail_draw

    def stopped(self):
        pass

    def initialize_budget(self, automatic):
        return self.initial_currency if automatic else None

    def check_settings(self):
        self.actions.append("settings")

    def read_state(self):
        self.actions.append("read")
        return next(self.states)

    def begin_operation(self,operation_id,before,decision,budget):
        self.operation={"operation_id":operation_id,"before":before.__dict__,"phase":"pool_verified"}

    def complete_operation(self,after):
        return self.operation | {"after":after.__dict__}

    def commit_operation(self):
        pass

    def draw_once(self, amount):
        self.actions.append(("draw",amount))
        if self.fail_draw:
            raise TimeoutError("unknown draw result")

    def reset_once(self):
        self.actions.append("reset")


def flow(ui, tmp_path, **options):
    return ExchangeFlow(ui, {"execute":True} | options, journal=ExchangeJournal(tmp_path / "ledger.json"), report=lambda text:None)


def test_preview_performs_zero_consumption(tmp_path):
    ui = UI([state()])
    assert flow(ui,tmp_path,execute=False).run()
    assert ui.actions == ["settings","read"]
    assert not (tmp_path/"ledger.json").exists()


def test_budget_cumulative_and_each_ten_reads_again(tmp_path):
    ui = UI([state(),state(remaining=170,currency=30722,cans=9),state(remaining=160,currency=30622,cans=8)])
    f=flow(ui,tmp_path,max_draws=25)
    assert f.run()
    assert f.draws == 20
    assert ui.actions.count(("draw",10)) == 2
    assert ui.actions.count("read") == 3


@pytest.mark.parametrize("amount,budget", ((10,9),(5,4)))
def test_no_single_draw_to_fill_budget(tmp_path,amount,budget):
    ui=UI([state(remaining=amount,cans=1)])
    assert flow(ui,tmp_path,max_draws=budget).run()
    assert not any(isinstance(a,tuple) for a in ui.actions)


def test_tail_cost_and_insufficient_currency_keep_balance(tmp_path):
    ui=UI([state(remaining=5,cans=1,currency=50),state(box=20,currency=0)])
    assert flow(ui,tmp_path,max_draws=5).run()
    assert ("draw",5) in ui.actions
    ui=UI([state(currency=99)])
    assert flow(ui,tmp_path).run()
    assert ("draw",10) not in ui.actions


def test_special_zero_unlocks_reset_then_draws_are_rechecked(tmp_path):
    ui=UI([state(cans=0,special=0),state(box=20),state(box=20,remaining=170,cans=9,currency=30722)])
    assert flow(ui,tmp_path,max_draws=10).run()
    assert ui.actions == ["settings","read","reset","read",("draw",10),"read"]


def test_unknown_draw_never_retried_and_next_run_is_blocked(tmp_path):
    ui=UI([state()],fail_draw=True)
    with pytest.raises(TimeoutError):
        flow(ui,tmp_path).run()
    assert ui.actions.count(("draw",10)) == 1
    with pytest.raises(RuntimeError, match="上次"):
        flow(UI([state()]),tmp_path).run()


@pytest.mark.parametrize("changes", ({"currency":30723}, {"remaining":169}, {"box":20}, {"cans":10,"special":1}))
def test_unreconciled_result_preserves_pending(tmp_path,changes):
    before=state(cans=9,special=0)
    after=replace(before, **({"remaining":170,"currency":30722}|changes))
    with pytest.raises(RuntimeError):
        flow(UI([before,after]),tmp_path,max_draws=10).run()
    with pytest.raises(RuntimeError):
        ExchangeJournal(tmp_path/"ledger.json").check_unresolved()


def test_reset_conflict_does_not_confirm_ledger(tmp_path):
    with pytest.raises(RuntimeError):
        flow(UI([state(cans=0,special=0),state(box=19)]),tmp_path).run()
    with pytest.raises(RuntimeError):
        ExchangeJournal(tmp_path/"ledger.json").check_unresolved()


@pytest.mark.parametrize("changes", ({"box":19},{"box":21},{"currency":30821},{"remaining":179},
    {"cans":9},{"special":0},{"total":230,"remaining":230}))
def test_reset_requires_next_full_box_and_unchanged_currency(tmp_path,changes):
    before=state(cans=0,special=0)
    after=replace(state(box=20),**changes)
    with pytest.raises(RuntimeError):
        flow(UI([before,after]),tmp_path,max_draws=10).run()
    with pytest.raises(RuntimeError):
        ExchangeJournal(tmp_path/"ledger.json").check_unresolved()


def test_unverified_production_protocol_blocks_before_consumption(tmp_path):
    ui=UI([state()]);ui.consumption_verified=False
    with pytest.raises(RuntimeError,match="消费输入前"):
        flow(ui,tmp_path).run()
    assert ui.actions == ["settings","read"]


def test_unverified_reset_stops_before_any_reset_input(tmp_path):
    ui=UI([state(cans=0,special=0)]);ui.reset_verified=False
    with pytest.raises(RuntimeError,match="换箱输入前"):
        flow(ui,tmp_path).run()
    assert ui.actions == ["settings","read"]


def test_mode_conflict_refuses_consumption(tmp_path):
    ui=UI([state()])
    with pytest.raises(RuntimeError,match="容量冲突"):
        flow(ui,tmp_path,mode="five230").run()
    assert ui.actions == ["settings","read"]


def test_ledger_exclusive_lock(tmp_path):
    journal=ExchangeJournal(tmp_path/"ledger.json")
    with journal.locked():
        with pytest.raises(RuntimeError,match="锁"):
            with ExchangeJournal(journal.path).locked():
                pass


@pytest.mark.parametrize("options", ({"execute":1},{"max_draws":True},{"max_draws":-1},{"mode":"other"}))
def test_options_strict(options):
    with pytest.raises(ValueError):validate_options(options)


def test_reader_rejects_wrong_page_and_dimmed_overlay():
    reader=ExchangeReader()
    with pytest.raises(ValueError):reader.main(np.zeros((720,1280,3),np.uint8))
    with pytest.raises(ValueError):reader.contents(np.zeros((720,1280,3),np.uint8))


def test_low_confidence_and_invalid_ocr_are_not_guessed():
    reader=ExchangeReader(ocr=lambda *a,**k:SimpleNamespace(text="10/10",confidence=.89))
    with pytest.raises(ValueError):reader.ratio(None,"cans",(0,0,10,10),(10,))
    reader.ocr=lambda *a,**k:SimpleNamespace(text="1O/10",confidence=.999)
    with pytest.raises(ValueError):reader.ratio(None,"cans",(0,0,10,10),(10,))


def test_stop_before_capture_recognition_and_callback_is_neutral(tmp_path):
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=True))
    captures=[]
    ui=ExchangeUI(context,capture=lambda context:captures.append(True))
    with pytest.raises(ExchangeStopped):ui.stable("main")
    assert captures == []
    assert ExchangeShopRun().run(context,SimpleNamespace())
    assert ExchangeShopConfigure().run(context,SimpleNamespace())


def test_stop_after_foreground_check_does_not_click(monkeypatch):
    clicks=[]
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False,controller=SimpleNamespace(post_click=lambda *p:clicks.append(p))))
    ui=ExchangeUI(context)
    def foreground(controller):context.tasker.stopping=True
    monkeypatch.setattr("agent.realtime.exchange_shop.require_game_foreground",foreground)
    with pytest.raises(ExchangeStopped):ui.click(1,2)
    assert clicks == []


def test_stop_during_capture_prevents_recognition():
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    recognition=[]
    reader=SimpleNamespace(page=lambda *args:recognition.append(True),readings={})
    def capture(context):
        context.tasker.stopping=True
        return np.zeros((720,1280,3),np.uint8)
    ui=ExchangeUI(context,reader=reader,capture=capture)
    with pytest.raises(ExchangeStopped):ui.stable("main")
    assert recognition == []


def test_stop_during_consumption_keeps_pending_and_no_followup(tmp_path):
    ui=UI([state()])
    def draw(amount):
        ui.actions.append(("draw",amount))
        raise ExchangeStopped()
    ui.draw_once=draw
    with pytest.raises(ExchangeStopped):flow(ui,tmp_path).run()
    assert ui.actions == ["settings","read",("draw",10)]
    with pytest.raises(RuntimeError):ExchangeJournal(tmp_path/"ledger.json").check_unresolved()


def test_stable_requires_two_matching_new_frames():
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    captures=[]
    reader=SimpleNamespace(page=lambda image,name:True,readings={})
    ui=ExchangeUI(context,reader=reader,capture=lambda context:captures.append(True),sleep=lambda seconds:None)
    reads=iter([1,2,2])
    assert ui.stable("main",lambda image:next(reads)) == 2
    assert len(captures)==3


def test_config_is_current_task_only_and_run_cleans_it(monkeypatch):
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    argv=SimpleNamespace(task_detail=SimpleNamespace(task_id=1701),custom_action_param='{"reset":true,"mode":"auto"}')
    assert ExchangeShopConfigure().run(context,argv)
    argv.custom_action_param='{"max_draws":30}'
    assert ExchangeShopConfigure().run(context,argv)
    assert _OPTIONS[1701]=={"mode":"auto","execute":False,"max_draws":30,"currency_budget":0}
    def stop_ui(context):raise ExchangeStopped()
    monkeypatch.setattr("agent.realtime.exchange_shop.ExchangeUI",stop_ui)
    assert ExchangeShopRun().run(context,argv)
    assert 1701 not in _OPTIONS


def test_unknown_confirmation_is_never_clicked(monkeypatch):
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    ui=ExchangeUI(context);ui.current_state=state()
    clicks=[]
    values=iter([(19,180,180,1,30822),(10,100),RuntimeError("unknown popup")])
    def stable(*args,**kwargs):
        value=next(values)
        if isinstance(value,Exception):raise value
        return value
    ui.stable=stable;ui.click=lambda *p:clicks.append(p)
    with pytest.raises(RuntimeError):ui.draw_once(10)
    assert clicks == [(1025,618)]


def test_confirmation_balance_mismatch_is_never_confirmed():
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    ui=ExchangeUI(context);ui.current_state=state()
    clicks=[];values=iter([(19,180,180,1,30822),(10,100),(10,100,30822,30723)])
    ui.stable=lambda *a,**k:next(values);ui.click=lambda *p:clicks.append(p)
    with pytest.raises(RuntimeError):ui.draw_once(10)
    assert clicks == [(1025,618)]


def test_complete_draw_only_clicks_right_result_close():
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    ui=ExchangeUI(context);ui.current_state=state()
    values=iter([(19,180,180,1,30822),(10,100),(10,100,30822,30722),30722,(19,170,180,1,30722)])
    clicks=[];ui.stable=lambda *a,**k:next(values);ui.click=lambda *p:clicks.append(p)
    ui.draw_once(10)
    assert clicks == [(1025,618),(770,525),(1068,648)]


def test_policy_logs_explain_unlimited_and_rechecking(tmp_path):
    messages=[]
    assert ExchangeFlow(UI([state()]),{"execute":False},report=messages.append).run()
    assert "可用预算=3082抽" in messages[0] and "重读并重算" in messages[0]
    assert "N=" not in messages[0]
    assert "抽数上限=不限" in messages[0]
    assert "None" not in "".join(messages)


def reset_ui(frames):
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    def page(image,name):
        if name=="main":return isinstance(image,tuple)
        if name=="reset_done":return isinstance(image,str) and image in ("done","wrong_done")
        return True
    reader=SimpleNamespace(main=lambda image:image,page=page,matches=lambda *args:True,
        reset_confirmation=lambda image:True,reset_done=lambda image:image=="done",readings={})
    sequence=iter(frames);captures=[]
    def capture(context):
        value=next(sequence);captures.append(value);return value
    ui=ExchangeUI(context,reader=reader,capture=capture,sleep=lambda seconds:None)
    ui.current_state=state(remaining=140,cans=7,special=0,currency=30422)
    clicks=[];ui.click=lambda *p:clicks.append(p)
    return ui,clicks,captures


def test_reset_waits_through_two_old_box_frames_then_new_box():
    old=(19,140,180,0,30422);new=(20,180,180,1,30422)
    ui,clicks,captures=reset_ui([old,old,True,True,old,old,new,new])
    ui.reset_once()
    assert clicks == [(773,465),(769,526)]
    assert len(captures)==8 and captures[-2:]==[new,new]


def test_reset_wait_exhaustion_never_resends_confirmation():
    old=(19,140,180,0,30422)
    ui,clicks,captures=reset_ui([old,old,True,True]+[old]*100)
    with pytest.raises(RuntimeError,match="未稳定"):ui.reset_once()
    assert clicks == [(773,465),(769,526)]
    assert len(captures)==104


@pytest.mark.parametrize("box", (state(remaining=140,cans=7,special=1),state(remaining=0,cans=0,special=0)))
def test_reset_refuses_locked_special_or_empty_box(box):
    ui,clicks,captures=reset_ui([]);ui.current_state=box
    with pytest.raises(RuntimeError,match="不允许手动换箱"):ui.reset_once()
    assert clicks == [] and captures == []


def test_reset_main_change_or_disabled_button_blocks_before_open():
    ui,clicks,_=reset_ui([(19,130,180,0,30422)]*2)
    with pytest.raises(RuntimeError,match="主页面已变化"):ui.reset_once()
    assert clicks == []
    ui,clicks,_=reset_ui([(19,140,180,0,30422)]*2)
    ui.reader.matches=lambda *args:False
    with pytest.raises(RuntimeError,match="按钮未确认可用"):ui.reset_once()
    assert clicks == []


def test_unknown_reset_popup_never_confirms():
    old=(19,140,180,0,30422)
    ui,clicks,_=reset_ui([old,old]+[False]*10)
    def reject(image):raise ValueError("这是交换确认")
    ui.reader.reset_confirmation=reject
    with pytest.raises(RuntimeError):ui.reset_once()
    assert clicks == [(773,465)]


@pytest.mark.parametrize("stop_at", ((773,465),(769,526)))
def test_reset_stop_sends_no_followup_input(stop_at):
    old=(19,140,180,0,30422)
    ui,clicks,_=reset_ui([old,old,True,True])
    def click(*target):
        clicks.append(target)
        if target==stop_at:ui.context.tasker.stopping=True
    ui.click=click
    with pytest.raises(ExchangeStopped):ui.reset_once()
    assert clicks[-1]==stop_at


def test_reset_stop_preserves_pending_ledger(tmp_path):
    ui=UI([state(cans=0,special=0)])
    def reset():
        ui.actions.append("reset")
        raise ExchangeStopped()
    ui.reset_once=reset
    with pytest.raises(ExchangeStopped):flow(ui,tmp_path).run()
    assert ui.actions == ["settings","read","reset"]
    with pytest.raises(RuntimeError):ExchangeJournal(tmp_path/"ledger.json").check_unresolved()


def test_reset_success_prompt_acknowledges_once_then_checks_new_main():
    old=(19,140,180,0,30422);new=(20,180,180,1,30422)
    ui,clicks,captures=reset_ui([old,old,True,True,"done","done",new,new])
    ui.reset_once()
    assert clicks == [(773,465),(769,526),(640,448)]
    assert captures[-2:]==[new,new]


def test_finish_reset_only_acknowledges_and_never_sends_reset_confirmation():
    new=(20,180,180,1,30422)
    ui,clicks,_=reset_ui(["done","done",new,new])
    assert ui.finish_reset_result(ui.current_state)==new
    assert clicks == [(640,448)]


def test_reset_without_success_prompt_still_accepts_new_main():
    new=(20,180,180,1,30422)
    ui,clicks,_=reset_ui([new,new])
    assert ui.finish_reset_result(ui.current_state)==new
    assert clicks == []


def test_repeated_reset_done_only_acknowledged_once():
    new=(20,180,180,1,30422)
    ui,clicks,_=reset_ui(["done"]*4+[new,new])
    assert ui.finish_reset_result(ui.current_state)==new
    assert clicks == [(640,448)]
    ui,clicks,_=reset_ui(["done"]*100)
    with pytest.raises(RuntimeError,match="未稳定"):ui.finish_reset_result(ui.current_state)
    assert clicks == [(640,448)]


def test_wrong_or_unknown_reset_done_sends_no_acknowledgement():
    for frame in ("wrong_done","unknown"):
        ui,clicks,_=reset_ui([frame]*100)
        with pytest.raises(RuntimeError,match="未稳定"):ui.finish_reset_result(ui.current_state)
        assert clicks == []


def test_stop_before_reset_ack_sends_no_input():
    ui,clicks,_=reset_ui(["done","done"])
    capture=ui.capture;count=0
    def stop_capture(context):
        nonlocal count
        count+=1
        image=capture(context)
        if count==2:context.tasker.stopping=True
        return image
    ui.capture=stop_capture
    with pytest.raises(ExchangeStopped):ui.finish_reset_result(ui.current_state)
    assert clicks == []


def test_stop_during_reset_ack_prevents_followup_capture_or_input():
    ui,clicks,captures=reset_ui(["done","done"])
    def click(*target):
        clicks.append(target);ui.context.tasker.stopping=True
    ui.click=click
    with pytest.raises(ExchangeStopped):ui.finish_reset_result(ui.current_state)
    assert clicks == [(640,448)] and len(captures)==2


FIXTURES=Path(__file__).resolve().parents[1]/".local/exchange-shop-20261024"


@pytest.mark.skipif(not (FIXTURES/"contents-page.png").exists(),reason="本地真实截图不入库")
def test_local_real_pages_use_production_ocr():
    from agent.realtime.vision_io import imread_unicode
    reader=ExchangeReader()
    main=imread_unicode(FIXTURES/"main-after-contents.png")
    assert reader.main(main)==(19,180,180,1,30822)
    assert reader.contents(imread_unicode(FIXTURES/"contents-page.png"))==(180,180,10,1)
    assert reader.settings(imread_unicode(FIXTURES/"settings-page.png"))==(True,True)
    with pytest.raises(ValueError):reader.main((main.astype(float)*.6).astype(np.uint8))


@pytest.mark.skipif(not (FIXTURES/"ten-01-result.png").exists(),reason="本地消费样本不入库")
def test_local_real_confirmation_and_result_exclude_animation():
    from agent.realtime.vision_io import imread_unicode
    reader=ExchangeReader()
    for file in ("ten-01-early.png","ten-01-late.png"):
        assert reader.draw_confirmation(imread_unicode(FIXTURES/file))==(10,100,30822,30722)
    assert reader.draw_result(imread_unicode(FIXTURES/"ten-01-result.png"))==30722
    for file in ("main-after-contents.png","ten-01-early.png","ten-01-reward-early.png","ten-01-reward-late.png"):
        with pytest.raises(ValueError):reader.draw_result(imread_unicode(FIXTURES/file))


@pytest.mark.skipif(not (FIXTURES/"ten-01-main-stable.png").exists(),reason="本地消费样本不入库")
def test_local_post_draw_reconciliation_rejects_sliding_panel():
    from agent.realtime.vision_io import imread_unicode
    reader=ExchangeReader()
    assert reader.main(imread_unicode(FIXTURES/"ten-01-main-stable.png"))==(19,170,180,1,30722)
    assert reader.contents(imread_unicode(FIXTURES/"ten-01-contents.png"))==(170,180,10,1)
    with pytest.raises(ValueError):reader.main(imread_unicode(FIXTURES/"ten-01-main.png"))
    validate_draw(state(),state(remaining=170,currency=30722),10)


@pytest.mark.skipif(not (FIXTURES/"replay/device-v3-failure.png").exists(),reason="本地特别礼物已获得截图不入库")
def test_local_special_zero_uses_tight_roi_without_lowering_confidence():
    from agent.realtime.song_title_ocr import recognize_song_title
    from agent.realtime.vision_io import imread_unicode
    image=imread_unicode(FIXTURES/"replay/device-v3-failure.png")
    old=recognize_song_title(image,roi=(424,291,57,31))
    assert old is not None and old.confidence < .90
    reader=ExchangeReader()
    assert reader.contents(image)==(150,180,9,0)
    assert reader.readings["contents_special"]["roi"]==(433,295,42,23)
    assert reader.readings["contents_special"]["confidence"] >= .90
    validate_draw(state(remaining=160,currency=30622),state(remaining=150,cans=9,special=0,currency=30522),10)


@pytest.mark.skipif(not (FIXTURES/"reset-confirm-b.png").exists(),reason="本地换箱确认截图不入库")
def test_local_reset_confirmation_is_specific_and_draw_is_rejected():
    from agent.realtime.vision_io import imread_unicode
    reader=ExchangeReader()
    for file in ("reset-confirm-a.png","reset-confirm-b.png"):
        image=imread_unicode(FIXTURES/file)
        assert reader.reset_confirmation(image) is True
        with pytest.raises(ValueError):reader.draw_confirmation(image)
    for file in ("ten-01-early.png","ten-01-late.png","contents-page.png","settings-page.png","ten-01-result.png"):
        with pytest.raises(ValueError):reader.reset_confirmation(imread_unicode(FIXTURES/file))
    main=imread_unicode(FIXTURES/"reset-before-main.png")
    assert reader.main(main)==(19,140,180,0,30422)
    assert reader.matches(main,"main_reset_enabled")
    assert not reader.matches(imread_unicode(FIXTURES/"main-after-contents.png"),"main_reset_enabled")


@pytest.mark.skipif(not (FIXTURES/"replay/device-v5-failure.png").exists(),reason="本地已重置提示截图不入库")
def test_local_reset_done_requires_specific_title_body_and_ack():
    from agent.realtime.vision_io import imread_unicode
    reader=ExchangeReader()
    image=imread_unicode(FIXTURES/"replay/device-v5-failure.png")
    assert reader.reset_done(image) is True
    for file in ("reset-done-a.png","reset-done-b.png"):
        assert reader.reset_done(imread_unicode(FIXTURES/file)) is True
    for file in ("reset-confirm-a.png","reset-confirm-b.png","ten-01-early.png","ten-01-result.png","contents-page.png","settings-page.png"):
        with pytest.raises(ValueError):reader.reset_done(imread_unicode(FIXTURES/file))
    partial=image.copy();partial[324:355,519:762]=255
    with pytest.raises(ValueError):reader.reset_done(partial)


def test_regression_finite_budget_can_reverse_long_run_choice():
    assert decide(state(remaining=20,cans=1,special=0), budget=10).action == "reset"
    assert decide(state(remaining=10,cans=1,special=0), budget=10).action == "draw"


@pytest.mark.parametrize("amount", (5,10))
def test_regression_empty_box_automatically_advances(amount):
    before=state(remaining=amount,cans=1,special=0,currency=1000)
    after=state(box=20,currency=1000-10*amount)
    validate_draw(before,after,amount)


def test_regression_long_run_high_density_continues():
    assert decide(state(remaining=20,cans=1,special=0)).action == "draw"
    assert decide(state(remaining=60,cans=1,special=0)).action == "reset"


@pytest.mark.parametrize("total", (180,230))
def test_finite_dp_matches_independent_fraction_bellman(total):
    from fractions import Fraction
    from functools import lru_cache
    from math import comb
    from agent.realtime.exchange_shop_policy import FiniteBudgetPolicy

    @lru_cache(None)
    def exact(b,x,y,s):
        if x == 0:
            return exact(b,total,10,1)
        n=min(10,x)
        draw=Fraction(0)
        if b >= n:
            for got in range(min(n,y)+1):
                for special in range(s+1):
                    other=n-got-special
                    if 0 <= other <= x-y-s:
                        p=Fraction(comb(y,got)*comb(s,special)*comb(x-y-s,other),comb(x,n))
                        draw+=p*(got+exact(b-n,x-n,y-got,s-special))
        return max(draw,exact(b,total,10,1)) if s == 0 else draw

    policy=FiniteBudgetPolicy(total)
    probes=((7,1,0),(10,1,0),(17,1,0),(20,1,0),(30,1,0),(20,2,1),(total,10,1))
    for budget in (0,7,10,17,20,30):
        policy.extend(budget,lambda:None)
        for probe in probes:
            actual=policy.values[budget%11][policy.index[probe]]
            assert actual == pytest.approx(float(exact(budget,*probe)),abs=2e-13)
            decision=decide(state(total=total,remaining=probe[0],cans=probe[1],special=probe[2]),budget)
            reset_value=exact(budget,total,10,1)
            if decision.action == "reset":
                assert probe[2] == 0 and reset_value == exact(budget,*probe)


@pytest.mark.parametrize("total,expected", ((180,172.73183118016208),(230,135.74040279370951)))
def test_normal_balance_dp_matches_independent_decimal_benchmark(total,expected):
    from agent.realtime.exchange_shop_policy import FiniteBudgetPolicy
    policy=FiniteBudgetPolicy(total)
    policy.extend(2812,lambda:None)
    assert policy.values[2812%11][policy.full] == pytest.approx(expected,abs=1e-10)


@pytest.mark.parametrize("budget,action", ((10,"reset"),(20,"reset"),(50,"draw"),(200,"reset"),(2810,"draw")))
def test_finite_policy_does_not_assume_large_budget_monotonicity(budget,action):
    assert decide(state(total=230,remaining=30,cans=1,special=0),budget).action == action


def test_dp_cancel_is_immediate_and_cache_resumes_completed_layers():
    from agent.realtime.exchange_shop_policy import FiniteBudgetPolicy
    policy=FiniteBudgetPolicy(180)
    calls=[]
    def stopped():
        calls.append(True)
        if len(calls)==4:raise ExchangeStopped()
    with pytest.raises(ExchangeStopped):policy.extend(100,stopped)
    assert policy.horizon == 3
    policy.extend(10,lambda:None)
    assert policy.horizon == 10 and len(policy.actions)==11


def test_exact_budget_limit_and_no_reward_stop_do_not_consume(tmp_path):
    ui=UI([state(currency=100010)])
    with pytest.raises(ValueError,match="预算过大"):flow(ui,tmp_path).run()
    assert ui.actions == [] and not (tmp_path/"ledger.json").exists()
    assert decide(state(remaining=20,cans=0,special=0),budget=9).action == "stop"


def test_flow_uses_smaller_of_coin_and_cumulative_draw_budget(tmp_path):
    before=state(remaining=20,cans=1,special=0,currency=100)
    after=state(box=20,currency=100)
    end=state(box=20,remaining=170,currency=0)
    ui=UI([before,after,end])
    messages=[]
    f=flow(ui,tmp_path,max_draws=100,currency_budget=100)
    f.report=messages.append
    assert f.run() and f.draws == 10
    assert ui.actions == ["settings","read","reset","read",("draw",10),"read"]
    assert "可用预算=10抽" in messages[0]


@pytest.mark.parametrize("changes", ({"box":21},{"remaining":179},{"cans":9},{"special":0},{"currency":899},
    {"total":230,"remaining":230}))
def test_auto_new_box_requires_exact_next_full_state(changes):
    with pytest.raises(RuntimeError):
        validate_draw(state(remaining=10,cans=1,special=0,currency=1000),replace(state(box=20,currency=900),**changes),10)
    with pytest.raises(RuntimeError):
        validate_draw(state(remaining=20,cans=1,special=0,currency=1000),state(box=20,currency=900),10)


@pytest.mark.parametrize("amount", (5,10))
def test_last_batch_passively_waits_and_only_closes_result_and_optional_done(amount):
    old=(19,0,180,0,1000-10*amount);new=(20,180,180,1,1000-10*amount)
    ui,clicks,_=reset_ui([old,old,"done","done","done",new,new])
    ui.stable=lambda *args,**kwargs:1000-10*amount
    ui.reader.draw_result=lambda image:1000-10*amount
    ui.finish_draw_result(1000-10*amount,state(remaining=amount,cans=1,special=0,currency=1000))
    assert clicks == [(1068,648),(640,448)]


def test_empty_start_waits_for_auto_new_box_before_opening_contents():
    old=(19,0,180,0,30422);new=(20,180,180,1,30422)
    ui,clicks,captures=reset_ui([old,old,new,new,"contents","contents",new,new])
    ui.reader.contents=lambda image:(180,180,10,1)
    assert ui.read_state() == state(box=20,currency=30422)
    assert clicks == [(1029,465),(640,642)]
    assert captures[:4]==[old,old,new,new]


def test_auto_new_box_timeout_never_clicks_next_or_confirms_reset():
    old=(19,0,180,0,30422)
    ui,clicks,_=reset_ui([old]*100)
    with pytest.raises(RuntimeError,match="未稳定"):ui.wait_new_box(old,30422)
    assert clicks == []


def test_manual_not_executed_resolution_preserves_pending_audit(tmp_path):
    import json
    journal=ExchangeJournal(tmp_path/"ledger.json")
    before=state(remaining=60,cans=6,special=0,currency=28122)
    journal.reserve(before,decide(before,budget=2812))
    original=json.loads(journal.path.read_text(encoding="utf-8"))
    with pytest.raises(RuntimeError):journal.resolve_not_executed(replace(before,currency=28121),{"main":"frames"})
    with pytest.raises(ValueError):journal.resolve_not_executed(before,{})
    journal.resolve_not_executed(before,{"main":["new-1","new-2"],"contents":["new-3","new-4"]})
    resolved=json.loads(journal.path.read_text(encoding="utf-8"))
    assert resolved["status"] == "resolved_not_executed"
    assert all(resolved[key] == original[key] for key in ("before","decision","created_ns"))
    assert resolved["after"] == resolved["before"] and resolved["resolution"]["manual"]
    journal.check_unresolved()


@pytest.mark.parametrize("cost,amount", (("消费蓝色礼物币100枚",10),("消费红币50枚",5),("活动兑换用礼物币100枚",10)))
def test_currency_name_and_color_are_not_used_in_confirmation(cost,amount):
    reader,image,_=confirmation_reader()
    balance=str(1000-amount*10)
    values=iter([cost,f"领取礼物{amount}个奖品。","确认吗？","1000","1000",balance,balance])
    reader.ocr=lambda *args,**kwargs:SimpleNamespace(text=next(values),confidence=.99)
    assert reader.draw_confirmation(image) == (amount,amount*10,1000,1000-amount*10)


@pytest.mark.parametrize("cost", ("消费第2种礼物币100枚","消费礼物币1000枚","消费礼物币100","消费礼物币10.0枚","消费礼物币-100枚"))
def test_confirmation_requires_one_unsigned_integer_cost_and_unit(cost):
    reader=ExchangeReader();reader.page=lambda *args:True
    values=iter([cost,"领取礼物10个奖品。","确认吗？"])
    reader.ocr=lambda *args,**kwargs:SimpleNamespace(text=next(values),confidence=.99)
    with pytest.raises(ValueError):reader.draw_confirmation(None)


@pytest.mark.skipif(not (FIXTURES/"ten-01-result.png").exists(),reason="本地真实截图不入库")
def test_synthetic_currency_color_and_label_changes_keep_numeric_contract():
    from agent.realtime.vision_io import imread_unicode
    reader=ExchangeReader()
    for file,kind,expected in (("main-after-contents.png","main",(19,180,180,1,30822)),
                               ("ten-01-early.png","draw_confirm",(10,100,30822,30722)),
                               ("ten-01-result.png","draw_result",30722)):
        image=imread_unicode(FIXTURES/file)
        # 合成变更仅证明签名不绑定货币外观，不冒充另一活动真机样本。
        regions=((554,387,153,30),) if kind == "draw_confirm" else ((671,540,192,30),)
        for x,y,w,h in regions:
            image[y:y+h,x:x+w]=[31,80,220]
        read={"main":reader.main,"draw_confirm":reader.draw_confirmation,"draw_result":reader.draw_result}[kind]
        assert read(image)==expected


def manual_clear_evidence():
    return {"operator_confirmed_manual_full_clear":True,
            "fresh_main_frames":["main-a.png","main-b.png"],
            "fresh_contents_frames":["contents-a.png","contents-b.png"]}


def test_manual_external_full_clear_is_narrow_audited_terminal(tmp_path):
    import json
    from agent.realtime.exchange_shop_policy import Decision
    journal=ExchangeJournal(tmp_path/"ledger.json")
    before=state(box=22,remaining=60,cans=6,special=0,currency=28122)
    journal.reserve(before,Decision("reset"))
    original=json.loads(journal.path.read_text(encoding="utf-8"))
    after=state(box=23,currency=27522)
    journal.resolve_external_full_clear(after,manual_clear_evidence())
    resolved=json.loads(journal.path.read_text(encoding="utf-8"))
    assert all(resolved[key] == original[key] for key in ("before","decision","created_ns"))
    assert resolved["status"] == "resolved_external_full_clear"
    assert resolved["resolution"]["old_reset"] == "not_executed"
    assert resolved["resolution"]["actual_draws"] == 60
    assert resolved["resolution"]["actual_cost"] == 600
    journal.check_unresolved()
    resolved["resolution"]["actual_cost"]=0
    journal.path.write_text(json.dumps(resolved),encoding="utf-8")
    with pytest.raises(RuntimeError):journal.check_unresolved()


@pytest.mark.parametrize("changes", ({"box":22},{"box":24},{"currency":28122},{"currency":27521},
    {"remaining":179},{"cans":9},{"special":0},{"total":230,"remaining":230}))
def test_manual_external_clear_rejects_other_states_without_changing_pending(tmp_path,changes):
    from agent.realtime.exchange_shop_policy import Decision
    journal=ExchangeJournal(tmp_path/"ledger.json")
    journal.reserve(state(box=22,remaining=60,cans=6,special=0,currency=28122),Decision("reset"))
    original=journal.path.read_bytes()
    with pytest.raises(RuntimeError):
        journal.resolve_external_full_clear(replace(state(box=23,currency=27522),**changes),manual_clear_evidence())
    assert journal.path.read_bytes() == original


@pytest.mark.parametrize("changes", ({"operator_confirmed_manual_full_clear":False},
    {"operator_confirmed_manual_full_clear":1},{"fresh_main_frames":[]},
    {"fresh_contents_frames":["one"]},{"fresh_main_frames":["same","same"]}))
def test_manual_external_clear_requires_declaration_and_two_distinct_frames(tmp_path,changes):
    from agent.realtime.exchange_shop_policy import Decision
    journal=ExchangeJournal(tmp_path/"ledger.json")
    journal.reserve(state(box=22,remaining=60,cans=6,special=0,currency=28122),Decision("reset"))
    original=journal.path.read_bytes()
    with pytest.raises(ValueError):
        journal.resolve_external_full_clear(state(box=23,currency=27522),manual_clear_evidence()|changes)
    assert journal.path.read_bytes() == original


def test_manual_external_clear_cannot_resolve_draw_pending(tmp_path):
    from agent.realtime.exchange_shop_policy import Decision
    journal=ExchangeJournal(tmp_path/"ledger.json")
    journal.reserve(state(box=22,remaining=60,cans=6,special=0,currency=28122),Decision("draw",10))
    with pytest.raises(RuntimeError):journal.resolve_external_full_clear(state(box=23,currency=27522),manual_clear_evidence())


@pytest.mark.parametrize("total", (180,230))
@pytest.mark.parametrize("remaining", (10,180))
def test_zero_can_locked_box_does_not_spend_last_batch_for_zero_reward(total,remaining):
    box=state(total=total,remaining=remaining,cans=0,special=1,currency=1000)
    assert decide(box,budget=10).action == "stop"
    assert decide(box,budget=20).action == "draw"


def test_zero_reward_stop_sends_no_consumption_and_does_not_create_pending(tmp_path):
    ui=UI([state(remaining=180,cans=0,special=1,currency=100)])
    assert flow(ui,tmp_path).run()
    assert ui.actions == ["settings","read"]
    assert not (tmp_path/"ledger.json").exists()


def navigation_ui(frames):
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    sequence=iter(frames);captures=[];clicks=[]
    def page(image,name):
        return isinstance(image,tuple) if name == "main" else image == name
    reader=SimpleNamespace(page=page,main=lambda image:image,contents=lambda image:(180,180,10,1),
        settings=lambda image:(True,True),reset_confirmation=lambda image:True,
        matches=lambda *args:True,readings={"contents_remaining":{"text":"30/180"},"cans":{"text":"0/10"}})
    def capture(context):
        value=next(sequence)
        captures.append(value)
        if isinstance(value,Exception):raise value
        return value
    ui=ExchangeUI(context,reader=reader,capture=capture,sleep=lambda seconds:None)
    ui.click=lambda *point:clicks.append(point)
    return ui,clicks,captures


def test_contents_open_retries_delivery_after_unchanged_main_then_reads_new_box():
    main=(29,180,180,1,19522)
    ui,clicks,captures=navigation_ui([main]*14+["contents"]*2+[main]*2)
    assert ui.read_state() == state(box=29,currency=19522)
    assert clicks == [(1029,465),(1029,465),(640,642)]
    assert len(captures) == 18


def test_contents_open_three_misses_only_retries_non_consuming_entry():
    main=(29,180,180,1,19522)
    ui,clicks,captures=navigation_ui([main]*36)
    with pytest.raises(RuntimeError,match="入口未稳定"):ui.read_state()
    assert clicks == [(1029,465)]*3 and len(captures)==36
    assert ui.navigation["attempt"] == 3 and not ui.navigation["target_seen"]
    assert "contents_remaining" not in ui.reader.readings and "cans" not in ui.reader.readings


@pytest.mark.parametrize("changed", ((30,180,180,1,19522),(29,170,180,1,19522),
    (29,180,230,1,19522),(29,180,180,0,19522),(29,180,180,1,19422)))
def test_contents_open_changed_main_stops_before_retry(changed):
    main=(29,180,180,1,19522)
    ui,clicks,captures=navigation_ui([main]*2+[changed])
    with pytest.raises(RuntimeError,match="主页面已变化"):ui.read_state()
    assert clicks == [(1029,465)] and len(captures)==3


def test_unknown_opening_animation_can_only_wait_for_target_not_retry_entry():
    main=(29,180,180,1,19522)
    ui,clicks,_=navigation_ui([main]*2+["unknown"]+["contents"]*2+[main]*2)
    assert ui.read_state() == state(box=29,currency=19522)
    assert clicks == [(1029,465),(640,642)]
    ui,clicks,_=navigation_ui([main]*2+["unknown"]+[main]*9)
    with pytest.raises(RuntimeError,match="入口未稳定"):ui.read_state()
    assert clicks == [(1029,465)]


@pytest.mark.parametrize("guard", ("unknown",(29,180,180,1,19422)))
def test_retry_requires_two_extra_new_frames_of_exact_original_main(guard):
    main=(29,180,180,1,19522)
    ui,clicks,captures=navigation_ui([main]*12+[main,guard])
    with pytest.raises(RuntimeError):ui.read_state()
    assert clicks == [(1029,465)] and len(captures)==14


def test_target_arrives_during_retry_guard_no_second_opener_is_sent():
    main=(29,180,180,1,19522)
    ui,clicks,_=navigation_ui([main]*12+["contents"]*3+[main]*2)
    assert ui.read_state() == state(box=29,currency=19522)
    assert clicks == [(1029,465),(640,642)]


def test_target_signature_seen_but_bad_ocr_never_reopens_or_uses_old_contents():
    main=(29,180,180,1,19522)
    ui,clicks,_=navigation_ui([main]*2+["contents"]*10)
    def bad_ocr(image):raise ValueError("本次数字低置信")
    ui.reader.contents=bad_ocr
    with pytest.raises(RuntimeError,match="本次数字低置信"):ui.read_state()
    assert clicks == [(1029,465)] and ui.navigation["target_seen"]
    assert ui.reader.readings == {}
    ui,clicks,_=navigation_ui([main]*2+["contents"]+[main]*9)
    with pytest.raises(RuntimeError):ui.read_state()
    assert clicks == [(1029,465)] and ui.navigation["target_seen"]


def test_unreadable_main_in_wait_never_authorizes_retry():
    main=(29,180,180,1,19522)
    ui,clicks,_=navigation_ui([main]*12)
    original=ui.reader.main;reads=0
    def fail_new_main(image):
        nonlocal reads
        reads+=1
        if reads>2:raise ValueError("主页面数字低置信")
        return original(image)
    ui.reader.main=fail_new_main
    with pytest.raises(RuntimeError,match="入口未稳定"):ui.read_state()
    assert clicks == [(1029,465)]


def test_capture_exception_after_opener_never_retries():
    main=(29,180,180,1,19522)
    ui,clicks,captures=navigation_ui([main]*2+[RuntimeError("截图框架错误")])
    with pytest.raises(RuntimeError,match="截图框架错误"):ui.read_state()
    assert clicks == [(1029,465)] and len(captures)==3


@pytest.mark.parametrize("stop_phase", ("before","after_click","during_capture","retry_guard"))
def test_open_navigation_stop_prevents_any_followup_input_or_capture(stop_phase):
    main=(29,180,180,1,19522)
    ui,clicks,captures=navigation_ui([main]*14)
    if stop_phase == "before":
        ui.context.tasker.stopping=True
    elif stop_phase == "after_click":
        def click(*point):
            clicks.append(point);ui.context.tasker.stopping=True
        ui.click=click
    elif stop_phase == "during_capture":
        original=ui.capture
        def capture(context):
            image=original(context)
            if len(captures)==3:context.tasker.stopping=True
            return image
        ui.capture=capture
    else:
        original=ui.reader.main
        def stop_main(image):
            if len(captures)==14:ui.context.tasker.stopping=True
            return original(image)
        ui.reader.main=stop_main
    with pytest.raises(ExchangeStopped):ui.read_state()
    assert len(captures) == {"before":0,"after_click":2,"during_capture":3,"retry_guard":14}[stop_phase]
    assert clicks == ([] if stop_phase == "before" else [(1029,465)])


def test_settings_opener_uses_same_delivery_retry_without_changing_options():
    main=(29,180,180,1,19522)
    ui,clicks,_=navigation_ui([main]*14+["settings"]*2+[main]*2)
    ui.check_settings()
    assert clicks == [(1068,127),(1068,127),(640,603)]


def test_manual_reset_only_retries_opener_and_confirm_stays_single():
    main=(28,30,180,0,19522);new=(29,180,180,1,19522)
    ui,clicks,_=navigation_ui([main]*14+["reset_confirm"]*2+[new]*2)
    ui.current_state=state(box=28,remaining=30,cans=0,special=0,currency=19522)
    ui.reset_once()
    assert clicks == [(773,465),(773,465),(769,526)]


DELIVERY_FAILURE=Path(__file__).resolve().parents[1]/"debug/exchange-shop/1792863019256890400/failure.png"


@pytest.mark.skipif(not DELIVERY_FAILURE.exists(),reason="本地真实导航失败图不入库")
def test_real_delivery_failure_is_new_box_main_and_not_contents():
    from agent.realtime.vision_io import imread_unicode
    reader=ExchangeReader()
    image=imread_unicode(DELIVERY_FAILURE)
    assert reader.main(image) == (29,180,180,1,19522)
    assert not reader.page(image,"contents")
    with pytest.raises(ValueError):reader.contents(image)


CONFIRM_FAILURE=Path(__file__).resolve().parents[1]/"debug/exchange-shop/1792889140576223500/failure.png"


@pytest.mark.skipif(not CONFIRM_FAILURE.exists(),reason="本地真实确认失败图不入库")
def test_real_confirmation_four_digit_balance_reads_then_reconciles():
    from agent.realtime.song_title_ocr import recognize_song_title
    from agent.realtime.vision_io import imread_unicode
    image=imread_unicode(CONFIRM_FAILURE)
    original=recognize_song_title(image,roi=(733,425,106,33))
    assert original is not None and original.confidence < .90
    reader=ExchangeReader()
    assert reader.draw_confirmation(image) == (10,100,10022,9922)
    assert reader.readings["confirm_after"]["confidence"] >= .90
    assert reader.readings["confirm_after"]["roi"] == (759,429,57,24)
    validate_draw(state(box=37,remaining=100,cans=6,special=0,currency=10022),
                  state(box=37,remaining=90,cans=6,special=0,currency=9922),10)


BALANCE_ROIS = {"confirm_before": (454,425,105,33), "confirm_after": (733,425,106,33)}


def confirmation_reader(after="9922", *, original_after="9922", original_confidence=.87, tight_confidence=.999,
                        field="confirm_after"):
    image=np.full((720,1280,3),255,np.uint8)
    values={"confirm_before":"10022","confirm_after":"9922"}
    values[field]=after
    # 两个字段都保留完整墨迹，避免固定高置信的另一字段掩盖遗漏。
    for key,(x,y,w,h) in BALANCE_ROIS.items():
        width=len(values[key])*8-4
        left=x+(w-width)//2
        for index in range(len(values[key])):
            image[y+7:y+25,left+index*8:left+index*8+4]=(255,170,80)
    calls=[]
    fixed={(460,251,360,31):"消费礼物币100枚",
           (528,281,221,31):"领取礼物10个奖品。",(586,311,115,30):"确认吗？"}
    def ocr(image, *, roi):
        calls.append(roi)
        if roi in fixed:return SimpleNamespace(text=fixed[roi],confidence=.999)
        key=next(key for key,(x,y,w,h) in BALANCE_ROIS.items() if x <= roi[0] < x+w)
        if roi == BALANCE_ROIS[field]:
            return SimpleNamespace(text=original_after,confidence=original_confidence)
        return SimpleNamespace(text=values[key],confidence=tight_confidence if key==field else .999)
    reader=ExchangeReader(ocr=ocr)
    reader.page=lambda *args:True
    return reader,image,calls


@pytest.mark.parametrize("field", BALANCE_ROIS)
def test_confirmation_low_confidence_balance_failure_pattern(field):
    reader,image,_=confirmation_reader("9922",field=field,original_confidence=.841663)
    expected=(10,100,9922,9922) if field=="confirm_before" else (10,100,10022,9922)
    assert reader.draw_confirmation(image)==expected
    assert reader.readings[field]["method"]=="complete_ink_crop"


@pytest.fixture(params=BALANCE_ROIS)
def balance_field(request):
    return request.param


def read_balance(reader,image,field):
    return reader.confirmation_balance(image,field,BALANCE_ROIS[field])


@pytest.mark.parametrize("value", ("0","22","122","9922","10022","999999999"))
def test_confirmation_ink_crop_handles_full_integer_widths(balance_field,value):
    field=balance_field
    reader,image,calls=confirmation_reader(value,original_after=value,field=field)
    assert read_balance(reader,image,field)==value
    x,y,w,h=calls[-1]
    assert (x,y,w,h)!=BALANCE_ROIS[field]
    assert np.all(image[y:y+h,x:x+w][[0,-1]]==255)
    assert reader.readings[field+"_original"]["confidence"]==.87
    assert reader.readings[field]["method"]=="complete_ink_crop"


@pytest.mark.parametrize("original,tight", (("9922","9923"),("0","9922"),("10022","9922")))
def test_confirmation_high_confidence_disagreement_is_rejected(balance_field,original,tight):
    reader,image,_=confirmation_reader(tight,original_after=original,original_confidence=.999,field=balance_field)
    with pytest.raises(ValueError,match="高置信读数冲突"):read_balance(reader,image,balance_field)


@pytest.mark.parametrize("invalid", ("-9922","99.22","9O22","9922枚","1000000000",""))
@pytest.mark.parametrize("original_confidence", (.87,.999))
def test_confirmation_requires_complete_integer_from_every_high_confidence_read(balance_field,invalid,original_confidence):
    field=balance_field
    reader,image,_=confirmation_reader("9922",original_after=invalid,original_confidence=original_confidence,field=field)
    if original_confidence < .90:
        original_ocr=reader.ocr
        reader.ocr=lambda image,*,roi:(SimpleNamespace(text=invalid,confidence=.999)
            if roi!=BALANCE_ROIS[field] else original_ocr(image,roi=roi))
    with pytest.raises(ValueError,match="余额.*无效"):read_balance(reader,image,field)


@pytest.mark.parametrize("confidence", (.0,.45,.899999))
def test_confirmation_does_not_lower_tight_ocr_threshold(balance_field,confidence):
    reader,image,_=confirmation_reader(tight_confidence=confidence,field=balance_field)
    with pytest.raises(ValueError,match="低置信"):read_balance(reader,image,balance_field)


def test_confirmation_keeps_exact_point_nine_threshold(balance_field):
    reader,image,_=confirmation_reader(tight_confidence=.90,field=balance_field)
    assert read_balance(reader,image,balance_field)=="9922"


@pytest.mark.parametrize("phase", ("original","tight"))
def test_confirmation_ocr_exception_does_not_use_stale_reading_or_try_crop(balance_field,phase):
    field=balance_field
    reader,image,calls=confirmation_reader(field=field)
    read_balance(reader,image,field)
    original_ocr=reader.ocr
    def fail(image,*,roi):
        if (roi==BALANCE_ROIS[field])==(phase=="original"):raise ValueError("识别器异常")
        return original_ocr(image,roi=roi)
    reader.ocr=fail;calls.clear()
    with pytest.raises(ValueError,match="识别器异常"):read_balance(reader,image,field)
    assert field not in reader.readings
    assert (field+"_original" in reader.readings)==(phase=="tight")
    assert len(calls)==(0 if phase=="original" else 1)


@pytest.mark.parametrize("damage", ("blank","left","right","top","bottom","overlay"))
def test_confirmation_empty_or_border_ink_cannot_authorize_consumption(balance_field,damage):
    reader,image,calls=confirmation_reader(original_confidence=.999,field=balance_field)
    x,y,w,h=BALANCE_ROIS[balance_field]
    region=image[y:y+h,x:x+w]
    if damage=="blank":region[:]=255
    elif damage=="left":region[9:23,:2]=0
    elif damage=="right":region[9:23,-2:]=0
    elif damage=="top":region[:2,32:42]=0
    elif damage=="bottom":region[-2:,32:42]=0
    else:region[:]=80
    with pytest.raises(ValueError,match="墨迹"):read_balance(reader,image,balance_field)
    assert calls==[BALANCE_ROIS[balance_field]]


def test_confirmation_ink_crop_keeps_nonblue_marks_instead_of_selecting_digits(balance_field):
    reader,image,calls=confirmation_reader(field=balance_field)
    x,y,_,_=BALANCE_ROIS[balance_field]
    image[y+9:y+21,x+5:x+8]=0
    read_balance(reader,image,balance_field)
    left,_,width,_=calls[-1]
    assert left < x+5 and left+width > x+8


@pytest.mark.parametrize("phase", ("original","tight"))
@pytest.mark.parametrize("error", (None,ValueError,RuntimeError))
def test_stop_during_balance_ocr_has_priority(balance_field,phase,error):
    reader,image,calls=confirmation_reader(field=balance_field)
    stopped=False
    original_ocr=reader.ocr
    def ocr(image,*,roi):
        nonlocal stopped
        value=original_ocr(image,roi=roi)
        if (roi==BALANCE_ROIS[balance_field])==(phase=="original"):
            stopped=True
            if error:raise error("识别器异常")
        return value
    def stop():
        if stopped:raise ExchangeStopped()
    reader.ocr=ocr;reader.stopped=stop
    with pytest.raises(ExchangeStopped):read_balance(reader,image,balance_field)
    assert len(calls)==(1 if phase=="original" else 2)


def test_tight_confirmation_requires_two_equal_frames_and_exact_balance_before_click(balance_field):
    reader,image,_=confirmation_reader()
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    captures=[];clicks=[]
    ui=ExchangeUI(context,reader=reader,capture=lambda context:captures.append(True) or image,
                  sleep=lambda seconds:None)
    assert ui.stable("draw_confirm",reader.draw_confirmation) == (10,100,10022,9922)
    assert len(captures) == 2
    ui.current_state=state(box=37,remaining=100,cans=6,special=0,currency=10022)
    real_stable=ui.stable
    ui.stable=lambda page,read=None,**kwargs: ((37,100,180,0,10022) if read == reader.main
        else (10,100) if read == reader.draw_offer else real_stable(page,read,**kwargs))
    ui.click=lambda *point:clicks.append(point)
    ui.finish_draw_result=lambda *args:None
    ui.draw_once(10)
    assert clicks == [(1025,618),(770,525)]
    wrong="10023" if balance_field=="confirm_before" else "9923"
    reader,image,_=confirmation_reader(wrong,original_after=wrong,field=balance_field)
    ui.reader=reader
    with pytest.raises(RuntimeError,match="余额冲突"):ui.draw_once(10)
    assert clicks == [(1025,618),(770,525),(1025,618)]


@pytest.mark.parametrize("field", BALANCE_ROIS)
def test_confirmation_changed_balance_between_frames_never_authorizes_click(field):
    first,image,_=confirmation_reader()
    wrong="10023" if field=="confirm_before" else "9923"
    second,_,_=confirmation_reader(wrong,original_after=wrong,field=field)
    readers=iter([first,second]*5)
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    ui=ExchangeUI(context,reader=first,capture=lambda context:image,sleep=lambda seconds:None)
    with pytest.raises(RuntimeError,match="未稳定"):
        ui.stable("draw_confirm",lambda frame:next(readers).draw_confirmation(frame))


# 外部清单使用 {"cases":[{"image":"相对清单的图片路径","expected":[10,100,9922,9822]}]}。
# 图片与环境清单均不入库，CI 未提供清单时明确跳过真实 OCR 回放。
CONFIRMATION_REPLAY_MANIFEST=os.environ.get("MAABANGDREAM_EXCHANGE_CONFIRMATION_REPLAY_MANIFEST")


@pytest.mark.skipif(not CONFIRMATION_REPLAY_MANIFEST,reason="未提供可移植真实确认图回放清单")
def test_real_confirmation_balances_from_portable_manifest():
    from agent.realtime.vision_io import imread_unicode
    manifest=Path(CONFIRMATION_REPLAY_MANIFEST)
    cases=json.loads(manifest.read_text(encoding="utf-8"))["cases"]
    assert cases
    for case in cases:
        kind=case.get("kind","draw_confirm")
        reader=ExchangeReader()
        reader.read_currency=case.get("read_currency",True)
        if kind=="new_box":
            images=[imread_unicode(manifest.parent/name) for name in case["images"]]
            assert len(images)>=2 and len(set(case["images"]))==len(images)
            frames=iter(images)
            ui=ExchangeUI(SimpleNamespace(tasker=SimpleNamespace(stopping=False)),reader=reader,
                          capture=lambda context:next(frames),sleep=lambda seconds:None)
            assert ui.wait_new_box(tuple(case["before"]),None)==tuple(case["expected"])
        else:
            image=imread_unicode(manifest.parent/case["image"])
            actual=getattr(reader,{"main":"main","contents":"contents","draw_confirm":"draw_confirmation",
                                   "draw_result":"draw_result"}[kind])(image)
            expected=tuple(case["expected"]) if isinstance(case["expected"],list) else case["expected"]
            assert actual==expected
        if kind=="draw_confirm" and reader.read_currency:
            for key in BALANCE_ROIS:
                assert reader.readings[key]["confidence"]>=.90
                assert reader.readings[key]["method"]=="complete_ink_crop"
        if not reader.read_currency:
            assert not any(key.startswith(("currency","confirm_before","confirm_after","result_currency"))
                           for key in reader.readings)


@pytest.mark.parametrize("options", ({"max_draws":10},{"currency_budget":100}))
def test_user_budget_needs_no_actual_currency(tmp_path,options):
    ui=UI([state(currency=None),state(remaining=170,cans=9,currency=None)])
    f=flow(ui,tmp_path,**options)
    assert f.run()
    assert f.draws==10
    data=json.loads((tmp_path/"ledger.json").read_text(encoding="utf-8"))
    assert data["budget"]["source"]=="user_budget"
    assert data["budget"]["estimated_spent_currency"]==100
    assert data["after"]["currency"] is None


def test_production_budget_mode_skips_all_balance_ocr():
    reader,image,_=confirmation_reader()
    reader.read_currency=False
    original=reader.ocr
    def ocr(image,*,roi):
        if roi[1]==425 or roi==(1017,539,111,31):pytest.fail("预算模式不得读取余额")
        return original(image,roi=roi)
    reader.ocr=ocr
    assert reader.draw_confirmation(image)==(10,100,None,None)
    assert reader.draw_result(image) is True


def test_auto_budget_reads_initial_balance_on_exactly_two_new_frames():
    reader=SimpleNamespace(read_currency=True,readings={},page=lambda image,page:True,
                           main=lambda image:(19,180,180,1,30822))
    captures=[]
    ui=ExchangeUI(SimpleNamespace(tasker=SimpleNamespace(stopping=False)),reader=reader,
                  capture=lambda context:captures.append(True),sleep=lambda seconds:None)
    assert ui.initialize_budget(True)==30822
    assert len(captures)==2 and reader.read_currency is False


@pytest.mark.parametrize("maximum,currency,expected", ((0,99,9),(20,105,10),(5,100,5),(0,9,0),(10,0,10)))
def test_explicit_budget_uses_tighter_limit_and_whole_ten_coin_units(maximum,currency,expected):
    budget=ExchangeBudget.from_options(validate_options({"max_draws":maximum,"currency_budget":currency}),None)
    assert budget.source=="user_budget" and budget.remaining==expected


@pytest.mark.parametrize("value", (True,-1,1.5,"100",1000000000))
def test_currency_budget_strict_type_and_bounds(value):
    with pytest.raises(ValueError):validate_options({"currency_budget":value})


def production_budget_ui(*, initial_currency=200, failure=None):
    reader=ExchangeReader()
    current={"page":"main","remaining":180,"cans":10,"special":1,"balance_reads":0}
    clicks=[]
    image=np.full((720,1280,3),255,np.uint8)
    reader.page=lambda image,page:current["page"]==page
    reader.settings=lambda image:(True,True)
    reader.contents=lambda image:(current["remaining"],180,current["cans"],current["special"])
    def ocr(image,*,roi):
        if roi==(1017,539,111,31):
            current["balance_reads"]+=1
            if not current.get("allow_initial"):pytest.fail("用户预算或初始化后不得OCR余额")
            return SimpleNamespace(text=str(initial_currency)+"枚",confidence=.999)
        if roi[1]==425:pytest.fail("不得OCR确认前后余额")
        values={(666,184,103,39):"第19箱",(853,184,100,38):f'{current["remaining"]}/180',
                (875,377,49,29):f'{current["special"]}/1',
                (989,599,128,31):"10个交换",(1030,635,94,31):"100枚",
                (460,251,360,31):"消费礼物币100枚",(528,281,221,31):"领取礼物10个奖品。",
                (586,311,115,30):"确认吗？"}
        return SimpleNamespace(text=values[roi],confidence=.999)
    reader.ocr=ocr
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    ui=ExchangeUI(context,reader=reader,capture=lambda context:image,sleep=lambda seconds:None)
    original_initialize=ui.initialize_budget
    def initialize(automatic):
        current["allow_initial"]=automatic
        result=original_initialize(automatic)
        current["allow_initial"]=False
        return result
    ui.initialize_budget=initialize
    def click(x,y):
        clicks.append((x,y))
        if (x,y)==(1068,127):current["page"]="settings"
        elif (x,y)==(640,603):current["page"]="main"
        elif (x,y)==(1029,465):current["page"]="contents"
        elif (x,y)==(640,642):current["page"]="main"
        elif (x,y)==(1025,618):current["page"]="unknown" if failure=="insufficient" else "draw_confirm"
        elif (x,y)==(770,525):
            current["remaining"]-=11 if failure=="delta" else 10
            current["cans"]-=1
            current["page"]="unknown" if failure=="result" else "draw_result"
            if failure=="stop":
                context.tasker.stopping=True
                ui.stopped()
        elif (x,y)==(1068,648):current["page"]="main"
        else:pytest.fail(f"意外输入{(x,y)}")
    ui.click=click
    return ui,current,clicks


@pytest.mark.parametrize("options,reads", (({"max_draws":20},0),({"currency_budget":200},0),({},2)))
def test_production_flow_reads_actual_currency_only_for_auto_initialization(tmp_path,options,reads):
    ui,current,clicks=production_budget_ui()
    f=flow(ui,tmp_path,**options)
    assert f.run() and f.draws==20
    assert current["balance_reads"]==reads
    assert clicks.count((770,525))==2
    assert ui.current_state.currency is None
    data=json.loads((tmp_path/"ledger.json").read_text(encoding="utf-8"))
    assert data["budget"]["source"]==("initial_balance_ocr" if reads else "user_budget")
    assert data["budget"]["estimated_spent_currency"]==200
    assert data["after"]["currency"] is None
    assert not any(key.startswith("confirm_before") or key.startswith("confirm_after") or key=="result_currency"
                   for key in ui.reader.readings)


@pytest.mark.parametrize("failure", ("result","delta","stop","insufficient"))
def test_budget_is_not_deducted_when_real_transaction_evidence_is_missing(tmp_path,failure):
    ui,current,clicks=production_budget_ui(failure=failure)
    f=flow(ui,tmp_path,max_draws=20)
    with pytest.raises((RuntimeError,ExchangeStopped)):f.run()
    assert f.draws==0 and f.budget.spent_draws==0
    assert clicks.count((770,525))==(0 if failure=="insufficient" else 1)
    data=json.loads((tmp_path/"ledger.json").read_text(encoding="utf-8"))
    assert data["status"]=="pending" and data["budget"]["estimated_spent_currency"]==0
    with pytest.raises(RuntimeError,match="上次"):ExchangeJournal(tmp_path/"ledger.json").check_unresolved()


@pytest.mark.parametrize("options", ({"currency_budget":9},{"max_draws":9},{"max_draws":10001},{"currency_budget":100010}))
def test_unusable_or_oversized_user_budget_never_confirms(tmp_path,options):
    ui,current,clicks=production_budget_ui()
    f=flow(ui,tmp_path,**options)
    if max(options.values())>10000:
        with pytest.raises(ValueError,match="预算过大"):f.run()
    else:assert f.run()
    assert (770,525) not in clicks and current["balance_reads"]==0


@pytest.mark.parametrize("changes", ({"box":20},{"remaining":169},{"cans":11},{"special":1},{"total":230,"remaining":170}))
def test_unobserved_currency_still_requires_exact_random_pool_transition(changes):
    before=state(currency=None,cans=9,special=0)
    after=state(currency=None,remaining=170,cans=8,special=0)
    with pytest.raises((ValueError,RuntimeError)):validate_draw(before,replace(after,**changes),10)


@pytest.mark.parametrize("save_failure", ("before_write","after_write"))
def test_budget_confirm_and_receipt_are_one_atomic_record_before_memory_update(tmp_path,save_failure):
    ui,_,_=production_budget_ui()
    f=flow(ui,tmp_path,max_draws=10)
    original=f.journal._save
    observed=[]
    def save(data):
        if data["status"]=="confirmed":
            observed.append(data)
            assert f.budget.spent_draws==0 and f.draws==0
            assert data["budget"]["spent_draws"]==10
            assert data["receipt"]["operation_id"]==data["operation_id"]
            assert data["receipt"]["run_id"]==data["run_id"]
            assert data["receipt"]["before"]==data["before"]
            assert data["receipt"]["after"]==data["after"]
            assert data["receipt"]["cost"]==100
            if save_failure=="after_write":original(data)
            raise OSError("模拟写盘或返回前进程终止")
        original(data)
    f.journal._save=save
    with pytest.raises(OSError):f.run()
    assert len(observed)==1 and f.draws==0 and f.budget.spent_draws==0
    data=json.loads((tmp_path/"ledger.json").read_text(encoding="utf-8"))
    assert data["status"]==("pending" if save_failure=="before_write" else "confirmed")
    assert data["budget"]["spent_draws"]==(0 if save_failure=="before_write" else 10)


def test_completed_receipt_contains_ordered_local_observations(tmp_path):
    ui,_,_=production_budget_ui()
    assert flow(ui,tmp_path,max_draws=10).run()
    data=json.loads((tmp_path/"ledger.json").read_text(encoding="utf-8"))
    receipt=data["receipt"]
    observations=receipt["observations"]
    assert observations["prepared"]<observations["confirmation_verified"]
    assert observations["submitted"]<observations["result_verified"]<observations["result_closed"]<observations["pool_verified"]
    assert receipt["phase"]=="confirmed" and receipt["budget"]["source"]=="user_budget"
    assert "device_timestamp" not in receipt


def test_old_receipt_or_wrong_stage_cannot_confirm_new_pending(tmp_path):
    from agent.realtime.exchange_shop_policy import Decision
    journal=ExchangeJournal(tmp_path/"ledger.json")
    before=state(currency=None);after=state(currency=None,remaining=170,cans=9)
    operation_id=journal.reserve(before,Decision("draw",10),run_id="current-run")
    valid={"operation_id":operation_id,"phase":"pool_verified","before":before.__dict__,"after":after.__dict__}
    for wrong in (valid|{"operation_id":"old-operation"},valid|{"phase":"submitted"},valid|{"before":after.__dict__}):
        with pytest.raises(RuntimeError,match="回执"):journal.confirm(after,receipt=wrong)
    assert json.loads(journal.path.read_text(encoding="utf-8"))["status"]=="pending"


def test_result_before_submission_and_duplicate_submit_are_rejected():
    from agent.realtime.exchange_shop_policy import Decision
    ui,current,clicks=production_budget_ui()
    ui.initialize_budget(False)
    ui.current_state=state(currency=None)
    ui.begin_operation("operation",ui.current_state,Decision("draw",10),{"source":"user_budget"})
    current["page"]="draw_result"
    with pytest.raises(RuntimeError,match="唯一提交"):ui.finish_draw_result(None)
    assert clicks==[]
    current["page"]="main"
    ui.draw_once(10)
    assert clicks.count((770,525))==1
    with pytest.raises(RuntimeError,match="禁止重复确认"):ui.draw_once(10)
    assert clicks.count((770,525))==1
    with pytest.raises(RuntimeError,match="上一笔"):ui.begin_operation("next",ui.current_state,Decision("draw",10),{})


def test_result_page_instead_of_new_confirmation_never_submits(tmp_path):
    ui,current,clicks=production_budget_ui()
    original=ui.click
    def click(x,y):
        original(x,y)
        if (x,y)==(1025,618):current["page"]="draw_result"
    ui.click=click
    f=flow(ui,tmp_path,max_draws=10)
    with pytest.raises(RuntimeError,match="未稳定"):f.run()
    assert (770,525) not in clicks and f.budget.spent_draws==0


@pytest.mark.parametrize("metadata", ({"reused":True,"sequence":2},{"sequence":1},{"sequence":0}))
def test_native_capture_metadata_cannot_reuse_or_reverse_frame_sequence(metadata):
    context=SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    captures=iter([{"image":"fresh","sequence":1},metadata|{"image":"stale"}])
    reader=SimpleNamespace(page=lambda *args:True,readings={})
    ui=ExchangeUI(context,reader=reader,capture=lambda context:next(captures),sleep=lambda seconds:None)
    with pytest.raises(RuntimeError,match="截图复用或序号未前进"):ui.stable("main")
    assert ui.observation==1 and ui.last_frame=="fresh"


def test_previous_task_pending_with_budget_stays_pending_and_never_submits(tmp_path):
    ui,current,clicks=production_budget_ui()
    f=flow(ui,tmp_path,max_draws=10)
    f.journal._save({"status":"pending","before":state(currency=None).__dict__,
                     "operation_id":"old","budget":{"source":"user_budget","spent_draws":0}})
    original=f.journal.path.read_bytes()
    with pytest.raises(RuntimeError,match="上次"):f.run()
    assert (770,525) not in clicks and f.journal.path.read_bytes()==original


def test_user_budget_tail_and_exact_next_box_without_observed_currency(tmp_path):
    ui=UI([state(currency=None,remaining=5,cans=1),state(box=20,currency=None)])
    f=flow(ui,tmp_path,currency_budget=59)
    assert f.run() and f.draws==5
    assert ui.actions.count(("draw",5))==1
    assert f.budget.remaining==0


@pytest.mark.parametrize("success_at", (0,1,2,3))
def test_initial_dark_balance_uses_bounded_complete_field_paths_and_stops_at_first_valid(success_at):
    calls=[]
    def ocr(image,*,roi):
        calls.append(roi)
        return SimpleNamespace(text="9922枚",confidence=.999 if len(calls)>success_at else .84)
    reader=ExchangeReader(ocr=ocr)
    assert reader.initial_balance(np.zeros((720,1280,3),np.uint8))==9922
    assert len(calls)==success_at+1
    x,y,w,h=calls[0]
    for left,top,width,height in calls:
        assert left<=x and top<=y and left+width>=x+w and top+height>=y+h


@pytest.mark.parametrize("invalid", ("9O22枚","-9922枚","99.22枚","9922星石","1000000000枚"))
def test_initial_balance_invalid_text_and_low_confidence_cannot_initialize(invalid):
    reader=ExchangeReader(ocr=lambda *a,**k:SimpleNamespace(text=invalid,confidence=.999))
    with pytest.raises(ValueError,match="请填写"):reader.initial_balance(None)


@pytest.mark.parametrize("exception", (ValueError,RuntimeError))
def test_initial_balance_recognizer_exception_is_not_hidden(exception):
    calls=[]
    def ocr(*a,**k):
        calls.append(True)
        raise exception("识别器异常")
    reader=ExchangeReader(ocr=ocr)
    with pytest.raises(exception,match="识别器异常"):reader.initial_balance(None)
    assert len(calls)==1


@pytest.mark.parametrize("stopped", (False,True))
def test_real_input_failed_receipt_keeps_budget_pending_and_sends_no_result_work(tmp_path,monkeypatch,stopped):
    ui,current,clicks=production_budget_ui()
    original=ui.click
    context=ui.context
    def post(x,y):
        clicks.append((x,y))
        def wait():
            if stopped:context.tasker.stopping=True
        return SimpleNamespace(wait=wait,succeeded=False)
    context.tasker.controller=SimpleNamespace(post_click=post)
    monkeypatch.setattr("agent.realtime.exchange_shop.require_game_foreground",lambda controller:None)
    def click(x,y):
        if (x,y)==(770,525):ExchangeUI.click(ui,x,y)
        else:original(x,y)
    ui.click=click
    f=flow(ui,tmp_path,max_draws=10)
    with pytest.raises(ExchangeStopped if stopped else RuntimeError):f.run()
    assert clicks.count((770,525))==1 and (1068,648) not in clicks
    assert f.budget.spent_draws==0 and ui.operation["phase"]=="submitted"
    assert json.loads((tmp_path/"ledger.json").read_text(encoding="utf-8"))["status"]=="pending"
