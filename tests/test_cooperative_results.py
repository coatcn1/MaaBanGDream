from __future__ import annotations

import base64
from types import SimpleNamespace
import zlib

import numpy as np
import pytest

from agent.realtime import profile_play_action
from agent.realtime.profile_play_action import ResultCollectionStatus
from agent.realtime.result_navigation import ResultNavigationOutcome, ResultNavigationStatus
from agent.realtime.result_parser import CooperativeResultParser, LiveResult
from agent.realtime.result_samples_v2 import RESULT_CROPS_V2_ZLIB_BASE64


def test_cooperative_parser_reads_known_digits_at_cooperative_positions():
    crops = np.frombuffer(
        zlib.decompress(base64.b64decode(RESULT_CROPS_V2_ZLIB_BASE64)), dtype=np.uint8,
    ).reshape(7, 32, 60)
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    positions = [(842, 308, 56), (842, 349, 56), (842, 391, 56),
                 (842, 431, 56), (842, 468, 56), (1087, 308, 60), (1087, 349, 60)]
    for crop, (x, y, width) in zip(crops, positions):
        image[y:y + 32, x:x + width] = (255 - crop[:, :width])[:, :, None]
    assert CooperativeResultParser().parse(image) == LiveResult(117, 11, 0, 0, 8, 4, 7, confidence=1.0)


def collect_cooperative(monkeypatch, readings, *, stop_at=None, capture_error=False,
                        lost_page=False, timeout_seconds=60.0):
    now = [0.0]
    parse_times = []
    captures = []
    advances = []
    values = iter(readings)
    last_value = [readings[-1]]
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    class Parser:
        def parse(self, image):
            parse_times.append(now[0])
            last_value[0] = next(values, last_value[0])
            if isinstance(last_value[0], Exception):
                raise last_value[0]
            return last_value[0]

    class Controller:
        def post_screencap(self):
            captures.append(now[0])
            if capture_error:
                raise OSError("截图暂时不可用")
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: frame))

    monkeypatch.setattr(profile_play_action, "CooperativeResultParser", Parser)
    monkeypatch.setattr(profile_play_action, "navigate_result_pages", lambda *args, **kwargs: ResultNavigationOutcome(
        ResultNavigationStatus.IDENTIFIED, image=frame, page_state="pggbm",
    ))
    monkeypatch.setattr(profile_play_action, "_template_click_point", lambda *args, **kwargs: (
        None if lost_page and captures else (100, 100)
    ))
    monkeypatch.setattr(profile_play_action, "accelerated_back", lambda *args, **kwargs: advances.append(now[0]))
    outcome = profile_play_action.collect_result(
        Controller(), lambda: stop_at is not None and now[0] >= stop_at,
        cooperative_mode=True, expected_notes=100,
        timeout_seconds=timeout_seconds,
        clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    return outcome, now[0], parse_times, captures, advances


def test_cooperative_waits_for_stable_full_counts_after_animation(monkeypatch):
    partial = LiveResult(45, 5, 0, 0, 0, 2, 3)
    full = LiveResult(90, 10, 0, 0, 0, 2, 3)
    outcome, elapsed, parsed, _, advances = collect_cooperative(monkeypatch, [partial, full, full])
    assert outcome.status is ResultCollectionStatus.ADVANCED
    assert outcome.result == full
    assert parsed == [0.0, 1.0, 2.0]
    assert elapsed == 2.0
    assert advances == [2.0]


@pytest.mark.parametrize("reading", [
    LiveResult(45, 5, 0, 0, 0, 2, 3),
    LiveResult(90, 10, 0, 0, 0, 2, 3, confidence=0.1),
    LiveResult(90, 10, 0, 0, 0, 80, 80),
    ValueError("数字置信度不足"),
])
def test_unreadable_cooperative_counts_do_not_block_navigation(monkeypatch, reading):
    outcome, elapsed, _, _, advances = collect_cooperative(monkeypatch, [reading])
    assert outcome.status is ResultCollectionStatus.ADVANCED
    assert outcome.result is None
    assert elapsed == 3.0
    assert advances == [3.0]


def test_changing_cooperative_counts_never_become_final_statistics(monkeypatch):
    readings = [LiveResult(p, 100 - p, 0, 0, 0, 2, 3) for p in (90, 80, 70)]
    outcome, elapsed, _, _, advances = collect_cooperative(monkeypatch, readings)
    assert outcome.result is None
    assert elapsed == 3.0
    assert advances == [3.0]


@pytest.mark.parametrize("failure", ["parser", "capture", "page-changed"])
def test_cooperative_collection_technical_faults_remain_warnings(monkeypatch, failure):
    reading = RuntimeError("分类器异常") if failure == "parser" else LiveResult(90, 10, 0, 0, 0, 2, 3)
    outcome, elapsed, _, _, advances = collect_cooperative(
        monkeypatch, [reading], capture_error=failure == "capture", lost_page=failure == "page-changed",
    )
    assert outcome.status is ResultCollectionStatus.ADVANCED
    assert outcome.result is None
    assert elapsed <= 1.0
    assert len(advances) == 1


def test_stop_during_cooperative_numeric_wait_prevents_capture_and_input(monkeypatch):
    outcome, elapsed, _, captures, advances = collect_cooperative(
        monkeypatch, [LiveResult(90, 10, 0, 0, 0, 2, 3)], stop_at=0.1,
    )
    assert outcome.status is ResultCollectionStatus.STOPPED
    assert elapsed == 0.1
    assert captures == []
    assert advances == []


def test_cooperative_numeric_budget_respects_remaining_navigation_timeout(monkeypatch):
    outcome, elapsed, _, _, advances = collect_cooperative(
        monkeypatch, [LiveResult(90, 10, 0, 0, 0, 2, 3)], timeout_seconds=0.5,
    )
    assert outcome.status is ResultCollectionStatus.ADVANCED
    assert outcome.result is None
    assert elapsed == 0.5
    assert advances == [0.5]
