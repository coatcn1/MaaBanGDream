from pathlib import Path
from uuid import uuid4

import pytest

from agent.device_runtime_guard import (DeviceSafetyError, Listener, RuntimeLease,
    validate_device, verify_endpoint_owner)

BOOT = '00000000-0000-4000-8000-000000000001'
OTHER_BOOT = '00000000-0000-4000-8000-000000000002'


def test_mumu_exact_listener_shadows_ld_wildcard():
    listeners = [Listener('0.0.0.0', 7555, 10), Listener('127.0.0.1', 7555, 20)]
    with pytest.raises(DeviceSafetyError):
        verify_endpoint_owner('127.0.0.1:7555', 10, listeners)
    verify_endpoint_owner('127.0.0.2:7555', 10, listeners)
    verify_endpoint_owner('127.0.0.1:7555', 20, listeners)


@pytest.mark.parametrize('listeners', [[], [Listener('0.0.0.0', 7555, 0)],
    [Listener('127.0.0.2', 7555, 10), Listener('127.0.0.2', 7555, 10)]])
def test_unknown_or_ambiguous_owner_is_rejected(listeners):
    with pytest.raises(DeviceSafetyError):
        verify_endpoint_owner('127.0.0.2:7555', 10, listeners)


def test_exact_listener_wins_even_when_wildcard_owner_differs():
    verify_endpoint_owner('127.0.0.1:16384', 20,
                          [Listener('0.0.0.0', 16384, 10), Listener('127.0.0.1', 16384, 20)])


@pytest.mark.parametrize('endpoint', ['emulator-7554', '192.168.1.2:7555', 'localhost:7555', '127.0.0.1:0'])
def test_nonliteral_or_nonloopback_endpoint_rejected(endpoint):
    with pytest.raises(DeviceSafetyError):
        verify_endpoint_owner(endpoint, 10, [])


def test_same_android_boot_cannot_be_leased_under_two_runtime_aliases(tmp_path):
    registry = tmp_path / 'registry'
    one = RuntimeLease(registry, tmp_path / 'a', BOOT, uuid4().hex)
    two = RuntimeLease(registry, tmp_path / 'b', BOOT, uuid4().hex)
    one.acquire()
    with pytest.raises(DeviceSafetyError):
        two.acquire()
    assert len(list(registry.iterdir())) == 2  # 占用失败时只能清理本运行目录自己的登记。
    one.release()
    two.acquire()
    two.release()


def test_duplicate_runtime_rejected_even_for_different_devices(tmp_path):
    one = RuntimeLease(tmp_path / 'registry', tmp_path / 'runtime', BOOT, 'a')
    two = RuntimeLease(tmp_path / 'registry', tmp_path / 'runtime', OTHER_BOOT, 'b')
    one.acquire()
    with pytest.raises(DeviceSafetyError):
        two.acquire()
    one.release()


def test_distinct_runtime_and_devices_can_run_together(tmp_path):
    one = RuntimeLease(tmp_path / 'registry', tmp_path / 'a', BOOT, 'a')
    two = RuntimeLease(tmp_path / 'registry', tmp_path / 'b', OTHER_BOOT, 'b')
    one.acquire()
    two.acquire()
    assert len(list((tmp_path / 'registry').iterdir())) == 4
    one.release()
    two.release()


def device_probes(boot=BOOT, fingerprint='test/build', owners=None):
    owners = iter(owners or [{'pid': 10, 'created_at': 10.0}] * 2)
    def shell(_adb, _endpoint, command):
        return boot if command[0] == 'cat' else fingerprint if command[0] == 'getprop' else 'android-id'
    return dict(listeners=lambda: [Listener('0.0.0.0', 7555, 10)],
                process_probe=lambda *_: next(owners), shell=shell)


def test_device_identity_checks_boot_fingerprint_and_process_create_time():
    identity = validate_device(Path('adb'), '127.0.0.2:7555', 10, BOOT, **device_probes())
    assert identity['fingerprint'] == 'test/build'
    with pytest.raises(DeviceSafetyError, match='boot identity'):
        validate_device(Path('adb'), '127.0.0.2:7555', 10, BOOT, **device_probes(boot=OTHER_BOOT))
    with pytest.raises(DeviceSafetyError, match='identity changed'):
        validate_device(Path('adb'), '127.0.0.2:7555', 10, BOOT, previous=identity,
                        **device_probes(fingerprint='other/build'))
    with pytest.raises(DeviceSafetyError, match='process identity changed'):
        validate_device(Path('adb'), '127.0.0.2:7555', 10, BOOT,
                        **device_probes(owners=[{'pid': 10, 'created_at': 10.0},
                                               {'pid': 10, 'created_at': 11.0}]))
