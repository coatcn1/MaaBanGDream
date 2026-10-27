"""借用本轮 Native 租约的独立时钟 helper；所有 ADB/RPC/清理仅在后台。"""

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import queue
import re
import secrets
import socket
import subprocess
import threading
import time

from .native_clock_assets import ASSET_ROOT, validate_assets
from .native_clock_rpc import ClockRpcClient, WireIdentity
from .native_observation_clock import ClockIdentity, ObservationClockMapper


class ClockObserverError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HelperSession:
    generation: int
    public_nonce: str
    auth_nonce: bytes = field(repr=False)

    def __post_init__(self):
        if (type(self.generation) is not int or not 0 < self.generation < 2**64
                or type(self.public_nonce) is not str or re.fullmatch(r"[0-9a-f]{32}", self.public_nonce) is None
                or type(self.auth_nonce) is not bytes or len(self.auth_nonce) != 16):
            raise ValueError("invalid clock helper session")

    @classmethod
    def fresh(cls):
        return cls(secrets.randbelow(2**64 - 1) + 1, secrets.token_hex(16), secrets.token_bytes(16))

    @property
    def remote(self):
        return f"/data/local/tmp/mbd-clock-g{self.generation}-n{self.public_nonce}"

    @property
    def socket_name(self):
        return f"mbd.clock.v1.g{self.generation}.n{self.public_nonce}"


@dataclass(frozen=True, slots=True)
class OwnedClockProcess:
    pid: int
    start_ticks: int
    cmdline: tuple[str, ...]
    executable: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ObserverStatus:
    state: str = "idle"
    reason: str | None = None
    generation: int | None = None
    pid: int | None = None
    samples: int = 0
    qualified_samples: int = 0
    authenticated_stop: bool = False
    eof_requested: bool = False
    process_gone: bool = False
    host_process_exited: bool = False
    forward_clean: bool = False
    file_clean: bool = False
    cleanup_warning: str | None = None
    cleanup_deferred: bool = False


def parse_ready(line, session):
    # 只兼容 LF 和末尾单 CRLF；未知输出、密钥及原始正文都不能进入异常/日志。
    if line.endswith(b"\r\n") and not line.endswith(b"\r\r\n"):
        body = line[:-2]
    elif line.endswith(b"\n") and not line.endswith(b"\r\n"):
        body = line[:-1]
    else:
        raise ClockObserverError("clock ready terminator mismatch")
    pattern = (rf"clock-rpc-v1 pid=([1-9][0-9]{{0,9}}) generation={session.generation} "
               rf"socket={re.escape(session.socket_name)}").encode("ascii")
    match = re.fullmatch(pattern, body)
    if match is None or not 0 < int(match[1]) < 2**31:
        raise ClockObserverError("clock ready identity mismatch")
    return int(match[1])


def parse_start_ticks(raw, pid):
    text = raw.decode("ascii", "strict").strip()
    if not text.startswith(f"{pid} (") or ") " not in text:
        raise ClockObserverError("clock process stat mismatch")
    fields = text[text.rfind(") ") + 2:].split()
    if len(fields) < 20 or not fields[19].isdigit() or not 0 < int(fields[19]) < 2**64:
        raise ClockObserverError("clock process starttime unavailable")
    return int(fields[19])


def parse_remote_sha(raw):
    match = re.fullmatch(rb"([0-9a-f]{64})[ \t]+[^\r\n]+\r?\n?", raw)
    if match is None:
        raise ClockObserverError("clock process hash unavailable")
    return match[1].decode("ascii")


def parse_forwards(raw):
    rows = []
    for line in raw.decode("ascii", "strict").splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ClockObserverError("clock forward list malformed")
        rows.append(tuple(fields))
    return rows


class ClockAdbRoute:
    def __init__(self, adb_path, serial):
        if not adb_path or type(serial) is not str or not serial:
            raise ValueError("clock observer requires explicit ADB route")
        self.adb_path, self.serial = str(adb_path), serial

    def call(self, *args, allowed=(0,), timeout_s=1):
        result = subprocess.run([self.adb_path, "-s", self.serial, *args], capture_output=True,
            timeout=timeout_s, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode not in allowed or len(result.stdout) > 65536 or len(result.stderr) > 65536:
            raise ClockObserverError("clock ADB command unavailable")
        return result.returncode, result.stdout

    def launch(self, session):
        return subprocess.Popen([self.adb_path, "-s", self.serial, "shell", "-T", session.remote,
            str(session.generation), session.public_nonce], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class _ReadyReader:
    def __init__(self, process):
        self.process = process
        self.result = queue.Queue(maxsize=1)
        self.threads = [threading.Thread(target=self._read, daemon=True, name="clock-ready"),
                        threading.Thread(target=self._discard, daemon=True, name="clock-stderr")]
        for thread in self.threads:
            thread.start()

    def _read(self):
        try:
            line = bytearray()
            while len(line) < 512:
                value = self.process.stdout.read(1)
                if not value:
                    break
                line.extend(value)
                if value == b"\n":
                    self.result.put_nowait(bytes(line))
                    return
        except (OSError, ValueError):
            pass
        self.result.put_nowait(b"")

    def _discard(self):
        try:
            while self.process.stderr.read(256):
                pass
        except (OSError, ValueError):
            pass


class NativeClockObserver:
    """不获得或释放租约，也不提供输入；lease_current 必须是本地缓存校验。"""

    def __init__(self, run_id, *, lease_current, adb_path=None, serial=None, adb=None,
                 assets_root=ASSET_ROOT, socket_factory=socket.create_connection, cleanup_allowed=None):
        if (type(run_id) is not str or not 1 <= len(run_id) <= 128
                or any(ord(value) < 32 for value in run_id) or not callable(lease_current)):
            raise ValueError("clock observer requires run and current Native lease")
        self.run_id, self.lease_current = run_id, lease_current
        self.adb = adb if adb is not None else ClockAdbRoute(adb_path, serial)
        self.assets_root, self.socket_factory = Path(assets_root), socket_factory
        self.session = HelperSession.fresh()
        self._status = ObserverStatus(generation=self.session.generation)
        self._stop, self._closed = threading.Event(), threading.Event()
        self._thread = self._client = self._mapper = self._process = self._reader = self._owned = None
        self._port = self._binary_sha = None
        self._upload_attempted = self._launch_attempted = self._forward_attempted = False
        self._cleaning = False
        self._lifecycle_lock = threading.Lock()
        self._cleanup_deadline = None
        self._cleanup_allowed = cleanup_allowed
        self._unavailable = threading.Event()

    def start(self):
        # 此锁只覆盖本地线程发布，不覆盖 ADB/RPC；并发 start 不创建第二个同名 helper。
        with self._lifecycle_lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._status = replace(self._status, state="starting")
            try:
                self._thread = threading.Thread(target=self._run, name="native-clock-observer", daemon=True)
                self._thread.start()
            except Exception as exc:
                self._stop.set()
                self._close_unstarted(type(exc).__name__)

    def _close_unstarted(self, reason=None):
        self._status = replace(self._status, state="stopped", reason=reason, process_gone=True,
                               host_process_exited=True, forward_clean=True, file_clean=True)
        self._closed.set()

    def snapshot(self):
        # 热路径只读引用；停止意图立即撤销资格，不等后台 RPC 或 ADB 清理。
        mapper = self._mapper
        return (mapper.snapshot() if mapper is not None and not self._stop.is_set()
                and not self._unavailable.is_set() and not self._closed.is_set() else None)

    def report(self):
        return dict(run_id=self.run_id, **asdict(self._status), closed=self._closed.is_set())

    def request_stop(self):
        self._stop.set()
        client = self._client
        if client is not None:
            client.close()
        # 停止不等待 start 锁；正在启动的 worker 会在首次副作用前读取停止标记。
        if self._lifecycle_lock.acquire(False):
            try:
                if self._thread is None:
                    self._close_unstarted()
            finally:
                self._lifecycle_lock.release()

    def wait_closed(self, timeout=0):
        """只供非实时清理调用方/测试等待；截图与输入路径不得调用。"""
        return self._closed.wait(timeout=max(0, min(float(timeout), 10)))

    def _guard(self):
        if self.lease_current() is not True:
            raise ClockObserverError("clock Native lease unavailable")
        if not self._cleaning and self._stop.is_set():
            raise ClockObserverError("clock observer stopped")

    def _call(self, *args, allowed=(0,)):
        self._guard()
        remaining = self._cleanup_remaining() if self._cleaning else 1.0
        return self.adb.call(*args, allowed=allowed, timeout_s=min(1.0, remaining))

    def _cleanup_remaining(self):
        remaining = self._cleanup_deadline - time.monotonic()
        if remaining <= 0:
            raise ClockObserverError("clock helper cleanup budget exhausted")
        return remaining

    def _shell(self, *args, allowed=(0,)):
        return self._call("shell", "-T", *args, allowed=allowed)

    def _inspect_owned(self, pid):
        first = parse_start_ticks(self._shell("cat", f"/proc/{pid}/stat")[1], pid)
        cmdline = self._shell("cat", f"/proc/{pid}/cmdline")[1]
        expected = (self.session.remote, str(self.session.generation), self.session.public_nonce)
        if cmdline != b"\0".join(value.encode("ascii") for value in expected) + b"\0":
            raise ClockObserverError("clock process argv mismatch")
        executable = self._shell("readlink", f"/proc/{pid}/exe")[1].decode("ascii", "strict").rstrip("\r\n")
        actual_sha = parse_remote_sha(self._shell("sha256sum", f"/proc/{pid}/exe")[1])
        last = parse_start_ticks(self._shell("cat", f"/proc/{pid}/stat")[1], pid)
        if first != last or executable != self.session.remote or actual_sha != self._binary_sha:
            raise ClockObserverError("clock process identity mismatch")
        return OwnedClockProcess(pid, first, expected, executable, actual_sha)

    def _setup(self):
        self._guard()
        manifest = validate_assets(self.assets_root)
        abi = self._shell("getprop", "ro.product.cpu.abi")[1].decode("ascii", "strict").strip()
        entry = next((row for row in manifest["binaries"] if row["abi"] == abi), None)
        if entry is None:
            raise ClockObserverError("clock helper device ABI unsupported")
        self._binary_sha = entry["sha256"]
        if self._shell("test", "-e", self.session.remote, allowed=(0, 1))[0] != 1:
            raise ClockObserverError("clock unique remote file already exists")
        self._upload_attempted = True
        self._call("push", str(self.assets_root / entry["file"]), self.session.remote)
        if parse_remote_sha(self._shell("sha256sum", self.session.remote)[1]) != self._binary_sha:
            raise ClockObserverError("clock uploaded helper hash mismatch")
        self._shell("chmod", "700", self.session.remote)
        self._guard()
        self._launch_attempted = True
        self._process = self.adb.launch(self.session)
        self._reader = _ReadyReader(self._process)
        self._process.stdin.write(self.session.auth_nonce.hex().encode("ascii") + b"\n")
        self._process.stdin.flush()
        ready_deadline = time.monotonic() + 3
        while True:
            self._guard()
            remaining = ready_deadline - time.monotonic()
            if remaining <= 0:
                raise ClockObserverError("clock ready timeout")
            try:
                line = self._reader.result.get(timeout=min(.05, remaining))
                break
            except queue.Empty:
                pass
        pid = parse_ready(line, self.session)
        self._owned = self._inspect_owned(pid)
        self._status = replace(self._status, pid=pid)
        remote = "localabstract:" + self.session.socket_name
        if any(row[2] == remote for row in parse_forwards(self._call("forward", "--list")[1])):
            raise ClockObserverError("clock unique forward already exists")
        self._forward_attempted = True
        raw_port = self._call("forward", "tcp:0", remote)[1].strip()
        if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
            raise ClockObserverError("clock allocated forward port unavailable")
        self._port = int(raw_port)
        expected = (self.adb.serial, f"tcp:{self._port}", remote)
        if parse_forwards(self._call("forward", "--list")[1]).count(expected) != 1:
            raise ClockObserverError("clock forward identity mismatch")
        self._guard()
        connection = self.socket_factory(("127.0.0.1", self._port), timeout=1)
        identity = ClockIdentity(self.run_id, self.session.generation, pid, self._owned.start_ticks)
        self._mapper = ObservationClockMapper(identity)
        self._client = ClockRpcClient(connection, WireIdentity(identity, self.session.auth_nonce))

    def _remove_forward(self):
        if not self._forward_attempted:
            return
        rows = parse_forwards(self._call("forward", "--list")[1])
        remote = "localabstract:" + self.session.socket_name
        owned = [row for row in rows if row[0] == self.adb.serial and row[2] == remote]
        if not owned:
            return
        if len(owned) != 1 or not re.fullmatch(r"tcp:[1-9][0-9]{0,4}", owned[0][1]):
            raise ClockObserverError("clock forward ownership ambiguous")
        if self._port is not None and owned[0][1] != f"tcp:{self._port}":
            raise ClockObserverError("clock forward ownership changed")
        port = owned[0][1]
        self._call("forward", "--remove", port)
        if any(row[0] == self.adb.serial and row[2] == remote
               for row in parse_forwards(self._call("forward", "--list")[1])):
            raise ClockObserverError("clock forward cleanup unconfirmed")

    def _cleanup(self):
        self._cleaning = True
        self._cleanup_deadline = time.monotonic() + 2.0
        client = self._client
        if client is not None:
            client.close()
        self._status = replace(self._status, eof_requested=client is not None,
                               authenticated_stop=bool(client and client.authenticated_stop))
        try:
            process_gone, host_exited = not self._launch_attempted, self._process is None
            if self._process is not None:
                try:
                    self._process.wait(timeout=min(.3, self._cleanup_remaining()))
                except subprocess.TimeoutExpired:
                    if self._owned is None or self._inspect_owned(self._owned.pid) != self._owned:
                        raise ClockObserverError("clock PID ownership unavailable; refusing signal")
                    self._shell("kill", "-TERM", str(self._owned.pid))
                    self._process.wait(timeout=self._cleanup_remaining())
                host_exited = self._process.poll() is not None
                if self._owned is None:
                    raise ClockObserverError("clock remote exit lacks PID evidence")
                process_gone = self._shell("test", "-e", f"/proc/{self._owned.pid}", allowed=(0, 1))[0] == 1
                if not process_gone:
                    raise ClockObserverError("clock owned process exit unconfirmed")
                for thread in self._reader.threads:
                    thread.join(timeout=min(.2, self._cleanup_remaining()))
                if any(thread.is_alive() for thread in self._reader.threads):
                    raise ClockObserverError("clock owned pipe reader exit unconfirmed")
                for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                    stream.close()
            self._status = replace(self._status, process_gone=process_gone, host_process_exited=host_exited)
            self._remove_forward()
            self._status = replace(self._status, forward_clean=True)
            if self._upload_attempted:
                if not process_gone or parse_remote_sha(self._shell("sha256sum", self.session.remote)[1]) != self._binary_sha:
                    raise ClockObserverError("clock file ownership changed; refusing removal")
                self._shell("rm", self.session.remote)
                if self._shell("test", "-e", self.session.remote, allowed=(0, 1))[0] != 1:
                    raise ClockObserverError("clock owned file cleanup unconfirmed")
            self._status = replace(self._status, file_clean=True)
        except Exception as exc:
            # 此处只记录 helper 附加清理失败；不能改写 Native 输入完成/触点释放或租约。
            self._status = replace(self._status, cleanup_warning=type(exc).__name__)

    def _run(self):
        try:
            self._setup()
            self._status = replace(self._status, state="running")
            while not self._stop.is_set():
                self._guard()
                sample = self._client.query()
                qualified = self._mapper.observe(sample)
                self._status = replace(self._status, samples=self._status.samples + 1,
                    qualified_samples=self._status.qualified_samples + int(qualified))
                # 错过的时隙不补 burst；单 socket 始终只有一个在途请求。
                if self._stop.wait(.05):
                    break
        except Exception as exc:
            if not self._stop.is_set():
                self._status = replace(self._status, reason=type(exc).__name__)
        finally:
            # 失效与本地RPC关闭立即执行；ADB清理只在输入stop尝试结束后开始计预算。
            self._unavailable.set()
            try:
                if self._mapper is not None:
                    self._mapper.invalidate("stopped" if self._stop.is_set() else "observer-unavailable")
                client = self._client
                if client is not None:
                    try:
                        client.close()
                    except Exception as exc:
                        self._status = replace(self._status, cleanup_warning=type(exc).__name__)
                deferred = self._cleanup_allowed is not None and not self._cleanup_allowed.is_set()
                self._status = replace(self._status, state="cleanup-deferred" if deferred else "degraded",
                                       cleanup_deferred=deferred, eof_requested=client is not None)
                if self._cleanup_allowed is not None:
                    self._cleanup_allowed.wait()
                self._status = replace(self._status, cleanup_deferred=False)
                self._cleanup()
            except Exception as exc:
                self._status = replace(self._status, cleanup_warning=self._status.cleanup_warning or type(exc).__name__)
            finally:
                self._status = replace(self._status, state="stopped", cleanup_deferred=False)
                self._closed.set()
