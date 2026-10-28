from dataclasses import replace
from types import SimpleNamespace
import threading

import numpy as np
import pytest

from agent.realtime.native_game_clock import NativeGameClockController
from test_native_clock_domains import fixture


class ViewMatcher:
    def __init__(self):
        self.calls = []
        self.projection = object()
        self.resets = []

    def reset(self, *, full=True):
        self.resets.append(full)
        if full:
            self.projection = None

    def observe(self, image, request, upper, anchor, clock_view):
        bounds = clock_view.host_to_execution(request, upper)
        self.calls.append((request, upper, clock_view))
        lo, hi = (value - anchor - image.game_time_s for value in bounds)
        return dict(status='matched', relative_lag_ms=(lo + hi) * 500, capture_bounds_ms=(lo * 1000, hi * 1000))


def emit(controller, backend, mapper, now, probe, sequence, *, shift, phase, width=.01):
    host = 200 + sequence * .05
    mapper.observe(probe(sequence, host))
    now[0] = host + .0008
    backend._refresh_observation_views()
    upper = host - .005
    request = upper - width
    image = SimpleNamespace(game_time_s=(request + upper) / 2 - 200 - shift - phase)
    controller.observe(image, dict(request_s=request, first_done_observed_s=upper,
        sequence=sequence, reused=False), now[0], visible=True)


@pytest.mark.parametrize('shift', [-.103, .103])
@pytest.mark.parametrize('phase', [-.030, 0., .030])
def test_required_production_controller_normalizes_common_shift_but_preserves_true_phase_direction(tmp_path, monkeypatch,
                                                                                                 shift, phase):
    backend, mapper, now, probe, _, _, _ = fixture(tmp_path, monkeypatch, shift=shift)
    monkeypatch.setattr(backend, 'clock_relation_snapshot', lambda: pytest.fail('C6 must not substitute jlog RX gap'))
    matcher = ViewMatcher()
    controller = NativeGameClockController(backend, matcher=matcher)
    for sequence in (11, 12, 13):
        emit(controller, backend, mapper, now, probe, sequence, shift=shift, phase=phase)
    assert controller.report()['clock_relation']['source'] == 'independent-clock-rpc'
    assert len(matcher.calls) == 3
    if phase:
        assert controller.trusted_lag_ms == pytest.approx(phase * 1000)
        assert backend._phase_slot[3] == (15 if phase > 0 else -15)
    else:
        assert backend._phase_slot is None and controller.summary['neutral-game-clock'] == 1


def test_full_73ms_capture_window_crosses_probes_and_retains_original_h_age_gate(tmp_path, monkeypatch):
    backend, mapper, now, probe, _, _, _ = fixture(tmp_path, monkeypatch, shift=.103)
    matcher = ViewMatcher()
    controller = NativeGameClockController(backend, matcher=matcher)
    for sequence in (11, 12, 13):
        emit(controller, backend, mapper, now, probe, sequence, shift=.103, phase=0, width=.073)
    assert len(matcher.calls) == 3 and backend._phase_slot is None
    assert all(upper - request == pytest.approx(.073) for request, upper, _view in matcher.calls)
    controller.observe(None, dict(sequence=14, request_s=now[0] - .081, reused=False), now[0], visible=True)
    assert len(matcher.calls) == 3


def test_effect_uses_full_h_interval_and_historical_post_effect_down_before_original_frame(tmp_path, monkeypatch):
    backend, mapper, now, probe, commit, _, applied = fixture(tmp_path, monkeypatch, shift=.103)
    for host in (200.2254, 200.3254, 200.4254):
        commit(host)
    backend._refresh_observation_views()
    controller = NativeGameClockController(backend, matcher=ViewMatcher())
    for sequence in (11, 12, 13):
        emit(controller, backend, mapper, now, probe, sequence, shift=.103, phase=.03)
    backend._consume_phase_candidate()
    assert applied
    effect = backend.phase_status_snapshot()
    backend._publish_phase_status(replace(effect, first_adjusted_due_s=200.657 - .103,
        first_affected_chunk_sequence=4, first_affected_action_token=4), 'effect-published')
    now[0] = 200.661
    commit(200.6604)
    now[0] = 200.696
    commit(200.6954)
    mapper.observe(probe(14, 200.7))
    now[0] = 200.7008
    backend._refresh_observation_views()
    latest = backend.phase_status_snapshot()
    assert latest.transient_action_token == 5
    view = backend.observation_clock_snapshot().frame_view(200.66, 200.695,
        now_s=now[0], execution_offset_s=100)
    effect = backend.phase_effect_snapshot()
    assert not controller._effect_execution_ready(effect, latest, 200.6604, now[0], view)
    # 最新DOWN已在frame request之后；较早真实post-effect DOWN仍能证明该截图覆盖效果。
    assert controller._effect_execution_ready(effect, latest, 200.685, now[0], view)
    assert controller._effect_ready(now[0], 200.685, view)
    assert controller.pending_sequence is None


def test_worker_waits_only_for_bracket_without_refreshing_original_73ms_metadata(tmp_path, monkeypatch):
    backend, mapper, now, probe, _, _, _ = fixture(tmp_path, monkeypatch)
    called = threading.Event()
    captured = []
    class Matcher:
        def observe(self, image, request, upper, anchor, clock_view):
            assert not image.flags.writeable
            captured.append((request, upper, clock_view))
            called.set()
            return dict(status='matched', relative_lag_ms=0, capture_bounds_ms=(-1, 1))
    controller = NativeGameClockController(backend, matcher=Matcher())
    original_observed = now[0]
    request = original_observed - .073
    controller.submit(np.zeros((720, 1280, 3), np.uint8),
        dict(sequence=1, request_s=request, reused=False), original_observed, visible=True)
    assert not called.wait(.02)
    mapper.observe(probe(11, 200.55))
    now[0] = 200.5508
    assert called.wait(1)
    epoch, _result, metadata, observed = controller._results.get(timeout=1)
    assert epoch == controller.epoch and observed == original_observed
    assert captured[0][0] == request and captured[0][1] == original_observed
    assert metadata['_clock_view'].host_to_execution(request, original_observed) is not None
    controller.close(now[0])


def test_recovered_clock_epoch_discards_old_worker_view_and_keeps_projection_reference(tmp_path, monkeypatch):
    backend, mapper, now, probe, _, _, _ = fixture(tmp_path, monkeypatch)
    matcher = ViewMatcher()
    projection = matcher.projection
    controller = NativeGameClockController(backend, matcher=matcher)
    emit(controller, backend, mapper, now, probe, 11, shift=0, phase=0)
    old_epoch = controller.epoch
    request, upper, old_view = matcher.calls[-1]
    controller._results.put_nowait((old_epoch, dict(status='matched', relative_lag_ms=30, capture_bounds_ms=(25,35)),
        dict(request_s=request, first_done_observed_s=upper, _clock_view=old_view), now[0]))
    mapper.invalidate('high-rtt')
    for sequence in (12, 13, 14, 15):
        mapper.observe(probe(sequence, 200 + sequence * .05))
    now[0] = 200.7508
    controller._thread = SimpleNamespace()
    controller.submit(np.zeros((720,1280,3), np.uint8), dict(sequence=15, request_s=200.735,
        first_done_observed_s=200.745, reused=False), now[0], visible=True)
    controller._reset_matcher_if_due()
    assert controller.epoch > old_epoch and not controller.samples and backend._phase_slot is None
    assert matcher.projection is projection and matcher.resets == []
    assert controller._results.empty()
    controller.close(now[0])


@pytest.mark.parametrize('late_error', [False, True])
def test_inflight_opening_calibration_cannot_freeze_after_hard_reader_loss(tmp_path, monkeypatch, late_error):
    backend, mapper, now, probe, _, _, _ = fixture(tmp_path, monkeypatch)
    entered, resume, fresh = threading.Event(), threading.Event(), threading.Event()
    class Matcher:
        projection = None
        def __init__(self):
            self.calls, self.resets = 0, []
        def observe(self, *_args):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert resume.wait(1)
                self.projection = object()
                if late_error:
                    raise OSError('late old worker failure after projection')
            else:
                fresh.set()
            return dict(status='unknown', reason='opening-needs-multiple-frames')
        def reset(self, *, full=True):
            self.resets.append(full)
            if full:
                self.projection = None
    matcher = Matcher()
    controller = NativeGameClockController(backend, matcher=matcher)
    image = np.zeros((720,1280,3), np.uint8)
    controller.submit(image, dict(sequence=1, request_s=200.47, first_done_observed_s=200.48, reused=False),
                      now[0], visible=True)
    assert entered.wait(1)
    backend._observation_clock = None
    assert backend.observation_clock_snapshot() is None
    backend._observation_clock = mapper
    mapper.invalidate('high-rtt')
    for sequence in (11,12,13,14):
        mapper.observe(probe(sequence, 200 + sequence * .05))
    now[0] = 200.7008
    controller.submit(image, dict(sequence=2, request_s=200.685, first_done_observed_s=200.695, reused=False),
                      now[0], visible=True)
    resume.set()
    assert fresh.wait(1)
    assert True in matcher.resets and matcher.projection is None and backend._phase_slot is None
    controller.close(now[0])
    controller._thread.join(1)


@pytest.mark.parametrize('interruption',['quality','hard','scene','effect'])
def test_real_matcher_third_seed_keeps_sealed_quality_evidence_but_rejects_semantic_changes(tmp_path, monkeypatch, interruption):
    from agent.realtime import native_game_clock_matcher as matching
    from test_native_game_clock import geometry
    backend, mapper, now, probe, commit, _, _ = fixture(tmp_path, monkeypatch)
    for host in (200.2254,200.3254,200.4254):
        commit(host)
    backend._refresh_observation_views()
    heads, notes, _projection, game = geometry()
    backend._first_action_anchor_s = 200.475 - game
    matcher = matching.NativeGameClockMatcher(SimpleNamespace(start_time_s=0., judgements=notes))
    monkeypatch.setattr(matching, 'extract_heads', lambda _image: heads)
    controller = NativeGameClockController(backend, matcher=matcher)
    image = np.zeros((720,1280,3), np.uint8)
    for sequence in (1,2):
        controller.observe(image, dict(sequence=sequence, request_s=200.469 + sequence * .001,
            first_done_observed_s=200.479 + sequence * .001, reused=False), now[0], visible=True)
    assert len(matcher.opening) == 2 and matcher.projection is None
    entered, resume, rolled_back = threading.Event(), threading.Event(), threading.Event()
    aggregate, reset = matching.aggregate_bootstrap, matcher.reset
    def blocked(*args, **kwargs):
        entered.set()
        assert resume.wait(1)
        return aggregate(*args, **kwargs)
    def recorded_reset(*, full=True):
        reset(full=full)
        if full:
            rolled_back.set()
    monkeypatch.setattr(matching, 'aggregate_bootstrap', blocked)
    monkeypatch.setattr(matcher, 'reset', recorded_reset)
    finished=threading.Event()
    match_frame=controller._match_frame
    def recorded_match(*args,**kwargs):
        try:
            return match_frame(*args,**kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(controller,'_match_frame',recorded_match)
    controller.submit(image, dict(sequence=3, request_s=200.472, first_done_observed_s=200.482, reused=False),
                      now[0], visible=True)
    assert entered.wait(1)
    if interruption in {'hard','quality'}:
        if interruption == 'hard':
            backend._observation_clock = None
            assert backend.observation_clock_snapshot() is None
            backend._observation_clock = mapper
        mapper.invalidate('high-rtt')
        for sequence in (11,12,13,14):
            mapper.observe(probe(sequence, 200 + sequence * .05))
        now[0] = 200.7008
        assert controller._clock_relation_ready(now[0])
    elif interruption == 'scene':
        controller.invalidate(now[0],'invalid-scene')
    else:
        assert backend.request_future_phase(15,now[0])
        backend._consume_phase_candidate()
        effect=backend.phase_status_snapshot()
        assert effect.state == 'applied'
        backend._publish_phase_status(replace(effect,first_adjusted_due_s=200.49,
            first_affected_chunk_sequence=4,first_affected_action_token=4),'effect-published')
        commit(200.4904)
        backend._refresh_observation_views()
    resume.set()
    assert finished.wait(1)
    if interruption == 'quality':
        assert matcher.projection is not None and len(matcher.opening) == 3 and not rolled_back.is_set()
    else:
        assert rolled_back.is_set()
        assert matcher.projection is None and matcher.baseline_lag is None and not matcher.opening
    assert backend._phase_slot is None
    controller.close(now[0])
    controller._thread.join(1)


def test_clockkey_only_stale_job_exception_keeps_worker_alive_for_next_epoch_frame(tmp_path, monkeypatch):
    backend, mapper, now, probe, _, _, _ = fixture(tmp_path, monkeypatch)
    entered, resume, fresh = threading.Event(), threading.Event(), threading.Event()
    class Matcher:
        projection = None
        calls = 0
        def observe(self, *_args):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert resume.wait(1)
                self.projection = object()
                raise OSError('stale producer epoch job')
            fresh.set()
            return dict(status='matched', relative_lag_ms=0, capture_bounds_ms=(-1,1))
        def reset(self, *, full=True):
            if full:
                self.projection = None
    matcher = Matcher()
    controller = NativeGameClockController(backend, matcher=matcher)
    image = np.zeros((720,1280,3), np.uint8)
    controller.submit(image, dict(sequence=1, request_s=200.47, first_done_observed_s=200.48, reused=False),
                      now[0], visible=True)
    assert entered.wait(1)
    mapper.invalidate('high-rtt')
    for sequence in (11,12,13,14):
        mapper.observe(probe(sequence, 200 + sequence * .05))
    now[0] = 200.7008
    # 主线程还没观察新epoch；后台不能把旧job异常当本epoch observer故障而退出。
    resume.set()
    old = controller._results.get(timeout=1)
    assert old[1]['reason'] == 'clock-frame-epoch-invalid'
    assert matcher.projection is None and controller._thread.is_alive() and not controller.closed
    controller._results.put_nowait(old)
    controller.submit(image, dict(sequence=2, request_s=200.685, first_done_observed_s=200.695, reused=False),
                      now[0], visible=True)
    assert fresh.wait(1) and controller._thread.is_alive() and not controller.closed
    controller.close(now[0])
    controller._thread.join(1)
