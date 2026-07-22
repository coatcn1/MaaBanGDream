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
    assert touch.active_contacts == {3}
    flick_moves = [call for call in controller.calls if call[0] == "move"]
    assert [call[3] for call in flick_moves] == [545, 490, 455]
    assert sum(delays) >= .03
    flick_contact = flick_moves[0][1]
    down_index = next(i for i, call in enumerate(controller.calls) if call[:2] == ("down", flick_contact))
    up_index = next(i for i, call in enumerate(controller.calls) if call == ("up", flick_contact))
    assert down_index < controller.calls.index(flick_moves[0]) < up_index


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
