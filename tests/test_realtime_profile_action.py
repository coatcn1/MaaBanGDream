from __future__ import annotations

import pytest

import ctypes

from maa.define import MaaControllerHandle
from maa.library import Library

from agent.realtime.profile_action import (
    build_draft_payload,
    ensure_post_shell_binding,
    parse_density,
)


def test_density_prefers_android_override():
    assert parse_density("Physical density: 320\nOverride density: 240\n") == 240


def test_density_rejects_unrecognised_output():
    with pytest.raises(ValueError, match="DPI"):
        parse_density("unknown")


def test_profile_draft_is_never_accepted_automatically():
    payload = build_draft_payload(
        {
            "difficulty": "Easy",
            "game_fps": 60,
            "render_quality": "standard",
            "note_speed": 2.0,
            "target_fps": 60,
            "timing_offset_ms": 0,
        },
        (1280, 720),
        "Override density: 240",
    )

    assert payload["accepted"] is False
    assert payload["environment"] == {
        "resolution": [1280, 720],
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
        "note_speed": 2.0,
    }


def test_maafw_5102_shell_binding_uses_full_controller_handle():
    ensure_post_shell_binding()
    function = Library.framework().MaaControllerPostShell

    assert function.argtypes == [MaaControllerHandle, ctypes.c_char_p, ctypes.c_int64]
