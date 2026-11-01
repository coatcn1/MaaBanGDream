"""隔离的 MaaFramework 开发运行器；导入本模块不会访问设备。"""
from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.device_runtime_guard import (DeviceSafetyError, RuntimeLease,
    reject_mfa_gui, validate_device, verify_endpoint_owner, tcp_listeners, write_json,
    process_identity, native_clock_trial_enabled, require_canonical_registry, NATIVE_BORROW_RECEIPT_ENV,
    NATIVE_CLOCK_TRIAL_ENV)

MANIFEST = 'runner-manifest.json'


def native_borrow_child_environment(runtime: Path, nonce: str, environment: dict) -> dict:
    child = dict(environment)
    if child.get(NATIVE_CLOCK_TRIAL_ENV) == '1':
        child[NATIVE_BORROW_RECEIPT_ENV] = str(runtime / 'control' / ('native-borrow-' + nonce + '.json'))
    return child


def load_native_borrow_receipt(runtime: Path, nonce: str) -> dict:
    expected = (runtime / 'control' / ('native-borrow-' + nonce + '.json')).resolve()
    configured = os.environ.get(NATIVE_BORROW_RECEIPT_ENV)
    if not configured or Path(configured).resolve() != expected or expected.stat().st_size > 16384:
        raise DeviceSafetyError('Native child requires its explicit parent borrow receipt')
    value = json.loads(expected.read_text(encoding='utf-8'))
    if value.get('nonce') != nonce or value.get('runtime_root') != str(runtime.resolve()):
        raise DeviceSafetyError('Native parent borrow receipt belongs to another runtime/session')
    descriptor = value.get('descriptor')
    if not isinstance(descriptor, dict):
        raise DeviceSafetyError('Native parent borrow descriptor unavailable')
    require_canonical_registry(Path(descriptor['registry_root']))
    return descriptor


def configure_shell_api(framework=None) -> None:
    """固定的 5.10.2 Python wheel 缺少这两个 64 位 C 函数签名。

    在各客户端和 Agent 进程内按 v5.10.2 的
    include/MaaFramework/Instance/MaaController.h 补齐，不修改已安装依赖。
    """
    if framework is None:
        from maa.library import Library
        framework = Library.framework()
    framework.MaaControllerPostShell.restype = ctypes.c_int64
    framework.MaaControllerPostShell.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int64]
    framework.MaaControllerGetShellOutput.restype = ctypes.c_uint8
    framework.MaaControllerGetShellOutput.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def record_exception(runtime: Path, nonce: str, result: dict, stage: str) -> None:
    details = traceback.format_exc()
    path = runtime / 'logs' / ('runner-' + nonce + '.log')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as output:
        output.write('stage=' + stage + '\n' + details + '\n')
    result.update(error_stage=stage, error_traceback=details, runner_log=str(path))


def binding(args) -> dict:
    return {'endpoint': args.endpoint, 'expected_pid': args.expected_pid,
            'expected_boot_id': args.expected_boot_id.lower(), 'engine': args.engine,
            'registry_root': str(args.registry_root.resolve()),
            'adb_path': str(args.adb_path.resolve())}


def checked_roots(source: Path, runtime: Path) -> tuple[Path, Path]:
    source, runtime = source.resolve(), runtime.resolve()
    if not runtime.name.endswith('.mbdruntime'):
        raise DeviceSafetyError('Runtime directory name must end in .mbdruntime')
    if source == runtime or runtime.is_relative_to(source) or source.is_relative_to(runtime):
        raise DeviceSafetyError('Runtime must be outside the source tree and cannot contain it')
    for name in ('agent/server.py', 'resource', 'profiles'):
        if not (source / name).exists():
            raise DeviceSafetyError('Source deployment inputs are missing: ' + name)
    return source, runtime


def prepare_runtime(source: Path, runtime: Path, requested_binding: dict) -> dict:
    source, runtime = checked_roots(source, runtime)
    manifest_path = runtime / MANIFEST
    if runtime.exists() and any(runtime.iterdir()) and not manifest_path.is_file():
        raise DeviceSafetyError('Nonempty runtime directory has no ownership manifest')
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding='utf-8'))
        if old['source_root'] != str(source) or old['binding'] != requested_binding:
            raise DeviceSafetyError('Existing runtime is bound to a different source/device/registry')
    runtime.mkdir(parents=True, exist_ok=True)
    # 禁止输入联接点或符号链接，避免复制后仍共享可变运行数据。
    for name in ('agent', 'resource', 'profiles'):
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
               for p in (source / name).rglob('*')):
            raise DeviceSafetyError('Linked deployment inputs are not supported')
        target = runtime / name
        if target.exists() and (target.is_symlink() or
                (hasattr(target, 'is_junction') and target.is_junction()) or
                any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
                    for p in target.rglob('*'))):
            raise DeviceSafetyError('Linked runtime state is not isolated')
        if name == 'profiles' and target.exists():
            continue  # 设备专属选择与校准参数始终优先。
        shutil.copytree(source / name, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git', '.process-conflict-guard*'))
    for name in ('debug', 'screencap', 'logs', 'control'):
        (runtime / name).mkdir(exist_ok=True)
    # 一并部署运行器，避免 Agent 将路径解析回源码目录。
    (runtime / 'scripts').mkdir(exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), runtime / 'scripts' / Path(__file__).name)
    if (source / 'interface.json').is_file():
        shutil.copy2(source / 'interface.json', runtime / 'interface.json')
    # 双端实测固定零消耗，只写隔离副本，不能把正式客户端默认值带进测试。
    selection_path = runtime / 'profiles/selection.json'
    selection = (json.loads(selection_path.read_text(encoding='utf-8-sig')) if selection_path.exists()
                 else {'version': 1, 'pinned': {}, 'runtime_options': {}})
    selection.setdefault('runtime_options', {}).update(
        live_boost_settings_enabled=True, live_boost_target=0)
    write_json(selection_path, selection)
    manifest = {'version': 1, 'source_root': str(source), 'runtime_root': str(runtime),
                'binding': requested_binding}
    write_json(manifest_path, manifest)
    return manifest


def load_manifest(args) -> dict:
    manifest = json.loads((args.runtime_root / MANIFEST).read_text(encoding='utf-8'))
    if (manifest.get('binding') != binding(args)
            or manifest.get('runtime_root') != str(args.runtime_root.resolve())
            or manifest.get('source_root') != str(args.source_root.resolve())):
        raise DeviceSafetyError('CLI device binding differs from prepared runtime')
    if not (args.runtime_root / 'agent/server.py').is_file():
        raise DeviceSafetyError('Prepared Agent is missing')
    selection = json.loads((args.runtime_root / 'profiles/selection.json').read_text(encoding='utf-8-sig'))
    if selection.get('runtime_options', {}).get('live_boost_settings_enabled') is not True:
        raise DeviceSafetyError('Isolated cooperative tests require enabled live boost checks; prepare again')
    boost = selection.get('runtime_options', {}).get('live_boost_target')
    if type(boost) is not int or boost != 0:
        raise DeviceSafetyError('Isolated cooperative tests require explicit zero live boost consumption; prepare again')
    for name in ('agent', 'resource', 'profiles', 'control', 'logs', 'debug'):
        target = args.runtime_root / name
        if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
            raise DeviceSafetyError('Prepared runtime contains shared linked state')
    return manifest


def safe_override(runtime: Path, supplied: dict, engine: str) -> dict:
    if not isinstance(supplied, dict):
        raise DeviceSafetyError('Pipeline override must be a JSON object')
    result = copy.deepcopy(supplied)
    # 覆盖所有执行 CommonRecover 自定义动作的节点，包括别名。
    baseline_nodes = {}
    for path in sorted((runtime / 'resource/pipeline').rglob('*.json')):
        nodes = json.loads(path.read_text(encoding='utf-8-sig'))
        for name, node in nodes.items():
            if isinstance(node, dict):
                # 节点可能由其他 Pipeline 文件扩展；Maa 覆盖会整块替换参数对象，
                # 不会递归合并其字段。
                baseline_nodes.setdefault(name, {}).update(copy.deepcopy(node))
    recovery_nodes = {'CommonRecover'}
    recovery_nodes.update(name for name, node in baseline_nodes.items()
                          if node.get('custom_action') == 'CommonRecover')
    recovery_nodes.update(name for name, node in result.items()
                          if isinstance(node, dict) and node.get('custom_action') == 'CommonRecover')
    for name in recovery_nodes:
        node = result.setdefault(name, {})
        if not isinstance(node, dict):
            raise DeviceSafetyError('Recovery override must be a JSON object')
        if 'custom_action_param' not in node:
            # 仅传安全开关会替换资源中的完整参数对象，丢失登录、弹窗取消和主页标记。
            node['custom_action_param'] = copy.deepcopy(
                baseline_nodes.get(name, {}).get('custom_action_param', {}))
        params = node['custom_action_param']
        if not isinstance(params, dict):
            raise DeviceSafetyError('Recovery parameters must be an object')
        params['reboot_emulator_on_failure'] = False
    # 引擎选择来自持久化运行配置，不能根据时序开关推断。
    selection = json.loads((runtime / 'profiles/selection.json').read_text(encoding='utf-8'))
    configured = selection.get('runtime_options', {}).get('native_realtime_enabled', False)
    if configured is not (engine == 'native'):
        raise DeviceSafetyError('Prepared runtime Profile options disagree with --engine')
    if selection.get('runtime_options', {}).get('skip_process_conflict_cleanup', False):
        raise DeviceSafetyError('ALAS process conflict protection must remain enabled')
    return result


def verify_controller(controller, identity: dict, args) -> None:
    verify_endpoint_owner(args.endpoint, args.expected_pid, tcp_listeners())
    info = controller.info
    if not isinstance(info, dict) or info.get('adb_serial') != args.endpoint:
        raise DeviceSafetyError('Connected controller route differs from the validated endpoint')
    actual = controller.post_shell('cat /proc/sys/kernel/random/boot_id', 5000).wait().get()
    fingerprint = controller.post_shell('getprop ro.build.fingerprint', 5000).wait().get()
    if str(actual).strip().lower() != identity['boot_id'] or str(fingerprint).strip() != identity['fingerprint']:
        raise DeviceSafetyError('Connected controller is not the validated Android device')


def checked_controller_config(args) -> dict:
    config = (json.loads(args.controller_config.read_text(encoding='utf-8-sig'))
              if args.controller_config else {})
    if not isinstance(config, dict):
        raise DeviceSafetyError('Controller config must be a JSON object')
    extras = config.get('extras', {})
    if not isinstance(extras, dict):
        raise DeviceSafetyError('Controller extras must be a JSON object')
    for kind, settings in extras.items():
        if not isinstance(settings, dict) or not settings.get('enable'):
            continue
        if (kind != 'ld' or args.endpoint != '127.0.0.2:7555'
                or settings.get('pid') != args.expected_pid):
            raise DeviceSafetyError('Screenshot extras must bind to the validated LD process')
    if args.screencap_method == 'EmulatorExtras' and not extras.get('ld', {}).get('enable'):
        raise DeviceSafetyError('EmulatorExtras requires an explicit verified LD controller config')
    return config


def wait_job(job, seconds: float, *, cancelled=lambda: False) -> bool:
    deadline = time.monotonic() + seconds
    while not job.done:
        if cancelled() or time.monotonic() >= deadline:
            return False
        time.sleep(.1)
    return bool(job.succeeded)


def stop_and_release(tasker, controller, identity: dict, args, nonce: str) -> dict:
    """失败或取消的任务必须停止完成，才能开始仅释放触点的清理。"""
    if tasker.running and not wait_job(tasker.post_stop(), args.stop_timeout_seconds):
        raise DeviceSafetyError('Tasker stop is unconfirmed; device lease retained')
    if tasker.running:
        raise DeviceSafetyError('Tasker is still running; device lease retained')
    verify_controller(controller, identity, args)
    cleanup = {'__CooperativeRunnerRelease': {'recognition': 'DirectHit', 'action': 'Custom',
               'custom_action': 'CooperativeRunnerRelease'}}
    released = wait_job(tasker.post_task('__CooperativeRunnerRelease', cleanup), args.stop_timeout_seconds)
    receipt_path = args.runtime_root / 'control' / ('release-' + nonce + '.json')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else {}
    if not released or receipt.get('nonce') != nonce or receipt.get('release_confirmed') is not True:
        raise DeviceSafetyError('Current-session release proof is missing; device lease retained')
    return receipt


def install_reboot_fence(recovery_module) -> None:
    """仅约束本隔离 Agent，也覆盖直接调用 CommonRecover 的路径。"""
    original = recovery_module.CommonRecover._run

    class RecoveryArgs:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            raw = getattr(wrapped, 'custom_action_param', {})
            params = copy.deepcopy(json.loads(raw) if isinstance(raw, str) and raw else raw or {})
            if not isinstance(params, dict):
                raise DeviceSafetyError('Recovery parameters must be a JSON object')
            params['reboot_emulator_on_failure'] = False
            self.custom_action_param = params

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def recover(self, context, argv):
        return original(self, context, RecoveryArgs(argv))

    def blocked_reboot(*_args, **_kwargs):
        raise DeviceSafetyError('Emulator reboot is forbidden in an isolated cooperative Agent')

    recovery_module.CommonRecover._run = recover
    recovery_module._reboot_ldplayer = blocked_reboot


def agent_main(runtime: Path, nonce: str, identifier: str, flags: list[str]) -> None:
    """使用真实服务注册，并补充最终触点释放凭据动作。"""
    os.chdir(runtime)
    sys.path.insert(0, str(runtime / 'agent'))
    import server
    configure_shell_api()
    import common_recover
    install_reboot_fence(common_recover)
    from maa.agent.agent_server import AgentServer
    from maa.custom_action import CustomAction
    from realtime.native_play import NativeMinitouchBackend
    from realtime.native_prearm import discard_prearmed_backend
    backends = []
    original_init = NativeMinitouchBackend.__init__
    borrowed_lease = load_native_borrow_receipt(runtime, nonce) if native_clock_trial_enabled() else None

    def tracked_init(self, *args, **kwargs):
        if borrowed_lease is not None:
            kwargs['borrowed_device_lease'] = borrowed_lease
        original_init(self, *args, **kwargs)
        backends.append(self)
    NativeMinitouchBackend.__init__ = tracked_init

    @AgentServer.custom_action('CooperativeRunnerSmoke')
    class Smoke(CustomAction):
        def run(self, context, argv):
            write_json(runtime / 'control' / ('smoke-' + nonce + '.json'),
                       {'nonce': nonce, 'server_loaded': True, 'reboot_fence_enabled': True})
            return True

    @AgentServer.custom_action('CooperativeRunnerRelease')
    class Release(CustomAction):
        def run(self, context, argv):
            errors, reports = [], []
            try:
                discard_prearmed_backend('isolated-runner-finalize')
                for backend in backends:
                    report = backend.report()
                    if report.get('release_confirmed') is not True:
                        backend.stop()
                        report = backend.report()
                    settled = backend.wait_native_cleanup(4.2)
                    report = backend.report()
                    if not settled:
                        errors.append('Native run claim/helper worker did not settle; parent lease retained')
                    reports.append({'release_confirmed': report.get('release_confirmed') is True,
                                    'release_proof': report.get('release_proof'), 'native_cleanup_settled': settled,
                                    'native_device_claim': report.get('native_device_claim'),
                                    'native_clock_observer': report.get('native_clock_observer')})
                for contact in range(10):
                    if not context.tasker.controller.post_touch_up(contact).wait().succeeded:
                        errors.append('controller touch-up failed for contact ' + str(contact))
            except Exception as exc:
                errors.append(type(exc).__name__ + ': ' + str(exc))
            confirmed = not errors and all(r['release_confirmed'] for r in reports)
            write_json(runtime / 'control' / ('release-' + nonce + '.json'),
                       {'nonce': nonce, 'release_confirmed': confirmed,
                        'native_backends': reports, 'errors': errors})
            return confirmed

    sys.argv = [str(runtime / 'agent/server.py'), *flags, identifier]
    server.main()


def smoke(args, nonce: str) -> dict:
    """使用内存控制器验证真实客户端、服务、资源和任务执行器。

    不创建 ADB 控制器、不调用 ADB，也不核验或操作模拟器。
    """
    runtime = args.runtime_root.resolve()
    environment = dict(os.environ, MAABANGDREAM_MFA_SESSION_ID=nonce,
                       MAABANGDREAM_MFA_ROOT=str(runtime), PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
    if args.maafw_binary_root:
        os.environ['MAAFW_BINARY_PATH'] = str(args.maafw_binary_root.resolve())
        environment['MAAFW_BINARY_PATH'] = os.environ['MAAFW_BINARY_PATH']
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    import numpy as np
    configure_shell_api()

    class MemoryController(CustomController):
        input_calls = 0
        def connect(self):
            return True
        def request_uuid(self):
            return 'isolated-smoke-' + nonce
        def screencap(self):
            return np.zeros((720, 1280, 3), dtype=np.uint8)
        def shell(self, command, timeout):
            return 'memory-shell-' + nonce if command == 'CooperativeRunnerMemoryShell' else None
        def reject_input(self, *_args):
            self.input_calls += 1
            return False
        start_app = stop_app = click = swipe = touch_down = touch_move = reject_input
        touch_up = click_key = input_text = key_down = key_up = reject_input

    Tasker.set_log_dir(runtime / 'debug')
    resource, controller, tasker = Resource(), MemoryController(), Tasker()
    client = AgentClient.create_tcp(0)
    flags = ['--native-timing-trial', '--regional-level-drift-trial', *args.agent_flag]
    log_path = runtime / 'logs' / ('smoke-agent-' + nonce + '.log')
    with log_path.open('w', encoding='utf-8') as output:
        if not client.bind(resource):
            raise RuntimeError('Smoke Agent resource binding failed')
        child = subprocess.Popen([sys.executable, '-B', str(runtime / 'scripts/run_cooperative_device.py'),
                                  '--agent-server', str(runtime), nonce, str(client.identifier), *flags],
                                 cwd=runtime, env=environment, stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            # 绑定、连接和加载资源的顺序与真实运行器一致。
            client.set_timeout(15000)
            if not client.connect():
                raise RuntimeError('Smoke Agent connection failed')
            if not wait_job(resource.post_bundle(runtime / 'resource'), 90):
                raise RuntimeError('Smoke resource bundle load failed')
            if not wait_job(controller.post_connection(), 10):
                raise RuntimeError('Smoke memory-controller connection failed')
            # 验证此前缺失的 ctypes 签名，不访问系统 shell 或设备。
            if controller.post_shell('CooperativeRunnerMemoryShell', 5000).wait().get() != 'memory-shell-' + nonce:
                raise RuntimeError('Smoke memory shell round-trip failed')
            if not tasker.bind(resource, controller) or not client.register_sink(resource, controller, tasker):
                raise RuntimeError('Smoke Tasker binding failed')
            pipeline = {'__CooperativeRunnerSmoke': {'recognition': 'DirectHit', 'action': 'Custom',
                        'custom_action': 'CooperativeRunnerSmoke'}}
            if not wait_job(tasker.post_task('__CooperativeRunnerSmoke', pipeline), 20):
                raise RuntimeError('Smoke Agent action failed')
            receipt = json.loads((runtime / 'control' / ('smoke-' + nonce + '.json')).read_text(encoding='utf-8'))
            if receipt.get('nonce') != nonce or controller.input_calls:
                raise DeviceSafetyError('Smoke receipt mismatch or unexpected memory-controller input')
            return {'status': 'smoke_succeeded', 'nonce': nonce, 'device_calls': 0,
                    'memory_input_calls': controller.input_calls, 'memory_shell_verified': True,
                    'agent_log': str(log_path)}
        finally:
            if tasker.running:
                wait_job(tasker.post_stop(), 10)
            client.disconnect()
            child.wait(timeout=15)


def execute(args, nonce: str, identity: dict) -> tuple[dict, bool]:
    """返回执行结果，以及是否已取得可安全释放共享租约的证据。"""
    reject_mfa_gui()
    runtime = args.runtime_root.resolve()
    override = safe_override(runtime, json.loads(args.pipeline_override.read_text(encoding='utf-8-sig'))
                             if args.pipeline_override else {}, args.engine)
    flags = ['--native-timing-trial', '--regional-level-drift-trial', *args.agent_flag]
    environment = dict(os.environ, MAABANGDREAM_MFA_SESSION_ID=nonce,
                       MAABANGDREAM_MFA_ROOT=str(runtime), PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
    environment = native_borrow_child_environment(runtime, nonce, environment)
    # Maa 默认加载已安装的固定版本二进制；显式指定目录时，客户端与服务必须共用，
    # 不能混入其他 MaaFW 版本。
    if args.maafw_binary_root:
        os.environ['MAAFW_BINARY_PATH'] = str(args.maafw_binary_root.resolve())
        environment['MAAFW_BINARY_PATH'] = os.environ['MAAFW_BINARY_PATH']
    os.chdir(runtime)
    from maa.agent_client import AgentClient
    from maa.controller import AdbController
    from maa.define import MaaAdbInputMethodEnum, MaaAdbScreencapMethodEnum
    from maa.resource import Resource
    from maa.tasker import Tasker
    configure_shell_api()
    Tasker.set_log_dir(runtime / 'debug')
    Tasker.set_save_on_error(True)
    client = AgentClient.create_tcp(0)
    resource = Resource()
    controller_config = checked_controller_config(args)
    controller = AdbController(adb_path=str(args.adb_path), address=args.endpoint,
                               screencap_methods=int(getattr(MaaAdbScreencapMethodEnum, args.screencap_method)),
                               input_methods=int(MaaAdbInputMethodEnum.MinitouchAndAdbKey),
                               config=controller_config)
    tasker = Tasker()
    child, final_safe, input_possible = None, False, False
    cancelled = False
    previous_handlers = {}
    last_owner_check = 0.0
    route_error = ''

    def cancel_signal(*_):
        nonlocal cancelled
        cancelled = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, cancel_signal)

    def should_cancel():
        nonlocal last_owner_check, route_error
        if cancelled:
            return True
        if time.monotonic() - last_owner_check >= 1.0:
            last_owner_check = time.monotonic()
            try:
                verify_endpoint_owner(args.endpoint, args.expected_pid, tcp_listeners())
                if process_identity(args.expected_pid, args.endpoint) != identity['owner']:
                    raise DeviceSafetyError('Emulator process identity changed during the task')
            except Exception as exc:
                route_error = str(exc)
                return True
        path = runtime / 'control/cancel.json'
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding='utf-8')).get('nonce') == nonce
            except (OSError, ValueError):
                return False
        return False

    result = {'nonce': nonce, 'status': 'failed', 'release_confirmed': False}
    log_path = runtime / 'logs' / ('agent-' + nonce + '.log')
    stage = 'agent_bind'
    with log_path.open('w', encoding='utf-8') as agent_log:
        try:
            if not client.bind(resource):
                raise RuntimeError('Agent resource binding failed')
            stage = 'agent_spawn'
            child = subprocess.Popen([sys.executable, '-B', str(runtime / 'scripts/run_cooperative_device.py'),
                                      '--agent-server', str(runtime), nonce, str(client.identifier), *flags],
                                     cwd=runtime, env=environment, stdout=agent_log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            client.set_timeout(15000)
            stage = 'agent_connect'
            if not client.connect():
                raise RuntimeError('Agent connection failed')
            stage = 'resource_bundle'
            if not wait_job(resource.post_bundle(runtime / 'resource'), 90):
                raise RuntimeError('Resource bundle load failed')
            stage = 'controller_connection'
            if not wait_job(controller.post_connection(), 60):
                raise RuntimeError('Controller connection failed')
            stage = 'controller_identity_shell'
            verify_controller(controller, identity, args)
            stage = 'game_foreground'
            from agent.foreground_guard import require_game_foreground
            require_game_foreground(controller)
            stage = 'tasker_bind'
            if not tasker.bind(resource, controller) or not client.register_sink(resource, controller, tasker):
                raise RuntimeError('Tasker/sink binding failed')
            reject_mfa_gui()
            write_json(runtime / 'control/active.json', {'nonce': nonce, 'pid': os.getpid(),
                       'agent_pid': child.pid, 'endpoint': args.endpoint})
            input_possible = True
            stage = 'task_post'
            job = tasker.post_task(args.entry, override)
            stage = 'task_wait'
            success = wait_job(job, args.timeout_seconds, cancelled=should_cancel)
            requested_stop = not job.done
            stage = 'stop_and_release'
            stop_and_release(tasker, controller, identity, args, nonce)
            receipt_path = runtime / 'control' / ('release-' + nonce + '.json')
            final_safe = True
            result.update(status=('cancelled' if requested_stop else 'succeeded' if success else 'failed'),
                          task_succeeded=success, release_confirmed=final_safe,
                          release_receipt=str(receipt_path), agent_log=str(log_path))
            if route_error:
                result.update(status='failed', error=route_error)
        except Exception as exc:
            result['error'] = type(exc).__name__ + ': ' + str(exc)
            record_exception(runtime, nonce, result, stage)
            if input_possible:
                try:
                    stop_and_release(tasker, controller, identity, args, nonce)
                    final_safe = True
                except Exception as cleanup_error:
                    result['cleanup_error'] = str(cleanup_error)
                    result['cleanup_traceback'] = traceback.format_exc()
            else:
                final_safe = True
        finally:
            if final_safe:
                client.disconnect()
                if child is not None:
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        final_safe = False
                        result['error'] = 'Agent exit unconfirmed; device lease retained'
                        result['agent_exit_traceback'] = traceback.format_exc()
                result['release_confirmed'] = final_safe
            else:
                result['lease_retained'] = True
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    return result, final_safe


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    for name in ('prepare-only', 'validate-only', 'run', 'cancel-only', 'smoke-only'):
        mode.add_argument('--' + name, action='store_true')
    for name in ('source-root', 'runtime-root', 'registry-root', 'adb-path'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--endpoint', choices=('127.0.0.2:7555', '127.0.0.1:16384'), required=True)
    p.add_argument('--expected-pid', type=int, required=True)
    p.add_argument('--expected-boot-id', required=True)
    p.add_argument('--engine', choices=('native', 'legacy'), required=True)
    p.add_argument('--entry', default='CooperativeLive')
    p.add_argument('--pipeline-override', type=Path)
    p.add_argument('--timeout-seconds', type=float, default=600)
    p.add_argument('--stop-timeout-seconds', type=float, default=30)
    p.add_argument('--agent-flag', action='append', default=[])
    p.add_argument('--maafw-binary-root', type=Path)
    p.add_argument('--controller-config', type=Path)
    p.add_argument('--screencap-method', choices=('RawWithGzip', 'RawByNetcat', 'EmulatorExtras', 'Encode'),
                   default='RawWithGzip')
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    args.runtime_root = args.runtime_root.resolve()
    args.source_root = args.source_root.resolve()
    args.adb_path = args.adb_path.resolve()
    binding(args)
    if args.timeout_seconds <= 0 or args.stop_timeout_seconds <= 0:
        raise DeviceSafetyError('Timeouts must be positive')
    registry = args.registry_root.resolve()
    if registry.is_relative_to(args.runtime_root) or registry.is_relative_to(args.source_root):
        raise DeviceSafetyError('Shared registry must be outside source and runtime directories')
    if args.pipeline_override:
        args.pipeline_override = args.pipeline_override.resolve()
    if args.controller_config:
        args.controller_config = args.controller_config.resolve()
    if args.cancel_only:
        load_manifest(args)
        active = json.loads((args.runtime_root / 'control/active.json').read_text(encoding='utf-8'))
        write_json(args.runtime_root / 'control/cancel.json', {'nonce': active['nonce']})
        print(json.dumps({'status': 'cancel_requested', 'nonce': active['nonce']}))
        return 0
    if native_clock_trial_enabled() and not (args.prepare_only or args.smoke_only):
        require_canonical_registry(args.registry_root)
    nonce = uuid4().hex
    lease = RuntimeLease(args.registry_root, args.runtime_root, args.expected_boot_id, nonce)
    lease.acquire()
    release = True
    try:
        if args.prepare_only:
            prepare_runtime(args.source_root, args.runtime_root, binding(args))
            print(json.dumps({'status': 'prepared', 'runtime_root': str(args.runtime_root)}))
            return 0
        load_manifest(args)
        if args.smoke_only:
            result = smoke(args, nonce)
            write_json(args.runtime_root / 'control/latest-smoke.json', result)
            print(json.dumps(result))
            return 0
        receipt_path = args.runtime_root / 'control/validated-device.json'
        previous = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else None
        identity = validate_device(args.adb_path, args.endpoint, args.expected_pid,
                                   args.expected_boot_id, previous=previous)
        write_json(receipt_path, identity)
        if args.validate_only:
            print(json.dumps({'status': 'validated', 'identity': identity}))
            return 0
        if native_clock_trial_enabled():
            write_json(args.runtime_root / 'control' / ('native-borrow-' + nonce + '.json'),
                {'nonce': nonce, 'runtime_root': str(args.runtime_root), 'descriptor': lease.descriptor()})
        # 确定性的预检失败尚未创建控制器，可以释放本次占用。
        reject_mfa_gui()
        safe_override(args.runtime_root,
                      json.loads(args.pipeline_override.read_text(encoding='utf-8-sig'))
                      if args.pipeline_override else {}, args.engine)
        checked_controller_config(args)
        # 控制器开始构造后，任何异常结果都视为清理状态尚不确定。
        release = False
        result, release = execute(args, nonce, identity)
        write_json(args.runtime_root / 'control' / ('result-' + nonce + '.json'), result)
        write_json(args.runtime_root / 'control/latest-result.json', result)
        print(json.dumps(result))
        return 0 if result['status'] == 'succeeded' and release else 2
    finally:
        if release:
            lease.release()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--agent-server':
        agent_main(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5:])
    else:
        try:
            raise SystemExit(main())
        except (DeviceSafetyError, OSError, ValueError, KeyError) as exc:
            print(json.dumps({'status': 'blocked', 'error': type(exc).__name__ + ': ' + str(exc)}), file=sys.stderr)
            raise SystemExit(2)
