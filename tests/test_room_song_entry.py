from types import SimpleNamespace
import json
import time

import numpy as np
import pytest

from agent.realtime import cooperative_action as cooperative
from agent.realtime import versus_action as versus
from agent.realtime import room_song_entry as entry
from agent.realtime.live_session import current_live_run, reset_live_run, update_live_run


MODES = [(cooperative, cooperative.CooperativeLiveFlow, "COOPERATIVE"),
         (versus, versus.VersusLiveFlow, "VERSUS")]


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_option_transition_to_ready_prepares_before_ready_input(monkeypatch, module, cls, prefix):
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {"song_choice": "unspecified"}
    flow.song_choice_pause_pending = False
    clock, page, calls = [0.0], ["song_unspecified"], []
    confirm = getattr(module, prefix + "_SONG_CONFIRM_POINT")

    def click(point):
        calls.append(("click", point, page[0]))
        page[0] = "playing" if point == confirm else "ready_button"
        return True

    def guarded_click(point):
        if page[0] != "song_unspecified":
            return False
        return click(point)

    flow.capture = lambda: page[0]
    flow.visible = lambda image, name, *args: image == name
    flow.click = click
    flow.guarded_song_choice_click = guarded_click
    flow.prepare = lambda: calls.append("prepare")
    flow.play = lambda: calls.append("play") or True
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value))
    monkeypatch.setattr(module, "mark_uncertain", lambda *args: None)
    assert flow.run_attempt(reuse_room=True)
    assert calls == [("click", getattr(module, prefix + "_SONG_UNSPECIFIED_POINT"), "song_unspecified"),
                     "prepare", "play"]


@pytest.mark.parametrize("module,cls,prefix", MODES)
@pytest.mark.parametrize("stop_at", ["before", "foreground", "task", None])
def test_guarded_click_checks_stop_and_fresh_framework_page(monkeypatch, module, cls, prefix, stop_at):
    flow = object.__new__(cls)
    tasker = SimpleNamespace(stopping=stop_at == "before", controller=object())
    calls = []
    page = ["selection"]
    flow.templates = {"song_unspecified": np.zeros((32, 50, 3))}

    def foreground(_controller):
        calls.append("foreground")
        page[0] = "ready"
        tasker.stopping = stop_at == "foreground"

    def run_task(node, pipeline):
        calls.append("task")
        params = pipeline[node]
        assert params["recognition"] == "TemplateMatch"
        assert params["template"] == [module.__name__.split(".")[-1].removesuffix("_action") + "/song_unspecified.png"]
        assert params["timeout"] == 0 and params["next"] == []
        assert "max_hit" not in params
        assert params["threshold"] == [0.90]
        tasker.stopping = stop_at == "task"
        return SimpleNamespace(status=SimpleNamespace(succeeded=page[0] == "selection"))

    flow.context = SimpleNamespace(tasker=tasker, run_task=run_task)
    monkeypatch.setattr(entry, "require_game_foreground", foreground)
    if stop_at is not None:
        with pytest.raises(InterruptedError):
            flow.guarded_song_choice_click((1067, 655))
    else:
        assert flow.guarded_song_choice_click((1067, 655)) is False
    assert calls == {"before": [], "foreground": ["foreground"],
                     "task": ["foreground", "task"], None: ["foreground", "task"]}[stop_at]


def test_preplay_recovery_dismisses_member_exit_popup_before_safe_page(monkeypatch):
    clock, state = [0.0], ["member_exit"]
    flow = SimpleNamespace(
        stopped=lambda: False,
        capture=lambda: state[0],
        visible=lambda image, name, *args: (
            (image == "member_exit" and name == "member_exit_title")
            or (image == "room" and name == "room_search")
        ),
        dismiss_member_exit=lambda: state.__setitem__(0, "room"),
        pipeline_box=lambda image, node: None,
    )
    monkeypatch.setattr(entry.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(entry.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value))
    entry.wait_for_preplay_recovery_page(flow, "room_wait", "CooperativeHomeMarker")
    assert state == ["room"]
    assert clock[0] == 0.3


def test_preplay_recovery_member_exit_dismiss_is_bounded(monkeypatch):
    clock = [0.0]
    flow = SimpleNamespace(
        stopped=lambda: False,
        capture=lambda: "member_exit",
        visible=lambda image, name, *args: (
            image == "member_exit" and name == "member_exit_title"
        ),
        dismiss_member_exit=lambda: None,
        pipeline_box=lambda image, node: None,
    )
    monkeypatch.setattr(entry.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(entry.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value))
    with pytest.raises(RuntimeError, match="反复出现"):
        entry.wait_for_preplay_recovery_page(flow, "room_wait", "CooperativeHomeMarker")
    assert clock[0] < 60.0


@pytest.mark.parametrize("module,cls,prefix", MODES)
@pytest.mark.parametrize("scenario", ["retry", "unknown", "gate-miss", "stop-after-option", "stop-after-confirm"])
def test_selection_confirmation_is_bounded_and_requires_selection(monkeypatch, module, cls, prefix, scenario):
    clock, page, inputs = [0.0], ["song_unspecified"], []
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {"song_choice": "unspecified"}
    flow.song_choice_pause_pending = False
    confirm = getattr(module, prefix + "_SONG_CONFIRM_POINT")
    option = getattr(module, prefix + "_SONG_UNSPECIFIED_POINT")
    flow.capture = lambda: page[0]
    flow.visible = lambda image, name, *args: image == name
    flow.click = lambda *args: pytest.fail("选曲不能使用通用坐标点击")

    def guarded(point):
        assert page[0] == "song_unspecified"
        inputs.append(point)
        if point == option and scenario == "unknown":
            page[0] = "unknown"
        if scenario == "stop-after-option" and point == option:
            flow.context.tasker.stopping = True
        if scenario == "stop-after-confirm" and point == confirm:
            flow.context.tasker.stopping = True
        return scenario != "gate-miss"

    flow.guarded_song_choice_click = guarded
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value))
    with pytest.raises(InterruptedError if scenario.startswith("stop-") else RuntimeError):
        flow.wait_for_preparation()
    assert inputs.count(confirm) == {"retry": 3, "unknown": 0, "gate-miss": 0,
                                      "stop-after-option": 0, "stop-after-confirm": 1}[scenario]
    assert inputs.count(option) == (3 if scenario == "gate-miss" else 1)
    assert clock[0] <= 60.35


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_option_gate_miss_after_page_became_ready_continues_to_prepare(monkeypatch, module, cls, prefix):
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {"song_choice": "random"}
    flow.song_choice_pause_pending = False
    page, calls = ["song_unspecified"], []
    flow.capture = lambda: page[0]
    flow.visible = lambda image, name, *args: image == name

    def guarded(point):
        calls.append(point)
        page[0] = "ready_button"
        return False

    flow.guarded_song_choice_click = guarded
    flow.prepare = lambda: calls.append("prepare")
    flow.play = lambda: True
    assert flow.run_attempt(reuse_room=True)
    assert calls == [getattr(module, prefix + "_SONG_RANDOM_POINT"), "prepare"]


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_stop_at_attempt_entry_preserves_previous_completed_run(module, cls, prefix):
    previous = reset_live_run(mode="versus", difficulty="Expert", prepared_for_play=True)
    update_live_run(play_completed=True)
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=True))
    flow.enter_room = lambda: pytest.fail("停止后不能入场")
    with pytest.raises(InterruptedError):
        flow.run_attempt()
    assert current_live_run().run_id == previous.run_id
    assert current_live_run().play_completed


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_new_attempt_failure_cannot_append_to_previous_completed_run(tmp_path, monkeypatch, module, cls, prefix):
    old_dir = tmp_path / "old-recording"
    old_dir.mkdir()
    lifecycle = old_dir / "lifecycle.jsonl"
    lifecycle.write_text("old-completed\n", encoding="utf-8")
    old = reset_live_run(mode="cooperative", difficulty="Expert", prepared_for_play=True)
    update_live_run(song_id="old-song", play_completed=True, recording_path=str(old_dir),
                    final_cover_confirmed=True, native_runtime_options={"old": True})
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {"difficulty": "Special", "diagnostic_trace": True}
    flow.stamina_argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=123))
    attempts = []

    def wait():
        attempts.append(flow._room_entry_evidence)
        raise RuntimeError("entry timeout")

    flow.wait_for_preparation = wait
    flow.capture = lambda: pytest.fail("早期失败只能使用已有截图")
    flow.prepare = lambda: pytest.fail("未到准备页不能 prepare")
    flow.play = lambda: pytest.fail("未准备不能 play")
    monkeypatch.setattr(module, "mark_uncertain", lambda *args: None)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="entry timeout"):
            flow.run_attempt(reuse_room=True)
        module.append_current_run_event(tmp_path, "room-preparation", "retry")
        new = current_live_run()
        assert new.run_id != old.run_id
        assert new.requested_difficulty == "Special"
        assert not new.prepared_for_play and not new.play_completed
        assert not new.final_cover_confirmed and new.recording_path is None
        assert new.native_runtime_options is None and new.song_id != "old-song"
    assert lifecycle.read_text(encoding="utf-8") == "old-completed\n"
    assert attempts[0].attempt_id != attempts[1].attempt_id
    assert [item.task_id for item in attempts] == [123, 123]


@pytest.mark.parametrize("module,cls,prefix", MODES)
@pytest.mark.parametrize("failure", ["init", "observe", "close"])
def test_diagnostics_failure_only_warns_and_keeps_prepare_play(monkeypatch, module, cls, prefix, failure, capsys):
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {"diagnostic_trace": True}
    flow.templates = {}
    calls = []

    class BrokenEvidence:
        def __init__(self, *args):
            if failure == "init":
                raise OSError("diagnostic init")

        def observe(self, *args):
            if failure == "observe":
                raise OSError("diagnostic observe")

        def close(self, *args):
            if failure == "close":
                raise OSError("diagnostic close")

    flow.wait_for_preparation = lambda: flow.capture()
    flow.prepare = lambda: calls.append("prepare")
    flow.play = lambda: calls.append("play") or True
    monkeypatch.setattr(module, "RoomEntryEvidence", BrokenEvidence)
    monkeypatch.setattr(module, "capture_image", lambda *args: np.zeros((1, 1, 3), dtype=np.uint8))
    assert flow.run_attempt(reuse_room=True)
    assert calls == ["prepare", "play"]
    assert f"diagnostic {failure}" in capsys.readouterr().out


@pytest.mark.parametrize("keep_frames", [False, True])
def test_entry_cache_is_bounded_and_writes_only_existing_frames(tmp_path, monkeypatch, keep_frames):
    written, done = [], []
    frame = np.arange(300, dtype=np.uint8).reshape(10, 10, 3)
    cache = entry.RoomEntryEvidence({"diagnostic_trace": True, "opening_recording": keep_frames},
                                   "versus", "task", {"song_unspecified": (0, 0)})
    template = frame[:5, :5].copy()
    cache.observe(frame, 1.0, {"song_unspecified": template})
    first = cache.last_image.copy()
    frame[:] = 0
    assert np.array_equal(cache.last_image, first)
    for index in range(1000):
        cache.phase = f"phase-{index}"
        cache.observe(frame, index + 2.0, {"song_unspecified": template})
    assert len(cache.events) == 511 and len(cache.frames) <= 22
    assert cache.observations_dropped > 0

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    class Recorder:
        def __init__(self, root, run_id, **kwargs):
            assert run_id is None
            self.output_dir = root

        def record(self, image, timestamp, details):
            written.append((image, details))

        def close(self, outcome):
            done.append(outcome)

    monkeypatch.setattr(entry.threading, "Thread", InlineThread)
    monkeypatch.setattr(entry, "CooperativeStartupRecorder", Recorder)
    cache.close(tmp_path, "stopped")
    cache.close(tmp_path, "failed")
    assert done == ["stopped"]
    assert len(written) <= 512
    assert sum(image is not None for image, _ in written) <= 23
    assert written[-1][0] is cache.last_image
    assert written[-1][1]["phase"] == "entry-terminal"
    assert written[-1][1]["task_id"] == "task"
    assert written[-1][1]["templates"]["song_unspecified"]["box"] == [0, 0, 5, 5]


@pytest.mark.parametrize("settings", [{"opening_recording": True}, {"debug_recording": True},
                                      {"diagnostic_trace": True}])
def test_entry_background_writer_persists_input_phases_and_terminal_frame(tmp_path, settings):
    frame = np.arange(1200, dtype=np.uint8).reshape(20, 20, 3)
    cache = entry.RoomEntryEvidence(settings, "versus", 42, {"song_unspecified": (0, 0)})
    template = frame[:5, :5].copy()
    cache.observe(frame, 10.0, {"song_unspecified": template})
    cache.phase, cache.target = "before-option", (780, 647)
    cache.mark()
    cache.phase = "after-option"
    cache.mark(input_result="sent")
    cache.observe(frame, 10.1, {"song_unspecified": template})
    cache.close(tmp_path, "stopped")
    deadline = time.monotonic() + 3
    summary, output = None, None
    while time.monotonic() < deadline:
        paths = list((tmp_path / cache.attempt_id).glob("*/summary.json"))
        if paths:
            try:
                summary = json.loads(paths[0].read_text(encoding="utf-8"))
                if summary["finalized"]:
                    output = paths[0].parent
                    break
            except json.JSONDecodeError:
                pass
        time.sleep(0.01)
    assert output is not None, summary
    assert summary["run_id"] is None
    assert summary["outcome"] == "stopped" and summary["write_errors"] == 0
    assert summary["last_frame"]["captured_at"] == 10.1
    rows = [json.loads(row) for row in (output / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(row["attempt_id"] == cache.attempt_id and row["task_id"] == 42 for row in rows)
    assert any(row["phase"] == "before-option" and row["input_target"] == [780, 647] for row in rows)
    assert any(row["phase"] == "after-option" and row.get("input_result") == "sent" for row in rows)
    if settings.get("opening_recording") or settings.get("debug_recording"):
        assert any(row["phase"] == "after-option" and row["captured_at"] == 10.1
                   and row["image_status"] == "saved" for row in rows)
    else:
        assert summary["frames_saved"] == 1
    assert (output / "last-frame.png").is_file()


def test_background_writer_close_failure_is_only_a_warning(tmp_path, monkeypatch, capsys):
    cache = entry.RoomEntryEvidence({"diagnostic_trace": True}, "versus", 42, {})
    cache.observe(np.zeros((1, 1, 3)), 1.0, {})

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    class Recorder:
        def __init__(self, root, run_id, **kwargs):
            self.output_dir = root

        def record(self, *args):
            pass

        def close(self, outcome):
            raise OSError("writer close failed")

    monkeypatch.setattr(entry.threading, "Thread", InlineThread)
    monkeypatch.setattr(entry, "CooperativeStartupRecorder", Recorder)
    cache.close(tmp_path, "failed")
    assert "RoomEntry diagnostics_warning=OSError: writer close failed" in capsys.readouterr().out


def test_disabled_diagnostics_never_copy_or_write_frames(tmp_path, monkeypatch):
    cache = entry.RoomEntryEvidence({}, "versus", None, {})
    cache.observe(object(), 1.0, {})
    monkeypatch.setattr(entry, "CooperativeStartupRecorder", lambda *args, **kwargs: pytest.fail("诊断关闭不能写盘"))
    cache.close(tmp_path, "failed")
    assert cache.events == [] and cache.frames == {} and cache.last_image is None


def _connection_flow(cls, monkeypatch, page="connection"):
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    clock, state, clicks = [0.0], [page], []
    flow.capture = lambda: state[0]
    flow.visible = lambda image, name, *args: (
        (image == "connection" and name in {"connect_failed_body", "member_exit_title", "ready_button"})
        or image == name)
    flow.pipeline_box = lambda image, node: object() if image == node else None
    flow.click = lambda point: clicks.append(point)
    monkeypatch.setattr(entry.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(entry.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    return flow, state, clicks, clock


@pytest.mark.parametrize("module,cls,prefix", MODES)
@pytest.mark.parametrize("window", ["wait", "entry", "delivery", "transition", "dismiss"])
def test_connection_body_precedes_generic_member_exit_in_every_preplay_window(monkeypatch, module, cls, prefix, window):
    flow, state, clicks, _ = _connection_flow(cls, monkeypatch)
    flow.playfield_entry_evidence = SimpleNamespace(reset=lambda: None)
    flow.make_final_cover_entry_resolver = lambda: None
    flow._startup_record = lambda *args, **kwargs: None
    monkeypatch.setattr(module, "_frame_is_black_transition", lambda image: False)
    calls = {
        "wait": lambda: flow.wait_for(("ready_button",), timeout=0),
        "entry": lambda: flow.verify_room_entry("timeout"),
        "delivery": lambda: flow.watch_ready_delivery_after_click(),
        "transition": lambda: flow.watch_member_exit_before_black(),
        "dismiss": flow.dismiss_member_exit,
    }
    with pytest.raises(entry.RoomEntryTransient, match="连接失败"):
        calls[window]()
    assert clicks == []


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_connection_recovery_retries_then_cancels_only_confirmed_popup(monkeypatch, module, cls, prefix):
    flow, state, clicks, clock = _connection_flow(cls, monkeypatch)
    def click(point):
        clicks.append(point)
        if point == (510, 527):
            state[0] = "room_search"
    flow.click = click
    flow.verify_preplay_recovery()
    assert clicks == [(748, 527)] * 3 + [(510, 527)]
    assert clock[0] < 60


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_connection_recovery_does_not_accept_background_ready_as_safe(monkeypatch, module, cls, prefix):
    flow, state, clicks, clock = _connection_flow(cls, monkeypatch)
    with pytest.raises(RuntimeError, match="安全页面"):
        flow.verify_preplay_recovery()
    assert (638, 525) not in clicks
    assert len(clicks) <= 6
    assert clock[0] <= 60.2


@pytest.mark.parametrize("module,cls,prefix", MODES)
@pytest.mark.parametrize("destination", ["room_search", "AutoLiveLoginScreenMarker", "ResourceDownloadConfirm", "ResourceDownloadProgressMarker"])
def test_connection_retry_can_deliver_safe_or_update_page(monkeypatch, module, cls, prefix, destination):
    flow, state, clicks, _ = _connection_flow(cls, monkeypatch)
    flow.click = lambda point: (clicks.append(point), state.__setitem__(0, destination))
    flow.verify_preplay_recovery()
    assert clicks == [(748, 527)]


@pytest.mark.parametrize("module,cls,prefix", MODES)
@pytest.mark.parametrize("stop_at", ["before", "capture", "click"])
def test_connection_recovery_stop_has_priority_over_next_input(monkeypatch, module, cls, prefix, stop_at):
    flow, state, clicks, _ = _connection_flow(cls, monkeypatch)
    flow.context.tasker.stopping = stop_at == "before"
    def capture():
        flow.context.tasker.stopping = stop_at == "capture"
        return "connection"
    def click(point):
        clicks.append(point)
        flow.context.tasker.stopping = True
    flow.capture, flow.click = capture, click
    with pytest.raises(InterruptedError):
        flow.verify_preplay_recovery()
    assert len(clicks) == (1 if stop_at == "click" else 0)


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_unknown_black_recovery_never_clicks(monkeypatch, module, cls, prefix):
    flow, _, clicks, _ = _connection_flow(cls, monkeypatch, page="black")
    with pytest.raises(RuntimeError, match="安全页面"):
        flow.verify_preplay_recovery()
    assert clicks == []


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_member_popup_reclassified_during_handler_still_recovers(monkeypatch, module, cls, prefix):
    flow, _, clicks, _ = _connection_flow(cls, monkeypatch)
    recoveries = []
    monkeypatch.setattr(module, "discard_prearmed_backend", lambda reason: None)
    flow.recover_room_preparation_timeout = recoveries.append
    assert flow.handle_member_exit(9) == 10
    assert len(recoveries) == 1 and "连接失败" in recoveries[0]
    assert clicks == []


@pytest.mark.parametrize("module,cls,prefix", MODES)
def test_repeated_connection_reentry_preserves_completed_count_and_play_budget(monkeypatch, module, cls, prefix):
    flow, _, _, _ = _connection_flow(cls, monkeypatch)
    flow.settings = {"count": 2, "entry_method": "normal", "post_live_action": "exit",
        "play_failure_retry_count": 0}
    results = iter([True] + [entry.RoomEntryTransient("连接失败")] * 8 + [True])
    calls, recoveries, progress = [], [], []
    def attempt(reuse_room=False):
        calls.append(reuse_room)
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result
    flow.run_attempt = attempt
    flow.return_to_room_selection = lambda: None
    flow.recover_room_preparation_timeout = recoveries.append
    flow.recover_after_play_failure = lambda reason: pytest.fail("连接失败不能消耗演出失败预算")
    flow.progress_callback = lambda done, total: progress.append((done, total))
    monkeypatch.setattr(module, "rematch_backoff", lambda *args: None)
    monkeypatch.setattr(module, "prepare_stamina_round", lambda *args: False)
    monkeypatch.setattr(module, "mark_stamina_settled", lambda *args: None)
    assert flow.run()
    assert len(calls) == 10 and len(recoveries) == 8
    assert progress == [(1, 2), (2, 2)]
