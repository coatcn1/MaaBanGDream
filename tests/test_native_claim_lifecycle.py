import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import pytest

from agent.device_runtime_guard import DeviceSafetyError, NativeRunDeviceClaim, RuntimeLease
from agent.realtime import native_engine, native_play
from agent.realtime.native_prearm import NativePrearmManager


BOOT = '00000000-0000-4000-8000-000000000001'
pytestmark = pytest.mark.skipif(not native_engine.available(), reason='Native 未构建')


class Device:
    max_x, max_y, max_contacts, surface_rotation = 1280, 720, 10, 0
    ready = True
    connected = True
    reset_executed = True
    last_release_error = None

    def __init__(self, order, *, release=True, start_error=False):
        self.order, self.release, self.start_error = order, release, start_error
        self.logs = []

    def start(self, **_kwargs):
        self.order.append('input-start')
        if self.start_error:
            raise OSError('injected device startup failure')

    def publish(self, script):
        now = time.perf_counter()
        for command in script.strip().splitlines():
            value = (now - 100) * 1000
            self.logs.append(('jlog ' + json.dumps({'st': value, 'et': value, 'c': 0, 'cmd': command}), now))
        return True

    def log_records_since(self, cursor):
        return len(self.logs), self.logs[cursor:]

    def request_reset(self):
        self.order.append('reset-request')
        return True

    def stop(self):
        self.order.append('input-up-confirmed' if self.release else 'input-up-unconfirmed')
        return self.release


class Observer:
    def __init__(self, order, **kwargs):
        self.order, self.current = order, kwargs['lease_current']
        self.closed = threading.Event()

    def start(self):
        assert self.current()
        self.order.append('observer-start')

    def request_stop(self):
        self.order.append('observer-stop')
        assert self.current()
        self.closed.set()

    def wait_closed(self, timeout):
        return self.closed.wait(timeout)

    def snapshot(self):
        return None


def backend_fixture(tmp_path, monkeypatch, *, borrowed=False, release=True, start_error=False, observer_error=False):
    chart = tmp_path / ('chart-' + uuid4().hex + '.json')
    chart.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]', encoding='utf-8')
    registry = tmp_path / 'registry'
    runtime = Path(native_play.__file__).resolve().parents[2]
    parent = RuntimeLease(registry, runtime, BOOT, uuid4().hex) if borrowed else None
    if parent is not None:
        parent.acquire()
    order, observers = [], []
    def factory(root, boot_id, run_id, *, borrow=None):
        assert root == runtime and boot_id == BOOT
        claim = NativeRunDeviceClaim(root, boot_id, run_id, borrow=borrow, registry_root=registry)
        original = claim.acquire
        def acquire():
            original()
            order.append('claim-acquired')
        claim.acquire = acquire
        return claim
    def identity(*_args, **kwargs):
        assert not kwargs['cancelled']()
        order.append('double-identity')
        return dict(boot_id=BOOT, fingerprint='test/build')
    def observer_factory(_run_id, **kwargs):
        if observer_error:
            raise OSError('injected optional observer failure')
        observer = Observer(order, **kwargs)
        observers.append(observer)
        return observer
    monkeypatch.setattr(native_play, 'NativeRunDeviceClaim', factory)
    monkeypatch.setattr(native_play, 'probe_native_boot_identity', identity)
    monkeypatch.setattr(native_play, 'NativeClockObserver', observer_factory)
    backend = native_play.NativeMinitouchBackend(chart, adb_path='unused', serial='alias-a',
        device=Device(order, release=release, start_error=start_error), life_feedback_enabled=True,
        observation_clock_trial_enabled=True, borrowed_device_lease=parent.descriptor() if parent else None,
        require_probe=True, publisher_poll_ms=2)
    return backend, parent, order, observers, registry


@pytest.mark.parametrize('borrowed', [False, True])
def test_real_backend_arm_requires_claim_before_input_and_stop_keeps_original_up_proof(tmp_path, monkeypatch, borrowed):
    backend, parent, order, observers, registry = backend_fixture(tmp_path, monkeypatch, borrowed=borrowed)
    backend.arm()
    assert backend.wait_until_ready(1)
    assert order.index('double-identity') < order.index('claim-acquired') < order.index('input-start')
    assert observers and backend._native_device_claim.current()
    backend.stop()
    assert backend.wait_native_cleanup(4)
    report = backend.report()
    assert report['release_confirmed'] and report['release_scope'] == 'injected-device-release'
    assert report['native_device_claim']['settled']
    assert order.index('observer-stop') < order.index('input-up-confirmed')
    if parent:
        assert len(parent.acquired) == 2 and all((path / 'owner.json').exists() for path in parent.paths)
        parent.release()
    assert not list(registry.iterdir())


@pytest.mark.parametrize('fault', ['observer', 'input-up', 'input-start', 'input-start-partial'])
def test_optional_observer_error_cannot_forge_release_or_invalidate_accepted_input_lifecycle(tmp_path, monkeypatch, fault):
    backend, _, order, _, registry = backend_fixture(tmp_path, monkeypatch,
        release=fault not in {'input-up', 'input-start-partial'},
        start_error=fault in {'input-start', 'input-start-partial'}, observer_error=fault == 'observer')
    backend.arm()
    if fault in {'input-start', 'input-start-partial'}:
        with pytest.raises(RuntimeError):
            backend.wait_until_ready(1)
    else:
        assert backend.wait_until_ready(1)
    backend.stop()
    settled = backend.wait_native_cleanup(4)
    if fault in {'input-up', 'input-start-partial'}:
        assert not settled and not backend.report()['release_confirmed']
        assert backend.report()['native_device_claim']['state'] == 'retained'
        assert list(registry.iterdir())
    else:
        assert settled and backend.report()['release_confirmed']
        assert not list(registry.iterdir())
        if fault == 'observer':
            assert backend._device_error is None and backend.report()['state'] != 'failed'


@pytest.mark.parametrize('reason', ['discard', 'ttl', 'replaced'])
def test_prearm_discard_and_ttl_stop_the_same_backend_and_settle_physical_claim(tmp_path, monkeypatch, reason):
    backend, _, order, _, registry = backend_fixture(tmp_path, monkeypatch)
    backend.arm()
    assert backend.wait_until_ready(1)
    manager = NativePrearmManager()
    manager.prepare(backend._run_id, backend._game_clock_chart_path, backend, ttl_s=.025 if reason == 'ttl' else 3)
    if reason == 'discard':
        manager.discard('test-discard')
    elif reason == 'replaced':
        from types import SimpleNamespace
        manager.prepare('replacement', backend._game_clock_chart_path, SimpleNamespace(stop=lambda: None), ttl_s=3)
        manager.discard('test-replacement-finished')
    else:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and 'input-up-confirmed' not in order:
            time.sleep(.005)
    assert backend.wait_native_cleanup(4)
    assert backend.report()['release_confirmed'] and not list(registry.iterdir())


def test_trial_defaults_off_without_claim_or_helper_side_effects(tmp_path, monkeypatch):
    monkeypatch.delenv('MAABANGDREAM_NATIVE_OBSERVATION_CLOCK_TRIAL', raising=False)
    chart = tmp_path / 'chart.json'
    chart.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]', encoding='utf-8')
    monkeypatch.setattr(native_play, 'probe_native_boot_identity', lambda *_args, **_kwargs: pytest.fail('trial disabled'))
    backend = native_play.NativeMinitouchBackend(chart, adb_path='unused', serial='unused', device=Device([]))
    backend.arm()
    assert backend.wait_until_ready(1)
    backend.stop()
    assert backend.wait_native_cleanup(0) and not backend.report()['native_device_claim']['required']


def test_production_default_claim_factory_uses_canonical_registry_for_two_adb_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'appdata'))
    one, _, order_one, _, _ = backend_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(native_play, 'NativeRunDeviceClaim', NativeRunDeviceClaim)
    one.arm()
    assert one.wait_until_ready(1)
    two, _, order_two, _, _ = backend_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(native_play, 'NativeRunDeviceClaim', NativeRunDeviceClaim)
    two._native_serial = 'alias-b-same-physical-boot'
    two.arm()
    with pytest.raises(RuntimeError, match='already leased'):
        two.wait_until_ready(1)
    two.stop()
    assert 'input-start' not in order_two and one._native_device_claim.current()
    one.stop()
    assert one.wait_native_cleanup(4)
    canonical = tmp_path / 'appdata/MaaBanGDream/device-runtime-registry'
    assert not list(canonical.iterdir())


def test_cancelled_prearm_never_starts_late_input_and_leaves_no_new_claim(tmp_path, monkeypatch):
    backend, _, order, _, registry = backend_fixture(tmp_path, monkeypatch)
    entered, resume = threading.Event(), threading.Event()
    def identity(*_args, **kwargs):
        entered.set()
        assert resume.wait(1)
        if kwargs['cancelled']():
            raise RuntimeError('cancelled before claim')
        return dict(boot_id=BOOT, fingerprint='test/build')
    monkeypatch.setattr(native_play, 'probe_native_boot_identity', identity)
    backend.arm()
    assert entered.wait(1)
    with pytest.raises(InterruptedError):
        backend.wait_until_ready(1, should_stop=lambda: True)
    backend._device_start_cancel.set()
    resume.set()
    backend._device_thread.join(1)
    backend.stop()
    assert backend.wait_native_cleanup(4)
    assert 'input-start' not in order and (not registry.exists() or not list(registry.iterdir()))


def test_borrowed_prearm_discard_waits_delayed_readonly_cleanup_before_next_run_slot(tmp_path, monkeypatch):
    backend, parent, order, observers, registry = backend_fixture(tmp_path, monkeypatch, borrowed=True)
    backend.arm()
    assert backend.wait_until_ready(1)
    observer = observers[0]
    def stop():
        order.append('observer-stop')
        def terminal():
            assert 'input-up-confirmed' in order
            order.append('helper-terminal')
            observer.closed.set()
        threading.Timer(.08, terminal).start()
    observer.request_stop = stop
    observer.report = lambda: {'closed': observer.closed.is_set(), 'cleanup_warning': 'read-only-residue'}
    manager = NativePrearmManager()
    manager.prepare(backend._run_id, backend._game_clock_chart_path, backend, ttl_s=3)
    before = time.monotonic()
    manager.discard('replacement')
    assert time.monotonic() - before >= .07
    assert order.index('input-up-confirmed') < order.index('helper-terminal')
    assert backend.report()['release_confirmed'] and backend.wait_native_cleanup(0)
    assert backend.report()['native_clock_observer']['cleanup_warning'] == 'read-only-residue'
    next_run = NativeRunDeviceClaim(parent.runtime_root, BOOT, 'next-run', borrow=parent.descriptor(), registry_root=registry)
    next_run.acquire()
    next_run.stopping()
    assert next_run.finish(input_released=True, observer_terminal=True)
    parent.release()


def test_nonterminal_helper_retains_run_slot_even_when_input_up_is_confirmed(tmp_path, monkeypatch):
    backend, parent, order, observers, registry = backend_fixture(tmp_path, monkeypatch, borrowed=True)
    backend.arm()
    assert backend.wait_until_ready(1)
    observers[0].request_stop = lambda: order.append('observer-stop')
    observers[0].wait_closed = lambda _timeout: False
    backend.stop()
    assert backend.report()['release_confirmed'] and not backend.wait_native_cleanup(4)
    assert backend.report()['native_device_claim']['state'] == 'retained'
    with pytest.raises(DeviceSafetyError):
        parent.release()
