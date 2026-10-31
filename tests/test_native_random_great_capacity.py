from types import SimpleNamespace

import pytest

from agent.realtime.live_session import reset_live_run, update_live_run
from agent.realtime.native_prearm import NativePrearmError, NativePrearmManager, prepare_native_for_settings_gate
from agent.realtime.profile_store import RealtimeProfileStore
from test_native_prearm import FakeBackend, TimerFactory, _repository


class CapacityBackend(FakeBackend):
    def __init__(self, *, capacity=10, needs=True, settled=True, proof=True, sequence=True,
                 release_error=None, ready=True, arm_error=False):
        super().__init__()
        self.capacity, self.needs, self.settled = capacity, needs, settled
        self.proof, self.sequence, self.release_error = proof, sequence, release_error
        self.ready, self.arm_error = ready, arm_error
        self.settle_calls = []

    def arm(self):
        self.arm_calls += 1
        if self.arm_error:
            raise RuntimeError("arm fault")

    def wait_until_ready(self, timeout_s, **kwargs):
        self.wait_calls.append(timeout_s)
        return self.ready

    def random_great_requires_contact_capacity(self):
        return self.needs

    def random_great_contact_capacity(self):
        return self.capacity

    def wait_native_cleanup(self, timeout_s):
        self.settle_calls.append(timeout_s)
        return self.settled

    def report(self):
        return dict(release_confirmed=self.proof, release_sequence_confirmed=self.sequence,
                    release_error=self.release_error)


def setup_prearm(tmp_path, monkeypatch, first, second=None, should_stop=None, mode="cooperative"):
    calls = []
    options = RealtimeProfileStore.DEFAULT_RUNTIME_OPTIONS | {"native_realtime_enabled": True,
        "native_random_great_enabled": True, "native_life_feedback_enabled": True}
    monkeypatch.setattr(RealtimeProfileStore, "runtime_options", lambda _: dict(options))
    monkeypatch.setattr("agent.realtime.native_prearm._cooperative_jittered_chart",
                        lambda *a: pytest.fail("容量回退不可叠加漏键"))
    run = reset_live_run(mode=mode, difficulty="Expert")
    run = update_live_run(song_id="test", song_title="test", song_level=26, prepared_for_play=True)
    chart = tmp_path / "expert.json"
    manager = NativePrearmManager(timer_factory=TimerFactory())
    def factory(path, **kwargs):
        calls.append((path, kwargs))
        assert len(calls) <= 2
        return first if len(calls) == 1 else second
    def prepare():
        return prepare_native_for_settings_gate(controller=SimpleNamespace(info={"adb_path": "unused", "adb_serial": "unused"}),
            live_run=run, difficulty="Expert", project_root=tmp_path, runtime_options=options,
            repository=_repository(chart), backend_factory=factory, manager=manager,
            should_stop=should_stop, ready_timeout_s=10)
    return prepare, calls, manager, run, chart


@pytest.mark.parametrize("capacity,needs", [(10, True), (None, False), (5, False)])
def test_verified_capacity_or_no_selected_hold_keeps_single_backend(tmp_path, monkeypatch, capacity, needs):
    first = CapacityBackend(capacity=capacity, needs=needs)
    prepare, calls, manager, run, chart = setup_prearm(tmp_path, monkeypatch, first)
    assert prepare() is not None
    assert len(calls) == 1 and first.stop_calls == 0 and first.arm_calls == 1
    assert manager.consume(run.run_id, chart) is first


@pytest.mark.parametrize("capacity", [None, 0, 5, 9])
def test_unverified_capacity_releases_old_before_one_fresh_baseline_backend(tmp_path, monkeypatch, capacity):
    first, second = CapacityBackend(capacity=capacity), CapacityBackend(needs=False)
    prepare, calls, manager, run, chart = setup_prearm(tmp_path, monkeypatch, first, second)
    assert prepare() is not None
    assert first.stop_calls == 1 and first.settle_calls == [4.2] and first.arm_calls == 1
    assert second.stop_calls == 0 and second.arm_calls == 1
    assert manager.consume(run.run_id, chart) is second
    assert calls[0][0] == calls[1][0] == chart
    assert calls[0][1]["random_great_options"]["native_random_great_enabled"] is True
    assert calls[1][1]["random_great_options"]["native_random_great_enabled"] is False
    assert calls[1][1]["life_feedback_enabled"] is True
    assert calls[1][1]["random_great_fallback_reason"] == "device-contact-capacity-unverified"
    assert 0 < second.wait_calls[0] <= 10


@pytest.mark.parametrize("failure", [dict(settled=False), dict(proof=False), dict(sequence=False), dict(release_error="missing jlog")])
def test_uncertain_previous_release_never_constructs_replacement(tmp_path, monkeypatch, failure):
    first = CapacityBackend(capacity=5, **failure)
    prepare, calls, manager, run, chart = setup_prearm(tmp_path, monkeypatch, first)
    with pytest.raises(RuntimeError, match="无法确认本轮触点完整释放"):
        prepare()
    assert len(calls) == 1 and first.stop_calls == 1
    with pytest.raises(NativePrearmError):
        manager.consume(run.run_id, chart)


def test_stop_during_capacity_cleanup_prevents_replacement_and_keeps_neutral_stop(tmp_path, monkeypatch):
    first = CapacityBackend(capacity=5)
    prepare, calls, _, _, _ = setup_prearm(tmp_path, monkeypatch, first, should_stop=lambda: first.stop_calls > 0)
    with pytest.raises(InterruptedError):
        prepare()
    assert len(calls) == 1 and first.stop_calls == 1


@pytest.mark.parametrize("arm_error", [True, False])
def test_replacement_failure_cleans_new_backend_once_without_loop_or_cache(tmp_path, monkeypatch, arm_error):
    first, second = CapacityBackend(capacity=5), CapacityBackend(arm_error=arm_error, ready=False)
    prepare, calls, manager, run, chart = setup_prearm(tmp_path, monkeypatch, first, second)
    with pytest.raises(RuntimeError):
        prepare()
    assert len(calls) == 2 and first.stop_calls == second.stop_calls == 1
    assert first.arm_calls == second.arm_calls == 1
    with pytest.raises(NativePrearmError):
        manager.consume(run.run_id, chart)


def test_cleanup_consumes_total_ready_budget_without_retrying(tmp_path, monkeypatch):
    now = [0.]
    monkeypatch.setattr("agent.realtime.native_prearm.time.monotonic", lambda: now[0])
    first = CapacityBackend(capacity=5)
    def settle(_):
        now[0] = 11.
        return True
    first.wait_native_cleanup = settle
    prepare, calls, _, _, _ = setup_prearm(tmp_path, monkeypatch, first)
    with pytest.raises(RuntimeError, match="耗尽预武装总等待预算"):
        prepare()
    assert first.stop_calls == 1 and len(calls) == 1


def test_capacity_replacement_restores_both_formal_feedback_dependencies(tmp_path, monkeypatch):
    first, second = CapacityBackend(capacity=5), CapacityBackend(needs=False)
    prepare, calls, _, _, _ = setup_prearm(tmp_path, monkeypatch, first, second, mode="formal")
    assert prepare() is not None
    for _, options in calls:
        assert options["life_feedback_enabled"]
        assert options["observation_clock_trial_enabled"] and options["visual_refresh_enabled"]


def test_real_constructed_fallback_retains_original_feedback_and_canonical_chart(tmp_path, monkeypatch):
    from pathlib import Path
    from agent.realtime import native_engine
    from agent.realtime.native_play import NativeMinitouchBackend
    if not native_engine.available():
        pytest.skip("Native 未构建")
    path = Path(__file__).resolve().parents[1] / "resource/charts/bestdori/431/expert.json"
    if not path.exists():
        pytest.skip("本地谱面未同步")
    run = reset_live_run(mode="formal", difficulty="Expert")
    run = update_live_run(song_id="test", song_title="test", song_level=28, prepared_for_play=True)
    options = RealtimeProfileStore.DEFAULT_RUNTIME_OPTIONS | {"native_realtime_enabled": True,
        "native_random_great_enabled": True, "native_life_feedback_enabled": True}
    instances, stops = [], []
    def factory(chart, **kwargs):
        backend = NativeMinitouchBackend(chart, device=SimpleNamespace(max_contacts=5, request_reset=lambda: True), **kwargs)
        original_report = backend.report
        backend.arm = lambda: None
        backend.wait_until_ready = lambda *_args, **_kwargs: True
        backend.stop = lambda: stops.append(backend)
        backend.wait_native_cleanup = lambda _: True
        backend.report = lambda: original_report() | dict(release_confirmed=True, release_sequence_confirmed=True, release_error=None)
        instances.append(backend)
        return backend
    selection = SimpleNamespace(path=path, level=28, expected_notes=1074)
    manager = NativePrearmManager(timer_factory=TimerFactory())
    assert prepare_native_for_settings_gate(controller=SimpleNamespace(info={"adb_path": "unused", "adb_serial": "unused"}),
        live_run=run, difficulty="Expert", project_root=tmp_path, runtime_options=options,
        repository=SimpleNamespace(resolve=lambda *a, **kw: SimpleNamespace(selection=selection, reason="matched")),
        backend_factory=factory, manager=manager) is selection
    assert len(instances) == 2 and stops == [instances[0]]
    old, fallback = instances
    assert old._random_great_report["selected_with_active_hold"] > 0 and not old.life_feedback_enabled
    assert fallback.life_feedback_enabled and fallback._native_clock_trial and fallback._native_visual_refresh_enabled
    assert fallback._actions == native_engine.compile_chart(path).compile_actions({})
    assert fallback._random_great_report["reason"] == "device-contact-capacity-unverified"
    assert manager.consume(run.run_id, path) is fallback
