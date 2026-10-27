import threading
from types import SimpleNamespace

import pytest

from agent.realtime.native_play import NativeMinitouchBackend
from test_native_claim_lifecycle import backend_fixture


def bare_backend():
    backend = NativeMinitouchBackend.__new__(NativeMinitouchBackend)
    backend._native_clock_trial = True
    backend._native_input_stop_requested = threading.Event()
    backend._native_stop_lifecycle_lock = threading.Lock()
    backend._native_stop_started = False
    backend._native_observer_cleanup_allowed = threading.Event()
    backend._native_ownership_finisher = None
    backend._native_ownership_cleanup_done = threading.Event()
    backend._borrowed_device_lease = object()
    backend._release_confirmed = False
    return backend


def test_stop_is_once_and_opens_cleanup_only_after_input_attempt():
    backend = bare_backend()
    order = []
    def input_stop():
        assert not backend._native_observer_cleanup_allowed.is_set()
        order.append('input-return')
    backend._stop_input = input_stop
    backend._stop_native_clock_observer = lambda: order.append('request-stop')
    def finish():
        assert backend._native_observer_cleanup_allowed.is_set()
        order.append('finish')
    backend._finish_native_ownership = finish
    backend.stop()
    backend._native_ownership_finisher.join(1)
    backend.stop()
    assert order == ['input-return', 'request-stop', 'finish']
    assert backend._release_confirmed is False


def test_input_and_observer_stop_exception_still_starts_finisher():
    backend = bare_backend()
    finished = threading.Event()
    def input_stop():
        raise ValueError('input failure')
    def observer_stop():
        raise OSError('optional observer failure')
    backend._stop_input = input_stop
    backend._stop_native_clock_observer = observer_stop
    backend._finish_native_ownership = finished.set
    with pytest.raises(ValueError, match='input failure'):
        backend.stop()
    assert backend._native_observer_cleanup_allowed.is_set()
    assert finished.wait(1)
    assert backend._release_confirmed is False


def test_start_failure_cannot_open_gate_after_stop_wins_the_decision(tmp_path, monkeypatch):
    backend, _, _, _, _ = backend_fixture(tmp_path, monkeypatch, start_error=True)
    checked, resume_start = threading.Event(), threading.Event()
    input_entered, resume_input = threading.Event(), threading.Event()
    original = backend._native_input_stop_requested
    real_stop = backend._stop_input
    class PausedEvent:
        def is_set(self):
            answer = original.is_set()
            checked.set()
            assert resume_start.wait(3)
            return answer
        def set(self):
            original.set()
    def input_stop():
        input_entered.set()
        assert resume_input.wait(3)
        real_stop()
    backend._native_input_stop_requested = PausedEvent()
    monkeypatch.setattr(backend, '_stop_input', input_stop)
    backend.arm()
    assert checked.wait(1)
    stopper = threading.Thread(target=backend.stop)
    stopper.start()
    try:
        # 允许无输入终态先完成；若stop已接管，启动线程不得在释放返回前开gate。
        stop_won = input_entered.wait(1)
        resume_start.set()
        backend._device_thread.join(1)
        assert not backend._device_thread.is_alive()
        assert not (stop_won and backend._native_observer_cleanup_allowed.is_set())
    finally:
        resume_start.set()
        resume_input.set()
        stopper.join(3)
    assert not stopper.is_alive()


def test_start_failure_cannot_open_gate_while_stop_has_taken_priority(tmp_path, monkeypatch):
    backend, _, _, _, _ = backend_fixture(tmp_path, monkeypatch, start_error=True)
    startup_cleanup, resume_startup = threading.Event(), threading.Event()
    input_entered, resume_input = threading.Event(), threading.Event()
    real_cleanup = backend._stop_device_with_budget
    real_stop = backend._stop_input
    def cleanup(budget):
        if threading.current_thread() is backend._device_thread:
            startup_cleanup.set()
            assert resume_startup.wait(2)
        return real_cleanup(budget)
    def input_stop():
        input_entered.set()
        assert resume_input.wait(2)
        real_stop()
    monkeypatch.setattr(backend, '_stop_device_with_budget', cleanup)
    monkeypatch.setattr(backend, '_stop_input', input_stop)
    backend.arm()
    assert startup_cleanup.wait(1)
    stopper = threading.Thread(target=backend.stop)
    stopper.start()
    try:
        assert input_entered.wait(1)
        resume_startup.set()
        backend._device_thread.join(1)
        assert not backend._device_thread.is_alive()
        assert not backend._native_observer_cleanup_allowed.is_set()
    finally:
        resume_startup.set()
        resume_input.set()
        stopper.join(3)
    assert not stopper.is_alive()
    assert backend._native_observer_cleanup_allowed.is_set()


def test_real_backend_input_cleanup_finishes_before_observer_cleanup(tmp_path, monkeypatch):
    backend, _, order, observers, _ = backend_fixture(tmp_path, monkeypatch)
    backend.arm()
    assert backend.wait_until_ready(1)
    observer = observers[0]
    worker = []
    def request_stop():
        if worker:
            return
        order.append('observer-stop')
        def cleanup():
            assert backend._native_observer_cleanup_allowed.wait(2)
            order.append('observer-cleanup')
            observer.closed.set()
        worker.append(threading.Thread(target=cleanup))
        worker[0].start()
    observer.request_stop = request_stop
    backend.stop()
    worker[0].join(1)
    assert backend.wait_native_cleanup(1)
    assert order.index('observer-stop') < order.index('input-up-confirmed') < order.index('observer-cleanup')
    assert backend.report()['release_confirmed'] is True
    backend.stop()
    assert order.count('observer-cleanup') == 1


def test_real_backend_diagnostics_exception_opens_gate_but_retains_claim(tmp_path, monkeypatch):
    backend, _, _, _, registry = backend_fixture(tmp_path, monkeypatch)
    backend.arm()
    assert backend.wait_until_ready(1)
    def diagnostics():
        raise ValueError('diagnostics failure')
    backend._device.release_diagnostics = diagnostics
    with pytest.raises(ValueError, match='diagnostics failure'):
        backend.stop()
    assert backend._native_observer_cleanup_allowed.is_set()
    assert not backend.wait_native_cleanup(1)
    assert backend._native_device_claim.report()['state'] == 'retained'
    assert list(registry.iterdir())
    previous = backend._release_confirmed
    backend.stop()
    assert backend._release_confirmed is previous


def test_finisher_thread_start_failure_keeps_gate_open_and_release_failure(monkeypatch):
    backend = bare_backend()
    backend._stop_input = lambda: None
    backend._stop_native_clock_observer = lambda: None
    backend._finish_native_ownership = lambda: pytest.fail('must not run')
    def fail_start(_thread):
        raise OSError('injected thread start failure')
    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    backend.stop()
    backend.stop()
    assert backend._native_observer_cleanup_allowed.is_set()
    assert backend._release_confirmed is False
    assert not backend._native_ownership_cleanup_done.is_set()
    assert backend._native_ownership_warning == 'claim-finisher-start:OSError'
