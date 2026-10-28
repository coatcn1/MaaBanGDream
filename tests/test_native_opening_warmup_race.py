from types import SimpleNamespace
import threading

import numpy as np
import pytest

from agent.realtime import native_game_clock_matcher as matching
from agent.realtime.native_game_clock import NativeGameClockController
from test_native_clock_domains import fixture
from test_native_game_clock import geometry


@pytest.mark.parametrize('pause_at', ['aggregate', 'calibrated-return', 'stop'])
def test_old_worker_warmup_does_not_cancel_real_third_seed_calibration(tmp_path, monkeypatch, pause_at):
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    for host in (200.2254, 200.3254, 200.4254):
        commit(host)
    backend._refresh_observation_views()
    heads, notes, _projection, game = geometry()
    backend._first_action_anchor_s = 200.475 - game
    matcher = matching.NativeGameClockMatcher(SimpleNamespace(start_time_s=0., judgements=notes))
    monkeypatch.setattr(matching, 'extract_heads', lambda _image: heads)
    controller = NativeGameClockController(backend, matcher=matcher)
    image = np.zeros((720, 1280, 3), np.uint8)
    def metadata(sequence):
        return dict(sequence=sequence, request_s=200.469+sequence*.001,
            first_done_observed_s=200.479+sequence*.001, reused=False)
    second_ready, second_resume = threading.Event(), threading.Event()
    third_ready, third_resume, third_done = threading.Event(), threading.Event(), threading.Event()
    fourth_ready, fourth_resume = threading.Event(), threading.Event()
    original_observe, aggregate, match_frame = matcher.observe, matching.aggregate_bootstrap, controller._match_frame
    calls, completed = [], {}
    def observe(*args, **kwargs):
        calls.append(1)
        number = len(calls)
        result = original_observe(*args, **kwargs)
        if number == 2:
            assert result['reason'] == 'opening-needs-multiple-frames'
            second_ready.set()
            assert second_resume.wait(3)
        elif number == 3 and pause_at == 'calibrated-return':
            assert matcher.projection is not None
            third_ready.set()
            assert third_resume.wait(3)
        return result
    def bootstrap(*args, **kwargs):
        if pause_at in {'aggregate', 'stop'}:
            third_ready.set()
            assert third_resume.wait(3)
        return aggregate(*args, **kwargs)
    def recorded_match(image, source, *args, **kwargs):
        sequence = source['sequence']
        if sequence == 4:
            fourth_ready.set()
            assert fourth_resume.wait(3)
        result = match_frame(image, source, *args, **kwargs)
        completed[sequence] = result
        if sequence == 3:
            third_done.set()
        return result
    monkeypatch.setattr(matcher, 'observe', observe)
    monkeypatch.setattr(matching, 'aggregate_bootstrap', bootstrap)
    monkeypatch.setattr(controller, '_match_frame', recorded_match)
    try:
        controller.observe(image, metadata(1), now[0], visible=True)
        assert len(matcher.opening) == 1 and matcher.projection is None
        controller.submit(image, metadata(2), now[0], visible=True)
        assert second_ready.wait(1)
        controller.submit(image, metadata(3), now[0], visible=True)
        second_resume.set()
        assert third_ready.wait(2)
        assert not controller._results.empty()
        if pause_at == 'stop':
            controller.close(now[0])
            third_resume.set()
            controller._thread.join(2)
            assert not controller._thread.is_alive()
            assert matcher.projection is None and matcher.baseline_lag is None and not matcher.opening
            assert controller.trusted_at is None and backend._phase_slot is None
            return
        controller.submit(image, metadata(4), now[0], visible=True)
        # 旧warming不产生视觉独占、可信目标或相位输入。
        assert controller.trusted_at is None and backend._phase_slot is None
        assert not controller.samples and controller.pending_sequence is None
        third_resume.set()
        assert third_done.wait(2) and fourth_ready.wait(1)
        assert completed[3].get('reason') != 'clock-frame-epoch-invalid'
        assert completed[3]['status'] == 'matched'
        assert matcher.projection is not None and matcher.baseline_lag is not None
        assert len(matcher.opening) == 3 and controller._thread.is_alive()
    finally:
        second_resume.set()
        third_resume.set()
        controller.close(now[0])
        fourth_resume.set()
        if controller._thread is not None:
            controller._thread.join(3)


@pytest.mark.parametrize('fault', ['expired', 'hard', 'phase', 'other-reason'])
def test_real_warmup_result_keeps_original_ttl_and_context_rejection(tmp_path, monkeypatch, fault):
    backend, mapper, now, probe, _, _, _ = fixture(tmp_path, monkeypatch)
    heads, notes, _projection, game = geometry()
    backend._first_action_anchor_s = 200.475-game
    matcher = matching.NativeGameClockMatcher(SimpleNamespace(start_time_s=0.,judgements=notes))
    monkeypatch.setattr(matching,'extract_heads',lambda image:heads)
    controller = NativeGameClockController(backend,matcher=matcher)
    original_match = controller._match_frame
    captured=[]
    def capture(image,source,*args,**kwargs):
        result=original_match(image,source,*args,**kwargs)
        captured.append((result,dict(source)))
        return result
    monkeypatch.setattr(controller,'_match_frame',capture)
    image=np.zeros((720,1280,3),np.uint8)
    controller.observe(image,dict(sequence=1,request_s=200.470,first_done_observed_s=200.480,reused=False),
        now[0],visible=True)
    result,source=captured[0]
    assert result['reason']=='opening-needs-multiple-frames'
    original_observed=now[0]
    semantic_before=controller._semantic_epoch
    if fault=='expired':
        for sequence in range(11,21):
            mapper.observe(probe(sequence,200+sequence*.05))
        now[0]=201.0018
        assert now[0]-original_observed>.5
        assert controller._observation_snapshot(now[0]) is not None
    elif fault=='hard':
        backend._observation_clock=None
        assert backend.observation_clock_snapshot() is None
        backend._observation_clock=mapper
    elif fault=='phase':
        watermark=controller._phase_watermark()
        monkeypatch.setattr(controller,'_phase_watermark',lambda:('changed',watermark))
    else:
        result=dict(status='unknown',reason='opening-inconsistent')
    controller._accept(result,source,original_observed,now[0])
    assert controller._semantic_epoch>semantic_before
    assert controller.trusted_at is None and controller.pending_sequence is None
    assert not controller.samples and backend._phase_slot is None
    controller.close(now[0])



def test_unclocked_warmup_still_uses_original_invalidation(tmp_path, monkeypatch):
    backend, _mapper, now, _probe, _, _, _ = fixture(tmp_path, monkeypatch)
    backend._native_clock_trial = False
    controller = NativeGameClockController(backend, matcher=SimpleNamespace())
    before = controller._semantic_epoch
    controller._accept(dict(status='unknown', reason='opening-needs-multiple-frames'), {}, now[0], now[0])
    assert controller._semantic_epoch > before
    assert controller.trusted_at is None and backend._phase_slot is None
    controller.close(now[0])
