from types import SimpleNamespace
import json

import numpy as np
import pytest

from agent.realtime import native_game_clock_matcher as module
from agent.realtime.native_observation_clock import ClockIdentity, ObservationClockView
from test_native_game_clock import geometry


def test_report_roundtrip_preserves_every_calibrated_family_baseline_and_geometry(monkeypatch):
    heads, notes, projection, game = geometry()
    matcher = module.NativeGameClockMatcher(SimpleNamespace(judgements=notes, start_time_s=0))
    current_heads = [heads]
    monkeypatch.setattr(module, 'extract_heads', lambda _image: current_heads[0])
    image = np.zeros((720, 1280, 3), np.uint8)
    for step in (0., .003, .006):
        matcher.observe(image, game + step - .004, game + step + .004, 0.)
    assert matcher.projection is not None and len(matcher.baseline_models) >= 2
    report = json.loads(json.dumps(matcher.report(), allow_nan=False))
    models = report['baseline_models']
    assert len(models) == len(matcher.projection['family']) == len(matcher.baseline_models)
    assert models == [dict(projection=dict(a=p['a'], h=p['h']), baseline_lag_s=baseline)
                      for p, baseline in matcher.baseline_models]
    assert len({(row['projection']['a'], row['projection']['h']) for row in models}) >= 2
    # 后续几何改变，使各family相对自身开场基线的差异实际参与bounds。
    later_game = game + .12
    intercept = later_game - projection['a'] / (590 - projection['h'])
    current_heads[0] = [dict(head, y=projection['a'] / (note.time_s - intercept) + projection['h'])
                        for head, note in zip(heads, notes)]
    lower, upper = later_game + .035, later_game + .045
    original = matcher.observe(image, lower, upper, 0.)
    assert original['status'] == 'matched'
    restored = module.NativeGameClockMatcher(matcher.chart)
    restored.projection = report['projection']
    restored.baseline_lag = report['baseline_lag_s']
    restored.baseline_interval = tuple(report['baseline_interval_s'])
    restored.baseline_models = [(row['projection'], row['baseline_lag_s']) for row in models]
    replayed = restored.observe(image, lower, upper, 0.)
    assert replayed['relative_lag_ms'] == original['relative_lag_ms']
    assert replayed['capture_bounds_ms'] == original['capture_bounds_ms']
    alternatives = [(lower + upper) / 2 - restored._game_for_pairs(original['match']['pairs'], p) - baseline
                    for p, baseline in restored.baseline_models]
    assert max(alternatives) - min(alternatives) > .000001
    assert matcher.report()['baseline_models_complete'] is True


def test_uncalibrated_report_exports_empty_json_baselines():
    report = module.NativeGameClockMatcher().report()
    assert json.loads(json.dumps(report, allow_nan=False))['baseline_models'] == []


def test_baseline_report_is_bounded_detached_and_failure_does_not_change_matcher():
    matcher = module.NativeGameClockMatcher()
    family = [dict(a=600., h=float(-index)) for index in range(352)]
    matcher.projection = dict(a=600., h=0., family=family)
    matcher.baseline_models = [(p, index / 10000) for index, p in enumerate(family)]
    report = json.loads(json.dumps(matcher.report(), allow_nan=False))
    assert len(report['baseline_models']) == report['baseline_models_capacity'] == 351
    assert report['baseline_models_dropped'] == 1 and not report['baseline_models_complete']
    report['baseline_models'][0]['projection']['a'] = -1
    assert matcher.baseline_models[0][0]['a'] == 600.
    original_projection = matcher.projection
    matcher.baseline_models = [(family[0], float('nan'))]
    report = matcher.report()
    assert report['baseline_models'] == [] and report['baseline_models_warning'] == 'ValueError'
    assert not report['baseline_models_complete']
    assert matcher.projection is original_projection and np.isnan(matcher.baseline_models[0][1])
    json.dumps(report, allow_nan=False)


def view(lower, upper, delta):
    return ObservationClockView(ClockIdentity('matcher-run', 1, 42, 100), 1, 1, 2,
        100 + delta - .0004, 100 + delta + .0004, 100, lower - .01, upper + .01,
        lower - 100 - delta - .01, upper - 100 - delta + .01)


@pytest.mark.parametrize('delta', [-.403, -.103, .103, .403])
@pytest.mark.parametrize('phase', [-.030, 0., .030])
def test_real_geometry_normalizes_search_prior_lag_and_full_bounds_in_same_frozen_e_domain(monkeypatch, delta, phase):
    heads, notes, projection, game = geometry()
    matcher = module.NativeGameClockMatcher(SimpleNamespace(judgements=notes, start_time_s=0))
    matcher.projection, matcher.baseline_lag, matcher.baseline_interval = projection, 0., (0., 0.)
    monkeypatch.setattr(module, 'extract_heads', lambda _image: heads)
    anchor = 1000.
    lower, upper = anchor + game + phase + delta - .005, anchor + game + phase + delta + .005
    result = matcher.observe(np.zeros((720, 1280, 3), np.uint8), lower, upper, anchor, view(lower, upper, delta))
    assert result['status'] == 'matched'
    assert result['relative_lag_ms'] == pytest.approx(phase * 1000, abs=.0001)
    lo, hi = result['capture_bounds_ms']
    assert lo <= phase * 1000 - 5.39 and hi >= phase * 1000 + 5.39


def test_original_h_frame_age_is_not_relaxed_by_execution_domain_transform(monkeypatch):
    heads, notes, projection, game = geometry()
    matcher = module.NativeGameClockMatcher(SimpleNamespace(judgements=notes, start_time_s=0))
    matcher.projection, matcher.baseline_lag, matcher.baseline_interval = projection, 0., (0., 0.)
    monkeypatch.setattr(module, 'extract_heads', lambda _image: heads)
    result = matcher.observe(np.zeros((720, 1280, 3), np.uint8), game - .041, game + .041, 0,
        view(game - .041, game + .041, .103))
    assert result == {'status': 'unknown', 'reason': 'unsupported-frame'}


def test_opening_baseline_freezes_normalized_window_and_does_not_absorb_later_common_clock_step(monkeypatch):
    image = np.zeros((720, 1280, 3), np.uint8)
    matcher = module.NativeGameClockMatcher(SimpleNamespace(judgements=[], start_time_s=0))
    physical_game = [1.]
    priors = []
    def best():
        game = physical_game[0]
        return dict(game_time_s=game, game_time_bounds_s=(game, game), pairs=[])
    def bootstrap(_heads, _notes, host):
        priors.append(host)
        return dict(status='matched', best=best())
    monkeypatch.setattr(module, 'extract_heads', lambda _image: [])
    monkeypatch.setattr(module, 'bootstrap', bootstrap)
    monkeypatch.setattr(module, 'aggregate_bootstrap', lambda frames, _notes, **_kwargs: dict(
        status='matched', projection={'a': 600, 'h': -100, 'family': []},
        checks=[{'best': seed['bootstrap_seed']['best']} for seed in frames]))
    monkeypatch.setattr(module, 'match', lambda *_args: dict(status='matched', best=best()))
    anchor, initial_delta, capture_lag = 100., .06, .017
    for game in (1., 1.1, 1.2):
        physical_game[0] = game
        lower, upper = anchor + game + capture_lag + initial_delta - .005, anchor + game + capture_lag + initial_delta + .005
        matcher.observe(image, lower, upper, anchor, view(lower, upper, initial_delta))
    assert matcher.baseline_lag == pytest.approx(capture_lag)
    assert priors == pytest.approx([1.017, 1.117, 1.217])
    physical_game[0] = 2.
    for delta in (initial_delta + .103, initial_delta - .103):
        lower, upper = anchor + 2 + capture_lag + delta - .005, anchor + 2 + capture_lag + delta + .005
        result = matcher.observe(image, lower, upper, anchor, view(lower, upper, delta))
        assert result['relative_lag_ms'] == pytest.approx(0, abs=.0001)
        assert result['capture_bounds_ms'][0] < 0 < result['capture_bounds_ms'][1]
