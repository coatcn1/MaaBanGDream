import copy
import json
from types import SimpleNamespace

import pytest

from agent.realtime import native_engine
from agent.realtime.chart_repository import ChartResolution, ChartSelection
from agent.realtime.live_session import reset_live_run, update_live_run
from agent.realtime.native_play import NativeMinitouchBackend
from agent.realtime.native_cooperative_jitter import plan_cooperative_jitter
from agent.realtime.native_prearm import (
    NativePrearmManager,
    frozen_native_runtime_options,
    prepare_native_for_settings_gate,
    resolve_confirmed_chart,
)
from agent.realtime.native_random_great import chart_random_great_inputs
from agent.realtime.profile_store import RealtimeProfileStore
from test_native_prearm import TimerFactory


requires_native = pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")


def _chart(tmp_path):
    source = tmp_path / "expert.json"
    chart = [{"type": "BPM", "beat": 0, "bpm": 120}]
    chart += [{"type": "Single", "beat": i, "lane": i % 7} for i in range(100)]
    chart += [
        {"type": "Long", "connections": [
            {"beat": 100, "lane": 2}, {"beat": 102, "lane": 2}]},
        {"type": "Single", "beat": 103, "lane": 0, "flick": True},
        {"type": "Single", "beat": 104, "lane": 1},
        {"type": "Single", "beat": 105, "lane": 2},
    ]
    source.write_text(json.dumps({"schema_version": 1, "chart": chart}), encoding="utf-8")
    return source


def _prepare(tmp_path, monkeypatch, *, mode="cooperative", great=True, jitter=True,
             frozen_jitter=None, run_id=None, backend_overrides=None):
    source = _chart(tmp_path)
    _, total = chart_random_great_inputs(source)
    options = RealtimeProfileStore.DEFAULT_RUNTIME_OPTIONS | {
        "native_realtime_enabled": True,
        "native_random_great_enabled": great,
        "cooperative_jitter_enabled": jitter,
    }
    monkeypatch.setattr(RealtimeProfileStore, "runtime_options", lambda _: dict(options))
    run = reset_live_run(mode=mode, difficulty="Expert")
    run = update_live_run(song_id="test", song_title="test", song_level=26,
                          prepared_for_play=True, **({"run_id": run_id} if run_id else {}))
    if frozen_jitter is not None:
        frozen_native_runtime_options(run, options | {"cooperative_jitter_enabled": frozen_jitter})
    selection = ChartSelection(1, "test", "Expert", source, None, expected_notes=total, level=26)
    repository = SimpleNamespace(resolve=lambda *a, **kw: ChartResolution(selection, "matched"))
    manager = NativePrearmManager(timer_factory=TimerFactory())
    instances = []

    def factory(path, **kwargs):
        backend = NativeMinitouchBackend(
            path, device=SimpleNamespace(max_contacts=10, request_reset=lambda: True), **kwargs)
        backend.arm = lambda: None
        backend.wait_until_ready = lambda *a, **kw: True
        for key, value in (backend_overrides or {}).items():
            setattr(backend, key, value)
        instances.append(backend)
        return backend

    prepared = prepare_native_for_settings_gate(
        controller=SimpleNamespace(info={"adb_path": "unused", "adb_serial": "unused"}),
        live_run=run, difficulty="Expert", project_root=tmp_path, runtime_options=options,
        repository=repository, backend_factory=factory, manager=manager)
    resolved = resolve_confirmed_chart(run, "Expert", repository=repository, project_root=tmp_path)
    backend = manager.consume(run.run_id, resolved.selection.path)
    assert backend is instances[-1]
    return source, total, backend, prepared, instances


@requires_native
@pytest.mark.parametrize("mode", ["cooperative", "versus", "formal"])
@pytest.mark.parametrize("great,jitter", [(True, True), (True, False), (False, True), (False, False)])
def test_prearm_keeps_both_user_options_effective(tmp_path, monkeypatch, mode, great, jitter):
    source, total, backend, prepared, _ = _prepare(
        tmp_path, monkeypatch, mode=mode, great=great, jitter=jitter)
    baseline = native_engine.compile_chart(source).compile_actions({})
    expected_jitter = jitter and mode in {"cooperative", "versus"}
    removed = len(baseline) - len(backend._actions)
    assert (removed in {1, 2}) if expected_jitter else removed == 0
    if great:
        report = backend.report()["native_random_great"]
        assert report["enabled"] and report["status"] == "planned"
        assert report["total_judgements"] == total
        assert report["denominator_source"] == "verified-chart-visible+catalog"
        assert prepared.path == source
    if great and expected_jitter:
        report = backend.report()["cooperative_jitter"]
        assert report["enabled"] and report["status"] == "planned"
        assert report["selected"] == removed
        dropped = set(report["dropped_note_indices"])
        assert dropped.isdisjoint(row["note_index"] for row in backend._actions)
        assert dropped.isdisjoint(row["note_index"] for row in backend.report()["native_random_great"]["selected_notes"])
        assert backend._actions[0] == baseline[0]
        assert [row for row in backend._actions if row["kind"] in {"down", "move", "up"}] == [
            row for row in baseline if row["kind"] in {"down", "move", "up"}]
    else:
        assert "cooperative_jitter" not in backend.report()


@requires_native
@pytest.mark.parametrize("mode", ["cooperative", "versus"])
def test_legacy_jitter_variant_does_not_report_an_unmeasured_disabled_plan(tmp_path, monkeypatch, mode):
    source, _, backend, prepared, _ = _prepare(
        tmp_path, monkeypatch, mode=mode, great=False, jitter=True)
    baseline = native_engine.compile_chart(source).compile_actions({})
    assert prepared.path != source and "jittered-charts" in str(prepared.path)
    assert len(baseline) - len(backend._actions) in {1, 2}
    assert "cooperative_jitter" not in backend.report()


@requires_native
@pytest.mark.parametrize("frozen,current", [(True, False), (False, True)])
def test_cooperative_jitter_setting_freezes_before_prearm(tmp_path, monkeypatch, frozen, current):
    source, _, backend, _, _ = _prepare(
        tmp_path, monkeypatch, jitter=current, frozen_jitter=frozen)
    baseline = native_engine.compile_chart(source).compile_actions({})
    assert (len(backend._actions) < len(baseline)) is frozen


@requires_native
def test_same_run_has_one_frozen_tail_plan_and_keeps_canonical_file(tmp_path, monkeypatch):
    counts = set()
    for index in range(16):
        case = tmp_path / str(index)
        case.mkdir()
        source, total, combined, _, _ = _prepare(case, monkeypatch, run_id=f"run-{index}")
        original = source.read_bytes()
        baseline = NativeMinitouchBackend(
            source, adb_path="unused", serial="unused", device=SimpleNamespace(request_reset=lambda: True),
            run_id=f"run-{index}", start_gate_mode="cooperative", chart_level=26, expected_notes=total,
            random_great_options=RealtimeProfileStore.DEFAULT_RUNTIME_OPTIONS | {"native_random_great_enabled": True})
        report = combined.report()["cooperative_jitter"]
        counts.add(report["selected"])
        dropped = set(report["dropped_note_indices"])
        assert combined._actions == [row for row in baseline._actions if row["note_index"] not in dropped]
        assert combined.report()["native_random_great"] == baseline.report()["native_random_great"]
        planned, repeated = plan_cooperative_jitter(
            baseline._actions, chart_path=source, run_id=f"run-{index}", enabled=True,
            mode="cooperative", protected_note_indices=[
                row["note_index"] for row in baseline.report()["native_random_great"]["selected_notes"]])
        assert planned == combined._actions and repeated == report
        assert source.read_bytes() == original
    assert counts == {1, 2}


@requires_native
def test_first_chord_and_invisible_path_numbering_are_preserved(tmp_path):
    source = tmp_path / "first-chord.json"
    source.write_text(json.dumps([
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Long", "connections": [{"beat": -1, "lane": 3, "hidden": True}]},
        {"type": "Single", "beat": 0, "lane": 0},
        {"type": "Single", "beat": 0, "lane": 1},
        {"type": "Slide", "connections": [{"beat": 1, "lane": 6},
            {"beat": 2, "lane": 5, "hidden": True}, {"beat": 3, "lane": 4}]},
        {"type": "Single", "beat": 4, "lane": 0, "flick": True},
        {"type": "Single", "beat": 5, "lane": 1},
    ]), encoding="utf-8")
    actions = native_engine.compile_chart(source).compile_actions({})
    for index in range(8):
        result, report = plan_cooperative_jitter(
            actions, chart_path=source, run_id=f"first-{index}", enabled=True, mode="cooperative")
        dropped = set(report["dropped_note_indices"])
        assert dropped <= {3, 4} and len(dropped) in {1, 2}
        assert [row for row in result if row["due_s"] == 0] == [row for row in actions if row["due_s"] == 0]
        assert [row for row in result if row["note_index"] == 2] == [row for row in actions if row["note_index"] == 2]


@requires_native
def test_great_selected_notes_and_hold_actions_cannot_be_removed(tmp_path):
    source = _chart(tmp_path)
    actions = native_engine.compile_chart(source).compile_actions({})
    protected = {102, 103}
    result, report = plan_cooperative_jitter(actions, chart_path=source, run_id="protected",
        enabled=True, mode="cooperative", protected_note_indices=protected)
    dropped = set(report["dropped_note_indices"])
    assert not dropped & protected
    assert [row for row in result if row["note_index"] in protected | {100}] == [
        row for row in actions if row["note_index"] in protected | {100}]
    assert result == [row for row in actions if row["note_index"] not in dropped]


@requires_native
@pytest.mark.parametrize("fault", ["planner", "stdout"])
def test_optional_jitter_failure_keeps_great_and_business_input(tmp_path, monkeypatch, fault):
    if fault == "planner":
        def fail(*a, **kw):
            raise OSError("jitter fault")
        monkeypatch.setattr("agent.realtime.native_cooperative_jitter.plan_cooperative_jitter", fail)
    else:
        import builtins
        original_print = builtins.print
        def fail_optional_log(*args, **kwargs):
            if args and str(args[0]).startswith(("NativeCooperativeJitter", "NativeRandomGreat")):
                raise OSError("stdout fault")
            return original_print(*args, **kwargs)
        monkeypatch.setattr("builtins.print", fail_optional_log)
    source, _, backend, _, _ = _prepare(tmp_path, monkeypatch)
    great = backend.report()["native_random_great"]
    assert great["enabled"] and great["status"] == "planned"
    jitter = backend.report()["cooperative_jitter"]
    assert jitter["status"] == ("fallback" if fault == "planner" else "planned")
    baseline = native_engine.compile_chart(source).compile_actions({})
    assert len(baseline) - len(backend._actions) == (0 if fault == "planner" else jitter["selected"])


@requires_native
def test_real_session_and_compiler_never_restore_dropped_actions(tmp_path, monkeypatch):
    source, _, backend, _, _ = _prepare(tmp_path, monkeypatch, run_id="native-session")
    actions = copy.deepcopy(backend._actions)
    dropped = set(backend.report()["cooperative_jitter"]["dropped_note_indices"])
    now, chunks, receipts, scripts = [0.], [], [], []
    compiler = native_engine.touch_script_compiler()

    def publish(chunk):
        chunks.append(chunk)
        rows = [dict(row["action"], due_s=row["engine_due_s"]) for row in chunk["actions"]]
        reservations = [dict(row["action"], due_s=row["engine_due_s"]) for row in chunk["future_down_reservations"]]
        scripts.extend(compiler.compile(rows, chunk["touch_config"], chunk["window_start_s"],
            chunk["final_chunk"], chunk["window_end_s"], reservations))
        receipts.extend(compiler.last_execution_receipts())
        return True

    session = native_engine.playback_session(publish=publish, clock=lambda: now[0])
    assert session.arm(actions, {}) and session.start(1.)
    for tick in range(600):
        now[0] = tick / 10
        session.publish()
        if chunks and chunks[-1]["final_chunk"]:
            break
    published = [row["action"] for chunk in chunks for row in chunk["actions"]]
    assert chunks[-1]["final_chunk"] and published == actions
    assert dropped.isdisjoint(row["note_index"] for row in published)
    assert len(receipts) == len(actions) and len({row["action_token"] for row in receipts}) == len(actions)
    assert compiler.phase_boundary_safe()
    active = set()
    for line in scripts:
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


@requires_native
def test_great_count_failure_does_not_silence_explicit_cooperative_jitter(tmp_path):
    source = _chart(tmp_path)
    baseline = native_engine.compile_chart(source).compile_actions({})
    backend = NativeMinitouchBackend(source, adb_path="unused", serial="unused",
        device=SimpleNamespace(request_reset=lambda: True), start_gate_mode="cooperative",
        random_great_options={"native_random_great_enabled": True}, expected_notes=9999,
        cooperative_jitter_enabled=True, run_id="count-fallback")
    assert backend.report()["native_random_great"]["status"] == "fallback"
    jitter = backend.report()["cooperative_jitter"]
    assert jitter["selected"] in {1, 2}
    assert backend._actions == [row for row in baseline if row["note_index"] not in jitter["dropped_note_indices"]]
