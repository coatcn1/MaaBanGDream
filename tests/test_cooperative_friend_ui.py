from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.cooperative_friend import FriendPage
from agent.realtime.cooperative_friend_ui import (
    CooperativeFriendUIAdapter, occupied_member_count, template_matches,
)


class Flow:
    def __init__(self):
        self.image = np.zeros((720, 1280, 3), np.uint8)
        self.names = set()
        self.clicks = []
    def stopped(self):
        return False
    def capture(self):
        return self.image
    def visible(self, image, name):
        return name in self.names
    def click(self, point):
        self.clicks.append(point)


def adapter(role="host"):
    flow = Flow()
    ui = CooperativeFriendUIAdapter(flow, role=role, templates={},
        reader=lambda *args, **kwargs: SimpleNamespace(text="'2''8''0''0''1''6'", confidence=.999))
    return flow, ui


def test_member_slots_do_not_count_colored_player_labels():
    image = np.full((720, 1280, 3), 210, np.uint8)
    image[60:90] = (0, 0, 255)
    assert occupied_member_count(image) == 0
    image[105:325, 115:300] = (80, 80, 200)
    assert occupied_member_count(image) == 1
    image[105:325, 330:515] = (80, 200, 80)
    assert occupied_member_count(image) == 2


def test_room_requires_two_consecutive_confident_readings():
    flow, ui = adapter()
    ui._matches = lambda image, name: [(100, 550)] if name == "private_label" else []
    assert ui.observe().state == FriendPage.UNKNOWN
    observation = ui.observe()
    assert observation.state == FriendPage.HOST_ROOM
    assert observation.room_key == "280016"
    ui.reader = lambda *a, **k: SimpleNamespace(text="28001", confidence=.999)
    assert ui.observe().state == FriendPage.UNKNOWN


def test_ambiguous_invitation_never_exposes_join():
    _, ui = adapter("guest")
    ui._matches = lambda image, name: ([(215, 90)] if name == "guest_title" else
        [(1038, 237), (1038, 367)] if name == "guest_join" else [])
    observation = ui.observe()
    assert observation.valid_invitation_count == 2
    assert "join_invitation" not in observation.actions


def test_invitation_unique_click_consumes_current_evidence():
    flow, ui = adapter("guest")
    ui._matches = lambda image, name: [(1038, 237)] if name in {"guest_title", "guest_join"} else []
    observation = ui.observe()
    ui.click(observation.actions["join_invitation"])
    assert flow.clicks == [(1038, 237)]
    with pytest.raises(ValueError, match="截图"):
        ui.click((1038, 237))
    ui._matches = lambda image, name: []
    ui.observe()
    with pytest.raises(ValueError):
        ui.click((1038, 237))


def test_modal_blocks_background_room_actions():
    _, ui = adapter()
    ui._stable_room = "280016"
    ui._matches = lambda image, name: [(770, 447)] if name in {
        "invite_confirm_body", "invite_confirm_button", "private_label", "invite_button"} else []
    observation = ui.observe()
    assert observation.state == FriendPage.INVITE_CONFIRM
    assert observation.actions == {"confirm_invite": (770, 447)}
    assert not observation.invitation_sent


def test_search_method_and_room_started_require_visible_template():
    flow, ui = adapter("guest")
    flow.names = {"search_friend"}
    assert ui.observe().actions == {"open_friend_list": (852, 528)}
    flow.names = {"song_unspecified"}
    assert ui.observe().state == FriendPage.ROOM_STARTED


def test_template_peak_suppression_counts_distinct_rows():
    rng = np.random.default_rng(7)
    template = rng.integers(0, 255, (45, 45, 3), dtype=np.uint8)
    image = np.zeros((720, 1280, 3), np.uint8)
    image[200:245, 1000:1045] = template
    image[350:395, 1000:1045] = template
    assert template_matches(image, template, (980, 175, 110, 355)) == [(1022, 222), (1022, 372)]


def test_start_confirmation_modal_blocks_room_and_started_background():
    flow, ui = adapter()
    ui._stable_room = "280016"
    flow.names = {"song_unspecified", "ready_button"}
    ui._matches = lambda image, name: [(769, 455)] if name in {
        "start_confirm_body", "start_confirm_button", "private_label", "start_room"} else []
    observation = ui.observe()
    assert observation.state == FriendPage.START_CONFIRM
    assert observation.room_key == "280016"
    assert observation.actions == {"confirm_start": (769, 455)}
    ui.click(observation.actions["confirm_start"])
    assert flow.clicks == [(769, 455)]


def test_start_confirmation_without_button_does_not_guess_action():
    _, ui = adapter()
    ui._matches = lambda image, name: [(640, 343)] if name == "start_confirm_body" else []
    observation = ui.observe()
    assert observation.state == FriendPage.START_CONFIRM
    assert observation.actions == {}
