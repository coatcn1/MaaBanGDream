"""从当前交换所启动的保守抽箱流程；未知消费页面不发送输入。"""
from __future__ import annotations

import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import time

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from .exchange_shop_policy import BoxState, decide, validate_draw, validate_reset
from .song_title_ocr import recognize_song_title
from .vision_io import imread_unicode, imwrite_unicode

try:
    from ..foreground_guard import require_game_foreground
    from ..screen_refresh import capture_image, ScreenRefreshCancelled
    from ..task_reporting import log_task, record_failure_reason
except ImportError:
    from foreground_guard import require_game_foreground
    from screen_refresh import capture_image, ScreenRefreshCancelled
    from task_reporting import log_task, record_failure_reason


ROOT = Path(__file__).resolve().parents[2]
REGIONS = {
    "main_settings": (989, 110, 173, 33),
    "main_contents": (914, 450, 216, 29),
    "contents_title": (365, 39, 221, 35),
    "contents_close": (528, 617, 224, 46),
    "special_label": (390, 173, 64, 22),
    "can_icon": (410, 469, 85, 83),
    "settings_title": (181, 76, 237, 34),
    "settings_all": (197, 157, 124, 25),
    "settings_special": (198, 343, 345, 26),
    "settings_off_all": (310, 205, 79, 33),
    "settings_off_special": (310, 391, 79, 34),
    "settings_close": (530, 579, 221, 48),
    "draw_confirm_title": (363, 155, 109, 34),
    "draw_confirm_yes": (660, 499, 223, 50),
    "draw_confirm_cancel": (399, 500, 223, 49),
    "draw_result_title": (829, 150, 137, 31),
    "draw_result_close": (960, 623, 218, 45),
    "main_reset_enabled": (662, 450, 222, 29),
    "reset_confirm_body": (510, 260, 250, 80),
    "reset_confirm_title": (361, 155, 109, 34),
    "reset_confirm_warning": (513, 374, 240, 49),
    "reset_confirm_cancel": (394, 500, 222, 49),
    "reset_confirm_yes": (762, 507, 51, 37),
    "reset_done_title": (366, 231, 128, 31),
    "reset_done_body": (519, 324, 243, 31),
    "reset_done_ack": (529, 421, 220, 52),
}
_OPTIONS = {}


class ExchangeStopped(ScreenRefreshCancelled):
    pass


def validate_options(values):
    if not isinstance(values, dict):
        raise ValueError("抽箱配置必须是对象")
    mode = values.get("mode", "auto")
    execute = values.get("execute", False)
    maximum = values.get("max_draws", 0)
    if mode not in ("auto", "versus180", "five230"):
        raise ValueError("未知抽箱策略")
    if type(execute) is not bool:
        raise ValueError("执行方式必须是布尔值")
    if type(maximum) is not int or not 0 <= maximum <= 999999:
        raise ValueError("最大抽数必须是0..999999整数")
    return {"mode": mode, "execute": execute, "max_draws": maximum}


def _task_id(argv):
    value = getattr(getattr(argv, "task_detail", None), "task_id", None)
    if type(value) is not int or value <= 0:
        raise ValueError("抽箱缺少可信任务ID")
    return value


class ExchangeReader:
    def __init__(self, stopped=lambda: None, ocr=recognize_song_title):
        self.stopped = stopped
        self.ocr = ocr
        self.readings = {}
        self.templates = {}
        for name in REGIONS:
            for base in (ROOT / "resource/image", ROOT / "resource/resource/image"):
                template = imread_unicode(base / "exchange_shop" / f"{name}.png")
                if template is not None:
                    self.templates[name] = template
                    break
            else:
                raise RuntimeError(f"抽箱模板缺失：{name}")

    def matches(self, image, name):
        self.stopped()
        if getattr(image, "shape", ())[:2] != (720, 1280):
            return False
        x, y, w, h = REGIONS[name]
        region, template = image[y:y+h, x:x+w, :3], self.templates[name]
        difference = float(np.abs(region.astype(np.int16) - template.astype(np.int16)).mean())
        return difference <= 18 and float(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED).max()) >= .95

    def page(self, image, name):
        groups = {
            "main": ("main_settings", "main_contents"),
            "contents": ("contents_title", "contents_close", "special_label"),
            "settings": ("settings_title", "settings_all", "settings_special", "settings_close"),
            "draw_confirm": ("draw_confirm_title", "draw_confirm_yes", "draw_confirm_cancel"),
            "draw_result": ("draw_result_title", "draw_result_close"),
            "reset_confirm": ("reset_confirm_title", "reset_confirm_body", "reset_confirm_warning",
                              "reset_confirm_cancel", "reset_confirm_yes"),
            "reset_done": ("reset_done_title", "reset_done_body", "reset_done_ack"),
        }
        return all(self.matches(image, part) for part in groups[name])

    def text(self, image, key, roi):
        self.stopped()
        reading = self.ocr(image, roi=roi)
        self.readings[key] = {"roi": roi, "text": reading.text if reading else None,
                              "confidence": float(reading.confidence) if reading else None}
        if reading is None or reading.confidence < .90:
            raise ValueError(f"抽箱数字低置信：{key}")
        text = re.sub(r"\s+", "", reading.text)
        # 本地 OCR 字典会将单个数字带上 YAML 引号，只解除逐数字包装。
        return re.sub(r"(['\"])([0-9])\1", r"\2", text)

    def ratio(self, image, key, roi, totals):
        text = self.text(image, key, roi)
        match = re.fullmatch(r"([0-9]{1,3})/([0-9]{1,3})", text)
        if not match:
            raise ValueError(f"抽箱比例格式无效：{key}={text}")
        value, total = map(int, match.groups())
        if total not in totals or not 0 <= value <= total:
            raise ValueError(f"抽箱比例范围冲突：{key}={text}")
        return value, total

    def main(self, image):
        if not self.page(image, "main"):
            raise ValueError("当前不是已确认的交换所主页面")
        box_text = self.text(image, "box", (666, 184, 103, 39))
        match = re.fullmatch(r"第([0-9]{1,5})箱", box_text)
        if not match or int(match[1]) < 1:
            raise ValueError("箱号读取失败")
        remaining, total = self.ratio(image, "remaining", (853, 184, 100, 38), (180, 230))
        special, _ = self.ratio(image, "special", (875, 377, 49, 29), (1,))
        currency_text = self.text(image, "currency", (1017, 539, 111, 31))
        match_currency = re.fullmatch(r"([0-9]{1,9})枚?", currency_text)
        if not match_currency:
            raise ValueError("活动礼物币余额读取失败")
        return int(match[1]), remaining, total, special, int(match_currency[1])

    def contents(self, image):
        if not self.page(image, "contents"):
            raise ValueError("当前不是已确认的礼物箱内容页")
        remaining, total = self.ratio(image, "contents_remaining", (553, 89, 101, 34), (180, 230))
        # 收紧特别礼物比例的留白，避免已获得后的“0/1”被宽裁剪压低置信度。
        special, _ = self.ratio(image, "contents_special", (433, 295, 42, 23), (1,))
        # 只在内容网格寻找本任务的小火罐图标，比例位置随图标而定。
        self.stopped()
        region = image[180:565, 398:882, :3]
        template = self.templates["can_icon"]
        scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(scores)
        if score < .95:
            raise ValueError("内容页未找到可信小火罐图标")
        px, py = location[0] + 398, location[1] + 180
        crop = image[py:py+83, px:px+85, :3]
        if float(np.abs(crop.astype(np.int16) - template.astype(np.int16)).mean()) > 18:
            raise ValueError("内容页小火罐被遮挡")
        # 排除同页重复图标，不能任选最高分。
        scores[max(0, location[1]-45):location[1]+46, max(0, location[0]-45):location[0]+46] = -1
        if scores.max() >= .95:
            raise ValueError("内容页小火罐位置不唯一")
        cans, _ = self.ratio(image, "cans", (px + 9, py + 101, 64, 28), (10,))
        return remaining, total, cans, special

    def settings(self, image):
        if not self.page(image, "settings"):
            raise ValueError("当前不是已确认的礼物箱设置页")
        return self.matches(image, "settings_off_all"), self.matches(image, "settings_off_special")

    def draw_offer(self, image):
        amount = self.text(image, "draw_offer_amount", (989, 599, 128, 31))
        cost = self.text(image, "draw_offer_cost", (1030, 635, 94, 31))
        a, c = re.fullmatch(r"([0-9]{1,2})个交换", amount), re.fullmatch(r"([0-9]{1,3})枚", cost)
        if not a or not c:
            raise ValueError("抽取按钮数量或礼物币费用无效")
        return int(a[1]), int(c[1])

    def confirmation_after(self, image):
        roi = (733, 425, 106, 33)
        self.readings.pop("confirm_after", None)
        self.readings.pop("confirm_after_original", None)
        try:
            original = self.text(image, "confirm_after_original", roi)
        except ValueError:
            # 仅容许已记录的低置信结果，识别器异常不能被裁剪重试掩盖。
            if "confirm_after_original" not in self.readings:
                raise
            original = None
        self.stopped()
        if original is not None and not re.fullmatch(r"[0-9]{1,9}", original):
            raise ValueError("交换确认余额原始读数无效")
        x, y, w, h = roi
        region = image[y:y+h, x:x+w, :3]
        # 只收紧本字段留白，保留全部非白墨迹及原彩色像素，不筛掉符号或遮挡。
        ys, xs = np.where(region.min(axis=2) < 240)
        if not xs.size:
            raise ValueError("交换确认余额没有完整墨迹")
        left, top, right, bottom = int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1
        padding = 2
        # 触边时无法证明数字完整，不能把被截断的高置信子串作为消费凭据。
        if left < padding or top < padding or right > w-padding or bottom > h-padding:
            raise ValueError("交换确认余额墨迹触边或被遮挡")
        tight_roi = (x+left-padding, y+top-padding, right-left+2*padding, bottom-top+2*padding)
        text = self.text(image, "confirm_after", tight_roi)
        self.readings["confirm_after"].update(source_roi=roi, method="complete_ink_crop")
        if not re.fullmatch(r"[0-9]{1,9}", text):
            raise ValueError("交换确认余额无效")
        if original is not None and original != text:
            raise ValueError("交换确认余额高置信读数冲突")
        return text

    def draw_confirmation(self, image):
        if not self.page(image, "draw_confirm"):
            raise ValueError("不是已验证的礼物币交换确认页")
        # 正文随活动币名居中排版，只取唯一末尾数值；币名、图标和颜色不参与判断。
        cost = self.text(image, "confirm_cost", (460, 251, 360, 31))
        amount = self.text(image, "confirm_amount", (528, 281, 221, 31))
        prompt = self.text(image, "confirm_prompt", (586, 311, 115, 30))
        c, a = re.fullmatch(r"[^0-9+−.\-]*([0-9]{1,3})枚", cost), re.fullmatch(r"领取礼物([0-9]{1,2})个奖品[。.]?", amount)
        if not c or not a or prompt not in ("确认吗?", "确认吗？"):
            raise ValueError("交换确认金额、数量或正文不匹配")
        values = []
        for key, roi in (("confirm_before", (454, 425, 105, 33)), ("confirm_after", (733, 425, 106, 33))):
            text = self.confirmation_after(image) if key == "confirm_after" else self.text(image, key, roi)
            if not re.fullmatch(r"[0-9]{1,9}", text):
                raise ValueError("交换确认余额无效")
            values.append(int(text))
        return int(a[1]), int(c[1]), *values

    def draw_result(self, image):
        if not self.page(image, "draw_result"):
            raise ValueError("抽取动画尚未结束或结果页未识别")
        text = self.text(image, "result_currency", (1017, 539, 111, 31))
        match = re.fullmatch(r"([0-9]{1,9})枚", text)
        if not match:
            raise ValueError("抽取结果礼物币余额无效")
        return int(match[1])

    def reset_confirmation(self, image):
        if not self.page(image, "reset_confirm"):
            raise ValueError("不是已确认的礼物箱重置专用确认页")
        parts = (
            ("reset_body_first", (507, 258, 256, 32), ("重置礼物箱中的奖品，", "重置礼物箱中的奖品,")),
            ("reset_body_next", (522, 288, 226, 28), ("进入下一个礼物箱。", "进入下一个礼物箱.")),
            ("reset_prompt", (579, 315, 120, 29), ("确认吗？", "确认吗?")),
            ("reset_warning_first", (550, 371, 160, 27), ("重置礼物箱后，", "重置礼物箱后,")),
            ("reset_warning_previous", (506, 399, 259, 28), ("无法再返回之前的礼物箱。", "无法再返回之前的礼物箱.")),
            ("reset_yes", (733, 510, 65, 31), ("确定",)),
            ("reset_cancel", (470, 508, 75, 36), ("取消",)),
        )
        for key, roi, expected in parts:
            if self.text(image, key, roi) not in expected:
                raise ValueError(f"礼物箱重置专用正文或按钮不匹配：{key}")
        return True

    def reset_done(self, image):
        if not self.page(image, "reset_done"):
            raise ValueError("不是已重置礼物箱的专用成功提示")
        parts = (
            ("reset_done_title", (397, 232, 92, 31), ("已重置",)),
            ("reset_done_body", (519, 324, 243, 31), ("已进入下一个礼物箱。", "已进入下一个礼物箱.")),
            ("reset_done_ack", (605, 428, 75, 39), ("确定",)),
        )
        for key, roi, expected in parts:
            if self.text(image, key, roi) not in expected:
                raise ValueError(f"礼物箱重置成功提示正文或按钮不匹配：{key}")
        return True


class ExchangeUI:
    # 输入协议只在各自真实页面签名通过后可用，未知弹窗仍不发送确认。
    consumption_verified = True
    reset_verified = True

    def __init__(self, context, *, reader=None, capture=capture_image, sleep=time.sleep):
        self.context, self.capture, self.sleep = context, capture, sleep
        self.reader = reader or ExchangeReader(self.stopped)
        self.last_frame = None
        self.stage = "start"
        self.navigation = None

    def stopped(self):
        if self.context.tasker.stopping:
            raise ExchangeStopped("用户停止抽箱")

    def stable(self, page, read=None, attempts=10):
        previous = None
        error = "页面未送达"
        for _ in range(attempts):
            self.stopped()
            self.last_frame = self.capture(self.context)
            self.stopped()
            try:
                if not self.reader.page(self.last_frame, page):
                    raise ValueError("页面签名不匹配")
                value = read(self.last_frame) if read else True
                if previous == value:
                    return value
                previous = value
            except ValueError as exc:
                error, previous = str(exc), None
            self.stopped()
            self.sleep(.15)
        raise RuntimeError(f"抽箱{page}未稳定：{error}；OCR={self.reader.readings}")

    def click(self, x, y):
        self.stopped()
        require_game_foreground(self.context.tasker.controller)
        self.stopped()
        # 前台保护可能触发重连，输入前重新获取代理。
        self.context.tasker.controller.post_click(x, y).wait()
        self.stopped()

    def open_from_main(self, page, read, before, point, *, available=None):
        """仅重试未送达的非消费入口；每次重发都重新确认原主页面。"""
        self.stopped()
        # 清除上一箱内容等旧 OCR，失败诊断只记录本次导航实际读取的证据。
        self.reader.readings.clear()
        self.navigation = {"target":page, "original_main":before, "last_main":before,
                           "attempt":0, "target_seen":False}

        def capture_new():
            self.stopped()
            self.last_frame = self.capture(self.context)
            self.stopped()

        def same_main():
            observed = self.reader.main(self.last_frame)
            self.navigation["last_main"] = observed
            if observed != before:
                raise RuntimeError("入口导航期间主页面已变化，禁止重发入口")

        error = "目标页面未送达"
        for attempt in range(1, 4):
            self.stopped()
            self.navigation["attempt"] = attempt
            if available and not self.reader.matches(self.last_frame, available):
                raise RuntimeError("进入下一个礼物箱按钮未确认可用")
            try:
                log_task("交换所抽箱", "导航", "INFO",
                         f"目标={page} 入口点击={attempt}/3 已核原主页面={before}")
            except Exception:
                pass
            self.click(*point)
            previous = None
            retry_allowed = True
            for _ in range(10):
                capture_new()
                if self.reader.page(self.last_frame, page):
                    self.navigation["target_seen"] = True
                    self.navigation["retry_blocked_reason"] = "目标页面已出现，不重发入口"
                    retry_allowed = False
                    try:
                        value = read(self.last_frame)
                        if previous == value:
                            return value
                        previous = value
                    except ValueError as exc:
                        error, previous = str(exc), None
                elif self.reader.page(self.last_frame, "main"):
                    try:
                        same_main()
                        error = "仍为本次原主页面"
                    except ValueError as exc:
                        error, retry_allowed = str(exc), False
                        self.navigation["retry_blocked_reason"] = "主页面数字未确认"
                    previous = None
                else:
                    # 动画或未知页只被动等待；即使随后回到原页，本轮也不再重发入口。
                    error, previous, retry_allowed = "曾出现未知页面，仅等待目标送达", None, False
                    self.navigation["retry_blocked_reason"] = "曾出现未知页面"
                self.stopped()
                self.sleep(.15)
            if not retry_allowed or attempt == 3:
                break
            # 不能拿等待窗口里的历史帧授权重发；额外核对两张全新的原主页面。
            for index in range(2):
                capture_new()
                if self.reader.page(self.last_frame, page):
                    self.navigation["target_seen"] = True
                    return self.stable(page, read)
                if not self.reader.page(self.last_frame, "main"):
                    raise RuntimeError("重发入口前页面未知，禁止重发入口")
                try:
                    same_main()
                except ValueError as exc:
                    raise RuntimeError("重发入口前主页面读数未确认，禁止重发入口") from exc
                if index == 0:
                    self.stopped()
                    self.sleep(.15)
        raise RuntimeError(f"抽箱{page}入口未稳定：{error}；导航={self.navigation}；本次OCR={self.reader.readings}")

    def check_settings(self):
        self.stage = "settings"
        before = self.main_ready()
        settings = self.open_from_main("settings", self.reader.settings, before, (1068, 127))
        self.click(640, 603)
        self.stable("main", self.reader.main)
        if settings != (True, True):
            raise RuntimeError("请先手动关闭礼物箱设置中的“全部交换”和“获得特别礼物时自动停止”")

    def read_state(self):
        self.stage = "read_state"
        before = self.main_ready()
        contents = self.open_from_main("contents", self.reader.contents, before, (1029, 465))
        self.click(640, 642)
        after = self.stable("main", self.reader.main)
        if before != after or (before[1], before[2], before[3]) != (contents[0], contents[1], contents[3]):
            raise RuntimeError("主页面与内容页读数冲突，禁止消费")
        self.current_state = BoxState(before[0], before[1], before[2], contents[2], before[3], before[4]).validate()
        return self.current_state

    def main_ready(self):
        before = self.stable("main", self.reader.main)
        if before[1] == 0:
            # 空箱只有自动送达，不把短暂旧空箱当成手动重置授权。
            return self.wait_new_box(before, before[4])
        return before

    def draw_once(self, amount):
        self.stage = "draw_confirm"
        before = self.current_state
        if self.stable("main", self.reader.main) != (before.box, before.remaining, before.total, before.special, before.currency):
            raise RuntimeError("抽取前主页面已变化，拒绝消费")
        offer = self.stable("main", self.reader.draw_offer)
        if offer != (amount, amount * 10):
            raise RuntimeError("抽取按钮数量或费用与计划冲突")
        self.click(1025, 618)
        confirmation = self.stable("draw_confirm", self.reader.draw_confirmation)
        if confirmation != (amount, amount * 10, before.currency, before.currency - amount * 10):
            raise RuntimeError("交换确认与本次预算或余额冲突，未发送确认")
        # 仅确认已核对的本次消费；超时或任何未知页面都不重发。
        self.click(770, 525)
        self.stage = "draw_result"
        self.finish_draw_result(before.currency - amount * 10, before if amount == before.remaining else None)

    def finish_draw_result(self, expected_currency, emptied=None):
        observed = self.stable("draw_result", self.reader.draw_result, attempts=200)
        if observed != expected_currency:
            raise RuntimeError("抽取结果余额不符，保留未决账本并禁止重试")
        # 结果左侧是“再次交换”消费按钮，唯一可关闭按钮位于右侧。
        self.click(1068, 648)
        if emptied is not None:
            self.wait_new_box((emptied.box, 0, emptied.total, 0, expected_currency), expected_currency)
        else:
            self.stable("main", self.reader.main)

    def reset_once(self):
        self.stage = "reset_confirm"
        before = self.current_state.validate()
        if before.special != 0 or before.remaining == 0:
            raise RuntimeError("尚未获得特别礼物或空箱不允许手动换箱")
        current = (before.box, before.remaining, before.total, before.special, before.currency)
        if self.stable("main", self.reader.main) != current:
            raise RuntimeError("换箱前主页面已变化，拒绝发送换箱输入")
        # 必须是特别礼物已获得后的可用按钮，避免只凭文字点击灰色控件。
        if not self.reader.matches(self.last_frame, "main_reset_enabled"):
            raise RuntimeError("进入下一个礼物箱按钮未确认可用")
        if self.open_from_main("reset_confirm", self.reader.reset_confirmation, current, (773, 465),
                               available="main_reset_enabled") is not True:
            raise RuntimeError("礼物箱重置确认未完整送达")
        self.click(769, 526)
        self.finish_reset_result(before)

    def finish_reset_result(self, before):
        """仅收取本次已提交换箱的回执，不打开或确认另一笔换箱。"""
        before.validate()
        if before.special != 0 or before.remaining == 0:
            raise RuntimeError("缺少允许换箱的原始状态，拒绝处理回执")
        return self.wait_new_box((before.box, before.remaining, before.total, before.special, before.currency), before.currency)

    def wait_new_box(self, before, expected_currency):
        """被动等精确下一箱；兼容成功提示，最多关闭一次，不发送重置输入。"""
        self.stage = "reset_result"
        expected = (before[0] + 1, before[2], before[2], 1, expected_currency)
        previous = None
        acknowledged = False
        error = "新礼物箱尚未送达"
        for _ in range(100):
            self.stopped()
            self.last_frame = self.capture(self.context)
            self.stopped()
            try:
                if self.reader.page(self.last_frame, "main"):
                    observed = self.reader.main(self.last_frame)
                    if observed != expected:
                        raise ValueError("新礼物箱箱号、容量、特别礼物或余额尚未核实")
                    marker = ("main", observed)
                    if previous == marker:
                        return observed
                    previous = marker
                elif self.reader.page(self.last_frame, "reset_done"):
                    if self.reader.reset_done(self.last_frame) is not True:
                        raise ValueError("已重置提示未完整识别")
                    marker = ("reset_done", True)
                    if previous == marker and not acknowledged:
                        # 点击前记为已发，超时、重复提示或旧帧都不能导致第二次点击。
                        acknowledged = True
                        self.stage = "reset_done_ack"
                        self.click(640, 448)
                        self.stage = "reset_result"
                        previous = None
                    else:
                        previous = marker
                    error = "已重置提示关闭后尚未读到新礼物箱"
                else:
                    raise ValueError("换箱结果为未知页面")
            except ValueError as exc:
                error, previous = str(exc), None
            self.stopped()
            self.sleep(.15)
        raise RuntimeError(f"换箱结果未稳定：{error}；OCR={self.reader.readings}")

    def save_failure(self, error):
        self.stopped()
        folder = ROOT / "debug/exchange-shop" / str(time.time_ns())
        try:
            folder.mkdir(parents=True)
            if self.last_frame is not None:
                imwrite_unicode(folder / "failure.png", self.last_frame)
            (folder / "failure.json").write_text(json.dumps({"stage": self.stage, "error": str(error),
                "readings": self.reader.readings, "navigation":self.navigation}, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(folder)
        except Exception:
            return "诊断保存失败"


class ExchangeFlow:
    def __init__(self, ui, options, *, journal=None, report=None):
        self.ui, self.options = ui, validate_options(options)
        self.journal, self.report = journal, report or (lambda text: log_task("交换所抽箱", "策略", "INFO", text))
        self.draws = 0

    def run(self):
        self.ui.stopped()
        self.ui.check_settings()
        state = self.ui.read_state()
        while True:
            self.ui.stopped()
            total = {"auto": state.total, "versus180": 180, "five230": 230}[self.options["mode"]]
            if total != state.total:
                raise RuntimeError("所选策略与实读箱容量冲突")
            maximum = self.options["max_draws"]
            budget = min(state.currency // 10, max(0, maximum-self.draws)) if maximum else state.currency // 10
            decision = decide(state, budget=budget, stopped=self.ui.stopped)
            action = {"reset":"换箱", "draw":"抽取", "stop":"结束", "wait":"等待自动换箱"}[decision.action]
            amount = str(decision.amount) if decision.action == "draw" else "不适用"
            limit = str(self.options["max_draws"]) if self.options["max_draws"] else "不限"
            self.report(f"第{state.box}箱 x={state.remaining}/{state.total} y={state.cans}/10 "
                        f"special={state.special}/1 活动礼物币={state.currency} 可用预算={budget}抽 "
                        f"下一步={action} 数量={amount} "
                        f"{decision.reason}；每次抽取后重读并重算；"
                        f"累计抽数={self.draws}，抽数上限={limit}")
            if not self.options["execute"]:
                self.report("预览完成，未抽取、未换箱")
                return True
            if decision.action == "stop":
                self.report("达到可用抽数预算，保留当前余额")
                return True
            if decision.action == "wait":
                raise RuntimeError("空箱自动换箱未送达，禁止发送重置输入")
            if not self.ui.consumption_verified:
                raise RuntimeError("消费与换箱页面尚未获得真实样本验证；已在消费输入前停止，请使用预览")
            if decision.action == "reset" and not getattr(self.ui, "reset_verified", False):
                raise RuntimeError("策略要求换箱；换箱确认页面尚未验证，已在换箱输入前停止")
            if self.journal is None:
                raise RuntimeError("缺少消费未决保护，拒绝执行")
            self.ui.stopped()
            self.journal.check_unresolved()
            self.journal.reserve(state, decision)
            if decision.action == "draw":
                self.ui.draw_once(decision.amount)
            else:
                self.ui.reset_once()
            self.ui.stopped()
            after = self.ui.read_state()
            if decision.action == "draw":
                validate_draw(state, after, decision.amount)
                self.draws += decision.amount
            else:
                validate_reset(state, after)
            self.ui.stopped()
            self.journal.confirm(after)
            state = after


class ExchangeJournal:
    def __init__(self, path=None):
        self.path = Path(path or ROOT / "debug/exchange-shop/consumption.json")
        self.pending = None

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise RuntimeError("抽箱消费锁已存在；请先核实另一任务或上次中断现场") from exc
        try:
            with handle:
                handle.write(str(time.time_ns()))
                handle.flush()
            self.check_unresolved()
            yield self
        finally:
            lock.unlink(missing_ok=True)

    def check_unresolved(self):
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            status = data.get("status")
            if status == "resolved_not_executed":
                resolution = data.get("resolution", {})
                if (data.get("after") != data.get("before") or resolution.get("manual") is not True
                        or not isinstance(resolution.get("evidence"), dict) or not resolution["evidence"]):
                    raise RuntimeError("人工未执行核账缺少原始状态或审计证据")
            elif status == "resolved_external_full_clear":
                resolution = data.get("resolution", {})
                expected = self._external_clear_resolution(data, BoxState(**data["after"]), resolution.get("evidence"))
                if any(resolution.get(key) != value for key,value in expected.items()):
                    raise RuntimeError("外部手动清空核账审计字段不完整")
            elif status != "confirmed":
                raise RuntimeError("上次抽箱消费或换箱结果未明，禁止自动重复；请保留现场与账本人工核对")

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def reserve(self, state, decision):
        self.pending = {"status": "pending", "before": state.__dict__, "decision": decision.__dict__,
                        "created_ns": time.time_ns()}
        self._save(self.pending)

    def confirm(self, state):
        if self.pending is None:
            raise RuntimeError("缺少本次消费预留")
        self._save(self.pending | {"status": "confirmed", "after": state.__dict__})
        self.pending = None

    def resolve_not_executed(self, after, evidence):
        """仅供人工核账调用；自动任务不推定未执行，也不清除原始预留。"""
        after.validate()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("status") != "pending" or data.get("before") != after.__dict__:
            raise RuntimeError("仅原始状态完全未变的未决操作可人工标记未执行")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("人工核账必须保留主页面与内容页证据说明")
        self._save(data | {"status":"resolved_not_executed", "after":after.__dict__,
                          "resolution":{"manual":True, "evidence":evidence, "resolved_ns":time.time_ns()}})
        self.pending = None

    @staticmethod
    def _external_clear_resolution(data, after, evidence):
        before = BoxState(**data["before"]).validate()
        after.validate()
        if (data.get("decision", {}).get("action") != "reset" or before.special != 0 or before.remaining <= 0
                or after.box != before.box+1 or after.total != before.total or after.remaining != before.total
                or after.cans != 10 or after.special != 1
                or after.currency != before.currency-10*before.remaining):
            raise RuntimeError("外部手动清空必须与原始未决换箱、下一满箱和精确扣款一致")
        if not isinstance(evidence, dict) or evidence.get("operator_confirmed_manual_full_clear") is not True:
            raise ValueError("缺少用户明确手动抽光原箱的声明")
        for key in ("fresh_main_frames", "fresh_contents_frames"):
            frames = evidence.get(key)
            if (not isinstance(frames, list) or len(frames) < 2
                    or any(not isinstance(path,str) or not path.strip() for path in frames)
                    or len(set(frames)) != len(frames)):
                raise ValueError("外部手动清空核账必须保留主页面与内容页各两张新帧路径")
        return {"manual":True, "old_reset":"not_executed", "external_action":"manual_full_clear",
                "actual_draws":before.remaining, "actual_cost":10*before.remaining, "evidence":evidence}

    def resolve_external_full_clear(self, after, evidence):
        """只核销用户明确手动抽光的旧未决换箱；不充当自动重试或成功回执。"""
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("status") != "pending":
            raise RuntimeError("仅原始未决操作可进行外部手动清空核账")
        resolution = self._external_clear_resolution(data, after, evidence)
        self._save(data | {"status":"resolved_external_full_clear", "after":after.__dict__,
                          "resolution":resolution | {"resolved_ns":time.time_ns()}})
        self.pending = None


@AgentServer.custom_action("ExchangeShopConfigure")
class ExchangeShopConfigure(CustomAction):
    def run(self, context, argv):
        if context.tasker.stopping:
            return True
        try:
            values = json.loads(argv.custom_action_param) if isinstance(argv.custom_action_param, str) else dict(argv.custom_action_param or {})
            task_id = _task_id(argv)
            current = {} if values.pop("reset", False) else _OPTIONS.get(task_id, {})
            _OPTIONS[task_id] = validate_options(current | values)
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            record_failure_reason(f"交换所抽箱配置失败：{exc}")
            return False


@AgentServer.custom_action("ExchangeShopRun")
class ExchangeShopRun(CustomAction):
    def run(self, context, argv):
        ui = None
        task_id = None
        try:
            if context.tasker.stopping:
                return True
            task_id = _task_id(argv)
            if task_id not in _OPTIONS:
                raise ValueError("缺少当前任务抽箱配置")
            ui = ExchangeUI(context)
            options = _OPTIONS[task_id]
            journal = ExchangeJournal()
            if options["execute"]:
                with journal.locked():
                    return ExchangeFlow(ui, options, journal=journal).run()
            return ExchangeFlow(ui, options).run()
        except ScreenRefreshCancelled:
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            try:
                evidence = ui.save_failure(exc) if ui else "未生成"
            except ScreenRefreshCancelled:
                return True
            if context.tasker.stopping:
                return True
            record_failure_reason(f"交换所抽箱停止：{exc}；证据：{evidence}")
            return False
        finally:
            if task_id is not None:
                _OPTIONS.pop(task_id, None)
