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
    touch = ControllerTouchDispatcher(controller, lambda: False)
    touch.dispatch([TouchAction(ActionKind.DOWN, 3, 1.0, 3)])
    touch.dispatch([
        TouchAction(ActionKind.TAP, 2, 1.1),
        TouchAction(ActionKind.FLICK, 4, 1.1),
    ])

    assert ("up", 3) not in controller.calls
    assert touch.active_contacts == {3}
    assert any(call[0] == "move" for call in controller.calls)


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
