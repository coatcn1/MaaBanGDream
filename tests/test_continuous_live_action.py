from agent.realtime.continuous_live_action import run_continuous_listener
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
