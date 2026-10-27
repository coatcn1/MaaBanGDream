import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent.device_runtime_guard import (
    DeviceSafetyError, NativeRunDeviceClaim, RuntimeLease, probe_native_boot_identity,
    require_canonical_registry, shared_device_registry_root, write_json,
)


BOOT = '00000000-0000-4000-8000-000000000001'


def test_canonical_registry_shared_between_runtime_aliases_without_legacy_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'appdata'))
    root = shared_device_registry_root()
    assert root == (tmp_path / 'appdata/MaaBanGDream/device-runtime-registry').resolve()
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    marker = legacy / 'stale-owner.json'
    marker.write_text('preserved', encoding='utf-8')
    one = NativeRunDeviceClaim(tmp_path / 'mfa-a', BOOT, 'one')
    one.acquire()
    two = NativeRunDeviceClaim(tmp_path / 'mfa-b', BOOT, 'two')
    with pytest.raises((DeviceSafetyError, FileExistsError)):
        two.acquire()
    assert marker.read_text() == 'preserved'
    one.stopping()
    assert one.current()
    assert one.finish(input_released=True, observer_terminal=True)
    two.acquire()
    two.stopping()
    assert two.finish(input_released=True, observer_terminal=True)
    assert not list(root.iterdir())
    with pytest.raises(DeviceSafetyError, match='canonical'):
        require_canonical_registry(legacy)


def test_explicit_borrow_is_bound_to_parent_and_single_run_slot(tmp_path):
    registry, runtime = tmp_path / 'registry', tmp_path / 'runtime'
    parent = RuntimeLease(registry, runtime, BOOT, uuid4().hex)
    parent.acquire()
    one = NativeRunDeviceClaim(runtime, BOOT, 'one', borrow=parent.descriptor(), registry_root=registry)
    one.acquire()
    two = NativeRunDeviceClaim(runtime, BOOT, 'two', borrow=parent.descriptor(), registry_root=registry)
    with pytest.raises((FileExistsError, DeviceSafetyError)):
        two.acquire()
    with pytest.raises(DeviceSafetyError, match='still holds'):
        parent.release()
    assert all((path / 'owner.json').exists() for path in parent.paths)
    one.stopping()
    assert one.current() and one.finish(input_released=True, observer_terminal=True)
    assert len(parent.acquired) == 2 and all(path.exists() for path in parent.paths)
    two.acquire()
    two.stopping()
    assert two.finish(input_released=True, observer_terminal=True)
    parent.release()
    assert not list(registry.iterdir())


@pytest.mark.parametrize('changed', ['nonce', 'pid', 'created_at', 'runtime_root', 'boot_id', 'registry_root'])
def test_borrow_cannot_infer_permission_from_visible_or_forged_owner(tmp_path, changed):
    registry, runtime = tmp_path / 'registry', tmp_path / 'runtime'
    parent = RuntimeLease(registry, runtime, BOOT, uuid4().hex)
    parent.acquire()
    descriptor = copy.deepcopy(parent.descriptor())
    if changed == 'registry_root':
        descriptor[changed] = str(tmp_path / 'other-registry')
    else:
        descriptor['owner'][changed] = {'nonce': 'foreign', 'pid': 2**30,
            'created_at': descriptor['owner']['created_at'] - 1,
            'runtime_root': str(tmp_path / 'foreign'), 'boot_id': str(uuid4())}[changed]
    claim = NativeRunDeviceClaim(runtime, BOOT, 'test', borrow=descriptor, registry_root=registry)
    with pytest.raises(DeviceSafetyError):
        claim.acquire()
    assert all((path / 'owner.json').exists() for path in parent.paths)
    parent.release()


@pytest.mark.parametrize('input_released,terminal', [(False, True), (True, False), (False, False)])
def test_claim_retains_uncertain_input_or_live_worker_and_never_auto_recovers(tmp_path, input_released, terminal):
    claim = NativeRunDeviceClaim(tmp_path / 'runtime', BOOT, 'test', registry_root=tmp_path / 'registry')
    claim.acquire()
    claim.stopping()
    assert not claim.finish(input_released=input_released, observer_terminal=terminal)
    assert claim.report()['state'] == 'retained'
    assert (claim._slot / 'owner.json').exists()
    other = NativeRunDeviceClaim(tmp_path / 'other-runtime', BOOT, 'other', registry_root=tmp_path / 'registry')
    with pytest.raises(DeviceSafetyError):
        other.acquire()


def test_boot_probe_double_reads_and_cancel_prevents_side_effects():
    calls = []
    def command(parts):
        calls.append(parts)
        return BOOT if parts[0] == 'cat' else 'build/fingerprint'
    assert probe_native_boot_identity('unused', 'alias-a', command=command) == {
        'boot_id': BOOT, 'fingerprint': 'build/fingerprint'}
    assert len(calls) == 4
    calls.clear()
    with pytest.raises(DeviceSafetyError, match='cancelled'):
        probe_native_boot_identity('unused', 'alias-a', command=command, cancelled=lambda: True)
    assert not calls
    answers = iter([BOOT, 'build/a', BOOT, 'build/b'])
    with pytest.raises(DeviceSafetyError, match='changed'):
        probe_native_boot_identity('unused', 'alias-a', command=lambda _parts: next(answers))


def test_slot_owner_change_disables_borrowed_cleanup_without_removing_foreign_data(tmp_path):
    registry, runtime = tmp_path / 'registry', tmp_path / 'runtime'
    parent = RuntimeLease(registry, runtime, BOOT, uuid4().hex)
    parent.acquire()
    claim = NativeRunDeviceClaim(runtime, BOOT, 'test', borrow=parent.descriptor(), registry_root=registry)
    claim.acquire()
    changed = dict(claim._slot_owner, nonce='foreign')
    write_json(claim._slot / 'owner.json', changed)
    claim.stopping()
    assert not claim.current() and not claim.finish(input_released=True, observer_terminal=True)
    assert json.loads((claim._slot / 'owner.json').read_text()) == changed


def test_root_only_parent_owner_replacement_prevents_current_and_all_partial_release(tmp_path):
    registry, runtime = tmp_path / 'registry', tmp_path / 'runtime'
    parent = RuntimeLease(registry, runtime, BOOT, uuid4().hex)
    parent.acquire()
    claim = NativeRunDeviceClaim(runtime, BOOT, 'test', borrow=parent.descriptor(), registry_root=registry)
    claim.acquire()
    replacement = dict(parent.owner, nonce='foreign-root-only')
    write_json(parent.paths[0] / 'owner.json', replacement)
    assert not claim.current()
    claim.stopping()
    assert not claim.finish(input_released=True, observer_terminal=True)
    assert (claim._slot / 'owner.json').exists()
    with pytest.raises(DeviceSafetyError):
        parent.release()
    assert all((path / 'owner.json').exists() for path in parent.paths)
    assert json.loads((parent.paths[1] / 'owner.json').read_text()) == parent.owner


@pytest.mark.parametrize('failed_write', [1, 2])
def test_acquire_owner_write_failure_rolls_back_only_this_attempt_and_preserves_original_error(tmp_path, monkeypatch,
                                                                                              failed_write):
    from agent import device_runtime_guard as module
    original = module.write_json
    writes = []
    def fail(path, value):
        writes.append(path)
        if len(writes) == failed_write:
            raise OSError('original owner-write failure')
        original(path, value)
    monkeypatch.setattr(module, 'write_json', fail)
    lease = RuntimeLease(tmp_path / 'registry', tmp_path / 'runtime', BOOT, uuid4().hex)
    with pytest.raises(OSError, match='original owner-write'):
        lease.acquire()
    assert not lease.acquired and not list(lease.registry_root.iterdir())
    monkeypatch.setattr(module, 'write_json', original)
    lease.acquire()
    lease.release()
