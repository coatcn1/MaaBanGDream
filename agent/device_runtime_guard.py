"""隔离开发运行器的宿主路由核验与默认拒绝不确定状态的租约。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from uuid import UUID, uuid4

import psutil


class DeviceSafetyError(RuntimeError):
    pass


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


@dataclass(frozen=True)
class Listener:
    address: str
    port: int
    pid: int


def tcp_listeners() -> list[Listener]:
    return [Listener(c.laddr.ip, c.laddr.port, c.pid or 0)
            for c in psutil.net_connections(kind='tcp')
            if c.status == psutil.CONN_LISTEN and c.laddr]


def verify_endpoint_owner(endpoint: str, expected_pid: int,
                          listeners: Iterable[Listener]) -> None:
    """沿用 MFA 的精确地址绑定优先于通配地址规则。"""
    try:
        address, raw_port = endpoint.rsplit(':', 1)
        parsed = ipaddress.ip_address(address)
        port = int(raw_port)
    except (ValueError, TypeError) as exc:
        raise DeviceSafetyError('An explicit IPv4 loopback endpoint is required') from exc
    if parsed.version != 4 or not parsed.is_loopback or not 1 <= port <= 65535 or expected_pid <= 0:
        raise DeviceSafetyError('Invalid explicit endpoint or expected process id')
    candidates = [c for c in listeners if c.port == port]
    exact = [c for c in candidates if c.address == address]
    routed = exact or [c for c in candidates if c.address == '0.0.0.0']
    if len(routed) != 1 or routed[0].pid != expected_pid:
        raise DeviceSafetyError('ADB endpoint owner does not uniquely match the expected emulator')


def process_identity(pid: int, endpoint: str) -> dict:
    process = psutil.Process(pid)
    name = process.name().lower()
    expected = {'127.0.0.2:7555': 'ld9boxheadless.exe',
                '127.0.0.1:16384': 'mumuvmmheadless.exe'}
    if endpoint not in expected or name != expected[endpoint]:
        raise DeviceSafetyError('Endpoint is not an approved LD/MuMu emulator route')
    return {'pid': pid, 'created_at': process.create_time(), 'name': name,
            'executable': process.exe()}


def adb_shell(adb_path: Path, endpoint: str, command: list[str]) -> str:
    completed = subprocess.run([str(adb_path), '-s', endpoint, 'shell', *command],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=10, check=True,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return completed.stdout.strip()


def validate_device(adb_path: Path, endpoint: str, expected_pid: int,
                    expected_boot_id: str, *, previous: dict | None = None,
                    listeners: Callable = tcp_listeners,
                    process_probe: Callable = process_identity,
                    shell: Callable = adb_shell) -> dict:
    boot_id = str(UUID(expected_boot_id)).lower()
    verify_endpoint_owner(endpoint, expected_pid, listeners())
    owner = process_probe(expected_pid, endpoint)
    actual_boot = shell(adb_path, endpoint, ['cat', '/proc/sys/kernel/random/boot_id']).lower()
    if actual_boot != boot_id:
        raise DeviceSafetyError('Android boot identity changed or does not match')
    fingerprint = shell(adb_path, endpoint, ['getprop', 'ro.build.fingerprint'])
    android_id = shell(adb_path, endpoint, ['settings', 'get', 'secure', 'android_id'])
    if not fingerprint or not android_id or android_id == 'null':
        raise DeviceSafetyError('Android fingerprint or android_id is unavailable')
    identity = {'boot_id': boot_id, 'fingerprint': fingerprint,
                'android_id': android_id, 'endpoint': endpoint, 'owner': owner}
    if previous is not None and identity != previous:
        raise DeviceSafetyError('Validated device identity changed; prepare a fresh validation receipt')
    # shell 查询后重新核验端口归属，路由变化不能被当作已通过的身份证据。
    verify_endpoint_owner(endpoint, expected_pid, listeners())
    if process_probe(expected_pid, endpoint) != owner:
        raise DeviceSafetyError('Emulator process identity changed while validating')
    return identity


def reject_mfa_gui() -> None:
    """GUI 任务状态未知时，不允许另一控制器接管其设备。"""
    for process in psutil.process_iter(['name']):
        if str(process.info.get('name', '')).lower() in {'mfaavalonia.exe', 'maabangdream.exe'}:
            raise DeviceSafetyError('An MFA GUI instance is open; verify it is idle and close it before headless run')


class RuntimeLease:
    """原子占用共享登记表；停止状态不确定时禁止自动回收租约。"""

    def __init__(self, registry_root: Path, runtime_root: Path, boot_id: str, nonce: str):
        self.registry_root = registry_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.nonce = nonce
        self.boot_id = str(UUID(boot_id)).lower()
        root_key = os.path.normcase(str(self.runtime_root))
        self.paths = [self.registry_root / ('root-' + hashlib.sha256(root_key.encode()).hexdigest()),
                      self.registry_root / ('device-' + hashlib.sha256(self.boot_id.encode()).hexdigest())]
        self.acquired: list[Path] = []
        self.owner = {'schema': 2, 'nonce': self.nonce, 'pid': os.getpid(),
                      'created_at': psutil.Process(os.getpid()).create_time(),
                      'runtime_root': str(self.runtime_root), 'boot_id': self.boot_id}

    def acquire(self) -> None:
        self.registry_root.mkdir(parents=True, exist_ok=True)
        try:
            for path in self.paths:
                try:
                    path.mkdir()
                except FileExistsError as exc:
                    raise DeviceSafetyError('Device or runtime root already leased; uncertain/stale leases require review') from exc
                self.acquired.append(path)
                write_json(path / 'owner.json', self.owner)
        except BaseException:
            try:
                self._rollback_acquire()
            except Exception as exc:
                self.rollback_warning = type(exc).__name__
            raise

    def _rollback_acquire(self):
        # 只回滚本实例本次 mkdir 的路径；正式 release 仍要求全部完整 owner 证据。
        # 首次 owner 写盘失败可留下空目录，允许立刻回滚，绝不扫描/回收历史空锁。
        for path in reversed(tuple(self.acquired)):
            owner_file = path / 'owner.json'
            children = list(path.iterdir())
            if owner_file.exists():
                if _read_owner(path) != self.owner or any(child != owner_file for child in children):
                    raise DeviceSafetyError('Acquire rollback ownership changed; retaining path')
                owner_file.unlink()
            elif children:
                raise DeviceSafetyError('Acquire rollback path is not empty; retaining path')
            path.rmdir()
            self.acquired.remove(path)

    def release(self) -> None:
        # run_slot 尚未收束时先保留完整 owner，不能删掉 owner 后才发现目录非空。
        for path in self.acquired:
            if _read_owner(path) != self.owner:
                raise DeviceSafetyError('Lease owner changed; refusing to release')
            if any(child.name != 'owner.json' for child in path.iterdir()):
                raise DeviceSafetyError('Native run still holds the device lease')
        for path in reversed(self.acquired):
            owner_file = path / 'owner.json'
            if owner_file.exists():
                owner = json.loads(owner_file.read_text(encoding='utf-8'))
                if owner != self.owner:
                    raise DeviceSafetyError('Lease owner changed; refusing to release')
                owner_file.unlink()
            path.rmdir()
        self.acquired.clear()

    def descriptor(self) -> dict:
        if len(self.acquired) != 2 or any(_read_owner(path) != self.owner for path in self.paths):
            raise DeviceSafetyError('Runtime lease ownership is unavailable')
        return {'schema': 1, 'registry_root': str(self.registry_root), 'owner': dict(self.owner)}


NATIVE_CLOCK_TRIAL_ENV = 'MAABANGDREAM_NATIVE_OBSERVATION_CLOCK_TRIAL'
NATIVE_BORROW_RECEIPT_ENV = 'MAABANGDREAM_NATIVE_BORROW_RECEIPT'


def native_clock_trial_enabled() -> bool:
    return os.environ.get(NATIVE_CLOCK_TRIAL_ENV) == '1'


def shared_device_registry_root() -> Path:
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise DeviceSafetyError('LOCALAPPDATA is required for the shared device registry')
    return (Path(local) / 'MaaBanGDream' / 'device-runtime-registry').resolve()


def require_canonical_registry(registry: Path) -> None:
    if registry.resolve() != shared_device_registry_root():
        raise DeviceSafetyError('Native clock trial requires the canonical shared registry; prepare a fresh runtime binding')


def _read_owner(path: Path) -> dict:
    file = path / 'owner.json'
    if file.stat().st_size > 16384:
        raise DeviceSafetyError('Lease owner record oversized')
    value = json.loads(file.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise DeviceSafetyError('Lease owner record invalid')
    return value


def _process_matches(owner: dict) -> bool:
    pid, created = owner.get('pid'), owner.get('created_at')
    if type(pid) is not int or pid <= 0 or type(created) not in {int, float} or not math.isfinite(created):
        return False
    try:
        return psutil.Process(pid).create_time() == created
    except (psutil.Error, OSError):
        return False


def probe_native_boot_identity(adb_path, serial, *, cancelled=lambda: False, command=None) -> dict:
    """只在 Native 后台预武装前读取，不进入截图、jlog 或输入派发热路径。"""
    def query(parts):
        if cancelled():
            raise DeviceSafetyError('Native identity preparation cancelled')
        if command is not None:
            value = command(parts)
        else:
            result = subprocess.run([str(adb_path), '-s', serial, 'shell', '-T', *parts],
                capture_output=True, timeout=.25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode or len(result.stdout) > 4096 or len(result.stderr) > 4096:
                raise DeviceSafetyError('Native boot identity unavailable')
            value = result.stdout.decode('utf-8', 'strict').strip()
        return value
    first = (str(UUID(query(['cat', '/proc/sys/kernel/random/boot_id']))).lower(),
             query(['getprop', 'ro.build.fingerprint']))
    second = (str(UUID(query(['cat', '/proc/sys/kernel/random/boot_id']))).lower(),
              query(['getprop', 'ro.build.fingerprint']))
    if cancelled() or first != second or not first[1] or len(first[1]) > 1024:
        raise DeviceSafetyError('Native device identity changed while preparing')
    return {'boot_id': first[0], 'fingerprint': first[1]}


class NativeRunDeviceClaim:
    """同物理 boot 的唯一 Native run；明确借父 owner，不借用任意可见的登记。"""

    def __init__(self, runtime_root, boot_id, run_id, *, borrow=None, registry_root=None):
        self.registry = (Path(registry_root) if registry_root is not None else shared_device_registry_root()).resolve()
        self.runtime = Path(runtime_root).resolve()
        self.boot_id = str(UUID(boot_id)).lower()
        if type(run_id) is not str or not run_id or len(run_id) > 128:
            raise DeviceSafetyError('Invalid Native run identity')
        self.run_id, self.borrow = run_id, borrow
        self._lease = None
        self._parent_owner = self._slot_owner = None
        self._slot_acquired = False
        self._state, self._warning = 'new', None
        self._device = self.registry / ('device-' + hashlib.sha256(self.boot_id.encode()).hexdigest())
        root_key = os.path.normcase(str(self.runtime))
        self._root = self.registry / ('root-' + hashlib.sha256(root_key.encode()).hexdigest())
        self._slot = self._device / 'native-run-slot'

    def acquire(self):
        if self._state not in {'new', 'unavailable'}:
            raise DeviceSafetyError('Native claim cannot acquire twice')
        self._slot_acquired = False
        self._slot_owner = None
        try:
            if self.borrow is None:
                self._lease = RuntimeLease(self.registry, self.runtime, self.boot_id, uuid4().hex)
                self._lease.acquire()
                self._parent_owner = dict(self._lease.owner)
            else:
                descriptor = self.borrow
                if (not isinstance(descriptor, dict) or descriptor.get('schema') != 1
                        or Path(descriptor.get('registry_root', '')).resolve() != self.registry):
                    raise DeviceSafetyError('Explicit parent lease capability invalid')
                owner = descriptor.get('owner')
                if (not isinstance(owner, dict) or owner.get('schema') != 2 or not owner.get('nonce')
                        or owner.get('runtime_root') != str(self.runtime) or owner.get('boot_id') != self.boot_id
                        or not _process_matches(owner) or _read_owner(self._device) != owner):
                    raise DeviceSafetyError('Explicit parent lease identity mismatch')
                if _read_owner(self._root) != owner:
                    raise DeviceSafetyError('Explicit parent runtime lease mismatch')
                self._parent_owner = dict(owner)
            self._slot.mkdir()
            self._slot_acquired = True
            self._slot_owner = {'schema': 1, 'nonce': uuid4().hex, 'run_id': self.run_id,
                'pid': os.getpid(), 'created_at': psutil.Process(os.getpid()).create_time(),
                'parent_nonce': self._parent_owner['nonce'], 'boot_id': self.boot_id}
            write_json(self._slot / 'owner.json', self._slot_owner)
            self._state = 'active'
        except Exception:
            try:
                if self._slot_acquired:
                    file = self._slot / 'owner.json'
                    if file.exists() and _read_owner(self._slot) == self._slot_owner:
                        file.unlink()
                    if not any(self._slot.iterdir()):
                        self._slot.rmdir()
                        self._slot_acquired = False
                if self._lease is not None and self._lease.acquired:
                    self._lease.release()
            except Exception as exc:
                self._warning = 'acquire-rollback:' + type(exc).__name__
            self._state = 'unavailable'
            raise

    def current(self) -> bool:
        # 只供后台副作用前校验本地登记；实时消费者不读文件或调用本方法。
        if self._state not in {'active', 'cleanup-authorized'}:
            return False
        try:
            return (_read_owner(self._device) == self._parent_owner and _read_owner(self._root) == self._parent_owner
                    and _process_matches(self._parent_owner)
                    and _read_owner(self._slot) == self._slot_owner)
        except (OSError, ValueError, DeviceSafetyError):
            return False

    def stopping(self):
        if self._state == 'active':
            self._state = 'cleanup-authorized'

    def finish(self, *, input_released, observer_terminal) -> bool:
        if self._state == 'released':
            return True
        if not input_released or not observer_terminal:
            self._state = 'retained'
            return False
        try:
            if not self.current():
                raise DeviceSafetyError('Native run claim ownership changed; retaining claim')
            (self._slot / 'owner.json').unlink()
            self._slot.rmdir()
            self._slot_acquired = False
            if self._lease is not None:
                self._lease.release()
            self._state = 'released'
            return True
        except Exception as exc:
            self._warning = type(exc).__name__
            self._state = 'retained'
            return False

    def report(self):
        return {'state': self._state, 'borrowed': self.borrow is not None,
                'settled': self._state == 'released', 'warning': self._warning}
