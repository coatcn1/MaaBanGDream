from types import SimpleNamespace
import cv2
import numpy as np
import pytest

from agent.realtime.live_boost_gate import (
    BoostDetector,
    LiveBoostGate,
    TEMPLATE_REGIONS,
)
from agent.realtime.profile_store import RealtimeProfileStore


def test_runtime_default_and_explicit_zero_are_preserved(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    assert store.runtime_options()["live_boost_target"] == 3
    store.update_runtime_options(dict(store.runtime_options(), live_boost_target=0))
    assert RealtimeProfileStore(tmp_path).runtime_options()["live_boost_target"] == 0


@pytest.mark.parametrize("enabled", [None, False, True])
def test_ensure_boost_disabled_skips_all_device_work_and_preserves_old_default(tmp_path, monkeypatch, enabled):
    import json
    from agent.realtime import live_boost_gate as module

    options = {"live_boost_target": 2}
    if enabled is not None:
        options["live_boost_settings_enabled"] = enabled
    (tmp_path / "selection.json").write_text(
        json.dumps({"version": 1, "pinned": {}, "runtime_options": options}), encoding="utf-8",
    )
    monkeypatch.setattr(module, "RealtimeProfileStore", lambda _: RealtimeProfileStore(tmp_path))
    calls = []

    class Gate:
        def __init__(self, context):
            assert enabled is not False, "关闭时不得构造设备门禁"
            calls.append(context)

        def ensure(self, target):
            assert target == 2
            return True

    monkeypatch.setattr(module, "LiveBoostGate", Gate)
    monkeypatch.setattr(module, "detector", lambda: pytest.fail("此边界不能构造 detector"))
    context = object()
    assert module.ensure_live_boost(context) is (enabled is not False)
    assert calls == ([] if enabled is False else [context])
    assert RealtimeProfileStore(tmp_path).runtime_options()["live_boost_target"] == 2


@pytest.mark.parametrize("bad", [True, False, 1.0, "3", -1, 4, None])
def test_runtime_boost_rejects_non_integer_or_out_of_range(bad):
    with pytest.raises(ValueError, match="live_boost_target"):
        RealtimeProfileStore._validated_runtime_options({"live_boost_target": bad})


def fixture_frame(detector, panel=False, selected=0):
    image = np.zeros((720, 1280, 3), np.uint8)
    for name, (x, y, w, h) in TEMPLATE_REGIONS.items():
        if name.startswith("page") == (not panel):
            image[y : y + h, x : x + w] = detector.templates[name]
    if panel:
        for value, y in [(0, 133), (1, 210), (2, 287), (3, 364), (10, 518)]:
            cv2.circle(image, (1002, y), 13, (240, 240, 240), -1)
            if selected == value:
                cv2.circle(image, (1002, y), 9, (150, 50, 255), -1)
    return image


def test_panel_reads_consumption_not_reward_multiplier():
    detector = BoostDetector()
    for selected in (0, 1, 2, 3, 10):
        image = fixture_frame(detector, True, selected)
        assert detector.panel_selection(image) == selected
    image = fixture_frame(detector, True, 0)
    cv2.circle(image, (1002, 210), 9, (150, 50, 255), -1)
    assert detector.panel_selection(image) is None
    assert detector.panel_selection(np.zeros((720, 1280, 3), np.uint8)) is None


def test_all_consumption_dynamic_number_is_not_a_layout_dependency():
    detector = BoostDetector()
    image = fixture_frame(detector, True, 10)
    cv2.putText(image, "4", (305, 532), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    assert detector.panel_selection(image) == 10


@pytest.mark.parametrize("selected", [3, None])
def test_visible_boost_panel_excludes_dimmed_background_page(selected):
    detector = BoostDetector()
    image = fixture_frame(detector, True, selected)
    for name in ("page_title", "page_settings"):
        x, y, w, h = TEMPLATE_REGIONS[name]
        image[y:y + h, x:x + w] = (detector.templates[name] * 0.5).astype(np.uint8)
        assert detector.matches(image, name)
    assert detector.matches(image, "panel_title")
    assert detector.panel_selection(image) == selected
    assert not detector.page(image)


class Context:
    def __init__(self, detector, *, saved=0):
        self.detector = detector
        self.panel = False
        self.saved = saved
        self.selected = saved
        self.clicks = []
        self.fail_save = False
        self.tasker = SimpleNamespace(stopping=False, controller=self)

    def post_click(self, x, y):
        self.clicks.append((x, y))
        if (x, y) == (1110, 145):
            self.panel = True
            self.selected = self.saved
        elif (x, y) == (640, 640):
            if not self.fail_save:
                self.saved = self.selected
            self.panel = False
        else:
            self.selected = {133: 0, 210: 1, 287: 2, 364: 3}[y]
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))

    def capture(self):
        return fixture_frame(self.detector, self.panel, self.selected)


def runner(context):
    return LiveBoostGate(
        context,
        detector=context.detector,
        capture=lambda _: context.capture(),
        foreground=lambda _: None,
        sleep=lambda _: None,
    )


def test_gate_changes_once_then_reopens_and_proves_saved():
    context = Context(BoostDetector(), saved=10)
    assert runner(context).ensure(3)
    assert context.clicks == [
        (1110, 145),
        (1002, 364),
        (640, 640),
        (1110, 145),
        (640, 640),
    ]
    assert context.saved == 3


def test_failed_readback_stops_without_second_correction():
    context = Context(BoostDetector(), saved=0)
    context.fail_save = True
    with pytest.raises(RuntimeError):
        runner(context).ensure(3)
    assert context.clicks.count((1002, 364)) == 1


def test_unknown_page_never_clicks():
    context = Context(BoostDetector())
    context.capture = lambda: np.zeros((720, 1280, 3), np.uint8)
    with pytest.raises(RuntimeError):
        runner(context).ensure(3)
    assert not context.clicks


def test_stopping_after_foreground_check_prevents_next_input():
    from agent.screen_refresh import ScreenRefreshCancelled

    context = Context(BoostDetector())
    gate = LiveBoostGate(
        context,
        detector=context.detector,
        capture=lambda _: context.capture(),
        foreground=lambda _: setattr(context.tasker, "stopping", True),
        sleep=lambda _: None,
    )
    with pytest.raises(ScreenRefreshCancelled):
        gate.ensure(3)
    assert not context.clicks


def test_swallowed_ok_click_is_retried_until_panel_closes():
    """面板开启动画期间 OK 点击被吞掉时，按面板可见性重试而不是直接失败。"""
    class Swallow(Context):
        def __init__(self, detector, *, saved=0, swallow=1):
            super().__init__(detector, saved=saved)
            self.swallow = swallow

        def post_click(self, x, y):
            if (x, y) == (640, 640) and self.swallow:
                self.swallow -= 1
                self.clicks.append((x, y))
                return SimpleNamespace(
                    wait=lambda: SimpleNamespace(succeeded=True)
                )
            return super().post_click(x, y)

    context = Swallow(BoostDetector(), saved=1)
    assert runner(context).ensure(1)
    assert context.clicks.count((640, 640)) == 3
    assert context.saved == 1


def test_ok_click_that_never_closes_fails_after_bounded_retries():
    class NeverCloses(Context):
        def post_click(self, x, y):
            if (x, y) == (640, 640):
                self.clicks.append((x, y))
                return SimpleNamespace(
                    wait=lambda: SimpleNamespace(succeeded=True)
                )
            return super().post_click(x, y)

    context = NeverCloses(BoostDetector(), saved=1)
    with pytest.raises(RuntimeError, match="OK 点击未生效"):
        runner(context).ensure(1)
    assert context.clicks.count((640, 640)) == 3


def test_foreground_proxy_replacement_is_consumed_before_click():
    context = Context(BoostDetector())
    fresh = SimpleNamespace(clicks=[])
    fresh.post_click = lambda x, y: (
        fresh.clicks.append((x, y))
        or SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))
    )
    gate = LiveBoostGate(
        context,
        detector=context.detector,
        foreground=lambda _: setattr(context.tasker, "controller", fresh),
    )
    gate.click(1110, 145)
    assert fresh.clicks == [(1110, 145)] and not context.clicks
