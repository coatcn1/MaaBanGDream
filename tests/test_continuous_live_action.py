import json

import numpy as np

from agent.realtime.continuous_live_action import (
    ListenerDiagnosticCapture,
    continuous_song_params,
    run_continuous_listener,
)
from agent.realtime.life_monitor import LifeReading


class Detector:
    def __init__(self, readings):
        self.readings = iter(readings)

    def detect(self, _image):
        return next(self.readings)


def test_listener_has_no_input_on_non_playfield_pages():
    captures = []
    plays = []
    stopped = False

    def capture():
        captures.append("capture")
        return object()

    def stopping():
        nonlocal stopped
        if len(captures) >= 5:
            stopped = True
        return stopped

    run_continuous_listener(
        capture,
        stopping,
        lambda: plays.append("play") or True,
        detector=Detector([LifeReading(False)] * 5),
        sleeper=lambda _: None,
    )

    assert len(captures) == 5
    assert plays == []


def test_listener_triggers_multiple_songs_after_three_visible_frames():
    readings = [
        LifeReading(False),
        LifeReading(True, 1000),
        LifeReading(True, 1000),
        LifeReading(True, 1000),
        LifeReading(False),
        LifeReading(True, 800),
        LifeReading(True, 800),
        LifeReading(True, 800),
    ]
    plays = []

    def play_song():
        plays.append("song")
        return True

    run_continuous_listener(
        lambda: object(),
        lambda: len(plays) >= 2,
        play_song,
        detector=Detector(readings),
        sleeper=lambda _: None,
    )

    assert plays == ["song", "song"]


def test_listener_observes_non_playfield_frames_for_stop_diagnostics():
    observed = []

    run_continuous_listener(
        lambda: "cooperative-live-frame",
        lambda: len(observed) >= 3,
        lambda: True,
        detector=Detector([LifeReading(False)] * 3),
        sleeper=lambda _: None,
        on_frame=observed.append,
    )

    assert observed == ["cooperative-live-frame"] * 3


def test_listener_diagnostic_saves_last_waiting_frame_on_stop(tmp_path):
    diagnostic = ListenerDiagnosticCapture(tmp_path)
    diagnostic.observe(np.full((4, 6, 3), 127, dtype=np.uint8))

    path = diagnostic.save("stopped")

    assert path is not None
    assert path.name == "last-frame.png"
    assert path.exists()
    metadata = json.loads((path.parent / "metadata.json").read_text(encoding="utf-8"))
    assert metadata == {"reason": "stopped"}


def test_one_key_playback_never_enables_life_safety_pause():
    params = continuous_song_params({
        "difficulty": "Expert",
        "use_life_safety": True,
        "continue_after_life_depleted": False,
    })

    assert params["use_life_safety"] is False
    assert params["continue_after_life_depleted"] is True
