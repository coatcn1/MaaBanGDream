import itertools
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime import native_game_clock_matcher as matching
from agent.realtime.native_game_clock import NativeGameClockController
from test_native_game_clock import Matcher, emit, geometry
from test_native_life_feedback import _phase_backend


def _scalar_bootstrap(heads, notes, host):
    """保存改前逐候选路径，核对指派、同分次序和全部竞争结果。"""
    hs, ns = matching.prepare(heads, notes, host)
    if not 4 <= len(hs) <= 16 or not matching._depth_supported(x['y'] for x in hs):
        return matching.evaluate([])
    candidates = []
    for h in np.arange(-300, 50.01, 2.):
        for i, j in itertools.combinations(range(len(hs)), 2):
            den = 1 / (hs[i]['y'] - h) - 1 / (hs[j]['y'] - h)
            if abs(den) < 1e-8:
                continue
            for ni in [n for n in ns if n.lane == hs[i]['lane']]:
                for nj in [n for n in ns if n.lane == hs[j]['lane']]:
                    a = (ni.time_s - nj.time_s) / den
                    if not 150 < a < 1500:
                        continue
                    c = ni.time_s - a / (hs[i]['y'] - h)
                    initial = matching.score(hs, ns, float(a), float(h), float(c), host)
                    if not initial or initial['count'] < 4 or initial['lanes'] < 3:
                        continue
                    design = np.array([[1 / (hs[p['head_index']]['y'] - h), 1]
                                       for p in initial['pairs']])
                    target = np.array([p['due_s'] for p in initial['pairs']])
                    aa, cc = np.linalg.lstsq(design, target, rcond=None)[0]
                    refined = matching.score(hs, ns, float(aa), float(h), float(cc), host)
                    if (refined and refined['count'] >= 4 and refined['lanes'] >= 3
                            and matching._depth_supported(p['head_y'] for p in refined['pairs'])):
                        candidates.append(refined)
    unique = {}
    for result in candidates:
        key = tuple((p['head_index'], p['note_index']) for p in result['pairs'])
        if key not in unique or matching.rank(result) < matching.rank(unique[key]):
            unique[key] = result
    return matching.evaluate(matching.clusters(list(unique.values())))


@pytest.mark.parametrize('variant', ['normal', 'ambiguous', 'duplicate-index', 'wrong-lane'])
def test_batched_candidates_keep_exact_scalar_results(monkeypatch, variant):
    heads, notes, _projection, host = geometry()
    if variant == 'ambiguous':
        notes += [SimpleNamespace(**{**vars(n), 'time_s': n.time_s + .1,
                                     'note_index': n.note_index + 10}) for n in notes]
    elif variant == 'duplicate-index':
        notes.insert(0, SimpleNamespace(**vars(notes[0])))
        heads.insert(1, dict(heads[0]))
    elif variant == 'wrong-lane':
        notes = [SimpleNamespace(**{**vars(n), 'lane': 6}) for n in notes]
    expected = _scalar_bootstrap(heads, notes, host)
    # 小批量强制跨批，避免只证明单批向量化而遗漏候选顺序。
    monkeypatch.setattr(matching, '_BOOTSTRAP_BATCH_SIZE', 3, raising=False)
    assert matching.bootstrap(heads, notes, host) == expected


def test_opening_three_frames_reuse_self_computed_seeds_exactly(monkeypatch):
    heads, notes, _projection, host = geometry()
    matcher = matching.NativeGameClockMatcher(SimpleNamespace(start_time_s=0., judgements=notes))
    monkeypatch.setattr(matching, 'extract_heads', lambda _image: heads)
    original = matching.bootstrap
    bootstrap_calls = []

    def counted(*args):
        bootstrap_calls.append(args[2])
        return original(*args)

    monkeypatch.setattr(matching, 'bootstrap', counted)
    image = np.zeros((720, 1280, 3), np.uint8)
    for offset in (0., .001, .002):
        result = matcher.observe(image, host + offset - .005, host + offset + .005, 0.)
    assert result['status'] == 'matched'
    assert len(bootstrap_calls) == 3
    expected = matching.aggregate_bootstrap(list(matcher.opening), notes)
    assert expected['projection'] == matcher.projection
    assert len(bootstrap_calls) == 6
    assert len(matcher.opening) == 3
    assert len(matcher.projection['family']) > 1


class OpeningMatcher(Matcher):
    def __init__(self):
        super().__init__(30)
        self.opening = []
        self.resets = []

    def observe(self, image, request_s, observed_s, anchor_s):
        self.opening.append(observed_s)
        return super().observe(image, request_s, observed_s, anchor_s)

    def reset(self, *, full):
        self.resets.append(full)
        self.opening.clear()


def _controller():
    backend, _calls = _phase_backend()
    backend._first_action_anchor_s = 0.
    return NativeGameClockController(backend, matcher=OpeningMatcher()), backend


def _metadata(now, sequence, **extra):
    return dict(request_s=now - .01, sequence=sequence, reused=False, **extra)


@pytest.mark.parametrize('problem', ['reused', 'slow'])
def test_async_short_bad_capture_preserves_opening_result_epoch_and_three_samples(problem):
    controller, backend = _controller()
    image = np.zeros((720, 1280, 3), np.uint8)

    def await_result():
        result = controller._results.get(timeout=3.)
        controller._results.put_nowait(result)

    try:
        controller.submit(image, _metadata(.8, 1), .8, visible=True)
        await_result()
        epoch = controller.epoch
        bad = dict(sequence=1, request_s=.79, reused=True) if problem == 'reused' else dict(
            sequence=2, request_s=.7, reused=False)
        controller.submit(image, bad, .85, visible=True)
        assert controller.epoch == epoch
        assert controller._frames.empty()
        assert not controller.samples
        for now, sequence in ((.9, 3), (.95, 4)):
            controller.submit(image, _metadata(now, sequence), now, visible=True)
            await_result()
        controller.submit(image, _metadata(1., 5), 1., visible=True)
        assert controller.matcher.opening[:3] == [.8, .9, .95]
        assert controller.matcher.resets == []
        assert controller.summary['trusted-target'] == 1
        assert backend._phase_slot[3] == 15
    finally:
        controller.close(1.01, 'stopped')
        controller._thread.join(timeout=3.)
        assert not controller._thread.is_alive()


@pytest.mark.parametrize('owner', ['game-clock', 'feedback'])
def test_short_reused_preserves_legal_pending_owner_and_expiry(owner):
    controller, backend = _controller()
    for sequence, now in enumerate((.8, .85, .9), 1):
        emit(controller, now, sequence)
    if owner == 'feedback':
        backend.revoke_future_phase('replace-fixture', owner='game-clock')
        backend.set_visual_phase_guard(False)
        assert backend.request_future_phase(-15, .91)
    pending = backend._phase_slot
    epoch, trusted, samples = controller.epoch, controller.trusted_at, list(controller.samples)
    controller.observe(None, dict(sequence=3, request_s=.89, reused=True), .95, visible=True)
    assert backend._phase_slot == pending
    assert controller.epoch == epoch
    assert controller.trusted_at == trusted
    assert list(controller.samples) == samples


def test_only_reused_frames_expire_trusted_target_and_reset_gap_once():
    controller, backend = _controller()
    for sequence, now in enumerate((.8, .85, .9), 1):
        emit(controller, now, sequence)
    assert backend._phase_slot is not None and backend._visual_phase_guard
    for now in (1.1, 1.41, 1.6, 1.9):
        controller.submit(None, dict(sequence=3, request_s=.89, reused=True), now, visible=True)
    assert controller.trusted_at is None
    assert backend._phase_slot is None and not backend._visual_phase_guard
    assert controller.summary['stale-target'] == 1
    assert controller.summary['sampling-gap'] == 1
    controller._reset_matcher_if_due()
    assert controller.matcher.resets == [False]
    assert not controller.matcher.opening


@pytest.mark.parametrize('reason', ['invalid-scene', 'stale-run', 'stopped', 'sampling-gap'])
def test_real_invalidations_still_drop_epoch_result_and_reset_opening(reason):
    controller, backend = _controller()
    emit(controller, .8, 1)
    epoch = controller.epoch
    controller._thread = SimpleNamespace()
    controller._results.put_nowait((epoch, dict(status='matched', relative_lag_ms=30),
                                    _metadata(.8, 1), .8))
    if reason == 'stale-run':
        backend._run_id = 'another-run'
        controller.submit(None, _metadata(.9, 2), .9, visible=True)
    elif reason == 'stopped':
        controller.close(.9, 'stopped')
    else:
        now = 1.31 if reason == 'sampling-gap' else .9
        controller.submit(None, dict(sequence=1, request_s=.79, reused=True), now,
                          visible=reason != 'invalid-scene')
    assert controller.epoch > epoch
    controller._reset_matcher_if_due()
    assert not controller.matcher.opening
    assert not controller.samples
    assert backend._phase_slot is None and not backend._visual_phase_guard
    assert controller.summary[reason] == 1


def test_old_epoch_and_expired_result_cannot_add_samples_or_request():
    controller, backend = _controller()
    controller._thread = SimpleNamespace()
    image = np.zeros((720, 1280, 3), np.uint8)
    controller.submit(image, _metadata(.8, 1), .8, visible=True)
    controller._results.put_nowait((controller.epoch - 1, dict(status='matched', relative_lag_ms=30),
                                    _metadata(.8, 1), .8))
    controller.submit(image, _metadata(.9, 2), .9, visible=True)
    assert not controller.samples
    controller.submit(image, _metadata(1.3, 3), 1.3, visible=True)
    controller._results.put_nowait((controller.epoch, dict(status='matched', relative_lag_ms=30),
                                    _metadata(.8, 1), .8))
    controller.submit(image, _metadata(1.31, 4), 1.31, visible=True)
    assert not controller.samples
    assert backend._phase_slot is None and not backend._visual_phase_guard
