from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.realtime.native_prearm import (
    NativePrearmError,
    NativePrearmManager,
    prepare_native_for_settings_gate,
)


class FakeBackend:
    def __init__(self, *, arm_error: Exception | None = None) -> None:
        self.arm_error = arm_error
        self.arm_calls = 0
        self.wait_calls: list[float] = []
        self.stop_calls = 0

    def arm(self) -> None:
        self.arm_calls += 1
        if self.arm_error is not None:
            raise self.arm_error

    def wait_until_ready(self, timeout_s: float) -> bool:
        self.wait_calls.append(timeout_s)
        return True

    def stop(self) -> None:
        self.stop_calls += 1


class FakeTimer:
    def __init__(self, seconds: float, callback) -> None:
        self.seconds = seconds
        self.callback = callback
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback()


class TimerFactory:
    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(self, seconds: float, callback) -> FakeTimer:
        timer = FakeTimer(seconds, callback)
        self.timers.append(timer)
        return timer


def test_prearm_cache_is_single_use_and_cancels_watchdog(tmp_path):
    timers = TimerFactory()
    manager = NativePrearmManager(timer_factory=timers, ttl_s=30)
    backend = FakeBackend()
    chart = tmp_path / "48" / "expert.json"

    manager.prepare("run-1", chart, backend)

    assert timers.timers[0].started is True
    assert timers.timers[0].daemon is True
    assert manager.consume("run-1", chart) is backend
    assert timers.timers[0].cancelled is True
    assert backend.stop_calls == 0
    with pytest.raises(NativePrearmError, match="不存在"):
        manager.consume("run-1", chart)


def test_prearm_watchdog_stops_only_unconsumed_backend(tmp_path):
    timers = TimerFactory()
    manager = NativePrearmManager(timer_factory=timers, ttl_s=30)
    backend = FakeBackend()
    chart = tmp_path / "48" / "expert.json"

    manager.prepare("run-1", chart, backend)
    timers.timers[0].fire()
    timers.timers[0].fire()

    assert backend.stop_calls == 1
    with pytest.raises(NativePrearmError, match="不存在|过期"):
        manager.consume("run-1", chart)


def test_consume_rejects_expired_entry_even_if_watchdog_is_delayed(tmp_path):
    now = [100.0]
    timers = TimerFactory()
    manager = NativePrearmManager(
        timer_factory=timers,
        ttl_s=30,
        clock=lambda: now[0],
    )
    backend = FakeBackend()
    chart = tmp_path / "48" / "expert.json"
    manager.prepare("run-1", chart, backend)

    now[0] = 130.0
    with pytest.raises(NativePrearmError, match="过期"):
        manager.consume("run-1", chart)

    assert timers.timers[0].cancelled is True
    assert backend.stop_calls == 1


@pytest.mark.parametrize(
    ("consume_run", "consume_name"),
    [
        ("run-2", "expert.json"),
        ("run-1", "special.json"),
    ],
)
def test_prearm_mismatch_fails_closed_and_cleans_cached_backend(
    tmp_path,
    consume_run,
    consume_name,
):
    timers = TimerFactory()
    manager = NativePrearmManager(timer_factory=timers)
    backend = FakeBackend()
    chart = tmp_path / "48" / "expert.json"

    manager.prepare("run-1", chart, backend)

    with pytest.raises(NativePrearmError, match="不匹配"):
        manager.consume(consume_run, chart.with_name(consume_name))
    assert backend.stop_calls == 1
    assert timers.timers[0].cancelled is True


def test_watchdog_start_failure_keeps_previous_cache_and_stops_new_backend(
    tmp_path,
):
    timers = TimerFactory()
    manager = NativePrearmManager(timer_factory=timers)
    old_backend = FakeBackend()
    new_backend = FakeBackend()
    old_chart = tmp_path / "48" / "expert.json"
    new_chart = tmp_path / "49" / "expert.json"
    manager.prepare("run-old", old_chart, old_backend)

    class BrokenTimer(FakeTimer):
        def start(self) -> None:
            raise RuntimeError("simulated watchdog start failure")

    manager._timer_factory = lambda seconds, callback: BrokenTimer(
        seconds,
        callback,
    )
    with pytest.raises(RuntimeError, match="watchdog start failure"):
        manager.prepare("run-new", new_chart, new_backend)

    assert new_backend.stop_calls == 1
    assert old_backend.stop_calls == 0
    assert manager.consume("run-old", old_chart) is old_backend


def _confirmed_run(*, mode: str | None = None) -> SimpleNamespace:
    values = dict(
        run_id="run-48",
        prepared_for_play=True,
        difficulty="Expert",
        song_id="song-48",
        song_level=26,
        song_title="test song",
    )
    if mode is not None:
        values["mode"] = mode
    return SimpleNamespace(**values)


def _repository(chart: Path):
    selection = SimpleNamespace(path=chart, timeline=object())
    return SimpleNamespace(
        resolve=lambda *args, **kwargs: SimpleNamespace(
            selection=selection,
            reason="confirmed",
        )
    )


def test_settings_prearm_disabled_is_a_strict_noop(tmp_path):
    manager = NativePrearmManager(timer_factory=TimerFactory())

    result = prepare_native_for_settings_gate(
        controller=SimpleNamespace(info={}),
        live_run=_confirmed_run(),
        difficulty="Expert",
        project_root=tmp_path,
        runtime_options={"native_realtime_enabled": False},
        backend_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Native 关闭时不得构造后端")
        ),
        manager=manager,
    )

    assert result is None


def test_settings_prearm_failure_stops_backend_and_leaves_no_cache(tmp_path):
    timers = TimerFactory()
    manager = NativePrearmManager(timer_factory=timers)
    chart = tmp_path / "charts" / "48" / "expert.json"
    backend = FakeBackend(arm_error=OSError("simulated arm failure"))

    with pytest.raises(RuntimeError, match="simulated arm failure"):
        prepare_native_for_settings_gate(
            controller=SimpleNamespace(info={
                "adb_path": "C:/tools/adb.exe",
                "adb_serial": "test-device",
            }),
            live_run=_confirmed_run(),
            difficulty="Expert",
            project_root=tmp_path,
            runtime_options={"native_realtime_enabled": True},
            repository=_repository(chart),
            backend_factory=lambda *args, **kwargs: backend,
            manager=manager,
        )

    assert backend.stop_calls == 1
    with pytest.raises(NativePrearmError, match="不存在"):
        manager.consume("run-48", chart)


def test_settings_prearm_success_arms_waits_and_caches_exact_key(tmp_path):
    timers = TimerFactory()
    manager = NativePrearmManager(timer_factory=timers)
    chart = tmp_path / "charts" / "48" / "expert.json"
    backend = FakeBackend()
    constructed: list[dict[str, object]] = []

    def factory(chart_path, **kwargs):
        constructed.append({"chart_path": chart_path, **kwargs})
        return backend

    selection = prepare_native_for_settings_gate(
        controller=SimpleNamespace(info={
            "adb_path": "C:/tools/adb.exe",
            "adb_serial": "test-device",
        }),
        live_run=_confirmed_run(mode="cooperative"),
        difficulty="Expert",
        project_root=tmp_path,
        runtime_options={"native_realtime_enabled": True},
        repository=_repository(chart),
        backend_factory=factory,
        manager=manager,
        ready_timeout_s=7.5,
        ttl_s=25,
    )

    assert selection.path == chart
    assert backend.arm_calls == 1
    assert backend.wait_calls == [7.5]
    assert constructed[0]["adb_path"] == "C:/tools/adb.exe"
    assert constructed[0]["serial"] == "test-device"
    assert constructed[0]["run_id"] == "run-48"
    assert constructed[0]["start_gate_mode"] == "cooperative"
    assert manager.consume("run-48", chart) is backend
    assert timers.timers[0].seconds == 25
