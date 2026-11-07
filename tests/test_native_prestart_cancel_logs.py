import json
import threading
from types import SimpleNamespace

import pytest

from agent.realtime.native_play import NativeMinitouchBackend
from agent.realtime.native_minitouch import NativeMinitouchDevice


@pytest.fixture
def cancelled_probe(tmp_path, monkeypatch):
    path = tmp_path / "chart.json"
    path.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]', encoding="utf-8")
    device = NativeMinitouchDevice("unused", "unused")
    device._reset_generation = 1
    device._closed = False
    device._max_contacts = 10
    sent, before, after, ticks = [], [], [], [10000.]
    def record(command):
        ticks[0] += 2
        device._record_log_line("jlog " + json.dumps(dict(st=ticks[0], et=ticks[0] + 1,
            c=1, cmd=command)), source_generation=1)
    def publish(text):
        sent.append(text)
        cleanup = text.startswith("w 0\nw 0\n")
        if cleanup:
            for command in before: record(command)
        for command in text.splitlines(): record(command)
        if cleanup:
            for command in after: record(command)
        return True
    device._client = SimpleNamespace(connected=True, publish=publish, close=lambda: None)
    class Session:
        state = "armed"
        def supports_window_end_cap(self): return True
        def apply_future_phase(self, *args): return False
        def cancel(self, *args): self.state = "cancelling"; return True
        def poll(self): return self.state
        def report(self): return dict(state=self.state, planned=1, sent=0, executed=0)
        def acknowledge_reset(self):
            assert device.reset_executed
            self.state = "cancelled"
            return True
    session = Session()
    backend = NativeMinitouchBackend(path, adb_path="unused", serial="unused", device=device,
        require_probe=True, observation_clock_trial_enabled=True, life_feedback_enabled=True,
        session_factory=lambda **kwargs: session)
    backend._state = "armed"
    backend._session_state = "armed"
    backend._publish_startup_probe_locked()
    monkeypatch.setattr(device, "_run_adb_cleanup", lambda *a, **kw: True)
    def reset():
        device.request_reset()
        device._reset_thread.join(.5)
        assert not device._reset_thread.is_alive()
    return SimpleNamespace(backend=backend, device=device, record=record, reset=reset,
        sent=sent, before=before, after=after)


def cancel(case):
    assert case.backend._handle_owner_command("cancel", ("unit-stop",))


def test_completed_probe_cancel_then_cleanup_markers_ack_without_new_probe(cancelled_probe):
    case = cancelled_probe
    case.backend._observe_new_logs()
    samples = case.backend._startup_probe_samples
    offset = (case.backend._device_clock_offset_s, case.backend._clock_uncertainty_ms,
              case.backend._startup_probe_frozen)
    cancel(case)
    case.reset()
    assert case.backend._handle_owner_command("acknowledge_reset", ()) == "cancelled"
    assert case.device.release_diagnostics["release_sequence_confirmed"] is True
    assert case.backend._observation_error is None
    assert case.backend._startup_probe_samples is samples
    assert offset == (case.backend._device_clock_offset_s, case.backend._clock_uncertainty_ms,
                      case.backend._startup_probe_frozen)
    assert case.backend._first_action_anchor_s is None
    assert case.backend._published_action_tokens == case.backend._observed_action_tokens == set()
    diagnostic = case.backend.report()["cancelled_prestart_logs"]
    assert diagnostic["cleanup_candidates"] == 15 and diagnostic["unknown"] == 0
    assert diagnostic["authorization"] is diagnostic["no_touch_evidence"] is False
    assert len(case.sent) == 2 and len(case.device.log_records_since(0)[1]) == 18


def test_queued_unfinished_probe_is_checked_without_completing_or_mapping(cancelled_probe):
    case = cancelled_probe
    assert not case.backend._probe_complete.is_set()
    cancel(case)
    case.reset()
    assert case.backend._handle_owner_command("acknowledge_reset", ()) == "cancelled"
    assert not case.backend._probe_complete.is_set()
    assert case.backend._startup_probe_samples == ()
    assert len(case.backend._probe_expected) == 3
    assert case.backend._device_clock_offset_s is None and not case.backend._startup_probe_frozen
    assert case.backend.report()["cancelled_prestart_logs"]["queued_probe"] == 3


@pytest.mark.parametrize("command", ["w 0", "w 1", "d 0 10 10 50", "m 0 10 10 50"])
def test_completed_probe_extra_before_cancel_still_fails(cancelled_probe, command):
    case = cancelled_probe
    case.backend._observe_new_logs()
    case.record(command)
    with pytest.raises(RuntimeError, match="启动探测"):
        case.backend._observe_new_logs()


@pytest.mark.parametrize("command", ["w 0", "w 1", "d 0 10 10 50", "m 0 10 10 50"])
def test_queued_bad_prefix_cannot_be_hidden_by_later_cancel_or_release(cancelled_probe, command):
    case = cancelled_probe
    case.backend._observe_new_logs()
    case.record(command)
    cancel(case)
    case.reset()
    with pytest.raises(RuntimeError, match="取消前启动探测"):
        case.backend._handle_owner_command("acknowledge_reset", ())
    # 设备的独立物理释放可成立，但不能改写旧输入证据异常或变成无触点证明。
    assert case.device.release_diagnostics["release_sequence_confirmed"] is True
    assert case.backend._observation_error is not None
    assert case.backend.report()["cancelled_prestart_logs"]["unknown"] == 1


@pytest.mark.parametrize("fault", ["missing-cursor", "bool-cursor", "old-generation", "reader-domain",
                                  "missing-cancel-snapshot", "probe-generation"])
def test_untrusted_release_boundary_keeps_conservative_probe_checks(cancelled_probe, monkeypatch, fault):
    case = cancelled_probe
    case.backend._observe_new_logs()
    cancel(case)
    case.reset()
    if fault == "missing-cursor": case.device._release_send_cursor = None
    elif fault == "bool-cursor": case.device._release_send_cursor = True
    elif fault == "old-generation": case.device._reset_generation = 2
    elif fault == "missing-cancel-snapshot": case.backend._cancelled_probe_snapshot = None
    elif fault == "probe-generation": case.backend._cancelled_probe_snapshot["source_generation"] = 2
    else:
        original = case.device.log_records_since
        monkeypatch.setattr(case.device, "log_records_since", lambda cursor: original(cursor))
    with pytest.raises(RuntimeError, match="取消前启动探测"):
        case.backend._observe_new_logs()
    assert case.backend._device_clock_offset_s is None


@pytest.mark.parametrize("command", ["d 0 10 10 50", "m 0 10 10 50", "w 1"])
def test_cancel_window_unknown_is_recorded_without_no_touch_claim(cancelled_probe, command):
    case = cancelled_probe
    case.backend._observe_new_logs()
    case.before.append(command)
    cancel(case)
    case.reset()
    assert case.backend._handle_owner_command("acknowledge_reset", ()) == "cancelled"
    diagnostic = case.backend.report()["cancelled_prestart_logs"]
    assert diagnostic["unknown"] == 1
    assert diagnostic["possible_touch"] == int(command.startswith(("d ", "m ")))
    assert diagnostic["no_touch_evidence"] is False
    assert case.backend._observed_action_tokens == set()


@pytest.mark.parametrize("command", ["d 0 10 10 50", "m 0 10 10 50"])
def test_touch_after_final_commit_still_invalidates_device_release(cancelled_probe, command):
    case = cancelled_probe
    case.backend._observe_new_logs()
    case.after.append(command)
    cancel(case)
    case.reset()
    assert case.backend._handle_owner_command("acknowledge_reset", ()) == "cancelling"
    assert case.device.release_diagnostics["release_sequence_confirmed"] is False
    assert case.device.release_diagnostics["release_sequence_error"] == "late-touch-after-release"
    assert case.backend.report()["cancelled_prestart_logs"]["possible_touch"] == 1


def test_original_observation_error_is_retained_during_cancelled_cleanup(cancelled_probe):
    case = cancelled_probe
    case.backend._observe_new_logs()
    case.backend._observation_error = "original-input-error"
    case.record("w 1")
    cancel(case)
    case.reset()
    with pytest.raises(RuntimeError, match="original-input-error"):
        case.backend._observe_new_logs()
    assert case.backend._observation_error == "original-input-error"


def test_cancel_diagnostics_are_bounded_and_raw_device_records_remain_complete(cancelled_probe):
    case = cancelled_probe
    case.backend._observe_new_logs()
    case.before.extend(["d 0 10 10 50"] * 80)
    cancel(case)
    case.reset()
    case.backend._observe_new_logs()
    diagnostic = case.backend.report()["cancelled_prestart_logs"]
    assert diagnostic["total"] == 95 and diagnostic["possible_touch"] == 80
    assert len(diagnostic["samples"]) == 16 and diagnostic["dropped"] == 79
    assert len(case.device.log_records_since(0)[1]) == 98


def test_user_stop_never_publishes_or_waits_for_additional_probe(cancelled_probe, monkeypatch):
    case = cancelled_probe
    case.backend._observe_new_logs()
    case.backend._device_start_cancel.set()
    monkeypatch.setattr(case.backend._probe_complete, "wait", lambda *_: pytest.fail("停止不能再等probe"))
    cancel(case)
    case.reset()
    assert case.backend._handle_owner_command("acknowledge_reset", ()) == "cancelled"
    with pytest.raises(InterruptedError):
        case.backend._publish_startup_probe_locked()
    assert len(case.sent) == 2


def test_busy_release_metadata_lock_never_blocks_owner(cancelled_probe):
    case = cancelled_probe
    case.backend._observe_new_logs()
    cancel(case)
    case.reset()
    errors = []
    def observe():
        try: case.backend._observe_new_logs()
        except Exception as exc: errors.append(exc)
    case.device._reset_lock.acquire()
    worker = threading.Thread(target=observe)
    try:
        worker.start()
        worker.join(.2)
        returned_without_release = not worker.is_alive()
    finally:
        case.device._reset_lock.release()
        worker.join(1)
    assert returned_without_release and len(errors) == 1
    assert "取消前启动探测" in str(errors[0])


@pytest.mark.parametrize("fault", ["nan", "negative-cost", "clock-rewind", "overlap", "empty", "malformed"])
def test_cancel_does_not_bypass_original_log_structure_or_time_checks(cancelled_probe, fault):
    case = cancelled_probe
    case.backend._observe_new_logs()
    cancel(case)
    event = dict(st=10007., et=10008., c=1, cmd="w 0")
    if fault == "nan": event["st"] = float("nan")
    elif fault == "negative-cost": event["c"] = -1
    elif fault == "clock-rewind": event.update(st=9999., et=10000.)
    elif fault == "overlap": event.update(st=10006., et=10008.)
    elif fault == "empty": event["cmd"] = ""
    line = "jlog {broken" if fault == "malformed" else "jlog " + json.dumps(event)
    case.device._record_log_line(line, source_generation=1)
    with pytest.raises(RuntimeError):
        case.backend._observe_new_logs()
    assert case.backend._device_clock_offset_s is None
    assert len(case.backend._startup_probe_samples) == 1


def test_missing_release_tail_never_acknowledges_cancelled_session(cancelled_probe):
    case = cancelled_probe
    case.backend._observe_new_logs()
    publish = case.device._client.publish
    case.device._client.publish = lambda text: publish("\n".join(text.splitlines()[:-1]) + "\n")
    cancel(case)
    case.reset()
    assert case.backend._handle_owner_command("acknowledge_reset", ()) == "cancelling"
    assert not case.device.release_diagnostics["release_sequence_confirmed"]
