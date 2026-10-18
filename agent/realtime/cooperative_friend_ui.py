"""基于当前截图的协力好友房间适配器（1280×720）。"""
from __future__ import annotations

import re
import time
from pathlib import Path

import cv2
import numpy as np

from .cooperative_friend import FriendObservation, FriendPage
from .song_title_ocr import recognize_song_title
from .vision_io import imread_unicode

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "resource/image/cooperative/friend"
# 坐标来自双模拟器实际页面；动作必须同时通过当前截图模板确认。
REGIONS = {
    "create_room": (365, 620, 190, 65),
    "create_title": (390, 138, 210, 42),
    "create_confirm": (648, 495, 245, 75),
    "private_label": (90, 530, 180, 47),
    "invite_button": (490, 620, 195, 55),
    "invite_confirm_body": (520, 320, 245, 45),
    "invite_confirm_button": (650, 412, 245, 73),
    "invitation_sent": (490, 620, 195, 55),
    "start_room": (910, 613, 280, 70),
    "start_confirm_body": (525, 320, 260, 48),
    "start_confirm_button": (650, 420, 245, 70),
    "guest_title": (205, 72, 245, 45),
    "guest_join": (980, 175, 110, 355),
}


def template_matches(image, template, roi, threshold=0.94):
    """返回分离的模板命中中心，禁止将一个按钮的多个峰值算作多条邀请。"""
    if template is None:
        return []
    x, y, w, h = roi
    crop = image[y:y+h, x:x+w]
    th, tw = template.shape[:2]
    if crop.shape[0] < th or crop.shape[1] < tw:
        return []
    scores = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
    points = []
    while scores.size:
        _, score, _, (left, top) = cv2.minMaxLoc(scores)
        if not np.isfinite(score) or score < threshold:
            break
        points.append((x + left + tw // 2, y + top + th // 2))
        scores[max(0, top-th//2):top+th//2+1,
               max(0, left-tw//2):left+tw//2+1] = -1
    return points


def occupied_member_count(image):
    """实际彩色卡片与灰色募集占位分开，底部徽章/顶部玩家色标不参与。"""
    count = 0
    for x in (105, 320, 535, 750, 965):
        card = image[105:325, x+10:x+195, :3]
        hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
        colorful = (hsv[:, :, 1] > 45) & (hsv[:, :, 2] > 35)
        if float(colorful.mean()) >= 0.12:
            count += 1
    return count


class CooperativeFriendUIAdapter:
    def __init__(self, flow, *, role="host", reader=None, templates=None):
        if role not in {"host", "guest"}:
            raise ValueError("好友房间角色必须为 host 或 guest")
        self.flow = flow
        self.role = role
        self.reader = reader or recognize_song_title
        self.templates = (templates if templates is not None else {
            name: imread_unicode(TEMPLATE_DIR / (name + ".png"))
            if (TEMPLATE_DIR / (name + ".png")).is_file() else None
            for name in REGIONS
        })
        self._previous_room = ""
        self._stable_room = ""
        self._allowed = set()

    def stopped(self):
        return self.flow.stopped()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def click(self, point):
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        if point not in self._allowed:
            raise ValueError("好友房间点击缺少当前截图按钮证据")
        self._allowed.clear()
        self.flow.click(point)

    def _matches(self, image, name):
        return template_matches(image, self.templates.get(name), REGIONS[name])

    def _visible(self, image, name):
        try:
            return self.flow.visible(image, name)
        except KeyError:
            return False

    def _room(self, image):
        reading = self.reader(image, roi=(280, 530, 120, 48))
        text = reading.text.replace("'", "").replace('"', "").strip() if reading else ""
        key = text if reading and reading.confidence >= 0.97 and re.fullmatch(r"[0-9]{6}", text) else ""
        self._stable_room = key if key and key == self._previous_room else ""
        self._previous_room = key
        return self._stable_room

    def observe(self):
        self._allowed.clear()
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        image = self.flow.capture()
        if not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3):
            return FriendObservation(FriendPage.UNKNOWN)
        actions = {}
        def result(state, **kwargs):
            self._allowed = set(actions.values())
            return FriendObservation(state, actions=actions, **kwargs)
        def button(name, action):
            hits = self._matches(image, name)
            if len(hits) == 1:
                actions[action] = hits[0]
        # 弹窗优先于背后可见的页面，防止点击被遮挡的旧按钮。
        if self._matches(image, "start_confirm_body"):
            button("start_confirm_button", "confirm_start")
            return result(FriendPage.START_CONFIRM, room_key=self._stable_room)
        if self._matches(image, "invite_confirm_body"):
            button("invite_confirm_button", "confirm_invite")
            return result(FriendPage.INVITE_CONFIRM, room_key=self._stable_room)
        if self._matches(image, "create_title"):
            button("create_confirm", "confirm_create")
            return result(FriendPage.CREATE_CONFIRM)
        if self._matches(image, "guest_title"):
            hits = self._matches(image, "guest_join")
            if len(hits) == 1:
                actions["join_invitation"] = hits[0]
            return result(FriendPage.GUEST_INVITES, valid_invitation_count=len(hits))
        if self._visible(image, "search_friend"):
            actions["open_friend_list"] = (852, 528)
            return result(FriendPage.SEARCH_METHOD)
        if self._matches(image, "private_label"):
            key = self._room(image)
            if not key:
                return result(FriendPage.UNKNOWN)
            count = occupied_member_count(image)
            if self.role == "guest":
                return result(FriendPage.GUEST_ROOM, room_key=key, member_count=count)
            button("invite_button", "open_invites")
            if count >= 2:
                button("start_room", "start_room")
            return result(FriendPage.HOST_ROOM, room_key=key, member_count=count,
                          invitation_sent=bool(self._matches(image, "invitation_sent")))
        self._previous_room = ""
        if self._visible(image, "song_unspecified") or self._visible(image, "ready_button"):
            return result(FriendPage.ROOM_STARTED, room_key=self._stable_room)
        if self._matches(image, "create_room"):
            button("create_room", "create_room")
            if self._visible(image, "room_search"):
                actions["open_search"] = (660, 650)
            return result(FriendPage.ROOM_SELECTION)
        return result(FriendPage.UNKNOWN)
