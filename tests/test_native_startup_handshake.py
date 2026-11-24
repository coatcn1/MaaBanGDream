import io
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from agent.device_runtime_guard import DeviceSafetyError, NativeRunDeviceClaim
from agent.realtime import native_engine, native_minitouch
from test_native_claim_lifecycle import BOOT, backend_fixture


@pytest.fixture
def minitouch_transport(monkeypatch):
    def setup(capabilities, pid_line="$ 3422\n"):
        state = SimpleNamespace(
            offset=0.0, published=[], cleanup_calls=[], fault=None, on_receive=lambda: None,
            pid=pid_line.strip().split()[-1] if pid_line.strip() else None,
        )

        class Client:
            connected = False

            def connect(self, _host, _port):
                self.connected = True
                return True

            def receive(self, _size, _timeout):
                state.on_receive()
                state.offset += 2.1
                return f"v 1\n{capabilities}\n{pid_line}"

            def close(self):
                self.connected = False

            def publish(self, payload):
                state.published.append(payload)
                return True

        class Process:
            stderr = io.StringIO("touch device\n")

            def poll(self):
                return None

            def kill(self):
                pass

            def wait(self, *, timeout):
                return 0

        def adb(*args, **kwargs):
            if args[:2] == ("shell", "getprop"):
                return "x86_64"
            if args[:2] == ("shell", "ls"):
                return native_minitouch._DEVICE_BINARY
            return ""

        device = native_minitouch.NativeMinitouchDevice("offline-adb", "offline-device")

        def adb_cleanup(*args, timeout_s, expected_line=None):
            state.cleanup_calls.append((args, expected_line))
            if expected_line == "startup-process-closed":
                assert BOOT in args[1]
                assert f"{native_minitouch._DEVICE_BINARY} -n {device._socket_name} " in args[1]
                assert f"/proc/{state.pid}/cmdline" in args[1]
                assert f"while [ -d /proc/{state.pid} ]" in args[1]
                return state.fault != "remote"
            if args == ("forward", "--list"):
                assert expected_line == f"offline-device tcp:15001 localabstract:{device._socket_name}"
                return state.fault != "foreign-forward"
            return state.fault != "forward-removal"

        monkeypatch.setattr(native_minitouch, "time", SimpleNamespace(
            monotonic=lambda: time.monotonic() + state.offset,
            perf_counter=time.perf_counter,
            sleep=time.sleep,
        ))
        monkeypatch.setattr(native_minitouch, "native_engine", SimpleNamespace(
            available=lambda: True,
            minitouch_client=Client,
        ))
        monkeypatch.setattr(native_minitouch, "subprocess", SimpleNamespace(
            Popen=lambda *args, **kwargs: Process(), PIPE=-1, DEVNULL=-3,
        ))
        monkeypatch.setattr(device, "_run_adb", adb)
        monkeypatch.setattr(device, "_run_adb_cleanup", adb_cleanup)
        monkeypatch.setattr(device, "_free_port", lambda: 15001)
        monkeypatch.setattr(device, "_read_logs", lambda *args: None)
        return device, state

    return setup


@pytest.mark.parametrize(("pressure", "pid_line"), [
    ("0", "$ 3422\n"), pytest.param("0", "$ 25956\n", id="captured-mumu-zero-pressure"), ("255", "$ 3422\n"),
])
def test_minitouch_start_accepts_devices_with_and_without_pressure(minitouch_transport, pressure, pid_line):
    device, state = minitouch_transport(f"^ 10 720 1280 {pressure}", pid_line)
    try:
        device.start()
        assert device.connected
        assert (device.max_contacts, device.max_x, device.max_y) == (10, 720, 1280)
        device.publish("w 0\n")
        assert state.published == ["w 0\n"]
    finally:
        device.emergency_stop_with_deadline(0.2)


@pytest.mark.parametrize("capabilities", [
    "^ 10 720 1280 -1", "^ 10 720 1280 invalid", "^ 10 720 1280",
    "^ 0 720 1280 0", "^ 11 720 1280 0", "^ 10 0 1280 0", "^ 10 720 0 0",
])
def test_invalid_minitouch_handshake_keeps_contact_capacity_closed(minitouch_transport, capabilities):
    device, state = minitouch_transport(capabilities)
    with pytest.raises(native_minitouch.MinitouchStartError, match="握手"):
        device.start()
    assert (device.max_contacts, device.max_x, device.max_y) == (0, 0, 0)
    assert not state.published


def test_minitouch_rejects_publish_during_handshake(minitouch_transport):
    device, state = minitouch_transport("^ 10 720 1280 0")
    blocked = []

    def publish_during_receive():
        assert device._client.connected and not device.connected
        with pytest.raises(native_minitouch.MinitouchStartError, match="发布已封闭"):
            device.publish("d 0 1 2 50\nc\n")
        blocked.append(True)

    state.on_receive = publish_during_receive
    try:
        device.start()
        assert blocked == [True]
        assert not state.published
        assert device.connected
    finally:
        device.emergency_stop_with_deadline(0.2)


def test_minitouch_emergency_close_before_start_commit_prevents_late_publish(monkeypatch, minitouch_transport):
    device, state = minitouch_transport("^ 10 720 1280 0")
    start_transport = device._start_transport

    def close_before_commit(cancel_event):
        start_transport(cancel_event)
        assert device.emergency_stop_with_deadline(0.2)

    monkeypatch.setattr(device, "_start_transport", close_before_commit)
    with pytest.raises(native_minitouch.MinitouchStartError, match="准备已取消"):
        device.start(rollback_boot_id=BOOT)
    assert not device.connected and not state.published
    assert device.startup_rollback_status == "confirmed"
    assert device.stop() is False
    assert device.release_diagnostics["release_sequence_confirmed"] is False


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
@pytest.mark.parametrize("borrowed", [False, True])
def test_unpublished_startup_can_release_only_this_claim(tmp_path, monkeypatch, minitouch_transport, borrowed):
    backend, parent, _, observers, registry = backend_fixture(tmp_path, monkeypatch, borrowed=borrowed)
    device, state = minitouch_transport("^ 10 720 1280 -1")
    backend._device = device
    backend.arm()
    with pytest.raises(RuntimeError, match="握手触点数量或坐标范围无效"):
        backend.wait_until_ready(2)
    backend.stop()
    assert backend.wait_native_cleanup(1 if borrowed else 0)
    report = backend.report()
    assert report["state"] == "failed"
    assert report["release_confirmed"] is False
    assert report["release_sequence_confirmed"] is False
    assert report["native_device_claim"]["startup_rollback_status"] == "confirmed"
    assert report["native_device_claim"]["release_reason"] == "unpublished-startup-aborted"
    assert not state.published and not observers
    assert not device._port and not device._spawned and device._client is None

    retry = NativeRunDeviceClaim(
        backend._native_device_claim.runtime, BOOT, "retry",
        borrow=parent.descriptor() if parent else None, registry_root=registry,
    )
    retry.acquire()
    assert retry.finish(input_released=True, observer_terminal=True)
    if parent:
        assert len(parent.acquired) == 2 and all((path / "owner.json").exists() for path in parent.paths)
        parent.release()


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
@pytest.mark.parametrize(("fault", "status"), [
    ("missing-pid", "missing-pid"),
    ("malformed-pid", "missing-pid"),
    ("partial-pid", "missing-pid"),
    ("remote", "remote-process-unconfirmed"),
    ("foreign-forward", "forward-unconfirmed"),
    ("forward-removal", "forward-unconfirmed"),
    ("local", "local-transport-unconfirmed"),
    ("observer", "confirmed"),
    ("foreign-owner", "confirmed"),
])
def test_unproven_startup_rollback_retains_claim(tmp_path, monkeypatch, minitouch_transport, fault, status):
    backend, _, _, _, registry = backend_fixture(tmp_path, monkeypatch)
    pid_line = {"missing-pid": "", "malformed-pid": "$ invalid\n", "partial-pid": "$ 34"}.get(fault, "$ 3422\n")
    device, state = minitouch_transport("^ 10 720 1280 -1", pid_line)
    state.fault = fault
    backend._device = device
    if fault == "local":
        monkeypatch.setattr(device, "emergency_stop_with_deadline", lambda _timeout: False)
    elif fault == "observer":
        backend._observation_clock = SimpleNamespace(request_stop=lambda: None, wait_closed=lambda _: False)
    backend.arm()
    with pytest.raises(RuntimeError, match="握手"):
        backend.wait_until_ready(2)
    if fault == "foreign-owner":
        owner = backend._native_device_claim._root / "owner.json"
        updated = json.loads(owner.read_text(encoding="utf-8"))
        updated["nonce"] = "changed-owner"
        owner.write_text(json.dumps(updated), encoding="utf-8")
    backend.stop()
    assert not backend.wait_native_cleanup(1)
    report = backend.report()
    assert report["release_confirmed"] is False
    assert report["native_device_claim"]["state"] == "retained"
    assert report["native_device_claim"]["startup_rollback_status"] == status
    assert not state.published
    if status == "missing-pid":
        assert all(args[0] != "shell" for args, _ in state.cleanup_calls)
    if fault == "foreign-forward":
        assert all(args != ("forward", "--remove", "tcp:15001") for args, _ in state.cleanup_calls)
    with pytest.raises(DeviceSafetyError, match="already leased"):
        NativeRunDeviceClaim(
            backend._native_device_claim.runtime, BOOT, "retry", registry_root=registry,
        ).acquire()


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
def test_failure_after_publication_opens_still_needs_up_jlog(tmp_path, monkeypatch, minitouch_transport):
    backend, _, _, _, _ = backend_fixture(tmp_path, monkeypatch)
    device, state = minitouch_transport("^ 10 720 1280 0")
    backend._device = device

    def fail_after_opening():
        device.publish("d 0 1 2 50\nc\n")
        raise OSError("offline injected probe failure")

    monkeypatch.setattr(backend, "_publish_startup_probe_locked", fail_after_opening)
    backend.arm()
    with pytest.raises(RuntimeError, match="offline injected probe failure"):
        backend.wait_until_ready(2)
    backend.stop()
    assert not backend.wait_native_cleanup(1)
    report = backend.report()
    assert report["native_device_claim"]["state"] == "retained"
    assert report["native_device_claim"]["startup_rollback_status"] is None
    assert report["release_confirmed"] is False
    assert state.published[0] == "d 0 1 2 50\nc\n"
    assert all(expected_line is None for _, expected_line in state.cleanup_calls)


@pytest.mark.parametrize(("returncode", "stdout", "expected"), [
    (0, "", False),
    (0, "unrelated\n", False),
    (0, "prefix-startup-process-closed\n", False),
    (1, "startup-process-closed\n", False),
    (0, "startup-process-closed\n", True),
])
def test_real_cleanup_helper_requires_positive_exact_process_receipt(monkeypatch, returncode, stdout, expected):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] <= .5
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(native_minitouch.subprocess, "run", run)
    device = native_minitouch.NativeMinitouchDevice("offline-adb", "offline-device")
    assert device._terminate_startup_process(BOOT, 25956, .5) is expected
    assert calls[0][:4] == ["offline-adb", "-s", "offline-device", "shell"]
    command = calls[0][-1]
    assert f'[ "$boot" = "{BOOT}" ] || exit 1' in command
    assert f'[ "$args" = "{native_minitouch._DEVICE_BINARY} -n {device._socket_name} " ] || exit 1' in command
    assert 'while [ -d /proc/25956 ]; do' in command


def test_cleanup_transport_timeout_cannot_produce_startup_receipt(monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(native_minitouch.subprocess, "run", run)
    device = native_minitouch.NativeMinitouchDevice("offline-adb", "offline-device")
    assert device._terminate_startup_process(BOOT, 25956, .5) is False


def test_startup_primary_error_survives_failed_rollback_and_local_disconnect(monkeypatch, minitouch_transport):
    device, _ = minitouch_transport("^ 10 720 1280 -1")

    def fail(*args):
        raise OSError("offline injected cleanup error")

    monkeypatch.setattr(device, "_rollback_startup", fail)
    monkeypatch.setattr(device, "emergency_stop_with_deadline", fail)
    with pytest.raises(native_minitouch.MinitouchStartError, match="握手触点数量或坐标范围无效") as error:
        device.start(rollback_boot_id=BOOT)
    assert device.startup_rollback_status == "cleanup-error:OSError"
    assert any("OSError" in note for note in error.value.__notes__)
