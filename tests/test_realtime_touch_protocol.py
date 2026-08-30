from __future__ import annotations

import io

from agent.realtime.touch_planner import ActionKind, TouchAction
from agent.realtime.touch_protocol import MaaTouchProtocol


def test_transient_inputs_do_not_release_an_active_hold_contact():
    stream = io.BytesIO()
    touch = MaaTouchProtocol(stream)
    touch.dispatch([TouchAction(ActionKind.DOWN, 3, 1.0, 0)])
    touch.dispatch([
        TouchAction(ActionKind.TAP, 2, 1.1),
        TouchAction(ActionKind.FLICK, 4, 1.1),
    ])

    lines = stream.getvalue().decode("ascii").splitlines()
    # The long note remains contact 0; tap/flick get contacts 1 and 2.
    assert "d 1 490 590 50" in lines
    assert "d 2 790 590 50" in lines
    assert "u 0" not in lines
    assert touch.active_contacts == {0}


def test_tap_and_linked_hold_release_share_one_commit():
    stream = io.BytesIO()
    touch = MaaTouchProtocol(stream)
    touch.dispatch([TouchAction(ActionKind.DOWN, 5, 1.0, 5)])
    stream.seek(0)
    stream.truncate()

    touch.dispatch([
        TouchAction(ActionKind.TAP, 1, 2.0),
        TouchAction(ActionKind.UP, 5, 2.0, 5),
    ])

    commits = stream.getvalue().decode("ascii").strip().split("c\n")
    assert "d 0 340 590 50" in commits[0]
    assert "u 5" in commits[0]


def test_directional_flick_uses_horizontal_move():
    stream = io.BytesIO()
    touch = MaaTouchProtocol(stream)

    touch.dispatch([
        TouchAction(ActionKind.FLICK, 3, 1.0, flick_direction="Left"),
    ])

    lines = stream.getvalue().decode("ascii").splitlines()
    assert "d 0 640 590 50" in lines
    assert "m 0 490 590 50" in lines
    assert "m 0 640 490 50" not in lines
