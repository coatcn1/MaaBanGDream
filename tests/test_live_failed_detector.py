from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np

from agent.realtime.live_failed_detector import (
    LiveFailedPopupDetector,
    PROJECT_ROOT,
    exit_failed_live,
)
from agent.realtime import live_failed_detector


TEMPLATE_PATH = PROJECT_ROOT / "resource" / "image" / "live_failed_continue.png"


def _frame_with_button_at(x: int, y: int) -> np.ndarray:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    template = cv2.imread(str(TEMPLATE_PATH), cv2.IMREAD_COLOR)
    assert template is not None
    height, width = template.shape[:2]
    frame[y:y + height, x:x + width] = template
    return frame


def test_detector_confirms_popup_inside_roi_after_debounce():
    detector = LiveFailedPopupDetector()
    frame = _frame_with_button_at(653, 414)
    assert detector.observe(frame, 0.0) is False
    assert detector.observe(frame, 0.2) is False
    assert detector.observe(frame, 0.4) is True
    assert detector.triggered is True


def test_detector_ignores_button_outside_roi():
    detector = LiveFailedPopupDetector()
    frame = _frame_with_button_at(400, 422)
    for offset in (0.0, 0.2, 0.4, 0.6, 0.8):
        assert detector.observe(frame, offset) is False
    assert detector.triggered is False


def _exit_context(monkeypatch, stages, *, missing_first_exit=False):
    clock = [0.0]
    frame_index = [0]
    clicks = []
    recognition_calls = []
    monkeypatch.setattr(live_failed_detector.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        live_failed_detector.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration),
    )

    def capture():
        index = min(frame_index[0], len(stages) - 1)
        frame_index[0] += 1
        return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: stages[index]))

    def recognize(name, stage):
        recognition_calls.append((name, stage))
        hit = (
            (name == "RealtimeLiveHomeMarker" and stage == "home")
            or (name == "RealtimeLiveFailedContinue" and stage == "first")
            or (name == "RealtimeLiveFailedExit" and stage == "first" and not missing_first_exit)
            or (name == "RealtimeQuitConfirmExit" and stage in {"first", "second"})
        )
        box = SimpleNamespace(x=390, y=414, w=238, h=71)
        if name == "RealtimeQuitConfirmExit":
            box = SimpleNamespace(x=653, y=414 if stage == "first" else 493, w=234, h=71)
        return SimpleNamespace(hit=hit, box=box)

    def click(x, y):
        clicks.append((x, y))
        return SimpleNamespace(wait=lambda: None)

    return SimpleNamespace(
        tasker=SimpleNamespace(
            stopping=False,
            controller=SimpleNamespace(post_screencap=capture, post_click=click),
        ),
        run_recognition=recognize,
    ), clicks, recognition_calls


def test_failed_exit_never_treats_first_continue_as_confirm_exit(monkeypatch):
    context, clicks, calls = _exit_context(
        monkeypatch, ["first", "first", "second", "home"],
    )
    assert exit_failed_live(context, timeout_seconds=10.0) is True
    assert clicks == [(509, 449), (509, 449), (770, 528)]
    assert ("RealtimeQuitConfirmExit", "first") not in calls


def test_failed_exit_missing_gray_button_cannot_click_paid_continue(monkeypatch):
    context, clicks, calls = _exit_context(
        monkeypatch, ["first"], missing_first_exit=True,
    )
    assert exit_failed_live(context, timeout_seconds=2.0) is False
    assert clicks == []
    assert ("RealtimeQuitConfirmExit", "first") not in calls


def test_failed_exit_requires_first_stage_before_second_confirmation(monkeypatch):
    context, clicks, _ = _exit_context(monkeypatch, ["second"])
    assert exit_failed_live(context, timeout_seconds=2.0) is False
    assert clicks == []


def test_failed_exit_stopped_before_capture_sends_no_input(monkeypatch):
    context, clicks, calls = _exit_context(monkeypatch, ["first"])
    context.tasker.stopping = True

    def unexpected_capture():
        raise AssertionError("人工停止后不得追加截图")

    context.tasker.controller.post_screencap = unexpected_capture
    assert exit_failed_live(context, timeout_seconds=2.0) is True
    assert clicks == []
    assert calls == []


def test_failed_exit_stopped_after_exit_recognition_sends_no_input(monkeypatch):
    context, clicks, calls = _exit_context(monkeypatch, ["first"])
    original_recognize = context.run_recognition

    def recognize_then_stop(name, stage):
        result = original_recognize(name, stage)
        if name == "RealtimeLiveFailedExit":
            context.tasker.stopping = True
        return result

    context.run_recognition = recognize_then_stop
    assert exit_failed_live(context, timeout_seconds=2.0) is True
    assert clicks == []
    assert [name for name, _stage in calls] == [
        "RealtimeLiveHomeMarker", "RealtimeLiveFailedContinue", "RealtimeLiveFailedExit",
    ]


def test_detector_ignores_plain_playfield():
    detector = LiveFailedPopupDetector()
    frame = np.full((720, 1280, 3), 24, dtype=np.uint8)
    for offset in (0.0, 0.2, 0.4, 0.6, 0.8):
        assert detector.observe(frame, offset) is False
    assert detector.triggered is False
