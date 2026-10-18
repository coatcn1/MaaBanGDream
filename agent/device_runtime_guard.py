"""隔离开发运行器的宿主路由核验与默认拒绝不确定状态的租约。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
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

    def acquire(self) -> None:
        self.registry_root.mkdir(parents=True, exist_ok=True)
        try:
            for path in self.paths:
                try:
                    path.mkdir()
                except FileExistsError as exc:
                    raise DeviceSafetyError('Device or runtime root already leased; uncertain/stale leases require review') from exc
                self.acquired.append(path)
                write_json(path / 'owner.json', {'nonce': self.nonce, 'pid': os.getpid(),
                           'runtime_root': str(self.runtime_root), 'boot_id': self.boot_id})
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        for path in reversed(self.acquired):
            owner_file = path / 'owner.json'
            if owner_file.exists():
                owner = json.loads(owner_file.read_text(encoding='utf-8'))
                if owner.get('nonce') != self.nonce:
                    raise DeviceSafetyError('Lease owner changed; refusing to release')
                owner_file.unlink()
            path.rmdir()
        self.acquired.clear()
