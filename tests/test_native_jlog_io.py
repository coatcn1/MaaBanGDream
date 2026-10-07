import json
import threading
import time
from types import SimpleNamespace

from agent.realtime.native_minitouch import NativeMinitouchDevice


class SlowFile:
    def __init__(self):
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.rows = []
        self.closed = False

    def write(self, text):
        self.rows.append(text)

    def flush(self):
        self.entered.set()
        assert self.unblock.wait(3)

    def close(self):
        self.closed = True


def install_writer(device, stream, **kwargs):
    # 红绿测试同时覆盖原始同步刷盘与候选异步实现。
    start = getattr(device, "_start_jlog_writer", None)
    if start is None:
        device._jlog_file = stream
        return None
    return start(stream=stream, **kwargs)


def test_blocked_disk_does_not_block_owner_memory_or_ack():
    device = NativeMinitouchDevice("adb", "test")
    stream = SlowFile()
    writer = install_writer(device, stream)
    record = threading.Thread(target=lambda: device._record_log_line("handshake"))
    record.start()
    assert stream.entered.wait(1)
    read_done = threading.Event()
    rows = []
    owner = threading.Thread(target=lambda: (rows.extend(device.logs_since(0)[1]), read_done.set()))
    owner.start()
    try:
        assert read_done.wait(.1), "owner读内存回执不得等待磁盘"
        assert rows == ["handshake"]
        device._closed = False
        device._touch_possible = True
        device._max_contacts = 3
        sent = []
        device._client = SimpleNamespace(connected=True, publish=lambda text: sent.append(text) or True)
        device.request_reset()
        device._reset_thread.join(.1)
        assert sent
        for index, command in enumerate(sent[0].splitlines()):
            device._record_log_line("jlog " + json.dumps({"st": index * 2, "et": index * 2 + 1,
                                                         "c": 1, "cmd": command}))
        assert device.reset_executed
        assert device.release_diagnostics["release_sequence_confirmed"]
    finally:
        stream.unblock.set()
        record.join(1)
        owner.join(1)
        if writer:
            writer.stop()
            writer.thread.join(1)


def test_disk_failure_does_not_abort_reader():
    class BadFile:
        def write(self, text):
            raise OSError("disk-fault")
        def flush(self):
            pass
        def close(self):
            pass
    device = NativeMinitouchDevice("adb", "test")
    writer = install_writer(device, BadFile())
    class Client:
        connected = True
        def receive(self, *args):
            self.connected = False
            return "first\nsecond\n"
    client = Client()
    device._client = client
    device._closed = False
    try:
        device._read_logs(client, device._reset_generation)
        assert device.logs_since(0) == (2, ["first", "second"])
    finally:
        if writer:
            writer.stop()
            writer.thread.join(1)
    if writer:
        report = device.jlog_io_diagnostics
        assert report["error"] == "OSError: disk-fault"
        assert not report["complete"]
        assert report["missing_lines"] == 2


def test_full_diagnostic_queue_preserves_memory_and_counts_missing_lines():
    device = NativeMinitouchDevice("adb", "test")
    stream = SlowFile()
    writer = device._start_jlog_writer(stream=stream, capacity=2)
    device._record_log_line("first")
    assert stream.entered.wait(1)
    try:
        for index in range(5):
            device._record_log_line(str(index))
        assert device.logs_since(0) == (6, ["first", "0", "1", "2", "3", "4"])
        report = device.jlog_io_diagnostics
        assert report["dropped_lines"] == 3
        assert report["pending_lines"] == 3
        assert report["missing_lines"] == 6
        assert not report["complete"]
    finally:
        stream.unblock.set()
        writer.stop()
        writer.thread.join(1)
    report = device.jlog_io_diagnostics
    assert report["missing_lines"] == report["dropped_lines"] == 3
    assert report["written_lines"] == 3


def test_stop_release_proof_and_cleanup_are_independent_of_blocked_disk(monkeypatch):
    device = NativeMinitouchDevice("adb", "test")
    stream = SlowFile()
    writer = device._start_jlog_writer(stream=stream)
    device._record_log_line("first")
    assert stream.entered.wait(1)
    device._closed = False
    device._touch_possible = True
    device._max_contacts = 3
    device._pid = 123
    class Client:
        connected = True
        def publish(self, text):
            for index, command in enumerate(text.splitlines()):
                device._record_log_line("jlog " + json.dumps({"st": index * 2, "et": index * 2 + 1,
                                                             "c": 1, "cmd": command}))
            return True
        def close(self):
            self.connected = False
    device._client = Client()
    monkeypatch.setattr(device, "_run_adb_cleanup", lambda *args, **kwargs: True)
    try:
        started = time.perf_counter()
        assert device.stop_with_deadline(.1)
        assert time.perf_counter() - started < .1
        assert device.release_diagnostics["release_sequence_confirmed"]
        assert device.release_diagnostics["release_proof"] == "current-release-sequence-jlog-and-cleanup"
        assert writer.thread.is_alive()
        assert not device.jlog_io_diagnostics["complete"]
        time.sleep(.03)
        assert device.jlog_io_diagnostics["drain_status"] == "timeout"
    finally:
        stream.unblock.set()
        writer.thread.join(1)
    assert device.jlog_io_diagnostics["dropped_lines"] == 8
    assert not device.jlog_io_diagnostics["complete"]


def test_new_device_cannot_overlap_same_path_writer_or_close_its_stream(tmp_path):
    path = tmp_path / "jlog.log"
    original = NativeMinitouchDevice("adb", "test", jlog_path=path)
    original._reset_generation = 1
    stream = SlowFile()
    writer = original._start_jlog_writer(stream=stream)
    original._record_log_line("old")
    assert stream.entered.wait(1)
    writer.stop()
    replacement = NativeMinitouchDevice("adb", "test", jlog_path=tmp_path / "." / "jlog.log")
    replacement._reset_generation = 2
    try:
        assert replacement._start_jlog_writer() is None
        replacement._record_log_line("ignored-old", source_generation=1)
        replacement._record_log_line("new-memory-only", source_generation=2)
        assert replacement.logs_since(0) == (1, ["new-memory-only"])
        report = replacement.jlog_io_diagnostics
        assert report["enabled"] is False
        assert report["missing_lines"] == 1
        assert report["warnings"] == ["previous-generation-writer-pending"]
        assert replacement.stop_with_deadline(.1)
        assert stream.closed is False
    finally:
        stream.unblock.set()
        writer.thread.join(1)
    new_writer = replacement._start_jlog_writer()
    assert new_writer is not None
    replacement._record_log_line("new-disk")
    new_writer.stop()
    new_writer.thread.join(1)
    assert path.read_text(encoding="utf-8") == "new-disk\n"
    assert replacement.jlog_io_diagnostics["complete"]


def test_blocked_close_is_reported_and_cannot_pollute_new_generation(tmp_path):
    class BlockClose:
        def __init__(self):
            self.entered = threading.Event()
            self.unblock = threading.Event()
        def write(self, text):
            pass
        def flush(self):
            pass
        def close(self):
            self.entered.set()
            assert self.unblock.wait(3)
    stream = BlockClose()
    original = NativeMinitouchDevice("adb", "test", jlog_path=tmp_path / "same.log")
    writer = original._start_jlog_writer(stream=stream)
    original._record_log_line("one")
    writer.stop()
    assert stream.entered.wait(1)
    replacement = NativeMinitouchDevice("adb", "test", jlog_path=tmp_path / "same.log")
    try:
        assert replacement._start_jlog_writer() is None
        started = time.perf_counter()
        assert original.stop_with_deadline(.1)
        assert time.perf_counter() - started < .1
        time.sleep(.03)
        assert original.jlog_io_diagnostics["drain_status"] == "timeout"
        assert not original.jlog_io_diagnostics["complete"]
    finally:
        stream.unblock.set()
        writer.thread.join(1)
    new = replacement._start_jlog_writer()
    assert new is not None
    replacement._record_log_line("two")
    new.stop()
    new.thread.join(1)
    assert replacement.jlog_io_diagnostics["accepted_lines"] == 1
