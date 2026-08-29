from __future__ import annotations

import cv2
import numpy as np
from agent.realtime import profile_play_action
from agent.realtime.profile_play_action import (
    ResultCollectionStatus,
    collect_result,
)
from agent.realtime.result_parser import LiveResult


class Job:
    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class Controller:
    def __init__(self):
        self.frames = [
            np.zeros((720, 1280, 3), dtype=np.uint8),
            np.ones((720, 1280, 3), dtype=np.uint8),
            np.ones((720, 1280, 3), dtype=np.uint8),
        ]
        self.backs = 0

    def post_screencap(self):
        return Job(self.frames.pop(0))

    def post_click_key(self, key):
        assert key == 4
        self.backs += 1
        return Job()


class Parser:
    def parse(self, image):
        if not image.any():
            raise ValueError("not result")
        return LiveResult(170, 42, 0, 0, 3, 33, 9, .8)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_result_collection_checks_each_frame_without_blind_back_input():
    controller = Controller()
    clock = Clock()

    outcome = collect_result(
        controller,
        lambda: False,
        stability_interval_seconds=1,
        parser=Parser(),
        clock=clock.monotonic,
        sleeper=clock.sleep,
        judgement_details_template=None,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.result.fast == 33
    assert outcome.result.slow == 9
    assert outcome.image.any()
    assert controller.backs == 0


def test_result_collection_clicks_recognised_rank_page_next_before_details():
    template = cv2.imread(str(profile_play_action.RESULT_NEXT_TEMPLATE))
    rank_page = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    rank_page[619:619 + height, 927:927 + width] = template
    details = np.ones((720, 1280, 3), dtype=np.uint8)
    frames = [rank_page, details, details]
    clicks = []
    backs = []
    foreground_checks = []

    class RankThenDetailsParser:
        def parse(self, image):
            if image is rank_page:
                # The static rank page can accidentally satisfy the fixed
                # digit crops and look stable.  Its recognised Next marker
                # must take priority over this impossible parse.
                return LiveResult(1888, 1170, 7048, 1481, 4111, 2888, 8888, .8)
            return LiveResult(170, 42, 0, 0, 3, 33, 9, .8)

    class RankThenDetailsController:
        def post_screencap(self):
            return Job(frames.pop(0))

        def post_click(self, x, y):
            clicks.append((x, y))
            return Job()

        def post_click_key(self, key):
            backs.append(key)
            return Job()

    clock = Clock()
    outcome = collect_result(
        RankThenDetailsController(),
        lambda: False,
        parser=RankThenDetailsParser(),
        clock=clock.monotonic,
        sleeper=clock.sleep,
        result_next_click_delay_seconds=0.0,
        before_input=lambda: foreground_checks.append(1),
        judgement_details_template=None,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.result is not None
    assert clicks == [(927 + width // 2, 619 + height // 2)]
    assert backs == []
    assert foreground_checks == [1]


def test_result_collection_stops_without_back_input():
    controller = Controller()

    outcome = collect_result(controller, lambda: True, parser=Parser())

    assert outcome.status is ResultCollectionStatus.STOPPED
    assert controller.backs == 0


def test_result_collection_checks_foreground_before_recognised_input():
    template = cv2.imread(str(profile_play_action.RESULT_NEXT_TEMPLATE))
    rank_page = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    rank_page[100:100 + height, 200:200 + width] = template

    class RankController:
        def post_screencap(self):
            return Job(rank_page)

        def post_click(self, *_point):
            raise AssertionError("foreground guard must run before click")

    controller = RankController()

    def reject_foreign_app():
        raise RuntimeError("foreign foreground")

    try:
        collect_result(
            controller, lambda: False, parser=Parser(),
            before_input=reject_foreign_app,
        )
    except RuntimeError as exc:
        assert "foreign foreground" in str(exc)
    else:
        raise AssertionError("foreground rejection must propagate")



class AnimatingController:
    """Result panel whose numbers count up across screenshots."""

    def __init__(self):
        self.frames = [
            np.full((720, 1280, 3), 1, dtype=np.uint8),
            np.full((720, 1280, 3), 2, dtype=np.uint8),
            np.full((720, 1280, 3), 3, dtype=np.uint8),
            np.full((720, 1280, 3), 3, dtype=np.uint8),
        ]
        self.backs = 0

    def post_screencap(self):
        return Job(self.frames.pop(0))

    def post_click_key(self, key):
        assert key == 4
        self.backs += 1
        return Job()


class AnimatingParser:
    COUNTS = {
        1: LiveResult(1, 0, 0, 0, 0, 1, 0, .9),
        2: LiveResult(120, 9, 0, 0, 1, 10, 2, .9),
        3: LiveResult(374, 24, 0, 0, 6, 23, 1, .9),
    }

    def parse(self, image):
        return self.COUNTS[int(image.flat[0])]


def test_result_collection_waits_for_count_up_animation_to_settle():
    controller = AnimatingController()
    clock = Clock()

    outcome = collect_result(
        controller,
        lambda: False,
        stability_interval_seconds=1,
        parser=AnimatingParser(),
        clock=clock.monotonic,
        sleeper=clock.sleep,
        judgement_details_template=None,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.result.perfect == 374
    assert outcome.result.miss == 6
    assert int(outcome.image.flat[0]) == 3
    # The panel is already visible, so settling must never press BACK.
    assert controller.backs == 0


def test_result_timeout_never_blindly_presses_escape():
    clock = Clock()

    class NoResultController:
        def __init__(self):
            self.esc_times = []

        def post_screencap(self):
            return Job(np.zeros((720, 1280, 3), dtype=np.uint8))

        def post_click_key(self, key):
            assert key == 4
            self.esc_times.append(clock.now)
            return Job()

    class NoResultParser:
        def parse(self, _image):
            raise ValueError("not result")

    controller = NoResultController()
    outcome = collect_result(
        controller,
        lambda: False,
        parser=NoResultParser(),
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert outcome.status is ResultCollectionStatus.TIMED_OUT
    assert outcome.elapsed_seconds == 60
    assert controller.esc_times == []


def test_result_collection_rejects_stable_impossible_total_against_chart():
    clock = Clock()
    frames = [
        np.full((720, 1280, 3), value, dtype=np.uint8)
        for value in (1, 1, 2, 2)
    ]

    class ExpectedCountController:
        def post_screencap(self):
            return Job(frames.pop(0))

    class ExpectedCountParser:
        def parse(self, image):
            if int(image.flat[0]) == 1:
                return LiveResult(200, 0, 0, 0, 0, 10, 5, .95)
            return LiveResult(320, 5, 0, 0, 1, 10, 5, .95)

    outcome = collect_result(
        ExpectedCountController(),
        lambda: False,
        parser=ExpectedCountParser(),
        expected_notes=326,
        clock=clock.monotonic,
        sleeper=clock.sleep,
        stability_interval_seconds=1,
        judgement_details_template=None,
    )
    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.result.total == 326


def test_result_collection_never_parses_without_judgement_page_identity():
    clock = Clock()
    rank_like_page = np.full((720, 1280, 3), 17, dtype=np.uint8)

    class RankLikeController:
        def post_screencap(self):
            return Job(rank_like_page)

    class MustNotParseRankPage:
        calls = 0

        def parse(self, _image):
            self.calls += 1
            return LiveResult(1888, 1170, 7048, 1481, 4111, 0, 0, .9)

    parser = MustNotParseRankPage()
    outcome = collect_result(
        RankLikeController(),
        lambda: False,
        parser=parser,
        timeout_seconds=2,
        slow_interval_seconds=1,
        medium_interval_seconds=1,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert outcome.status is ResultCollectionStatus.TIMED_OUT
    assert outcome.page_state == "unknown"
    assert parser.calls == 0


def test_result_collection_parses_only_after_judgement_identity_matches():
    template = cv2.imread(
        str(profile_play_action.JUDGEMENT_DETAILS_TEMPLATE)
    )
    details = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    details[270:270 + height, 760:760 + width] = template
    frames = [details, details]

    class DetailsController:
        def post_screencap(self):
            return Job(frames.pop(0))

    class DetailsParser:
        def parse(self, _image):
            return LiveResult(350, 18, 1, 0, 1, 8, 11, .95)

    clock = Clock()
    outcome = collect_result(
        DetailsController(),
        lambda: False,
        parser=DetailsParser(),
        expected_notes=370,
        stability_interval_seconds=1,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.page_state == "judgement-details"
    assert outcome.result is not None
    assert outcome.result.total == 370


def test_reward_popup_that_does_not_disappear_is_technical_failure():
    template = cv2.imread(str(profile_play_action.REWARD_CONFIRM_TEMPLATE))
    reward = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    reward[100:100 + height, 200:200 + width] = template
    clicks = []

    class PersistentRewardController:
        def post_screencap(self):
            return Job(reward.copy())

        def post_click(self, x, y):
            clicks.append((x, y))
            return Job()

    class NoResultParser:
        def parse(self, _image):
            raise ValueError("not result")

    clock = Clock()
    outcome = collect_result(
        PersistentRewardController(),
        lambda: False,
        parser=NoResultParser(),
        reward_templates=(profile_play_action.REWARD_CONFIRM_TEMPLATE,),
        reward_dismiss_limit=1,
        reward_click_delay_seconds=0,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert outcome.status is ResultCollectionStatus.BLOCKED
    assert outcome.page_state == "reward-popup"
    assert "did not disappear" in outcome.reason
    assert len(clicks) == 1


def test_activity_points_page_is_advanced_before_collecting_judgement_details():
    template = cv2.imread(
        str(profile_play_action.ACTIVITY_POINTS_TEMPLATE)
    )
    page = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    page[375:375 + height, 838:838 + width] = template

    details_template = cv2.imread(
        str(profile_play_action.JUDGEMENT_DETAILS_TEMPLATE)
    )
    details = np.zeros((720, 1280, 3), dtype=np.uint8)
    details_height, details_width = details_template.shape[:2]
    details[270:270 + details_height, 760:760 + details_width] = (
        details_template
    )

    frames = [page, details, details]
    clicks = []
    foreground_checks = []

    class ActivityPointsController:
        def post_screencap(self):
            return Job(frames.pop(0))

        def post_click(self, *point):
            clicks.append(point)
            return Job()

    class DetailsParser:
        def parse(self, _image):
            return LiveResult(382, 17, 0, 0, 2, 9, 8, .95)

    clock = Clock()
    outcome = collect_result(
        ActivityPointsController(),
        lambda: False,
        parser=DetailsParser(),
        expected_notes=401,
        clock=clock.monotonic,
        sleeper=clock.sleep,
        activity_points_click_delay_seconds=0,
        stability_interval_seconds=0,
        before_input=lambda: foreground_checks.append(1),
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.page_state == "judgement-details"
    assert outcome.result is not None
    assert outcome.result.total == 401
    assert outcome.elapsed_seconds == 0
    assert clicks == [(902, 399)]
    assert foreground_checks == [1]


def test_activity_points_page_retries_a_recognised_button_once_if_first_click_is_ignored():
    template = cv2.imread(
        str(profile_play_action.ACTIVITY_POINTS_TEMPLATE)
    )
    page = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    page[375:375 + height, 838:838 + width] = template

    details_template = cv2.imread(
        str(profile_play_action.JUDGEMENT_DETAILS_TEMPLATE)
    )
    details = np.zeros((720, 1280, 3), dtype=np.uint8)
    details_height, details_width = details_template.shape[:2]
    details[270:270 + details_height, 760:760 + details_width] = (
        details_template
    )

    frames = [page, page, details, details]
    clicks = []

    class RetryActivityPointsController:
        def post_screencap(self):
            return Job(frames.pop(0))

        def post_click(self, *point):
            clicks.append(point)
            return Job()

    class DetailsParser:
        def parse(self, _image):
            return LiveResult(482, 7, 1, 0, 0, 2, 6, .95)

    clock = Clock()
    outcome = collect_result(
        RetryActivityPointsController(),
        lambda: False,
        parser=DetailsParser(),
        expected_notes=490,
        clock=clock.monotonic,
        sleeper=clock.sleep,
        activity_points_click_delay_seconds=0,
        stability_interval_seconds=0,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert clicks == [(902, 399), (902, 399)]


def test_activity_points_page_that_does_not_disappear_is_technical_failure():
    template = cv2.imread(
        str(profile_play_action.ACTIVITY_POINTS_TEMPLATE)
    )
    page = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    page[375:375 + height, 838:838 + width] = template
    clicks = []

    class PersistentActivityPointsController:
        def post_screencap(self):
            return Job(page.copy())

        def post_click(self, *point):
            clicks.append(point)
            return Job()

    clock = Clock()
    outcome = collect_result(
        PersistentActivityPointsController(),
        lambda: False,
        parser=Parser(),
        clock=clock.monotonic,
        sleeper=clock.sleep,
        activity_points_click_delay_seconds=0,
    )

    assert outcome.status is ResultCollectionStatus.BLOCKED
    assert outcome.page_state == "activity-points"
    assert "未消失" in outcome.reason
    assert clicks == [(902, 399), (902, 399)]
