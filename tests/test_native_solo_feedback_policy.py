from types import SimpleNamespace

import pytest

from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime import native_engine
from agent.realtime.live_session import current_live_run, reset_live_run, update_live_run
from agent.realtime.native_play import NativeMinitouchBackend
from agent.realtime.native_prearm import NativePrearmManager, prepare_native_for_settings_gate
from test_native_clock_domains import fixture


def _prepare(tmp_path, mode, enabled, run=None):
    calls = []
    backend = SimpleNamespace(arm=lambda: None, wait_until_ready=lambda _: True)
    manager = NativePrearmManager(timer_factory=lambda *_: SimpleNamespace(start=lambda: None, cancel=lambda: None))
    if run is None:
        run = SimpleNamespace(run_id="solo-policy-test", mode=mode, difficulty="Expert", song_id="song",
                              song_level=26, song_title="test song", prepared_for_play=True)
    selection = SimpleNamespace(path=tmp_path / "chart.json", timeline=None)
    prepare_native_for_settings_gate(
        controller=SimpleNamespace(info={"adb_path": "mock-adb", "adb_serial": "mock-device"}),
        live_run=run, difficulty="Expert", project_root=tmp_path,
        runtime_options={"native_realtime_enabled": True, "native_life_feedback_enabled": enabled},
        repository=SimpleNamespace(resolve=lambda *_, **__: SimpleNamespace(selection=selection, reason="matched")),
        backend_factory=lambda *_, **values: calls.append(values) or backend, manager=manager,
    )
    return calls[0]


def test_enabled_solo_formal_prearm_selects_both_clock_and_direction_window(tmp_path):
    options = _prepare(tmp_path, "formal", True)
    assert options["observation_clock_trial_enabled"] is True
    assert options["visual_refresh_enabled"] is True


@pytest.mark.parametrize("mode", ["formal", "challenge", "cooperative", "versus", "continuous", "medley",
                                  "rehearsal", "calibration-formal", "calibration-rehearsal", "unknown"])
@pytest.mark.parametrize("enabled", [False, True])
def test_other_routes_keep_existing_environment_selection(tmp_path, mode, enabled):
    if mode == "formal" and enabled:
        return
    options = _prepare(tmp_path, mode, enabled)
    assert "observation_clock_trial_enabled" not in options
    assert "visual_refresh_enabled" not in options


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_visual_environment_is_frozen_with_the_prearmed_backend(tmp_path, monkeypatch, initial):
    monkeypatch.setenv("MAABANGDREAM_NATIVE_VISUAL_REFRESH_TRIAL", "1" if initial else "0")
    backend, *_ = fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("MAABANGDREAM_NATIVE_VISUAL_REFRESH_TRIAL", "0" if initial else "1")
    assert (NativeLifeFeedback(backend)._visual_refresh is not None) is initial


@pytest.mark.parametrize("initial", [False, True])
def test_deferred_prearm_retains_frozen_ui_feedback_choice(tmp_path, initial):
    run = reset_live_run(mode="formal", difficulty="Expert")
    run = update_live_run(song_id="song", song_title="test song", song_level=26, prepared_for_play=True)
    first = _prepare(tmp_path, "formal", initial, run)
    rebuilt = _prepare(tmp_path, "formal", not initial, run)
    assert first["life_feedback_enabled"] is initial
    assert rebuilt["life_feedback_enabled"] is initial
    assert rebuilt.get("observation_clock_trial_enabled", False) is initial
    assert rebuilt.get("visual_refresh_enabled", False) is initial
    assert current_live_run().run_id == run.run_id


@pytest.mark.parametrize("clock", [False, True])
@pytest.mark.parametrize("visual", [False, True])
@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_explicit_backend_flags_override_environment_and_report_selection(tmp_path, monkeypatch, clock, visual):
    monkeypatch.setenv("MAABANGDREAM_NATIVE_OBSERVATION_CLOCK_TRIAL", "1" if not clock else "0")
    monkeypatch.setenv("MAABANGDREAM_NATIVE_VISUAL_REFRESH_TRIAL", "1" if not visual else "0")
    chart = tmp_path / "chart.json"
    chart.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]', encoding="utf-8")
    backend = NativeMinitouchBackend(
        chart, adb_path="mock-adb", serial="mock-device", life_feedback_enabled=True,
        device=SimpleNamespace(request_reset=lambda: True), observation_clock_trial_enabled=clock,
        visual_refresh_enabled=visual,
    )
    assert backend._native_clock_trial is clock
    assert backend._native_visual_refresh_enabled is visual
    assert (NativeLifeFeedback(backend)._visual_refresh is not None) is (clock and visual)
    assert backend.report()["life_feedback"]["configuration"] == {
        "observation_clock_enabled": clock, "visual_refresh_requested": visual,
        "visual_refresh_selected": clock and visual,
    }
