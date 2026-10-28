from dataclasses import replace
import pytest

from agent.realtime.native_game_clock import NativeGameClockController
from test_native_clock_domains import fixture


class Matcher:
    def __init__(self, lag, bounds):
        self.lag, self.bounds, self.projection = lag, bounds, object()
    def observe(self, *_args):
        return dict(status='matched', relative_lag_ms=self.lag, capture_bounds_ms=self.bounds)
    def reset(self, *, full=True):
        if full:
            self.projection = None


class Run:
    def __init__(self, tmp_path, monkeypatch, *, lag=100., bounds=(90.,110.), construct=True):
        self.backend, self.mapper, self.now, self.probe, self.commit, self.accepted, self.applied = fixture(tmp_path, monkeypatch)
        self.sequence = 10
        self.backend._session.future_phase_offset_ms = 0
        for host in (200.2254,200.3254,200.4254):
            self.commit(host)
        self.backend._refresh_observation_views()
        self.matcher = Matcher(lag,bounds)
        self.controller = NativeGameClockController(self.backend,matcher=self.matcher) if construct else None

    def input_tick(self):
        self.sequence += 1
        host = 200 + self.sequence * .05
        self.mapper.observe(self.probe(self.sequence,host))
        self.now[0] = host + .0008
        self.commit(host - .025)
        self.backend._refresh_observation_views()
        return dict(sequence=self.sequence,request_s=host-.015,first_done_observed_s=host-.005,reused=False)

    def frames(self,count=3):
        for _ in range(count):
            metadata = self.input_tick()
            self.controller.observe(None,metadata,self.now[0],visible=True)

    def execute(self):
        delta = self.backend._phase_slot[3]
        self.backend._session.future_phase_offset_ms += delta
        self.backend._consume_phase_candidate()
        effect = self.backend.phase_status_snapshot()
        assert effect.state == 'applied'
        token = max(self.backend._observed_action_tokens) + 1
        self.backend._publish_phase_status(replace(effect,first_adjusted_due_s=self.now[0]+.005,
            first_affected_chunk_sequence=token,first_affected_action_token=token),'effect-published')
        return delta

    def feedback(self,delta=15):
        self.backend.set_visual_phase_guard(False)
        self.now[0] += .001
        assert self.backend.request_future_phase(delta,self.now[0])
        self.execute()
        self.input_tick()

    def visual30(self):
        self.frames()
        assert self.execute() == 15
        self.frames()
        assert self.execute() == 15
        self.matcher.bounds=(25.,175.) if self.matcher.lag == 100 else self.matcher.bounds
        self.frames()
        assert self.backend._phase_slot is None


def test_initial_feedback60_raw_neutral_never_undone_by_visual(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch,lag=0,bounds=(-10,10),construct=False)
    for _ in range(4):
        run.feedback()
    run.controller=NativeGameClockController(run.backend,matcher=run.matcher)
    run.frames()
    assert run.backend._phase_slot is None
    assert run.backend.phase_owner_cumulative('feedback') == 60
    assert run.backend.phase_owner_cumulative('game-clock') == 0


def test_actual_controller_visual30_feedback60_tighter_bounds_cannot_double_compensate(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch)
    run.visual30()
    for _ in range(4):
        run.feedback()
    run.frames()
    assert run.backend._phase_slot is None
    run.matcher.bounds=(90.,110.)
    run.frames()
    assert run.backend._phase_slot is None
    assert run.backend.phase_owner_cumulative('game-clock') == 30
    assert run.backend.phase_owner_cumulative('feedback') == 60
    budget=run.controller.report()['shared_budget']
    assert budget['feedback_credit_ms'] == 60 and len(budget['credited_effects']) == 4
    assert [row for row in run.controller.events if row['reason']=='trusted-target'][-1]['target_ms'] == 100
    run.frames()
    assert run.controller.report()['shared_budget']['feedback_credit_ms'] == 60


def test_raw_return_returns_only_own_visual30_and_preserves_later_foreign15(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch,lag=30,bounds=(30,30))
    run.visual30()
    run.matcher.lag,run.matcher.bounds=0,(-10,10)
    run.feedback()
    for _ in range(2):
        run.frames()
        assert run.execute() == -15
    run.frames()
    assert run.backend._phase_slot is None
    assert run.backend.phase_owner_cumulative('game-clock') == 0
    assert run.backend.phase_owner_cumulative('feedback') == 15
    assert run.controller.report()['shared_budget']['active'] is False


def test_same_e_clock_epoch_bridge_retains_budget_while_full_scene_cannot_clear_debt(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch)
    run.visual30()
    for _ in range(4):
        run.feedback()
    run.frames()
    run.matcher.bounds=(90.,110.)
    projection=run.matcher.projection
    run.mapper.invalidate('high-rtt')
    assert not run.controller._clock_relation_ready(run.now[0])
    for _ in range(4):
        run.input_tick()
    run.frames()
    assert run.matcher.projection is projection and run.backend._phase_slot is None
    assert run.controller.report()['shared_budget']['feedback_credit_ms'] == 60
    run.controller.invalidate(run.now[0],'invalid-scene')
    run.frames()
    assert run.backend._phase_slot is None
    assert run.backend.phase_owner_cumulative('game-clock') == 30
    assert run.controller.visual_applied_ms == 30 and run.controller.visual_origin_ms == 0
    assert run.controller.report()['shared_budget']['feedback_credit_ms'] == 60


def test_owner_totals_recheck_rejects_visual_request_racing_with_foreign_application(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch)
    original=run.backend.request_future_phase
    raced=[]
    def request(delta,observed,*,owner='feedback',**kwargs):
        if owner=='game-clock' and not raced:
            raced.append(True)
            run.feedback()
            run.backend.set_visual_phase_guard(True)
        return original(delta,observed,owner=owner,**kwargs)
    monkeypatch.setattr(run.backend,'request_future_phase',request)
    run.frames()
    assert run.backend._phase_slot is None and run.backend.phase_owner_cumulative('game-clock') == 0
    assert run.backend.phase_owner_cumulative('feedback') == 15
    assert run.backend._phase_request_rejection_reason == 'shared-budget-changed-or-unavailable'


def test_effect_history_fault_is_optional_and_cannot_erase_actual_phase_or_input_receipts(tmp_path,monkeypatch,capsys):
    run=Run(tmp_path,monkeypatch)
    run.visual30()
    def fail(_status):
        raise OSError('private history record')
    monkeypatch.setattr(run.backend,'_publish_phase_effect_history',fail)
    run.feedback()
    run.frames()
    assert run.backend._phase_slot is None and run.backend.phase_owner_cumulative('feedback') == 15
    assert run.backend.phase_owner_cumulative('game-clock') == 30
    assert run.backend.phase_effect_history_snapshot() is None
    assert run.accepted and run.backend._observation_error is None and run.backend._device_error is None
    output=capsys.readouterr().out
    assert output.count('history_warning=OSError') == 1 and 'private history record' not in output


def test_temporary_history_reader_fault_recovers_without_new_foreign_effect(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch)
    run.visual30()
    original=run.backend.phase_effect_history_snapshot
    monkeypatch.setattr(run.backend,'phase_effect_history_snapshot',lambda:None)
    run.frames()
    assert run.backend._phase_slot is None
    monkeypatch.setattr(run.backend,'phase_effect_history_snapshot',original)
    run.matcher.bounds=(90.,110.)
    run.frames()
    assert run.backend._phase_slot is not None
    assert run.controller.report()['shared_budget']['unresolved_reason'] is None


@pytest.mark.parametrize('field,value',[('run_id','foreign'),('request_sequence',True),
                                      ('request_sequence',2**80),('delta_ms',float('nan'))])
def test_invalid_history_cannot_rebase_neutral_budget(tmp_path,monkeypatch,field,value):
    run=Run(tmp_path,monkeypatch,lag=0,bounds=(-10,10))
    run.feedback()
    history=run.backend.phase_effect_history_snapshot()
    monkeypatch.setattr(run.backend,'phase_effect_history_snapshot',lambda:(replace(history[-1],**{field:value}),))
    run.frames()
    assert run.backend._phase_slot is None
    assert run.backend.phase_owner_cumulative('feedback') == 15
    assert run.controller.report()['shared_budget']['unresolved_reason'].startswith('effect-history:')


def test_actual_reverse_preserves_foreign_credit_and_changes_only_visual_ownership(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch)
    run.visual30()
    for _ in range(4):
        run.feedback()
    run.frames()
    run.matcher.lag,run.matcher.bounds=-100,(-100,-100)
    executed=[]
    for _ in range(36):
        run.frames(1)
        if run.backend._phase_slot is not None:
            executed.append(run.execute())
    assert executed == [-15]*8
    run.frames()
    assert run.backend._phase_slot is None
    assert run.backend.phase_owner_cumulative('game-clock') == -90
    assert run.backend.phase_owner_cumulative('feedback') == 60
    assert run.controller.report()['shared_budget']['feedback_credit_ms'] == 60
