import math
import threading
from dataclasses import replace

import pytest

from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.timing_feedback import TimingFeedback
from test_native_clocked_shared_budget import Run
from test_native_clocked_feedback import advance
from test_native_life_feedback import _feedback_image
from test_native_clocked_game_clock import ViewMatcher, emit
from agent.realtime.native_game_clock import NativeGameClockController
from test_native_clock_domains import fixture


@pytest.mark.parametrize('ticks', [7,13])
def test_sealed_geometry_survives_quality_change_during_bounded_background_work(tmp_path,monkeypatch,ticks):
    run=Run(tmp_path,monkeypatch,lag=0,bounds=(-1,1))
    entered,resume=threading.Event(),threading.Event()
    run.matcher.projection=None
    def match(*_args):
        entered.set()
        assert resume.wait(1)
        run.matcher.projection=object()
        return dict(status='matched',relative_lag_ms=0,capture_bounds_ms=(-1,1))
    monkeypatch.setattr(run.matcher,'observe',match)
    worker=threading.Thread(target=run.frames,args=(1,))
    worker.start()
    assert entered.wait(1)
    run.mapper.invalidate('unqualified-probe')
    assert not run.controller._clock_relation_ready(run.now[0])
    for _ in range(ticks):
        run.input_tick()
    run.controller._clock_relation_ready(run.now[0])
    resume.set()
    worker.join(1)
    assert not worker.is_alive() and run.matcher.projection is not None
    assert run.backend._phase_slot is None and not run.controller.samples


@pytest.mark.parametrize('fault', [None,'expired','hard','foreign'])
def test_pending_quality_gap_requires_new_receipts_preserves_ttl_and_applies_at_most_once(tmp_path,monkeypatch,fault):
    run=Run(tmp_path,monkeypatch)
    original_h=run.now[0]
    assert run.backend.request_future_phase(-15,original_h)
    run.mapper.invalidate('unqualified-probe')
    run.backend._consume_phase_candidate()
    assert run.backend._phase_slot[2:] == (original_h,-15,original_h+2)
    if fault == 'hard':
        run.backend._observation_clock=None
        assert run.backend.observation_clock_snapshot() is None
        run.backend._observation_clock=run.mapper
    for _ in range(5):
        run.input_tick()
    run.backend._consume_phase_candidate()
    # 三个新 probe 之后仍需三条新真实回执，不能拼接原 epoch 的三条。
    assert not run.applied
    if fault == 'expired':
        run.now[0]=original_h+2.001
    elif fault == 'foreign':
        run.backend._phase_owner_totals['game-clock']=15
    else:
        run.input_tick()
    run.backend._consume_phase_candidate()
    run.backend._consume_phase_candidate()
    assert len(run.applied) == (1 if fault is None else 0)
    if fault:
        assert run.backend._phase_slot is None
        assert run.backend.phase_status_snapshot().state in {'rejected','expired'}


class FeedbackRun(Run):
    def __init__(self,tmp_path,monkeypatch):
        super().__init__(tmp_path,monkeypatch)
        self.sampler=NativeLifeFeedback(self.backend)
        self.sampler.observe_scene(self.now[0],visible=True)
        self.frame_sequence=0

    def probes_to(self,end):
        advance(self.backend,self.mapper,self.now,self.probe,self.sequence,end)
        self.sequence=end

    def quality_gap(self):
        self.mapper.invalidate('unqualified-probe')
        self.sampler.capture_due(self.now[0])
        for _ in range(3):
            self.probes_to(self.sequence+1)
            # 连续采集仍在进行；采样质量变差不等于真实画面采集中断。
            self.frame_sequence+=1
            assert self.sampler.fresh(dict(sequence=self.frame_sequence,request_s=self.now[0]-.01,
                first_done_observed_s=self.now[0]-.005,reused=False),self.now[0])
            self.sampler.observe_frame(_feedback_image(None),self.now[0])

    def vote(self,target,direction):
        self.now[0]=target+.001
        self.commit(target)
        observed=target+.0754
        self.probes_to(math.floor((observed-200-.0008)/.05+1e-6))
        self.now[0]=observed
        self.sampler.capture_due(self.now[0])
        self.frame_sequence+=1
        assert self.sampler.fresh(dict(sequence=self.frame_sequence,request_s=target+.03,
            first_done_observed_s=target+.06,reused=False),self.now[0])
        self.sampler.observe_frame(_feedback_image(direction),self.now[0])
        self.now[0]=target+.101
        self.commit(target+.1)
        self.probes_to(math.ceil((target+.1-200)/.05))
        self.sampler.capture_due(self.now[0])


def test_closed_direction_votes_cross_quality_but_never_duplicate_or_refresh_original_h(tmp_path,monkeypatch):
    run=FeedbackRun(tmp_path,monkeypatch)
    run.vote(200.4254,TimingFeedback.SLOW)
    first=run.sampler.votes[0]
    for index in (1,2):
        run.quality_gap()
        assert run.sampler.votes[0] == first
        count=run.sampler.summary['direction_votes']
        for _ in range(3):
            run.sampler.capture_due(run.now[0])
        assert run.sampler.summary['direction_votes'] == count
        run.vote(200.4254+index*.3,TimingFeedback.SLOW)
    assert run.sampler.summary['accepted_requests'] == 1
    assert run.backend._phase_slot[2] == pytest.approx(201.1008)
    assert run.backend._phase_slot[4] == pytest.approx(203.1008)


@pytest.mark.parametrize('fault',['capture-gap','hard','scene','effect'])
def test_historical_vote_cannot_cross_semantic_or_capture_fault(tmp_path,monkeypatch,fault):
    run=FeedbackRun(tmp_path,monkeypatch)
    run.vote(200.4254,TimingFeedback.SLOW)
    assert len(run.sampler.votes) == 1
    if fault == 'hard':
        run.backend._observation_clock=None
        run.sampler.capture_due(run.now[0])
        run.backend._observation_clock=run.mapper
    elif fault == 'scene':
        run.sampler.observe_scene(run.now[0],visible=False)
    elif fault == 'effect':
        run.feedback()
        run.sampler.refresh_phase_status(run.now[0])
    else:
        run.now[0] += .51
        run.frame_sequence+=1
        run.sampler.fresh(dict(sequence=run.frame_sequence,request_s=run.now[0]-.01,reused=False),run.now[0])
    assert not run.sampler.votes


def test_only_closed_opposite_evidence_revokes_exact_feedback_pending(tmp_path,monkeypatch):
    run=FeedbackRun(tmp_path,monkeypatch)
    for index in range(3):
        run.vote(200.4254+index*.1,TimingFeedback.FAST)
    status=run.backend.phase_status_snapshot()
    assert status.state == 'requested'
    # 新图原本新鲜但仍没有最早真实后继闭合，不能用裸颜色撤销原请求。
    target=200.8254
    run.now[0]=target+.001
    run.commit(target)
    run.probes_to(18)
    run.now[0]=target+.0754
    run.frame_sequence+=1
    assert run.sampler.fresh(dict(sequence=run.frame_sequence,request_s=target+.03,
        first_done_observed_s=target+.06,reused=False),run.now[0])
    run.sampler.observe_frame(_feedback_image(TimingFeedback.SLOW),run.now[0])
    assert run.backend.phase_status_snapshot().state == 'requested'
    run.now[0]=target+.101
    run.commit(target+.1)
    run.probes_to(19)
    run.sampler.capture_due(run.now[0])
    assert run.backend._phase_slot is None
    assert run.backend.phase_status_snapshot().request_sequence == status.request_sequence
    assert run.backend.phase_status_snapshot().terminal_reason == 'direction-reversed'
    assert len(run.sampler.votes) == 1 and run.sampler.votes[0][0] is TimingFeedback.SLOW


@pytest.mark.parametrize('fault',['quality','hard','expired','anchor'])
def test_unsealed_frame_never_receives_retroactive_new_context(tmp_path,monkeypatch,fault):
    run=Run(tmp_path,monkeypatch,lag=30,bounds=(30,30))
    metadata=run.input_tick()
    assert run.controller._fresh(metadata,run.now[0],True)
    metadata.update(_clock_key=run.controller._observation_key,
        _intake_provenance=run.controller._provenance(),
        _intake_phase_watermark=run.controller._phase_watermark())
    observed=run.now[0]
    called=[]
    monkeypatch.setattr(run.matcher,'observe',lambda *_args:called.append(True))
    if fault == 'hard':
        run.backend._observation_clock=None
        run.backend.observation_clock_snapshot()
        run.backend._observation_clock=run.mapper
    elif fault == 'anchor':
        run.backend._first_action_anchor_s += .001
    elif fault == 'quality':
        run.mapper.invalidate('unqualified-probe')
    else:
        run.now[0] += .501
    if fault in {'quality','hard'}:
        for _ in range(4):
            run.input_tick()
    result=run.controller._match_frame(None,metadata,observed,200)
    assert result['status'] == 'unknown' and not called and '_clock_view' not in metadata


def test_slow_sealed_calibration_material_never_renews_phase_result_timestamp(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch,lag=30,bounds=(30,30))
    run.matcher.projection=None
    original=run.now[0]
    def match(*_args):
        run.now[0] += .501
        run.matcher.projection=object()
        return dict(status='matched',relative_lag_ms=30,capture_bounds_ms=(30,30))
    monkeypatch.setattr(run.matcher,'observe',match)
    run.controller.observe(None,dict(sequence=1,request_s=original-.03,
        first_done_observed_s=original-.01,reused=False),original,visible=True)
    assert run.matcher.projection is not None
    assert run.controller.trusted_at is None and not run.controller.samples and run.backend._phase_slot is None


@pytest.mark.parametrize('shift',[-.116,.116])
@pytest.mark.parametrize('phase',[-.03,0,.03])
def test_common_clock_step_uses_sealed_e_reference_without_inventing_game_phase(tmp_path,monkeypatch,shift,phase):
    backend,mapper,now,probe,_,_,_=fixture(tmp_path,monkeypatch)
    matcher=ViewMatcher()
    controller=NativeGameClockController(backend,matcher=matcher)
    projection=matcher.projection
    for sequence in (11,12,13):
        emit(controller,backend,mapper,now,probe,sequence,shift=0,phase=0)
    def shifted(sequence,host):
        row=probe(sequence,host)
        return replace(row,device_receive_s=row.device_receive_s-shift,
            device_send_s=row.device_send_s-shift,monotonic_receive_s=row.monotonic_receive_s-shift,
            monotonic_send_s=row.monotonic_send_s-shift)
    mapper.invalidate('clock-discontinuity')
    for sequence in (14,15,16):
        mapper.observe(shifted(sequence,200+sequence*.05))
    for sequence in (17,18,19):
        emit(controller,backend,mapper,now,shifted,sequence,shift=shift,phase=phase)
    assert matcher.projection is projection and backend._device_clock_offset_s == 100
    assert controller.trusted_lag_ms == pytest.approx(phase*1000,abs=1e-6)
    assert (backend._phase_slot[3] if backend._phase_slot else 0) == (15 if phase>0 else -15 if phase<0 else 0)


def test_visual_intent_waits_for_three_new_fresh_proofs_without_changing_original_ttl(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch,lag=30,bounds=(30,30))
    run.frames()
    original=run.backend._phase_slot
    for _ in range(12):
        run.input_tick()
    run.backend._consume_phase_candidate()
    assert not run.applied and run.backend._phase_slot == original
    run.frames(2)
    run.backend._consume_phase_candidate()
    assert not run.applied and run.backend._phase_slot == original
    run.frames(1)
    assert run.backend._phase_slot == original
    run.backend._consume_phase_candidate()
    run.backend._consume_phase_candidate()
    assert len(run.applied) == 1
    assert run.backend.phase_status_snapshot().request_sequence == original[1]


@pytest.mark.parametrize('problem',['earliest-frame-stale','bounds-too-wide'])
def test_visual_revalidation_requires_every_frames_original_deadline_and_whole_delta(tmp_path,monkeypatch,problem):
    run=Run(tmp_path,monkeypatch,lag=30,bounds=(30,30))
    run.frames()
    original=run.backend._phase_slot
    if problem == 'bounds-too-wide':
        run.matcher.bounds=(10,50)
        run.frames()
    else:
        run.frames(1)
        for _ in range(4):
            run.input_tick()
        run.frames(1)
        for _ in range(4):
            run.input_tick()
        original_match=run.matcher.observe
        def delayed(*args):
            run.now[0]+=.001
            return original_match(*args)
        monkeypatch.setattr(run.matcher,'observe',delayed)
        run.frames(1)
    assert run.backend._phase_slot == original and run.backend._phase_slot_visual_qualification is None
    run.backend._consume_phase_candidate()
    assert not run.applied and run.backend._phase_slot == original


def test_revalidated_visual_intent_still_expires_at_original_two_seconds(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch,lag=30,bounds=(30,30))
    run.frames()
    original=run.backend._phase_slot
    run.frames()
    qualification=run.backend._phase_slot_visual_qualification
    assert qualification is not None
    run.now[0]=original[4]+.001
    run.backend._consume_phase_candidate()
    assert not run.applied and run.backend._phase_slot is None
    assert run.backend.phase_status_snapshot().terminal_reason == 'expired'
    assert not run.backend.refresh_visual_phase_qualification(original[1],qualification)


def test_delayed_feedback_revoke_never_invalidates_new_foreign_owner_slot(tmp_path,monkeypatch):
    run=Run(tmp_path,monkeypatch,construct=False)
    run.feedback()
    old=run.backend.phase_status_snapshot()
    run.controller=NativeGameClockController(run.backend,matcher=run.matcher)
    run.frames()
    pending=run.backend._phase_slot
    assert run.backend.phase_status_snapshot().owner == 'game-clock'
    original=run.backend.phase_status_snapshot
    reads=[]
    def stale_once():
        reads.append(True)
        return old if len(reads)==1 else original()
    monkeypatch.setattr(run.backend,'phase_status_snapshot',stale_once)
    run.backend.revoke_future_phase('old-opposite',owner='feedback',expected_sequence=old.request_sequence)
    assert run.backend._phase_slot == pending
    run.backend._consume_phase_candidate()
    assert run.backend.phase_owner_cumulative('game-clock') == 15
