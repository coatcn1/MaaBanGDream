import json
import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_cooperative_device as runner
from agent.device_runtime_guard import DeviceSafetyError, write_json


def source_tree(tmp_path):
    source = tmp_path / 'source'
    (source / 'agent').mkdir(parents=True)
    (source / 'agent/server.py').write_text('source server', encoding='utf-8')
    (source / 'resource/pipeline').mkdir(parents=True)
    write_json(source / 'resource/pipeline/common.json', {
        'CommonRecover': {'custom_action': 'CommonRecover', 'custom_action_param': {'restart_limit': 2}},
        'AliasRecover': {'custom_action': 'CommonRecover'}})
    write_json(source / 'profiles/selection.json', {'version': 1, 'runtime_options': {
        'native_realtime_enabled': False, 'skip_process_conflict_cleanup': False}})
    (source / 'profiles/user-profile.json').write_text('original user data', encoding='utf-8')
    return source


def test_native_borrow_receipt_is_explicit_canonical_and_only_added_to_child_env(tmp_path, monkeypatch):
    from agent.device_runtime_guard import (RuntimeLease, shared_device_registry_root,
        NATIVE_BORROW_RECEIPT_ENV, NATIVE_CLOCK_TRIAL_ENV)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'appdata'))
    runtime = tmp_path / 'native.mbdruntime'
    parent = RuntimeLease(shared_device_registry_root(), runtime,
        '00000000-0000-4000-8000-000000000001', 'parent-nonce')
    parent.acquire()
    path = runtime / 'control/native-borrow-parent-nonce.json'
    write_json(path, {'nonce': 'parent-nonce', 'runtime_root': str(runtime.resolve()), 'descriptor': parent.descriptor()})
    original = {NATIVE_CLOCK_TRIAL_ENV: '1'}
    child = runner.native_borrow_child_environment(runtime, 'parent-nonce', original)
    assert NATIVE_BORROW_RECEIPT_ENV not in original
    assert child[NATIVE_BORROW_RECEIPT_ENV] == str(path)
    monkeypatch.setenv(NATIVE_BORROW_RECEIPT_ENV, child[NATIVE_BORROW_RECEIPT_ENV])
    assert runner.load_native_borrow_receipt(runtime, 'parent-nonce') == parent.descriptor()
    with pytest.raises(DeviceSafetyError):
        runner.load_native_borrow_receipt(runtime, 'foreign-session')
    monkeypatch.delenv(NATIVE_BORROW_RECEIPT_ENV)
    with pytest.raises(DeviceSafetyError, match='explicit parent'):
        runner.load_native_borrow_receipt(runtime, 'parent-nonce')
    parent.release()


def test_snapshot_is_independent_and_reprepare_preserves_profile_data(tmp_path):
    source = source_tree(tmp_path)
    runtime = tmp_path / 'guest.mbdruntime'
    runner.prepare_runtime(source, runtime, {'device': 'one'})
    assert not (runtime / '.git').exists()
    assert (runtime / 'agent/server.py').read_text() == 'source server'
    (runtime / 'profiles/user-profile.json').write_text('device settings')
    (runtime / 'profiles/private-profile.json').write_text('new calibration')
    (source / 'agent/server.py').write_text('updated server')
    runner.prepare_runtime(source, runtime, {'device': 'one'})
    assert (runtime / 'profiles/user-profile.json').read_text() == 'device settings'
    assert (runtime / 'profiles/private-profile.json').read_text() == 'new calibration'
    assert (runtime / 'agent/server.py').read_text() == 'updated server'
    assert (source / 'profiles/user-profile.json').read_text() == 'original user data'
    assert not (source / 'debug').exists()


def test_existing_runtime_binding_cannot_be_reassigned(tmp_path):
    source = source_tree(tmp_path)
    runtime = tmp_path / 'guest.mbdruntime'
    runner.prepare_runtime(source, runtime, {'device': 'one'})
    with pytest.raises(DeviceSafetyError, match='different'):
        runner.prepare_runtime(source, runtime, {'device': 'two'})


def test_isolated_runner_uses_zero_boost_without_modifying_source(tmp_path):
    source = source_tree(tmp_path)
    selection_path = source / 'profiles/selection.json'
    original = json.loads(selection_path.read_text(encoding='utf-8'))
    original['runtime_options']['live_boost_target'] = 3
    original['runtime_options']['live_boost_settings_enabled'] = False
    write_json(selection_path, original)
    before = selection_path.read_bytes()
    runtime = tmp_path / 'guest.mbdruntime'
    runner.prepare_runtime(source, runtime, {'device': 'one'})
    copied = json.loads((runtime / 'profiles/selection.json').read_text(encoding='utf-8'))
    assert copied['runtime_options']['live_boost_target'] == 0
    assert copied['runtime_options']['live_boost_settings_enabled'] is True
    assert selection_path.read_bytes() == before


def test_isolated_runner_supports_empty_source_profile_selection(tmp_path):
    source = source_tree(tmp_path)
    selection = source / 'profiles/selection.json'
    selection.unlink()
    runtime = tmp_path / 'guest.mbdruntime'
    runner.prepare_runtime(source, runtime, {'device': 'one'})
    copied = json.loads((runtime / 'profiles/selection.json').read_text(encoding='utf-8'))
    assert copied['runtime_options']['live_boost_target'] == 0
    assert copied['runtime_options']['live_boost_settings_enabled'] is True
    assert not selection.exists()


@pytest.mark.parametrize("enabled", [False, None, "missing"])
def test_prepared_runner_rejects_disabled_or_missing_boost_check(tmp_path, monkeypatch, enabled):
    source = source_tree(tmp_path)
    runtime = tmp_path / 'guest.mbdruntime'
    requested = {'device': 'one'}
    runner.prepare_runtime(source, runtime, requested)
    args = SimpleNamespace(source_root=source, runtime_root=runtime)
    monkeypatch.setattr(runner, 'binding', lambda _: requested)
    assert runner.load_manifest(args)
    selection = runtime / 'profiles/selection.json'
    state = json.loads(selection.read_text(encoding='utf-8'))
    if enabled == 'missing':
        del state['runtime_options']['live_boost_settings_enabled']
    else:
        state['runtime_options']['live_boost_settings_enabled'] = enabled
    write_json(selection, state)
    with pytest.raises(DeviceSafetyError, match='enabled live boost checks'):
        runner.load_manifest(args)


def test_source_nested_or_unowned_destination_is_rejected(tmp_path):
    source = source_tree(tmp_path)
    with pytest.raises(DeviceSafetyError, match='outside'):
        runner.prepare_runtime(source, source / 'guest.mbdruntime', {})
    runtime = tmp_path / 'guest.mbdruntime'
    runtime.mkdir()
    (runtime / 'user-data').write_text('preserve')
    with pytest.raises(DeviceSafetyError, match='ownership'):
        runner.prepare_runtime(source, runtime, {})
    assert (runtime / 'user-data').read_text() == 'preserve'


def test_recovery_override_disables_reboot_for_every_alias_and_preserves_input(tmp_path):
    source = source_tree(tmp_path)
    runtime = tmp_path / 'guest.mbdruntime'
    runner.prepare_runtime(source, runtime, {})
    supplied = {'CommonRecover': {'custom_action_param': {'reboot_emulator_on_failure': True, 'restart_limit': 1}},
                'NewRecover': {'custom_action': 'CommonRecover', 'custom_action_param': {'reboot_emulator_on_failure': True}},
                'CooperativeStart': {'custom_action_param': {'mode': 'friend'}}}
    result = runner.safe_override(runtime, supplied, 'legacy')
    for name in ('CommonRecover', 'AliasRecover', 'NewRecover'):
        assert result[name]['custom_action_param']['reboot_emulator_on_failure'] is False
    assert result['CommonRecover']['custom_action_param']['restart_limit'] == 1
    assert supplied['CommonRecover']['custom_action_param']['reboot_emulator_on_failure'] is True
    assert result['CooperativeStart'] == supplied['CooperativeStart']


def test_real_cooperative_recovery_preserves_every_parameter_except_reboot(tmp_path):
    runtime = tmp_path / 'runtime'
    actual_pipeline = Path(__file__).resolve().parents[1] / 'resource/pipeline/cooperative_live.json'
    original = json.loads(actual_pipeline.read_text(encoding='utf-8-sig'))
    baseline = original['CooperativeRecover']['custom_action_param']
    # 仅覆盖开关参数时会丢失这些真实导航保护，回归必须保留完整参数。
    for key in ('home_node', 'modal_cancel_nodes', 'click_nodes', 'login_start_node',
                'login_start_target', 'login_tap_target', 'startup_grace_ms', 'escape_after_login_start'):
        assert key in baseline
    write_json(runtime / 'resource/pipeline/cooperative_live.json', original)
    write_json(runtime / 'profiles/selection.json', {'runtime_options': {'native_realtime_enabled': True}})
    supplied = {'CooperativeRecover': {'focus': {'Node.Action.Starting': {'content': 'test'}}}}
    result = runner.safe_override(runtime, supplied, 'native')
    expected = dict(baseline, reboot_emulator_on_failure=False)
    assert result['CooperativeRecover']['custom_action_param'] == expected
    assert result['CooperativeRecover']['focus'] == supplied['CooperativeRecover']['focus']
    result['CooperativeRecover']['custom_action_param']['click_nodes'].append('mutation-check')
    assert 'mutation-check' not in baseline['click_nodes']
    assert 'custom_action_param' not in supplied['CooperativeRecover']


def test_explicit_recovery_parameter_object_remains_a_whole_user_override(tmp_path):
    source = source_tree(tmp_path)
    runtime = tmp_path / 'guest.mbdruntime'
    runner.prepare_runtime(source, runtime, {})
    explicit = {'home_node': 'UserVerifiedHome', 'click_nodes': [], 'reboot_emulator_on_failure': True}
    result = runner.safe_override(runtime, {'CommonRecover': {'custom_action_param': explicit}}, 'legacy')
    assert result['CommonRecover']['custom_action_param'] == dict(explicit, reboot_emulator_on_failure=False)
    assert 'restart_limit' not in result['CommonRecover']['custom_action_param']
    assert explicit['reboot_emulator_on_failure'] is True


def test_partial_node_extensions_keep_effective_baseline_recovery_parameters(tmp_path):
    source = source_tree(tmp_path)
    params = {'home_node': 'AliasHome', 'modal_cancel_nodes': ['Cancel'], 'click_nodes': ['Login'],
              'login_start_node': 'LoginMarker', 'login_start_target': [640, 635]}
    write_json(source / 'resource/pipeline/01-alias.json', {'AliasRecover': {
        'custom_action': 'CommonRecover', 'custom_action_param': params}})
    write_json(source / 'resource/pipeline/02-alias-extension.json', {'AliasRecover': {'next': ['Done']}})
    runtime = tmp_path / 'guest.mbdruntime'
    runner.prepare_runtime(source, runtime, {})
    result = runner.safe_override(runtime, {}, 'legacy')
    assert result['AliasRecover']['custom_action_param'] == dict(params, reboot_emulator_on_failure=False)


def test_engine_and_alas_guard_must_match_prepared_runtime(tmp_path):
    source = source_tree(tmp_path)
    runtime = tmp_path / 'guest.mbdruntime'
    runner.prepare_runtime(source, runtime, {})
    with pytest.raises(DeviceSafetyError, match='engine'):
        runner.safe_override(runtime, {}, 'native')
    selection = runtime / 'profiles/selection.json'
    write_json(selection, {'runtime_options': {'skip_process_conflict_cleanup': True}})
    with pytest.raises(DeviceSafetyError, match='ALAS'):
        runner.safe_override(runtime, {}, 'legacy')


class Job:
    done = True
    succeeded = True


def test_stop_precedes_release_task_and_requires_current_nonce(monkeypatch, tmp_path):
    events = []
    args = SimpleNamespace(runtime_root=tmp_path, stop_timeout_seconds=.1)
    class Tasker:
        running = True
        def post_stop(self):
            events.append('stop')
            self.running = False
            return Job()
        def post_task(self, entry, override):
            assert not self.running
            events.append('release')
            write_json(tmp_path / 'control/release-current.json', {'nonce': 'old', 'release_confirmed': True})
            return Job()
    monkeypatch.setattr(runner, 'verify_controller', lambda *_: events.append('verify'))
    with pytest.raises(DeviceSafetyError, match='release proof'):
        runner.stop_and_release(Tasker(), None, {}, args, 'current')
    assert events == ['stop', 'verify', 'release']


def test_unconfirmed_stop_does_not_send_any_release_input(monkeypatch, tmp_path):
    events = []
    args = SimpleNamespace(runtime_root=tmp_path, stop_timeout_seconds=.01)
    class Pending:
        done = False
    class Tasker:
        running = True
        def post_stop(self):
            events.append('stop')
            return Pending()
        def post_task(self, *_):
            pytest.fail('must not start release task before stop confirmation')
    monkeypatch.setattr(runner, 'verify_controller', lambda *_: pytest.fail('must not query controller'))
    with pytest.raises(DeviceSafetyError, match='stop is unconfirmed'):
        runner.stop_and_release(Tasker(), None, {}, args, 'current')
    assert events == ['stop']


def test_current_native_release_receipt_is_required(monkeypatch, tmp_path):
    args = SimpleNamespace(runtime_root=tmp_path, stop_timeout_seconds=.1)
    class Tasker:
        running = False
        def post_task(self, *_):
            write_json(tmp_path / 'control/release-current.json', {'nonce': 'current', 'release_confirmed': True})
            return Job()
    monkeypatch.setattr(runner, 'verify_controller', lambda *_: None)
    assert runner.stop_and_release(Tasker(), None, {}, args, 'current')['release_confirmed']


def test_mumu_explicit_native_prepares_new_root_without_reassigning_legacy_root(tmp_path):
    source = source_tree(tmp_path)
    args = SimpleNamespace(endpoint='127.0.0.1:16384', engine='native', expected_pid=20,
                           expected_boot_id='00000000-0000-4000-8000-000000000001',
                           registry_root=tmp_path / 'registry', adb_path=tmp_path / 'adb.exe')
    native_binding = runner.binding(args)
    assert native_binding['engine'] == 'native'
    native_root = tmp_path / 'host-native.mbdruntime'
    assert runner.prepare_runtime(source, native_root, native_binding)['binding'] == native_binding
    legacy_root = tmp_path / 'host-legacy.mbdruntime'
    legacy_binding = dict(native_binding, engine='legacy')
    runner.prepare_runtime(source, legacy_root, legacy_binding)
    with pytest.raises(DeviceSafetyError, match='different'):
        runner.prepare_runtime(source, legacy_root, native_binding)
    assert json.loads((legacy_root / runner.MANIFEST).read_text())['binding']['engine'] == 'legacy'


def test_screenshot_extras_cannot_point_to_another_emulator(tmp_path):
    config_path = tmp_path / 'controller.json'
    args = SimpleNamespace(controller_config=config_path, endpoint='127.0.0.2:7555',
                           expected_pid=10, screencap_method='EmulatorExtras')
    write_json(config_path, {'extras': {'ld': {'enable': True, 'pid': 20}}})
    with pytest.raises(DeviceSafetyError, match='validated LD'):
        runner.checked_controller_config(args)
    write_json(config_path, {'extras': {'ld': {'enable': True, 'pid': 10, 'index': 1000}}})
    assert runner.checked_controller_config(args)['extras']['ld']['pid'] == 10


def test_prepare_only_never_probes_device(monkeypatch, tmp_path):
    source = source_tree(tmp_path)
    runtime = tmp_path / 'guest.mbdruntime'
    monkeypatch.setattr(runner, 'validate_device', lambda *_a, **_k: pytest.fail('prepare must be file-only'))
    argv = ['--prepare-only', '--source-root', str(source), '--runtime-root', str(runtime),
            '--registry-root', str(tmp_path / 'registry'), '--adb-path', str(tmp_path / 'adb.exe'),
            '--endpoint', '127.0.0.1:16384', '--expected-pid', '20',
            '--expected-boot-id', '00000000-0000-4000-8000-000000000001', '--engine', 'legacy']
    assert runner.main(argv) == 0
    assert not list((tmp_path / 'registry').iterdir())


def test_isolated_reboot_fence_covers_direct_recovery_and_emulator_helper():
    seen = []
    class CommonRecover:
        def _run(self, context, argv):
            seen.append((argv.custom_action_param, argv.task_detail))
            return True
    module = SimpleNamespace(CommonRecover=CommonRecover,
                             _reboot_ldplayer=lambda *_: pytest.fail('must not reboot'))
    runner.install_reboot_fence(module)
    argv = SimpleNamespace(custom_action_param=json.dumps({'reboot_emulator_on_failure': True}),
                           task_detail='real-task')
    assert CommonRecover()._run(None, argv)
    assert seen == [({'reboot_emulator_on_failure': False}, 'real-task')]
    assert json.loads(argv.custom_action_param)['reboot_emulator_on_failure'] is True
    with pytest.raises(DeviceSafetyError, match='forbidden'):
        module._reboot_ldplayer('console', 1000, 'adb', 'wrong-device')


def test_direct_recovery_missing_option_is_also_fenced():
    class CommonRecover:
        def _run(self, context, argv):
            return argv.custom_action_param['reboot_emulator_on_failure']
    module = SimpleNamespace(CommonRecover=CommonRecover, _reboot_ldplayer=None)
    runner.install_reboot_fence(module)
    assert CommonRecover()._run(None, SimpleNamespace(custom_action_param={'restart_limit': 2})) is False


def test_smoke_mode_never_validates_or_connects_to_an_emulator(monkeypatch, tmp_path):
    source = source_tree(tmp_path)
    runtime = tmp_path / 'guest.mbdruntime'
    argv = ['--prepare-only', '--source-root', str(source), '--runtime-root', str(runtime),
            '--registry-root', str(tmp_path / 'registry'), '--adb-path', str(tmp_path / 'adb.exe'),
            '--endpoint', '127.0.0.1:16384', '--expected-pid', '20',
            '--expected-boot-id', '00000000-0000-4000-8000-000000000001', '--engine', 'legacy']
    runner.main(argv)
    monkeypatch.setattr(runner, 'validate_device', lambda *_a, **_k: pytest.fail('must not query ADB'))
    monkeypatch.setattr(runner, 'execute', lambda *_a, **_k: pytest.fail('must not execute a device task'))
    monkeypatch.setattr(runner, 'smoke', lambda *_: {'status': 'smoke_succeeded', 'device_calls': 0})
    argv[0] = '--smoke-only'
    assert runner.main(argv) == 0
    assert not list((tmp_path / 'registry').iterdir())


def test_shell_api_preserves_64bit_handle_job_id_and_timeout():
    class Function:
        pass
    framework = SimpleNamespace(MaaControllerPostShell=Function(), MaaControllerGetShellOutput=Function())
    runner.configure_shell_api(framework)
    assert framework.MaaControllerPostShell.restype is ctypes.c_int64
    assert framework.MaaControllerPostShell.argtypes == [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int64]
    assert framework.MaaControllerGetShellOutput.argtypes == [ctypes.c_void_p, ctypes.c_void_p]
    assert framework.MaaControllerGetShellOutput.restype is ctypes.c_uint8


def test_exception_receipt_records_callsite_and_stage(tmp_path):
    result = {}
    try:
        raise ctypes.ArgumentError('argument 1: OverflowError: int too long to convert')
    except Exception:
        runner.record_exception(tmp_path, 'test-run', result, 'controller_identity_shell')
    assert result['error_stage'] == 'controller_identity_shell'
    assert 'ArgumentError' in result['error_traceback']
    assert 'test_exception_receipt_records_callsite_and_stage' in result['error_traceback']
    assert 'stage=controller_identity_shell' in Path(result['runner_log']).read_text(encoding='utf-8')
