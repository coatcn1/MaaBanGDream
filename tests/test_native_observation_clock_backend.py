from collections import deque
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent.realtime import native_engine, native_play
from agent.realtime.native_observation_clock import ClockIdentity, ObservationClockMapper, ObservationProbe


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
@pytest.mark.parametrize("shift", [-.103, .103])
@pytest.mark.parametrize("publisher_error", [False, True])
def test_real_backend_injection_preserves_raw_device_execution_and_host_domains(tmp_path, monkeypatch,
                                                                              capsys, shift, publisher_error):
    chart = tmp_path / "chart.json"
    chart.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]',
                     encoding="utf-8")
    identity = ClockIdentity("production-seam", 23, 42, 100)
    mapper = ObservationClockMapper(identity)
    for index in range(1, 10):
        host = 200 + index * .05
        device = host + .0004 - (100 + shift)
        mapper.observe(ObservationProbe(identity, index, host, host + .0008,
            device, device, device - 1, device - 1))
    device_commit = 100.4254 - shift
    event = dict(command="c", start_ms=device_commit * 1000, end_ms=device_commit * 1000, cost_ms=0.)
    device = SimpleNamespace(request_reset=lambda: True,
        log_records_since=lambda cursor: (1, [("jlog test", 200.4264)] if cursor == 0 else []))
    backend = native_play.NativeMinitouchBackend(chart, adb_path="unused", serial="unused",
        device=device, run_id=identity.run_id, observation_clock=mapper, life_feedback_enabled=True,
        clock=lambda: 200.4508)
    assert backend._clock_relation_required
    assert backend.observation_clock_snapshot() is mapper.snapshot()
    backend._first_action_anchor_s = 200
    backend._device_clock_offset_s = 100
    backend._clock_basis, backend._clock_uncertainty_ms = "probe-midpoint", .5
    backend._playback_observation_started = True
    planned = device_commit + 100 - .002
    receipt = dict(action_token=1, planned_engine_s=planned, action_kind="tap", command="d",
                   feedback_single_tap=True)
    backend._expected_commands = deque([native_play._ExpectedCommand("c", 1, receipts=(receipt,))])
    accepted = []
    backend._session = SimpleNamespace(observe_minitouch_log=lambda _event: None,
        observe_execution=lambda *args: accepted.append(args) or True, report=lambda: dict(state="running"))
    monkeypatch.setattr(native_engine, "parse_minitouch_log", lambda _line: event)
    if publisher_error:
        def fail(*_args):
            raise OSError("private observation data")
        monkeypatch.setattr(backend, "_publish_execution_commit", fail)
    backend._observe_new_logs()
    assert len(accepted) == 1
    if publisher_error:
        assert backend.execution_commit_snapshot() == ()
        assert backend.observation_clock_snapshot() is None
        assert backend._observed_action_tokens == {1}
        assert backend._device_clock_offset_s == 100
        backend._log_cursor = 0
        backend._last_device_start_ms = backend._last_device_end_ms = None
        receipt2 = dict(receipt, action_token=2)
        backend._expected_commands.append(native_play._ExpectedCommand("c", 2, receipts=(receipt2,)))
        backend._observe_new_logs()
        assert len(accepted) == 2 and backend._observed_action_tokens == {1, 2}
        assert backend.execution_commit_snapshot() == ()
        assert backend.observation_clock_snapshot() is None
        assert backend._observation_error is None
        assert not backend.report()["release_confirmed"]
        output = capsys.readouterr().out
        assert output.count("commit_warning=OSError") == 1
        assert "private observation data" not in output
        return
    commit, = backend.execution_commit_snapshot()
    assert commit.device_actual_s == pytest.approx(device_commit)
    assert commit.actual_execution_s == pytest.approx(device_commit + 100)
    assert commit.received_host_s == pytest.approx(200.4264)
    assert accepted[0][1] - accepted[0][0] == pytest.approx(.002)
    assert backend._device_clock_offset_s == 100
    source, = backend._feedback_input_history
    assert source.device_actual_s == pytest.approx(device_commit)
    assert source.device_uncertainty_s == .000002
    assert source.actual_s == pytest.approx(commit.actual_execution_s)
    ticket = backend.feedback_observation_snapshot().ticket
    assert ticket.device_actual_s == pytest.approx(device_commit)
    assert ticket.device_uncertainty_s == .000002
    assert ticket.actual_s == pytest.approx(commit.actual_execution_s)
    view = backend.observation_clock_snapshot().commit_view(commit.device_actual_s,
        now_s=200.4508, execution_offset_s=100, device_uncertainty_s=commit.device_uncertainty_s)
    assert view is not None
    lower, upper = view.device_to_host(commit.device_actual_s)
    assert lower <= 200.4254 <= upper
    assert upper - lower >= .000004 and view.uncertainty_ms <= 1
    assert commit.actual_execution_s + shift == pytest.approx(200.4254)
    with pytest.raises(FrozenInstanceError):
        commit.actual_execution_s = 999
    backend._phase_lock.acquire()
    try:
        assert backend.execution_commit_snapshot() == (commit,)
        assert backend.observation_clock_snapshot() is mapper.snapshot()
    finally:
        backend._phase_lock.release()
    mapper.invalidate("timeout")
    assert backend.observation_clock_snapshot().commit_view(commit.device_actual_s,
        now_s=200.4508, execution_offset_s=100) is None
    assert backend.execution_commit_snapshot() == (commit,)


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
@pytest.mark.parametrize("source", [None, "foreign", "error", "untyped"])
def test_real_backend_observation_source_fails_closed_without_touch_side_effects(tmp_path, capsys, source):
    chart = tmp_path / "chart.json"
    chart.write_text('[{"type":"BPM","beat":0,"bpm":120},{"type":"Single","beat":2,"lane":2}]',
                     encoding="utf-8")
    touches = []
    if source == "foreign":
        observer = ObservationClockMapper(ClockIdentity("foreign", 23, 42, 100))
    elif source == "error":
        def fail():
            raise RuntimeError("private response must not be logged")
        observer = SimpleNamespace(snapshot=fail)
    elif source == "untyped":
        observer = SimpleNamespace(snapshot=lambda: SimpleNamespace(identity=SimpleNamespace(run_id="production-seam")))
    else:
        observer = None
    backend = native_play.NativeMinitouchBackend(chart, adb_path="unused", serial="unused",
        device=SimpleNamespace(request_reset=lambda: touches.append("reset")),
        run_id="production-seam", observation_clock=observer, life_feedback_enabled=True)
    for _ in range(3):
        assert backend.observation_clock_snapshot() is None
    assert not touches
    assert backend._device_clock_offset_s is None
    assert not backend.report()["release_confirmed"]
    output = capsys.readouterr().out
    assert "private response" not in output
    assert output.count("reader_warning=") == (1 if source == "error" else 0)
