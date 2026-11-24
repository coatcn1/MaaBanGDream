from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime import cooperative_action as module
from agent.realtime.cooperative_action import CooperativeLiveFlow, MemberExited, RoomPreparationTimeout
from agent.realtime.final_cover import FinalCoverStartupFailure
from agent.realtime.live_session import current_live_run, reset_live_run, update_live_run


@pytest.fixture
def countdown_flow(monkeypatch):
    now = [0.0]
    images = {name: np.full((720, 1280, 3), value, dtype=np.uint8)
              for name, value in (("song_unspecified", 100), ("ready_button", 101),
                                  ("unknown", 102), ("cover", 103), ("member_exit_title", 104))}
    images["black"] = np.zeros((720, 1280, 3), dtype=np.uint8)
    state = {id(image): name for name, image in images.items()}
    flow = object.__new__(CooperativeLiveFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {"song_choice": "manual", "difficulty": "Expert", "debug_recording": False,
                     "cooperative_member_loading_guard_enabled": True}
    flow.capture = lambda: images["song_unspecified"]
    flow.visible = lambda image, name, *_: state[id(image)] == name
    flow.click = lambda _: pytest.fail("自动准备观察不能发送点击")
    flow.play = lambda: pytest.fail("缺少实际难度及 Profile 时不能启动输入")
    flow.playfield_entry_evidence = SimpleNamespace(reset=lambda: None)
    flow._start_startup_diagnostics = lambda: None
    flow._startup_playfield_visible = lambda _: False
    monkeypatch.setattr(module, "check_preplay_connection_failed", lambda *_: None)
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    resolution = SimpleNamespace(confirmation=SimpleNamespace(song_id="current-cover"))
    calls = []

    def resolver():
        consecutive = [0]

        def observe(image):
            calls.append((now[0], state[id(image)]))
            consecutive[0] = consecutive[0] + 1 if image is images["cover"] else 0
            return resolution if consecutive[0] >= 2 else None

        return SimpleNamespace(observe=observe)

    flow.make_final_cover_entry_resolver = resolver
    reset_live_run(mode="cooperative", difficulty="Expert", prepared_for_play=False)
    return flow, now, images, calls


def test_confirmed_song_page_deadline_survives_unrecognised_frames(countdown_flow):
    flow, now, images, _ = countdown_flow
    flow.capture = lambda: images["song_unspecified"] if now[0] < 1 else images["unknown"]

    flow.wait_for_preparation()

    assert flow._song_choice_observed_at == 0
    assert 60 <= now[0] < 60.4
    assert flow._automatic_start_transition.reason == "countdown-expired"


def test_ready_page_has_thirty_second_deadline(countdown_flow):
    flow, now, images, _ = countdown_flow
    flow.capture = lambda: images["ready_button"] if now[0] < 1 else images["unknown"]
    flow.wait_for_preparation()
    assert flow._automatic_preparation_deadline == 30
    now[0] = 1
    flow.capture = lambda: images["cover"] if now[0] >= 30 else images["unknown"]

    with pytest.raises(FinalCoverStartupFailure, match="实际难度"):
        flow.prepare()

    assert 30 <= now[0] < 30.4


def test_early_black_hands_same_frame_to_cover_observation_without_input(countdown_flow):
    flow, now, images, calls = countdown_flow
    flow.capture = lambda: (images["song_unspecified"] if now[0] < 1 else
                            images["black"] if 12 <= now[0] < 13 else
                            images["cover"] if now[0] >= 13 else images["unknown"])
    flow.wait_for_preparation()
    assert 12 <= now[0] < 12.4
    assert flow._automatic_start_transition.image is images["black"]

    with pytest.raises(FinalCoverStartupFailure, match="未启动演奏输入"):
        flow.prepare()

    assert now[0] < 13.5
    assert current_live_run().startup_final_cover_image is not None
    assert len([entry for entry in calls if entry[1] == "cover"]) == 2


def test_stable_cover_is_observed_once_per_capture_and_reused(countdown_flow):
    flow, now, images, calls = countdown_flow
    flow.capture = lambda: images["song_unspecified"] if now[0] < 1 else images["cover"]
    flow.wait_for_preparation()
    assert [name for _, name in calls] == ["cover", "cover"]
    saved = current_live_run().startup_final_cover_resolution
    flow.capture = lambda: pytest.fail("已确认封面必须沿用同帧证据")

    with pytest.raises(FinalCoverStartupFailure, match="实际难度"):
        flow.prepare()

    assert current_live_run().startup_final_cover_resolution is saved


def test_unconfirmed_room_keeps_original_bounded_wait(countdown_flow):
    flow, now, images, _ = countdown_flow
    flow.capture = lambda: images["unknown"]
    with pytest.raises(RoomPreparationTimeout, match="180秒"):
        flow.wait_for_preparation()
    assert 180 <= now[0] < 181
    assert getattr(flow, "_automatic_preparation_deadline", None) is None


@pytest.mark.parametrize("event", ["stop", "member-exit"])
def test_automatic_wait_prioritises_stop_and_member_exit(countdown_flow, event):
    flow, now, images, _ = countdown_flow

    def capture():
        if now[0] >= 5:
            if event == "stop":
                flow.context.tasker.stopping = True
                raise InterruptedError("用户已停止任务")
            return images["member_exit_title"]
        return images["song_unspecified"] if now[0] < 1 else images["unknown"]

    flow.capture = capture
    with pytest.raises(InterruptedError if event == "stop" else MemberExited):
        flow.wait_for_preparation()
    assert now[0] < 6


def test_verified_preparation_can_reuse_confirmed_automatic_cover(countdown_flow):
    flow, _, images, _ = countdown_flow
    resolution = SimpleNamespace(confirmation=SimpleNamespace(song_id="current-cover"))
    update_live_run(startup_final_cover_image=images["cover"], startup_final_cover_resolution=resolution)
    flow._automatic_start_transition = module._AutomaticPreparationTransition(images["cover"], "final-cover")
    flow._preparation_gates_complete = True
    flow.capture = lambda: pytest.fail("稳定封面不能再采样")
    flow.prepare()
    assert current_live_run().startup_final_cover_resolution is resolution


def test_temporary_ready_marker_loss_resumes_verified_page(countdown_flow):
    flow, now, images, _ = countdown_flow
    flow._observe_preparation_countdown("ready_button", None)
    flow.capture = lambda: images["ready_button"] if now[0] >= 0.5 else images["unknown"]

    restored = flow._wait_for_automatic_start(images["unknown"])

    assert restored is images["ready_button"]
    assert 0.5 <= now[0] < 0.7
    assert flow._automatic_preparation_deadline == 30


def test_confirmed_current_song_hands_early_black_out_of_selection_loop(countdown_flow):
    flow, now, images, _ = countdown_flow
    flow.settings["song_choice"] = "current"
    flow.song_choice_pause_pending = False
    confirms = []

    def confirm(point):
        confirms.append(point)
        return True

    flow.guarded_song_choice_click = confirm
    flow.capture = lambda: images["black"] if confirms else images["song_unspecified"]
    flow.wait_for_preparation()

    assert confirms == [module.COOPERATIVE_SONG_CONFIRM_POINT]
    assert flow._automatic_start_transition.reason == "black-transition"
    assert flow._automatic_start_transition.image is images["black"]
    assert now[0] < 1


@pytest.mark.parametrize("failure", [MemberExited("退出"), RuntimeError("Profile 环境签名不符")])
def test_prepare_propagates_hard_failures_without_observation(countdown_flow, failure):
    flow, _, _, _ = countdown_flow
    flow._automatic_preparation_deadline = 30
    flow._prepare_from_page = lambda: (_ for _ in ()).throw(failure)
    flow._observe_automatic_start = lambda *_: pytest.fail("硬失败不能改写为自动准备")
    with pytest.raises(type(failure), match=str(failure)):
        flow.prepare()
