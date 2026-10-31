import copy
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.realtime import native_engine
from agent.realtime.live_session import current_live_run, reset_live_run, update_live_run
from agent.realtime.native_play import NativeMinitouchBackend
from agent.realtime.native_prearm import frozen_native_runtime_options, resolve_confirmed_chart
from agent.realtime.native_random_great import (
    RANDOM_GREAT_DEFAULTS, RANDOM_GREAT_MODES, chart_random_great_inputs,
    plan_random_great, validate_random_great_options, build_random_great_safety_context,
    _balanced_sample,
)
from agent.realtime.profile_store import RealtimeProfileStore


ENABLED = RANDOM_GREAT_DEFAULTS | {"native_random_great_enabled": True}
ROOT = Path(__file__).resolve().parents[1]
requires_native = pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")


def action(due, index, kind="tap", contact=-1):
    return dict(kind=kind, due_s=float(due), lane=index % 7, contact=contact,
                target_x=640., note_index=index, flick_direction=None)


def plan(actions, **overrides):
    return plan_random_great(actions, **(dict(options=ENABLED, mode="formal", run_id="random-great-test",
        level=26, total_judgements=100, ordinary_single_note_indices=set(range(1000))) | overrides))


@pytest.mark.parametrize("level,percent", [(1, 2), (26, 2), (27, 3), (28, 4), (34, 10), (40, 10), (None, 2), (True, 2)])
def test_level_rule_uses_whole_chart_denominator(level, percent):
    actions = [action(i * .5, i) for i in range(100)]
    shifted, report = plan(actions, level=level)
    assert report["target_percent"] == percent
    assert report["desired"] == report["selected"] == percent
    assert report["planned_percent"] == percent
    assert abs(report["fast"] - report["slow"]) <= 1
    assert report["actual_great_measured"] is False
    assert shifted[0] == actions[0] and shifted[-1] == actions[-1]


def test_configurable_formula_and_missing_level_fallback():
    options = ENABLED | {"native_random_great_base_percent": 1.5, "native_random_great_level_start": 25,
                         "native_random_great_per_level_percent": .5, "native_random_great_max_percent": 5}
    _, report = plan([action(i, i) for i in range(100)], level=28, options=options)
    assert report["target_percent"] == 3
    _, fallback = plan([action(i, i) for i in range(100)], level=None, options=options)
    assert fallback["target_percent"] == 1.5
    assert fallback["level_source"] == "base-fallback"


@pytest.mark.parametrize("mode", sorted(RANDOM_GREAT_MODES))
def test_only_formal_modes_apply(mode):
    _, report = plan([action(i, i) for i in range(100)], mode=mode)
    assert report["enabled"] and report["selected"] == 2


@pytest.mark.parametrize("mode", ["rehearsal", "calibration-formal", "calibration-rehearsal", "legacy", "unknown", ""])
def test_other_modes_preserve_exact_actions(mode):
    actions = [action(i, i) for i in range(100)]
    result, report = plan(actions, mode=mode)
    assert result == actions and not report["enabled"]


def test_disabled_default_does_not_modify_or_reorder_actions():
    actions = [action(i, i) for i in range(100)]
    original = copy.deepcopy(actions)
    result, report = plan(actions, options={})
    assert result == original == actions
    assert all(left is right for left, right in zip(result, actions))
    assert report["selected"] == 0


@pytest.mark.parametrize("key,value", [
    ("native_random_great_enabled", "false"), ("native_random_great_enabled", 1),
    *[(key, value) for key in ("native_random_great_base_percent", "native_random_great_per_level_percent", "native_random_great_max_percent")
      for value in (True, "2", None, float("nan"), float("inf"), -1, 20.1)],
    *[("native_random_great_level_start", value) for value in (True, 26., 0, 41, "26")],
    *[("native_random_great_offset_ms", value) for value in (True, 45., 34, 61, "45")],
])
def test_strict_configuration_and_atomic_profile_save(tmp_path, key, value):
    store = RealtimeProfileStore(tmp_path)
    before = store.runtime_options()
    with pytest.raises(ValueError, match=key):
        store.update_runtime_options(before | {key: value})
    assert store.runtime_options() == before


def test_old_options_roundtrip_and_all_fields_freeze_before_next_run(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    old = store.update_runtime_options({"native_realtime_enabled": True})
    assert {key: old[key] for key in RANDOM_GREAT_DEFAULTS} == RANDOM_GREAT_DEFAULTS
    options = old | ENABLED | {"native_random_great_base_percent": 3.5, "native_random_great_level_start": 25,
                              "native_random_great_per_level_percent": .5, "native_random_great_max_percent": 7,
                              "native_random_great_offset_ms": 50}
    assert store.update_runtime_options(options) == options
    run = reset_live_run(mode="formal", difficulty="Expert")
    assert frozen_native_runtime_options(run, options) == options
    assert frozen_native_runtime_options(run, old) == options
    assert frozen_native_runtime_options(current_live_run(), old) == options
    assert frozen_native_runtime_options(reset_live_run(mode="formal", difficulty="Expert"), old) == old


def test_same_run_reproducible_other_runs_randomized_without_mutation():
    actions = [action(i * .5, i) for i in range(100)]
    original = copy.deepcopy(actions)
    first, report = plan(actions)
    assert (first, report) == plan(actions)
    assert first != plan(actions, run_id="another-run")[0]
    assert original == actions
    differences = [(old, new) for old, new in zip(actions, first) if old != new]
    assert len(differences) == report["selected"]
    for old, new in differences:
        assert old | {"due_s": new["due_s"]} == new
        assert abs(new["due_s"] - old["due_s"]) == pytest.approx(.045)
    assert [row["due_s"] for row in first] == sorted(row["due_s"] for row in first)


def test_raw_visible_points_include_slide_middle_and_ignore_hidden_and_degenerate_types(tmp_path):
    chart = tmp_path / "chart.json"
    chart.write_text(json.dumps([
        {"type": "BPM", "beat": 0, "bpm": 120}, {"type": "System", "beat": 0},
        {"type": "Long", "connections": [{"beat": 0, "lane": 2, "hidden": True}]},
        {"type": "Single", "beat": 1, "lane": 1},
        {"type": "Slide", "connections": [{"beat": b, "lane": 2, "hidden": b == 3} for b in (2, 3, 4, 5)]},
        {"type": "Slide", "connections": [{"beat": 6, "lane": 2}]},
        {"type": "Single", "beat": 7, "lane": 1, "flick": True},
        {"type": "Directional", "beat": 8, "lane": 1, "direction": "Left"},
        {"type": "Single", "beat": 9, "lane": 1},
    ]), encoding="utf-8")
    indices, total = chart_random_great_inputs(chart)
    assert indices == {0, 5} and total == 8
    if native_engine.available():
        actions = native_engine.compile_chart(chart).compile_actions({})
        assert {row["due_s"] for row in actions if row["note_index"] in indices} == {.5, 4.5}


def test_real_chart_denominator_counts_visible_connections():
    path = ROOT / "resource/charts/bestdori/165/expert.json"
    if not path.exists():
        pytest.skip("本地谱面未同步")
    _, total = chart_random_great_inputs(path)
    assert total == 734


@pytest.mark.parametrize("offset", [35, 45, 60])
def test_holds_chords_dense_notes_flicks_and_entire_first_last_groups_never_shift(offset):
    protected = [action(0, 0), action(0, 1), action(1, 2), action(1, 3),
        action(2, 4, "flick"), action(2.1, 5), action(3, 6, "down", 1),
        action(3.5, 7), action(3.8, 6, "move", 1), action(4, 6, "up", 1),
        action(4.1, 8), action(4.9, 9), action(5, 10, "down", 1),
        action(6, 10, "up", 1), action(7, 11), action(7.05, 12),
        action(8, 13), action(9, 14), action(10, 15), action(10, 16)]
    shifted, report = plan(protected, total_judgements=1000,
        options=ENABLED | {"native_random_great_base_percent": 20, "native_random_great_max_percent": 20,
                           "native_random_great_offset_ms": offset},
        ordinary_single_note_indices=set(range(17)) - {13})
    changed = [old["note_index"] for old, new in zip(protected, shifted) if old != new]
    assert changed == [14]
    assert report["eligible"] == report["selected"] == 1
    assert report["shortfall"] == report["desired"] - 1


@pytest.mark.parametrize("offset", [35, 45, 60])
def test_all_selected_neighbors_retain_up_margin_even_when_both_move_inward(offset):
    gap = 2 * offset / 1000 + .071
    actions = [action(i * gap, i) for i in range(100)]
    for run in range(20):
        shifted, report = plan(actions, run_id=str(run), total_judgements=1000,
            options=ENABLED | {"native_random_great_offset_ms": offset,
                               "native_random_great_base_percent": 20, "native_random_great_max_percent": 20})
        assert report["selected"] == 98
        assert min(b["due_s"] - a["due_s"] for a, b in zip(shifted, shifted[1:])) >= .070 - 1e-9


def write_chart(tmp_path):
    path = tmp_path / "chart.json"
    path.write_text(json.dumps([{"type": "BPM", "beat": 0, "bpm": 120}]
        + [{"type": "Single", "beat": i, "lane": i % 7} for i in range(100)]), encoding="utf-8")
    return path


@requires_native
def test_real_backend_disables_phase_feedback_but_default_and_modes_do_not(tmp_path):
    path = write_chart(tmp_path)
    baseline = native_engine.compile_chart(path).compile_actions({})
    for mode, options, expected in (("formal", {}, False), ("formal", ENABLED, True),
            ("calibration-formal", ENABLED, False), ("rehearsal", ENABLED, False)):
        backend = NativeMinitouchBackend(path, adb_path="unused", serial="unused",
            device=SimpleNamespace(request_reset=lambda: True), life_feedback_enabled=True,
            random_great_options=options, start_gate_mode=mode, chart_level=26, expected_notes=100,
            run_id="backend-plan")
        assert backend.report()["native_random_great"]["enabled"] is expected
        assert backend.life_feedback_enabled is (not expected)
        assert (backend._actions != baseline) is expected
        if expected:
            assert backend.request_future_phase(15, 1) is False
            assert backend._consume_phase_candidate() is None
            assert backend._feedback_tickets is None


@requires_native
@pytest.mark.parametrize("failure", ["count", "planner"])
def test_planning_or_denominator_failure_keeps_original_actions_and_task(tmp_path, monkeypatch, failure):
    path = write_chart(tmp_path)
    baseline = native_engine.compile_chart(path).compile_actions({})
    if failure == "planner":
        monkeypatch.setattr("agent.realtime.native_random_great.plan_random_great", lambda *a, **kw: (_ for _ in ()).throw(OSError("planner fault")))
    backend = NativeMinitouchBackend(path, adb_path="unused", serial="unused",
        device=SimpleNamespace(request_reset=lambda: True), life_feedback_enabled=True,
        random_great_options=ENABLED, start_gate_mode="formal", chart_level=26,
        expected_notes=99 if failure == "count" else 100,
        observation_clock_trial_enabled=True, visual_refresh_enabled=True)
    assert backend._actions == baseline
    assert backend.report()["native_random_great"]["status"] == "fallback"
    assert backend.life_feedback_enabled
    assert backend._native_clock_trial and backend._native_visual_refresh_enabled


@requires_native
def test_optional_plan_logging_failure_keeps_complete_valid_plan(tmp_path, monkeypatch):
    path = write_chart(tmp_path)
    monkeypatch.setattr("builtins.print", lambda *a, **kw: (_ for _ in ()).throw(OSError("stdout fault")))
    backend = NativeMinitouchBackend(path, adb_path="unused", serial="unused",
        device=SimpleNamespace(request_reset=lambda: True), life_feedback_enabled=True,
        random_great_options=ENABLED, start_gate_mode="formal", chart_level=26, expected_notes=100,
        observation_clock_trial_enabled=True, visual_refresh_enabled=True)
    report = backend.report()["native_random_great"]
    assert report["status"] == "planned" and report["selected"] == 2
    assert report["phase_feedback_requested"] and report["phase_feedback_suppressed"]
    assert not backend.life_feedback_enabled and not backend._native_clock_trial and not backend._native_visual_refresh_enabled


@pytest.mark.parametrize("terminal", ["completed", "life-failed", "stop"])
def test_feedback_disabled_engine_still_reads_life_terminal_and_manual_stop(monkeypatch, terminal):
    from test_native_life_feedback import _virtual_feedback_engine
    stats, data, backend = _virtual_feedback_engine(monkeypatch, enabled=False, terminal=terminal)
    assert data["life"] and data["terminal"] and data["requests"] == []
    assert stats.completed is (terminal == "completed")
    assert stats.life_failed is (terminal == "life-failed")
    assert stats.stopped is (terminal == "stop")


@pytest.mark.parametrize("mode,native,random_enabled,skip", [("cooperative", True, True, True),
    ("versus", True, True, True), ("cooperative", True, False, False), ("cooperative", False, True, False)])
def test_cooperative_jitter_uses_frozen_random_choice_without_changing_user_setting(tmp_path, monkeypatch, mode, native, random_enabled, skip):
    calls = []
    options = RealtimeProfileStore.DEFAULT_RUNTIME_OPTIONS | {"native_realtime_enabled": native,
        "native_random_great_enabled": random_enabled, "cooperative_jitter_enabled": True}
    monkeypatch.setattr(RealtimeProfileStore, "runtime_options", lambda self: options)
    monkeypatch.setattr("agent.realtime.native_prearm._cooperative_jittered_chart", lambda path, *_: calls.append(path) or path)
    run = reset_live_run(mode=mode, difficulty="Expert")
    run = update_live_run(song_id="test", song_level=26, song_title="test", prepared_for_play=True)
    frozen_native_runtime_options(run, options)
    options["native_random_great_enabled"] = not random_enabled
    path = tmp_path / "chart.json"
    selection = SimpleNamespace(path=path)
    result = resolve_confirmed_chart(run, "Expert", project_root=tmp_path,
        repository=SimpleNamespace(resolve=lambda *a, **kw: SimpleNamespace(selection=selection, reason="matched")))
    assert result.selection is selection
    assert bool(calls) is (not skip)
    assert options["cooperative_jitter_enabled"] is True


@requires_native
def test_actual_native_session_and_compiler_keep_all_actions_receipts_and_first_anchor(tmp_path):
    path = write_chart(tmp_path)
    actions = native_engine.compile_chart(path).compile_actions({})
    ordinary, total = chart_random_great_inputs(path)
    shifted, report = plan(actions, total_judgements=total, ordinary_single_note_indices=ordinary)
    assert report["selected"] == 2
    now = [0.]
    compiler = native_engine.touch_script_compiler()
    chunks, receipts, scripts = [], [], []
    def publish(chunk):
        chunks.append(chunk)
        rows = [dict(row["action"], due_s=row["engine_due_s"]) for row in chunk["actions"]]
        reservations = [dict(row["action"], due_s=row["engine_due_s"]) for row in chunk["future_down_reservations"]]
        scripts.extend(compiler.compile(rows, chunk["touch_config"], chunk["window_start_s"],
            chunk["final_chunk"], chunk["window_end_s"], reservations))
        receipts.extend(compiler.last_execution_receipts())
        return True
    session = native_engine.playback_session(publish=publish, clock=lambda: now[0])
    assert session.arm(shifted, {})
    assert session.start(1.)
    for tick in range(550):
        now[0] = tick / 10
        session.publish()
        if chunks and chunks[-1]["final_chunk"]:
            break
    flattened = [item for chunk in chunks for item in chunk["actions"]]
    assert [row["action"]["note_index"] for row in flattened] == list(range(100))
    assert flattened[0]["engine_due_s"] == 1.
    assert len(receipts) == 100
    assert [row["action_token"] for row in receipts] == list(range(1, 101))
    assert sum(line.startswith("d ") for line in scripts) == 100
    assert sum(line.startswith("u ") for line in scripts) == 100
    assert compiler.phase_boundary_safe()


def geometry_plan(tmp_path, *, connections=None, tap_at=2., lane=0, extra=(), max_contacts=10):
    path = tmp_path / "geometry.json"
    raw = [{"type": "BPM", "beat": 0, "bpm": 60}, {"type": "Single", "beat": 0, "lane": 0},
           {"type": "Slide", "connections": connections or [{"beat": .5, "lane": 2}, {"beat": 5, "lane": 2}]},
           {"type": "Single", "beat": tap_at, "lane": lane}, *extra,
           {"type": "Single", "beat": 8, "lane": 0}]
    path.write_text(json.dumps(raw), encoding="utf-8")
    actions = native_engine.compile_chart(path).compile_actions({})
    ordinary, total = chart_random_great_inputs(path)
    context = build_random_great_safety_context(path, actions, max_contacts=max_contacts)
    shifted, report = plan(actions, options=ENABLED | {"native_random_great_base_percent": 20,
        "native_random_great_max_percent": 20}, total_judgements=total,
        ordinary_single_note_indices=ordinary, safety_context=context)
    return path, actions, context, shifted, report


@requires_native
@pytest.mark.parametrize("distance,expected", [(299, 0), (300, 1), (301, 1)])
def test_entire_hold_window_uses_actual_mapping_and_strict_300px_boundary(tmp_path, distance, expected):
    path, actions, context, _, _ = geometry_plan(tmp_path)
    centers = (190., 340., 190. + distance, 640., 790., 940., 1090.)
    actions = native_engine.compile_chart(path).compile_actions({"lane_centers": list(centers)})
    context = build_random_great_safety_context(path, actions, lane_centers=centers, judgement_y=580.)
    _, report = plan(actions, total_judgements=len(context.visible), ordinary_single_note_indices=context.ordinary,
        safety_context=context, options=ENABLED | {"native_random_great_base_percent": 20, "native_random_great_max_percent": 20})
    assert report["eligible"] == report["selected"] == expected
    assert report["selected_with_active_hold"] == expected


@requires_native
@pytest.mark.parametrize("hidden,expected", [(True, 1), (False, 0)])
def test_visible_middle_connection_forms_chord_but_hidden_geometry_is_not_judgement(tmp_path, hidden, expected):
    _, _, _, _, report = geometry_plan(tmp_path, connections=[{"beat": .5, "lane": 6},
        {"beat": 2, "lane": 6, "hidden": hidden}, {"beat": 5, "lane": 6}])
    assert report["eligible"] == expected


@requires_native
@pytest.mark.parametrize("middle", [1.85, 2.15])
def test_visible_middle_global_isolation_excludes_dense_original_judgement(tmp_path, middle):
    _, _, _, _, report = geometry_plan(tmp_path, connections=[{"beat": .5, "lane": 6},
        {"beat": middle, "lane": 6}, {"beat": 5, "lane": 6}])
    assert report["eligible"] == 0


@requires_native
@pytest.mark.parametrize("points", [
    [{"beat": .5, "lane": 0}, {"beat": 5, "lane": 0}],
    [{"beat": 1.8, "lane": 4}, {"beat": 2.2, "lane": 0}],
    [{"beat": .5, "lane": 1}, {"beat": 1.95, "lane": 1, "hidden": True},
     {"beat": 1.96, "lane": 6, "hidden": True}, {"beat": 5, "lane": 6}],
    [{"beat": .5, "lane": 6}, {"beat": 2.09, "lane": 1, "hidden": True}, {"beat": 5, "lane": 6}],
])
def test_same_lane_crossing_and_upstream_downstream_geometry_block_tap(tmp_path, points):
    _, _, _, _, report = geometry_plan(tmp_path, connections=points)
    assert report["eligible"] == 0


@requires_native
@pytest.mark.parametrize("middle,fast,slow", [(1.81, 0, 1), (2.19, 1, 0)])
def test_direction_only_candidate_moves_away_to_200ms_visible_non_tap_guard(tmp_path, middle, fast, slow):
    _, _, _, _, report = geometry_plan(tmp_path, connections=[{"beat": .5, "lane": 6},
        {"beat": middle, "lane": 6}, {"beat": 5, "lane": 6}])
    assert report["eligible_fast_only"] == fast and report["eligible_slow_only"] == slow
    assert report["fast"] == fast and report["slow"] == slow


@requires_native
@pytest.mark.parametrize("middle,both", [(1.755, 1), (1.755001, 0)])
def test_offset_non_tap_200ms_boundary_is_inclusive_without_rounding_to_frame(tmp_path, middle, both):
    _, _, _, _, report = geometry_plan(tmp_path, connections=[{"beat": .5, "lane": 6},
        {"beat": middle, "lane": 6}, {"beat": 5, "lane": 6}])
    assert report["eligible_both"] == both
    assert report["eligible_slow_only"] == 1 - both


@requires_native
def test_capacity_exhaustion_and_unknown_or_unclosed_paths_fail_closed(tmp_path):
    path, actions, context, _, report = geometry_plan(tmp_path, max_contacts=1)
    assert report["eligible"] == 0
    broken = [row for row in actions if row["kind"] != "up"]
    with pytest.raises(ValueError, match="未闭合"):
        build_random_great_safety_context(path, broken)
    with pytest.raises(ValueError, match="映射"):
        build_random_great_safety_context(path, actions, lane_centers=[0, 1])
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[2]["width"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="宽度"):
        build_random_great_safety_context(path, actions)
    with pytest.raises(ValueError, match="上下文"):
        plan([dict(row, due_s=row["due_s"] + .01) for row in actions],
            safety_context=context, total_judgements=len(context.visible), ordinary_single_note_indices=context.ordinary)


@requires_native
def test_new_backend_plan_freezes_offsets_and_unknown_path_falls_back_with_original_feedback(tmp_path):
    path, actions, _, _, _ = geometry_plan(tmp_path)
    options = ENABLED | {"native_random_great_base_percent": 20, "native_random_great_max_percent": 20}
    kwargs = dict(adb_path="unused", serial="unused", device=SimpleNamespace(request_reset=lambda: True),
        life_feedback_enabled=True, observation_clock_trial_enabled=True, visual_refresh_enabled=True,
        random_great_options=options, start_gate_mode="formal", chart_level=26, expected_notes=5)
    backend = NativeMinitouchBackend(path, **kwargs)
    assert backend._random_great_report["selected_with_active_hold"] == 1
    frozen = copy.deepcopy(backend._actions)
    backend.configure_timing_offset(37)
    assert backend._actions == frozen and backend._frozen_timing_offset_ms == 37
    assert backend._actions[0] == actions[0] and backend._actions[-1] == actions[-1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[2]["width"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    fallback = NativeMinitouchBackend(path, **kwargs)
    assert fallback._actions == actions and fallback._random_great_report["status"] == "fallback"
    assert fallback.life_feedback_enabled and fallback._native_clock_trial and fallback._native_visual_refresh_enabled


@pytest.mark.parametrize("fast_only,slow_only,both,desired,expected", [
    (10, 0, 0, 10, 1), (10, 1, 0, 10, 3), (1, 10, 0, 10, 3),
    (10, 0, 10, 12, 12), (0, 10, 10, 12, 12), (6, 6, 39, 43, 43), (0, 0, 0, 5, 0)])
def test_balanced_direction_quotas_choose_feasible_candidates_before_sampling(fast_only, slow_only, both, desired, expected):
    candidates = [(i, (-1,), False) for i in range(fast_only)]
    candidates += [(i + 100, (1,), False) for i in range(slow_only)]
    candidates += [(i + 200, (-1, 1), False) for i in range(both)]
    for seed in range(20):
        selected = _balanced_sample(candidates, desired, random.Random(seed))
        assert len(selected) == expected
        assert len({row[0] for row, _ in selected}) == expected
        assert abs(sum(sign == -1 for _, sign in selected) - sum(sign == 1 for _, sign in selected)) <= 1
        assert all(sign in row[1] for row, sign in selected)


@requires_native
@pytest.mark.parametrize("song,level,eligible,selected", [(82, 26, 97, 16), (431, 28, 51, 43)])
@pytest.mark.parametrize("offset", [45, 55])
def test_real_chart_new_hold_policy_keeps_raw_chords_non_taps_and_native_receipts(tmp_path, song, level, eligible, selected, offset):
    path = ROOT / f"resource/charts/bestdori/{song}/expert.json"
    if not path.exists():
        pytest.skip("本地谱面未同步")
    baseline = native_engine.compile_chart(path).compile_actions({})
    ordinary, total = chart_random_great_inputs(path)
    context = build_random_great_safety_context(path, baseline)
    for seed in range(4):
        shifted, report = plan(baseline, options=ENABLED | {"native_random_great_offset_ms": offset},
            run_id=str(seed), level=level, total_judgements=total, ordinary_single_note_indices=ordinary, safety_context=context)
        assert report["eligible"] == eligible and report["selected"] == selected
        assert abs(report["fast"] - report["slow"]) <= 1
        assert all(a == b for a, b in zip(baseline, shifted) if a["kind"] != "tap")
        if song == 431:
            assert next(row for row in baseline if row["note_index"] == 785) == next(row for row in shifted if row["note_index"] == 785)
        now, lines, receipts, chunks = [0.], [], [], []
        compiler = native_engine.touch_script_compiler()
        def publish(chunk):
            chunks.append(chunk)
            rows = [dict(row["action"], due_s=row["engine_due_s"]) for row in chunk["actions"]]
            reservations = [dict(row["action"], due_s=row["engine_due_s"]) for row in chunk["future_down_reservations"]]
            lines.extend(compiler.compile(rows, chunk["touch_config"], chunk["window_start_s"],
                chunk["final_chunk"], chunk["window_end_s"], reservations))
            receipts.extend(compiler.last_execution_receipts())
            return True
        session = native_engine.playback_session(publish=publish, clock=lambda: now[0])
        assert session.arm(shifted, {}) and session.start(1.)
        for tick in range(3000):
            now[0] = tick / 10
            session.publish()
            if chunks and chunks[-1]["final_chunk"]:
                break
        assert chunks[-1]["final_chunk"] and compiler.phase_boundary_safe()
        assert len(receipts) == len(baseline)
        assert len({row["action_token"] for row in receipts}) == len(baseline)
        active = set()
        for line in lines:
            parts = line.split()
            if parts[0] == "d":
                assert parts[1] not in active
                active.add(parts[1])
            elif parts[0] == "m":
                assert parts[1] in active
            elif parts[0] == "u":
                assert parts[1] in active
                active.remove(parts[1])
        assert not active
