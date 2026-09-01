from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import agent.realtime.cooperative_action as cooperative_action
from agent.realtime.cooperative_action import (
    COOPERATIVE_DIFFICULTY_TARGETS,
    MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
    CooperativeLiveFinalize,
    CooperativeLiveFlow,
    MemberExited,
    classify_room_tier,
    configure_cooperative_settings,
    cooperative_play_params,
    current_cooperative_settings,
    should_stay_in_room,
)


ROOT = Path(__file__).parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def room_frame(hue: int) -> np.ndarray:
    hsv = np.zeros((720, 1280, 3), dtype=np.uint8)
    hsv[194:487, 525:753] = (hue, 220, 220)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def test_room_tier_classifier_covers_all_four_carousel_cards():
    assert classify_room_tier(room_frame(90)) == "free"
    assert classify_room_tier(room_frame(174)) == "beginner"
    assert classify_room_tier(room_frame(103)) == "chief"
    assert classify_room_tier(room_frame(19)) == "legend"
    assert classify_room_tier(room_frame(55)) is None


@pytest.mark.parametrize(
    ("target", "initial_hue", "target_hue", "start", "end"),
    [
        ("free", 19, 90, (250, 360), (1050, 360)),
        ("legend", 90, 19, (1050, 360), (250, 360)),
    ],
)
def test_endpoint_room_selection_swipes_directly_without_resetting_carousel(
    monkeypatch, target, initial_hue, target_hue, start, end,
):
    flow = object.__new__(CooperativeLiveFlow)
    flow.context = object()
    flow.settings = {"room_tier": target}
    flow.ensure_room_page = lambda: room_frame(initial_hue)
    flow.capture = lambda: room_frame(target_hue)
    flow.close_sss_guide = lambda: None
    clicks = []
    flow.click = clicks.append
    verifications = []
    flow.verify_room_entry = verifications.append
    swipes = []
    monkeypatch.setattr(
        cooperative_action,
        "_maa_swipe",
        lambda _context, actual_start, actual_end, duration: swipes.append(
            (actual_start, actual_end, duration)
        ),
    )
    monkeypatch.setattr(cooperative_action.time, "sleep", lambda _seconds: None)

    flow.select_normal_room()

    assert swipes == [(start, end, 500)]
    assert clicks == [(1060, 650)]
    assert len(verifications) == 1


def test_room_entry_accepts_stable_departure_from_room_selection_without_narrow_lobby_marker(
    monkeypatch,
):
    selection = np.ones((2, 2, 3), dtype=np.uint8)
    transition = np.zeros((2, 2, 3), dtype=np.uint8)
    frames = iter([selection, transition, transition, transition])
    flow = object.__new__(CooperativeLiveFlow)
    flow.capture = lambda: next(frames)

    def visible(image, name, threshold=0.9):
        if name == "member_exit_title":
            return False
        if name == "room_search":
            return bool(image.any())
        return False

    flow.visible = visible
    monkeypatch.setattr(cooperative_action.time, "sleep", lambda _seconds: None)

    flow.verify_room_entry("must not be raised")


def test_normal_entry_ignores_stale_room_code_and_stay_setting():
    configure_cooperative_settings({"reset": True, "entry_method": "normal"})
    configure_cooperative_settings({"room_code": "941093"})
    configure_cooperative_settings({"post_live_action": "stay"})
    configure_cooperative_settings({"difficulty": "Special"})
    configure_cooperative_settings({"member_exit_policy": "reconnect"})
    settings = current_cooperative_settings()
    assert settings["entry_method"] == "normal"
    assert settings["room_code"] == "941093"
    assert settings["difficulty"] == "Special"
    assert settings["member_exit_policy"] == "reconnect"
    assert should_stay_in_room(settings) is False


def test_private_entry_requires_explicit_entry_selection():
    configure_cooperative_settings({"reset": True, "entry_method": "private"})
    configure_cooperative_settings({"room_code": "941093"})
    configure_cooperative_settings({"post_live_action": "stay"})
    settings = current_cooperative_settings()
    assert settings["entry_method"] == "private"
    assert should_stay_in_room(settings) is True


def test_invalid_cooperative_count_is_rejected_without_corrupting_settings():
    configure_cooperative_settings({"reset": True, "count": 3})
    with pytest.raises(ValueError, match="1到99"):
        configure_cooperative_settings({"count": 0})
    assert current_cooperative_settings()["count"] == 3


def test_cooperative_play_disables_life_abort_but_keeps_start_gate():
    params = cooperative_play_params(
        {
            "difficulty": "Hard",
            "debug_recording": False,
            "diagnostic_trace": False,
        }
    )
    assert MEMBER_DOWNLOAD_TIMEOUT_SECONDS == 60.0
    assert params["startup_timeout_seconds"] == 60
    assert params["completion_missing_frames"] == 30
    assert params["use_life_safety"] is False
    assert params["continue_after_life_depleted"] is True
    assert params["require_completion"] is True
    assert params["run_mode"] == "cooperative"
    assert params["diagnostic_trace"] is False


def test_cooperative_interface_exposes_requested_modes_and_five_difficulties():
    interface = load(ROOT / "interface.json")
    task = next(task for task in interface["task"] if task["name"] == "CooperativeLive")
    assert task["entry"] == "CooperativeLive"
    assert task["option"] == [
        "CooperativeEntryMethod",
        "CooperativeDifficulty",
        "CooperativeCount",
        "CooperativeMemberExitPolicy",
        "CooperativeDebug",
    ]
    options = interface["option"]
    assert [case["name"] for case in options["CooperativeEntryMethod"]["cases"]] == [
        "Normal", "Friend", "Private",
    ]
    private_case = options["CooperativeEntryMethod"]["cases"][2]
    assert options["CooperativeEntryMethod"]["cases"][0]["option"] == [
        "CooperativeRoomTier"
    ]
    assert options["CooperativeEntryMethod"]["cases"][1]["option"] == [
        "CooperativePostLiveAction"
    ]
    assert private_case["option"] == [
        "CooperativeRoomCode", "CooperativePostLiveAction"
    ]
    assert [case["name"] for case in options["CooperativeRoomTier"]["cases"]] == [
        "Free", "Beginner", "Chief", "Legend",
    ]
    assert [case["name"] for case in options["CooperativeDifficulty"]["cases"]] == [
        "Easy", "Normal", "Hard", "Expert", "Special",
    ]
    assert set(COOPERATIVE_DIFFICULTY_TARGETS) == {
        "Easy", "Normal", "Hard", "Expert", "Special",
    }
    assert [
        case["name"] for case in options["CooperativeMemberExitPolicy"]["cases"]
    ] == ["Fail", "Reconnect"]
    debug = options["CooperativeDebug"]
    assert debug["default_case"] == "Light"
    assert [case["name"] for case in debug["cases"]] == [
        "Light", "Off", "Full",
    ]
    params = {
        case["name"]: case["pipeline_override"]["CooperativeDebugConfigure"][
            "custom_action_param"
        ]
        for case in debug["cases"]
    }
    assert params == {
        "Light": {"debug_recording": False, "diagnostic_trace": True},
        "Off": {"debug_recording": False, "diagnostic_trace": False},
        "Full": {"debug_recording": True, "diagnostic_trace": True},
    }
    room_code = options["CooperativeRoomCode"]
    assert "description" not in room_code
    assert room_code["inputs"][0]["label"] == "输入房间号（六位）"
    assert room_code["inputs"][0]["verify"] == r"^(?:|[0-9]{6})$"
    count = options["CooperativeCount"]
    assert count["inputs"][0]["verify"] == r"^(?:[1-9]|[1-9][0-9])$"
    assert count["pipeline_override"]["CooperativeCountConfigure"][
        "custom_action_param"
    ] == {"count": "{Count}"}
    post_live = options["CooperativePostLiveAction"]
    assert post_live["default_case"] == "Exit"
    assert [case["name"] for case in post_live["cases"]] == ["Exit", "Stay"]
    stay_override = post_live["cases"][1]["pipeline_override"]
    assert stay_override["CooperativePostLiveConfigure"][
        "custom_action_param"
    ] == {"post_live_action": "stay"}
    assert "CooperativeRun" not in stay_override


def test_cooperative_pipeline_is_one_round_and_backs_out_of_repeat_popup():
    nodes = load(ROOT / "resource" / "pipeline" / "cooperative_live.json")
    assert nodes["CooperativeLive"]["next"] == ["CooperativeProcessConflictGuard"]
    assert nodes["CooperativeRun"]["next"] == ["CooperativeReturnHome"]
    assert nodes["CooperativeReturnHome"]["next"] == ["CooperativeComplete"]
    assert nodes["CooperativeReturnHome"]["custom_action"] == (
        "CooperativeLiveFinalize"
    )
    assert nodes["CooperativeCountConfigure"]["custom_action_param"] == {
        "count": 1
    }
    assert "max_hit" not in nodes["CooperativeRun"]
    params = nodes["CooperativeReturnHome"]["custom_action_param"]
    assert params["back_only"] is True
    assert "CooperativeRepeatRoomPopup" not in params["back_only_click_nodes"]
    assert params["back_acceleration_click_point"] == [1279, 719]
    assert nodes["CooperativeRepeatRoomPopup"]["template"] == (
        "cooperative/repeat_room_title.png"
    )


def test_cooperative_home_live_click_reuses_the_known_good_navigation_contract():
    cooperative = load(ROOT / "resource" / "pipeline" / "cooperative_live.json")
    realtime = load(ROOT / "resource" / "pipeline" / "realtime_multi_live.json")
    shared_fields = {
        "recognition",
        "template",
        "threshold",
        "action",
        "custom_action",
        "target",
        "post_delay",
    }
    cooperative_home = cooperative["CooperativeHomeLive"]
    realtime_home = realtime["RealtimeLiveHomeLive"]
    assert {
        key: cooperative_home[key] for key in shared_fields
    } == {
        key: realtime_home[key] for key in shared_fields
    }


def test_cooperative_templates_are_deployed_and_nonempty():
    image_dir = ROOT / "resource" / "image" / "cooperative"
    required = {
        "live_entry.png",
        "room_search.png",
        "search_private.png",
        "search_friend.png",
        "friend_invite_title.png",
        "private_room_title.png",
        "room_wait.png",
        "song_unspecified.png",
        "ready_button.png",
        "member_exit_title.png",
        "repeat_room_title.png",
        "sss_guide_close.png",
    }
    assert required == {path.name for path in image_dir.glob("*.png")}
    assert all(
        cv2.imread(str(image_dir / name), cv2.IMREAD_COLOR) is not None
        for name in required
    )


def test_member_exit_default_confirms_and_fails_without_reconnect():
    flow = object.__new__(CooperativeLiveFlow)
    flow.settings = {"member_exit_policy": "fail", "max_reconnects": 3}
    calls = []
    flow.run_attempt = lambda reuse_room=False: (
        _ for _ in ()
    ).throw(MemberExited())
    flow.dismiss_member_exit = lambda: calls.append("dismiss")
    flow.ensure_room_page = lambda timeout=15.0: calls.append("reconnect")
    assert flow.run() is False
    assert calls == ["dismiss"]


def test_member_exit_reconnect_is_bounded_and_reuses_original_route():
    flow = object.__new__(CooperativeLiveFlow)
    flow.settings = {"member_exit_policy": "reconnect", "max_reconnects": 3}
    attempts = iter([MemberExited(), MemberExited(), True])
    calls = []

    def run_attempt(reuse_room=False):
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    flow.run_attempt = run_attempt
    flow.dismiss_member_exit = lambda: calls.append("dismiss")
    flow.ensure_room_page = lambda timeout=15.0: calls.append("room")
    assert flow.run() is True
    assert calls == ["dismiss", "room", "dismiss", "room"]


def test_download_timeout_invokes_jump_instead_of_starting_engine():
    flow = object.__new__(CooperativeLiveFlow)
    flow.wait_for = lambda names, timeout, interval: (None, np.zeros((1, 1, 3)))
    jumped = []

    def jump():
        jumped.append(True)
        raise RuntimeError("jumped")

    flow.jump_after_download_timeout = jump
    with pytest.raises(RuntimeError, match="jumped"):
        flow.wait_for_playfield()
    assert jumped == [True]


def test_capture_uses_shared_safe_refresh_instead_of_cached_reverse_controller(
    monkeypatch,
):
    class ReverseControllerMustNotBeUsed:
        def post_screencap(self):
            raise OSError(
                "exception: access violation reading 0xFFFFFFFFFFFFFFFF"
            )

    context = SimpleNamespace(
        tasker=SimpleNamespace(
            stopping=False,
            controller=ReverseControllerMustNotBeUsed(),
        )
    )
    expected = np.zeros((720, 1280, 3), dtype=np.uint8)
    refreshes = []

    def safe_refresh(actual_context):
        refreshes.append(actual_context)
        return expected

    monkeypatch.setattr(
        cooperative_action,
        "capture_image",
        safe_refresh,
        raising=False,
    )
    flow = object.__new__(CooperativeLiveFlow)
    flow.context = context

    assert flow.capture() is expected
    assert refreshes == [context]


def test_access_violation_is_not_masked_by_a_second_screenshot_attempt():
    flow = object.__new__(CooperativeLiveFlow)
    captures = []
    flow.enter_room = lambda: (_ for _ in ()).throw(
        OSError("exception: access violation reading 0xFFFFFFFFFFFFFFFF")
    )
    flow.capture = lambda: captures.append(True)

    with pytest.raises(OSError, match="access violation"):
        flow.run_attempt()

    assert captures == []


def test_private_room_accepts_six_digit_code_and_types_it(monkeypatch):
    class Job:
        def wait(self):
            return self

    class Controller:
        def __init__(self):
            self.inputs = []

        def post_input_text(self, value):
            self.inputs.append(value)
            return Job()

    controller = Controller()
    flow = object.__new__(CooperativeLiveFlow)
    flow.context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller)
    )
    flow.settings = {"room_code": "941093"}
    flow.open_room_search = lambda: None
    flow.wait_for = lambda names, timeout: (
        "private_room_title",
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )
    flow.verify_room_entry = lambda _reason: None
    clicks = []
    flow.click = clicks.append
    monkeypatch.setattr(cooperative_action, "require_game_foreground", lambda _c: None)
    monkeypatch.setattr(cooperative_action.time, "sleep", lambda _seconds: None)

    flow.enter_private_room()

    assert controller.inputs == ["941093"]
    assert clicks == [(635, 528), (640, 370), (767, 474)]


def test_stay_in_room_confirms_repeat_popup_and_verifies_lobby(monkeypatch):
    flow = object.__new__(CooperativeLiveFlow)
    flow.settings = {"entry_method": "friend"}
    states = iter(["repeat_room_title", "room_wait"])
    flow.wait_for = lambda names, timeout: (
        next(states),
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )
    clicks = []
    flow.click = clicks.append
    monkeypatch.setattr(cooperative_action.time, "sleep", lambda _seconds: None)

    flow.stay_in_room()

    assert clicks == [(768, 447)]


def test_play_uses_realtime_result_navigator_without_a_second_pggbm_wait(monkeypatch):
    calls = []

    class Play:
        def run(self, _context, _argv):
            calls.append("realtime")
            return True

    monkeypatch.setattr(cooperative_action, "RealtimeProfilePlay", Play)
    flow = object.__new__(CooperativeLiveFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = cooperative_action.DEFAULT_SETTINGS.copy()

    assert flow.play() is True
    assert calls == ["realtime"]


def test_stay_in_room_rechecks_repeat_popup_after_every_accelerated_back(monkeypatch):
    class Job:
        def wait(self):
            return self

    class Controller:
        def __init__(self, actions):
            self.actions = actions

        def post_click_key(self, key):
            self.actions.append(("key", key))
            return Job()

    actions = []
    controller = Controller(actions)
    flow = object.__new__(CooperativeLiveFlow)
    flow.context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller)
    )
    flow.settings = {"entry_method": "private"}
    # There can be any number and kind of result pages between the settled
    # cooperative score and the repeat-room popup.
    states = iter([None, None, None, "repeat_room_title", "room_wait"])
    flow.wait_for = lambda names, timeout: (
        next(states),
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )
    flow.click = lambda point: actions.append(("click", point))
    flow.pipeline_box = lambda _image, _node: None
    monkeypatch.setattr(cooperative_action.time, "sleep", lambda _seconds: None)

    flow.stay_in_room()

    assert actions == [
        ("click", (1279, 719)),
        ("key", 4),
        ("click", (1279, 719)),
        ("click", (1279, 719)),
        ("key", 4),
        ("click", (1279, 719)),
        ("click", (768, 447)),
    ]


def test_return_to_room_selection_accelerates_each_page_without_extra_match():
    class Job:
        def wait(self):
            return self

    actions = []

    class Controller:
        def post_click_key(self, key):
            actions.append(("key", key))
            return Job()

    flow = object.__new__(CooperativeLiveFlow)
    flow.context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=Controller())
    )
    states = iter([None, "room_search"])
    flow.wait_for = lambda names, timeout: (
        next(states),
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )
    flow.click = lambda point: actions.append(("click", point))
    flow.pipeline_box = lambda _image, _node: None

    flow.return_to_room_selection()

    assert actions == [
        ("click", (1279, 719)),
        ("key", 4),
        ("click", (1279, 719)),
    ]


def test_return_to_room_selection_recognizes_home_and_reenters_before_next_round():
    class Job:
        def wait(self):
            return self

    actions = []

    class Controller:
        def post_click_key(self, key):
            actions.append(("key", key))
            return Job()

    flow = object.__new__(CooperativeLiveFlow)
    flow.context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=Controller())
    )
    unknown = np.zeros((1, 1, 3), dtype=np.uint8)
    home = np.ones((1, 1, 3), dtype=np.uint8)
    frames = iter([(None, unknown), (None, home)])
    flow.wait_for = lambda names, timeout: next(frames)
    flow.pipeline_box = lambda image, node: (
        SimpleNamespace(x=0, y=0, w=1, h=1)
        if image is home and node == "CooperativeHomeMarker"
        else None
    )
    flow.click = lambda point: actions.append(("click", point))
    reentries = []
    flow.navigate_to_cooperative_room_selection = reentries.append

    flow.return_to_room_selection()

    assert actions == [
        ("click", (1279, 719)),
        ("key", 4),
        ("click", (1279, 719)),
    ]
    assert reentries == ["home"]


def test_home_reentry_explicitly_opens_live_and_cooperative_room_selection(
    monkeypatch,
):
    frames = iter(["home", "live-select", "room-selection"])
    clicks = []
    flow = object.__new__(CooperativeLiveFlow)
    flow.capture = lambda: next(frames)
    flow.click = clicks.append
    flow.visible = lambda image, name, threshold=0.9: (
        (image == "live-select" and name == "live_entry")
        or (image == "room-selection" and name == "room_search")
    )
    flow.pipeline_box = lambda image, node: (
        SimpleNamespace(x=0, y=0, w=1, h=1)
        if image == "home" and node == "CooperativeHomeMarker"
        else None
    )
    flow.template_box = lambda image, name, threshold=0.9: (
        (975, 448, 170, 78)
        if image == "live-select" and name == "live_entry"
        else None
    )
    monkeypatch.setattr(cooperative_action.time, "sleep", lambda _seconds: None)

    flow.navigate_to_cooperative_room_selection("home")

    assert clicks == [(1175, 645), (1060, 487)]


def test_normal_matching_count_stops_without_extra_match_or_stay():
    flow = object.__new__(CooperativeLiveFlow)
    flow.settings = {
        "entry_method": "normal",
        "room_code": "941093",
        "post_live_action": "stay",
        "count": 2,
        "member_exit_policy": "fail",
        "max_reconnects": 3,
    }
    reuse_flags = []
    progress = []
    returns = []
    flow.progress_callback = lambda completed, total: progress.append(
        (completed, total)
    )
    flow.run_attempt = lambda reuse_room=False: reuse_flags.append(reuse_room) or True
    flow.return_to_room_selection = lambda: returns.append(True)
    flow.stay_in_room = lambda: pytest.fail("normal matching must ignore stay")

    assert flow.run() is True
    assert reuse_flags == [False, False]
    assert returns == [True]
    assert progress == [(1, 2), (2, 2)]


def test_private_stay_reuses_room_until_requested_count_is_complete():
    flow = object.__new__(CooperativeLiveFlow)
    flow.settings = {
        "entry_method": "private",
        "room_code": "941093",
        "post_live_action": "stay",
        "count": 3,
        "member_exit_policy": "fail",
        "max_reconnects": 3,
    }
    reuse_flags = []
    stays = []
    progress = []
    flow.progress_callback = lambda completed, total: progress.append(
        (completed, total)
    )
    flow.run_attempt = lambda reuse_room=False: reuse_flags.append(reuse_room) or True
    flow.stay_in_room = lambda: stays.append(True)
    flow.return_to_room_selection = lambda: pytest.fail(
        "stay mode must not leave and re-enter the room"
    )

    assert flow.run() is True
    assert reuse_flags == [False, True, True]
    assert stays == [True, True, True]
    assert progress == [(1, 3), (2, 3), (3, 3)]


def test_finalize_normal_matching_ignores_stay_and_returns_home(monkeypatch):
    configure_cooperative_settings(
        {"reset": True, "entry_method": "normal", "post_live_action": "stay"}
    )
    calls = []

    class Recover:
        def run(self, context, argv):
            calls.append((context, argv))
            return True

    monkeypatch.setattr(cooperative_action, "CommonRecover", Recover)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    argv = SimpleNamespace(custom_action_param="{}")

    assert CooperativeLiveFinalize().run(context, argv) is True
    assert calls == [(context, argv)]


def test_finalize_private_stay_does_not_leave_or_reenter_room(monkeypatch):
    configure_cooperative_settings(
        {"reset": True, "entry_method": "private", "post_live_action": "stay"}
    )

    class RecoverMustNotRun:
        def run(self, _context, _argv):
            raise AssertionError("completed stay mode must not return home")

    monkeypatch.setattr(cooperative_action, "CommonRecover", RecoverMustNotRun)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))

    assert CooperativeLiveFinalize().run(
        context, SimpleNamespace(custom_action_param="{}")
    ) is True
