import io
from dataclasses import replace
import socket
import subprocess
import threading
import time

import pytest

from agent.realtime.native_clock_observer import (
    ClockObserverError, HelperSession, NativeClockObserver, parse_ready, parse_start_ticks,
    parse_forwards,
)
from agent.realtime.native_clock_rpc import WIRE
from test_native_clock_assets import assets


class Process:
    def __init__(self, route, session):
        self.route, self.session = route, session
        self.stdin, self.stderr = io.BytesIO(), io.BytesIO(b"private stderr must disappear")
        self.stdout = io.BytesIO((f"clock-rpc-v1 pid=42 generation={session.generation} "
                                  f"socket={session.socket_name}\r\n").encode("ascii"))
        self.alive = True

    def wait(self, timeout):
        if self.route.cleanup_identity_changed:
            self.route.cleanup_phase = True
            raise subprocess.TimeoutExpired("owned-clock", timeout)
        self.alive = False
        return 1

    def poll(self):
        return None if self.alive else 1


class Adb:
    serial = "test-clock-route"

    def __init__(self, sha):
        self.sha, self.calls, self.rows = sha, [], [("foreign", "tcp:40000", "localabstract:foreign")]
        self.file, self.process, self.session = False, None, None
        self.cleanup_identity_changed = self.cleanup_phase = False
        self.file_hash_changed = False

    def launch(self, session):
        self.calls.append(("launch", session.remote, str(session.generation), session.public_nonce))
        self.session = session
        self.process = Process(self, session)
        return self.process

    def call(self, *args, allowed=(0,), timeout_s=1):
        self.calls.append(args)
        if args[0] == "push":
            self.file = True
            return 0, b""
        if args[:2] == ("forward", "--list"):
            return 0, ("\n".join(" ".join(row) for row in self.rows) + "\n").encode("ascii")
        if args[:2] == ("forward", "tcp:0"):
            self.rows.append((self.serial, "tcp:40123", args[2]))
            return 0, b"40123\n"
        if args[:2] == ("forward", "--remove"):
            self.rows = [row for row in self.rows if row[1] != args[2]]
            return 0, b""
        assert args[:2] == ("shell", "-T")
        command = args[2:]
        if command[:2] == ("getprop", "ro.product.cpu.abi"):
            return 0, b"x86_64\n"
        if command[:2] == ("test", "-e"):
            exists = self.file if not command[2].startswith("/proc/") else bool(self.process and self.process.alive)
            return (0 if exists else 1), b""
        if command[0] == "sha256sum":
            value = "0" * 64 if self.file_hash_changed and not command[1].startswith("/proc/") else self.sha
            return 0, (value + "  " + command[1] + "\n").encode("ascii")
        if command[0] == "chmod":
            return 0, b""
        if command[0] == "cat" and command[1].endswith("/stat"):
            fields = ["S"] + ["0"] * 18 + ["999" if self.cleanup_phase else "100"]
            return 0, ("42 (mbd-clock) " + " ".join(fields)).encode("ascii")
        if command[0] == "cat" and command[1].endswith("/cmdline"):
            return 0, b"\0".join(value.encode("ascii") for value in (
                self.session.remote, str(self.session.generation), self.session.public_nonce)) + b"\0"
        if command[0] == "readlink":
            return 0, self.session.remote.encode("ascii") + b"\n"
        if command[:2] == ("kill", "-TERM"):
            self.process.alive = False
            return 0, b""
        if command[0] == "rm":
            self.file = False
            return 0, b""
        raise AssertionError(command)


def socket_fixture():
    client, server = socket.socketpair()
    requests = []
    def serve():
        try:
            while True:
                data = bytearray()
                while len(data) < 128:
                    part = server.recv(128 - len(data))
                    if not part:
                        return
                    data.extend(part)
                values = list(WIRE.unpack(data))
                requests.append(values)
                device_ns = time.perf_counter_ns() + 100_000_000_000
                values[2] |= 0x8000
                values[12:16] = (device_ns, device_ns, device_ns - 1_000_000_000, device_ns - 1_000_000_000)
                server.sendall(WIRE.pack(*values))
        except OSError:
            pass
        finally:
            server.close()
    thread = threading.Thread(target=serve)
    thread.start()
    return (lambda *_args, **_kwargs: client), requests, thread


def wait_for_samples(observer, minimum=4):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if observer.report()["samples"] >= minimum:
            return
        if observer.report()["closed"]:
            break
        time.sleep(.005)
    raise AssertionError(observer.report())


def test_background_helper_borrows_lease_has_unique_assets_and_precise_eof_cleanup(tmp_path):
    manifest = assets(tmp_path)
    route = Adb(next(row["sha256"] for row in manifest["binaries"] if row["abi"] == "x86_64"))
    connect, requests, server_thread = socket_fixture()
    guards = []
    observer = NativeClockObserver("native-run", lease_current=lambda: guards.append(threading.get_ident()) or True,
                                   adb=route, assets_root=tmp_path, socket_factory=connect)
    assert not route.calls and observer.snapshot() is None
    owner_thread = threading.get_ident()
    observer.start()
    wait_for_samples(observer)
    snapshot = observer.snapshot()
    assert snapshot.identity.run_id == "native-run" and snapshot.identity.start_ticks == 100
    assert snapshot.reason is None
    assert guards and all(thread != owner_thread for thread in guards)
    assert route.process.stdin.getvalue() == observer.session.auth_nonce.hex().encode("ascii") + b"\n"
    assert all(observer.session.auth_nonce.hex() not in repr(call) for call in route.calls)
    before = time.perf_counter()
    observer.request_stop()
    assert time.perf_counter() - before < .05
    assert observer.snapshot() is None
    assert observer.wait_closed(3)
    report = observer.report()
    assert all(report[key] for key in ("process_gone", "host_process_exited", "forward_clean", "file_clean", "eof_requested"))
    assert report["cleanup_warning"] is None and not report["authenticated_stop"]
    assert not route.file and route.rows == [("foreign", "tcp:40000", "localabstract:foreign")]
    assert not any("lease_released" in key or "release_confirmed" in key for key in report)
    assert requests and all(row[6] == observer.session.generation for row in requests)
    server_thread.join(1)
    assert not server_thread.is_alive()


def test_missing_or_lost_native_lease_cannot_start_or_clean_other_device(tmp_path):
    manifest = assets(tmp_path)
    route = Adb(next(row["sha256"] for row in manifest["binaries"] if row["abi"] == "x86_64"))
    observer = NativeClockObserver("native-run", lease_current=lambda: False, adb=route, assets_root=tmp_path)
    observer.start()
    assert observer.wait_closed(1)
    assert not route.calls and observer.snapshot() is None
    with pytest.raises(ValueError):
        NativeClockObserver("native-run", lease_current=None, adb=route)


@pytest.mark.parametrize("failure", ["pid-reused", "file-changed", "lease-lost"])
def test_cleanup_mismatch_never_signals_or_removes_foreign_resources(tmp_path, failure):
    manifest = assets(tmp_path)
    route = Adb(next(row["sha256"] for row in manifest["binaries"] if row["abi"] == "x86_64"))
    connect, _, thread = socket_fixture()
    lease = [True]
    observer = NativeClockObserver("native-run", lease_current=lambda: lease[0], adb=route,
                                   assets_root=tmp_path, socket_factory=connect)
    observer.start()
    wait_for_samples(observer)
    if failure == "pid-reused":
        route.cleanup_identity_changed = True
    elif failure == "file-changed":
        route.file_hash_changed = True
    else:
        lease[0] = False
    observer.request_stop()
    assert observer.wait_closed(3)
    assert observer.snapshot() is None and observer.report()["cleanup_warning"] is not None
    assert not any(call[:3] == ("shell", "-T", "kill") for call in route.calls)
    assert not any(call[:3] == ("shell", "-T", "rm") for call in route.calls)
    assert route.file
    thread.join(1)
    assert not thread.is_alive()


@pytest.mark.parametrize("terminator", [b"\n", b"\r\n", b"\r\r\n", b"", b" \n"])
def test_ready_strictly_accepts_only_lf_or_single_crlf_without_logging_raw(terminator):
    session = HelperSession.fresh()
    line = (f"clock-rpc-v1 pid=42 generation={session.generation} socket={session.socket_name}").encode("ascii")
    if terminator in {b"\n", b"\r\n"}:
        assert parse_ready(line + terminator, session) == 42
    else:
        with pytest.raises(ClockObserverError):
            parse_ready(line + terminator, session)
    with pytest.raises(ClockObserverError) as captured:
        parse_ready(session.auth_nonce.hex().encode("ascii") + b"\n", session)
    assert session.auth_nonce.hex() not in str(captured.value)


def test_proc_starttime_keeps_parentheses_in_comm_and_rejects_invalid_pid():
    raw = ("42 (name with ) parentheses) S " + "0 " * 18 + "123").encode("ascii")
    assert parse_start_ticks(raw, 42) == 123
    with pytest.raises(ClockObserverError):
        parse_start_ticks(raw, 43)


@pytest.mark.parametrize("raw", [b"", b"\n", b"\r\n", b" \t\r\n"])
def test_forward_list_empty_or_blank_rows_are_valid(raw):
    assert parse_forwards(raw) == []


@pytest.mark.parametrize("tail", [b"\n", b"\n\n", b"\r\n\r\n", b"\n \t\n"])
def test_forward_list_preserves_exact_three_columns_and_accepts_trailing_blank_rows(tail):
    assert parse_forwards(b"serial tcp:1 localabstract:clock" + tail) == [("serial", "tcp:1", "localabstract:clock")]
    with pytest.raises(ClockObserverError):
        parse_forwards(b"serial tcp:1 localabstract:clock extra" + tail)


@pytest.mark.parametrize("fields", [dict(generation=0), dict(generation=True), dict(generation=2**64),
    dict(public_nonce="A" * 32), dict(public_nonce="a" * 31), dict(auth_nonce=b"secret"), dict(auth_nonce="a" * 16)])
def test_helper_session_constructor_rejects_invalid_scope_without_secret_echo(fields):
    with pytest.raises(ValueError, match="invalid clock helper session"):
        replace(HelperSession.fresh(), **fields)


def test_stop_before_start_and_repeated_stop_never_creates_a_worker_or_adb(tmp_path):
    manifest = assets(tmp_path)
    route = Adb(next(row["sha256"] for row in manifest["binaries"] if row["abi"] == "x86_64"))
    observer = NativeClockObserver("native-run", lease_current=lambda: True, adb=route, assets_root=tmp_path)
    observer.request_stop()
    observer.request_stop()
    observer.start()
    assert observer.wait_closed(0) and not route.calls and observer._thread is None
    assert observer.report()["state"] == "stopped" and observer.report()["process_gone"]


def test_thread_start_failure_is_optional_and_has_no_device_effects(tmp_path, monkeypatch):
    manifest = assets(tmp_path)
    route = Adb(next(row["sha256"] for row in manifest["binaries"] if row["abi"] == "x86_64"))
    observer = NativeClockObserver("native-run", lease_current=lambda: True, adb=route, assets_root=tmp_path)
    def fail(_thread):
        raise OSError("private thread implementation details")
    monkeypatch.setattr(threading.Thread, "start", fail)
    observer.start()
    observer.start()
    observer.request_stop()
    assert observer.wait_closed(0) and not route.calls
    assert observer.report()["reason"] == "OSError"
    assert "private" not in str(observer.report())


def test_start_stop_race_only_publishes_one_worker_and_no_late_device_setup(tmp_path, monkeypatch):
    manifest = assets(tmp_path)
    route = Adb(next(row["sha256"] for row in manifest["binaries"] if row["abi"] == "x86_64"))
    observer = NativeClockObserver("native-run", lease_current=lambda: True, adb=route, assets_root=tmp_path)
    entered, continue_setup = threading.Event(), threading.Event()
    owners = []
    def setup():
        owners.append(threading.get_ident())
        entered.set()
        assert continue_setup.wait(1)
        observer._guard()
        raise AssertionError("stopped worker must not continue setup")
    monkeypatch.setattr(observer, "_setup", setup)
    starters = [threading.Thread(target=observer.start) for _ in range(6)]
    for thread in starters:
        thread.start()
    assert entered.wait(1)
    observer.request_stop()
    continue_setup.set()
    for thread in starters:
        thread.join(1)
    assert observer.wait_closed(1)
    assert len(owners) == 1 and not route.calls and observer.snapshot() is None


def test_stop_inflight_wire_is_nonblocking_and_does_not_fake_authenticated_stop(tmp_path):
    manifest = assets(tmp_path)
    route = Adb(next(row["sha256"] for row in manifest["binaries"] if row["abi"] == "x86_64"))
    client, server = socket.socketpair()
    requested = threading.Event()
    def serve():
        try:
            server.recv(128)
            requested.set()
            assert server.recv(1) == b""
        finally:
            server.close()
    thread = threading.Thread(target=serve)
    thread.start()
    observer = NativeClockObserver("native-run", lease_current=lambda: True, adb=route, assets_root=tmp_path,
                                   socket_factory=lambda *_args, **_kwargs: client)
    observer.start()
    assert requested.wait(1)
    before = time.perf_counter()
    observer.request_stop()
    assert time.perf_counter() - before < .05
    assert observer.wait_closed(3)
    assert observer.report()["eof_requested"] and not observer.report()["authenticated_stop"]
    thread.join(1)
    assert not thread.is_alive()


def test_cleanup_has_one_global_2s_budget_and_never_starts_late_adb(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent.realtime import native_clock_observer as module
    now = [0.]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    calls = []
    observer = NativeClockObserver('native-run', lease_current=lambda: True, adb=SimpleNamespace(serial='route'),
                                   assets_root=tmp_path)
    observer._forward_attempted = observer._upload_attempted = True
    def command(*args, allowed=(0,), timeout_s=1):
        calls.append((args, timeout_s))
        assert args == ('forward', '--list') and 0 < timeout_s <= 1
        now[0] = 2.01
        return 0, f'route tcp:40123 localabstract:{observer.session.socket_name}\n'.encode('ascii')
    observer.adb.call = command
    observer._cleanup()
    assert len(calls) == 1 and observer.report()['cleanup_warning'] == 'ClockObserverError'
    assert not observer.report()['forward_clean'] and not observer.report()['file_clean']


@pytest.mark.parametrize('gated', [False, True])
def test_wire_failure_invalidates_immediately_and_cleans_only_after_gate(gated):
    from types import SimpleNamespace
    from agent.realtime.native_clock_rpc import ClockWireError
    gate = threading.Event() if gated else None
    observer = NativeClockObserver('native-run', lease_current=lambda: True,
                                   adb=SimpleNamespace(serial='fake-route'), cleanup_allowed=gate)
    invalidated, closed, cleaned = threading.Event(), threading.Event(), threading.Event()
    calls = []
    class Mapper:
        def invalidate(self, reason):
            calls.append(('invalidate', reason))
            invalidated.set()
        def snapshot(self):
            return object()
    class Client:
        def query(self):
            raise ClockWireError('injected transport failure')
        def close(self):
            closed.set()
    observer._mapper = Mapper()
    observer._client = Client()
    observer._setup = lambda: None
    def cleanup():
        calls.append(('cleanup', None))
        cleaned.set()
    observer._cleanup = cleanup
    observer.start()
    try:
        assert invalidated.wait(1) and closed.wait(1)
        assert observer.snapshot() is None
        if gated:
            assert not cleaned.wait(.03)
            assert not observer.wait_closed(0)
            assert observer.report()['cleanup_deferred']
            assert observer.report()['reason'] == 'ClockWireError'
            observer.request_stop()
            observer.request_stop()
            assert not cleaned.is_set()
            gate.set()
        assert observer.wait_closed(1)
        assert calls.count(('cleanup', None)) == 1
        assert observer.report()['reason'] == 'ClockWireError'
    finally:
        if gate is not None:
            gate.set()
        observer.request_stop()
        observer.wait_closed(1)


def test_cleanup_failure_still_marks_worker_terminal_after_gate():
    from types import SimpleNamespace
    gate = threading.Event()
    gate.set()
    observer = NativeClockObserver('native-run', lease_current=lambda: True,
        adb=SimpleNamespace(serial='fake-route'), cleanup_allowed=gate)
    observer._setup = lambda: (_ for _ in ()).throw(ClockObserverError('setup failed'))
    observer._cleanup = lambda: (_ for _ in ()).throw(OSError('cleanup failed'))
    observer.start()
    assert observer.wait_closed(1)
    assert observer.report()['reason'] == 'ClockObserverError'
    assert observer.report()['cleanup_warning'] == 'OSError'
    assert not observer.report()['cleanup_deferred']
    assert observer.snapshot() is None
