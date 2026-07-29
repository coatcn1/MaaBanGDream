from __future__ import annotations

import pytest

from agent.realtime.controller_touch import ControllerTouchDispatcher
from agent.realtime.touch_planner import ActionKind, TouchAction


class Job:
    def wait(self):
        return self


class Controller:
    def __init__(self):
        self.calls = []

    def post_touch_down(self, x, y, contact=0, pressure=1):
        self.calls.append(("down", contact, x, y, pressure))
        return Job()

    def post_touch_move(self, x, y, contact=0, pressure=1):
        self.calls.append(("move", contact, x, y, pressure))
        return Job()

    def post_touch_up(self, contact=0):
        self.calls.append(("up", contact))
        return Job()


def test_native_dispatch_keeps_hold_contact_while_tapping_and_flicking():
    controller = Controller()
    delays = []
    touch = ControllerTouchDispatcher(controller, lambda: False, sleeper=delays.append)
    touch.dispatch([TouchAction(ActionKind.DOWN, 3, 1.0, 3)])
    touch.dispatch([
        TouchAction(ActionKind.TAP, 2, 1.1),
        TouchAction(ActionKind.FLICK, 4, 1.1),
    ])

    assert ("up", 3) not in controller.calls
    assert delays == []
    flick_contacts = set(touch.active_contacts) - {3}
    assert len(flick_contacts) == 1
    assert touch.active_contacts == {3, *flick_contacts}

    touch.advance(1.117)
    touch.advance(1.134)
    touch.advance(1.151)
    touch.advance(1.168)

    assert touch.active_contacts == {3}
    flick_moves = [
        call for call in controller.calls
        if call[0] == "move" and call[1] != 3
    ]
    assert [call[3] for call in flick_moves] == [545, 490, 455]
    flick_contact = flick_moves[0][1]
    down_index = next(i for i, call in enumerate(controller.calls) if call[:2] == ("down", flick_contact))
    up_index = next(i for i, call in enumerate(controller.calls) if call == ("up", flick_contact))
    assert down_index < controller.calls.index(flick_moves[0]) < up_index


def test_flick_advance_can_share_ten_contacts_without_blocking():
    controller = Controller()
    touch = ControllerTouchDispatcher(controller, lambda: False)

    touch.dispatch([
        TouchAction(ActionKind.FLICK, lane, 1.0)
        for lane in range(7)
    ])

    assert len(touch.active_contacts) == 7
    touch.advance(1.017)
    touch.advance(1.034)
    touch.advance(1.051)
    touch.advance(1.068)
    assert touch.active_contacts == set()


def test_held_contact_converts_into_flick_swipe_without_repress():
    controller = Controller()
    touch = ControllerTouchDispatcher(controller, lambda: False)
    touch.dispatch([TouchAction(ActionKind.DOWN, 3, 1.0, 3)])
    downs_before = len([call for call in controller.calls if call[0] == "down"])

    touch.dispatch([TouchAction(ActionKind.FLICK, 3, 1.1, 3)])

    assert len([call for call in controller.calls if call[0] == "down"]) == downs_before
    assert ("up", 3) not in controller.calls
    assert touch.active_contacts == {3}

    touch.advance(1.117)
    touch.advance(1.134)
    touch.advance(1.151)
    touch.advance(1.168)

    assert touch.active_contacts == set()
    moves = [call for call in controller.calls if call[0] == "move" and call[1] == 3]
    assert [call[3] for call in moves] == [545, 490, 455]
    assert ("up", 3) in controller.calls


def test_same_frame_hold_release_precedes_contact_reuse():
    controller = Controller()
    touch = ControllerTouchDispatcher(controller, lambda: False)
    touch.dispatch([
        TouchAction(ActionKind.DOWN, 3, 1.0, contact=6, target_x=640),
    ])

    touch.dispatch([
        TouchAction(ActionKind.UP, 3, 1.1, contact=6, reason="new-hold-head"),
        TouchAction(
            ActionKind.DOWN,
            6,
            1.1,
            contact=6,
            reason="rescue",
            target_x=1060,
        ),
    ])

    assert controller.calls == [
        ("down", 6, 640, 590, 50),
        ("up", 6),
        ("down", 6, 1060, 590, 50),
    ]
    assert touch.active_contacts == {6}
    assert touch.active_positions == {6: 1060}


def test_stop_during_dispatch_releases_every_active_contact():
    controller = Controller()
    checks = 0

    def stopping():
        nonlocal checks
        checks += 1
        return checks >= 2

    touch = ControllerTouchDispatcher(controller, stopping)
    touch.active_contacts.add(5)

    with pytest.raises(InterruptedError):
        touch.dispatch([TouchAction(ActionKind.TAP, 1, 1.0)])

    assert ("up", 5) in controller.calls
    assert touch.active_contacts == set()


def test_close_always_releases_contacts():
    controller = Controller()
    touch = ControllerTouchDispatcher(controller, lambda: False)
    touch.active_contacts.update({1, 6})

    touch.close()

    assert controller.calls == [("up", 1), ("up", 6)]


def test_move_keeps_the_existing_hold_contact_and_uses_detected_x():
    controller = Controller()
    touch = ControllerTouchDispatcher(
        controller, lambda: False, maximum_move_step=100,
    )

    touch.dispatch([
        TouchAction(
            ActionKind.DOWN, 5, 1.0, contact=5, reason="hold",
            target_x=921,
        )
    ])
    touch.dispatch([
        TouchAction(
            ActionKind.MOVE, 6, 1.1, contact=5, reason="hold-follow",
            target_x=1013,
        )
    ])

    assert controller.calls == [
        ("down", 5, 921, 590, 50),
        ("move", 5, 1013, 590, 50),
    ]


def test_long_hold_move_is_interpolated_into_continuous_steps():
    controller = Controller()
    touch = ControllerTouchDispatcher(
        controller, lambda: False, maximum_move_step=80,
    )

    touch.dispatch([
        TouchAction(
            ActionKind.DOWN, 1, 1.0, contact=3, reason="hold",
            target_x=200,
        )
    ])
    touch.dispatch([
        TouchAction(
            ActionKind.MOVE, 4, 1.1, contact=3, reason="hold-follow",
            target_x=605,
        )
    ])

    moves = [call for call in controller.calls if call[0] == "move"]
    positions = [200, *(call[2] for call in moves)]
    assert moves[-1] == ("move", 3, 605, 590, 50)
    assert all(
        0 < right - left <= 80
        for left, right in zip(positions, positions[1:])
    )
    assert touch.active_positions == {3: 605}
