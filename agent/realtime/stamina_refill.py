"""仅在主页体力不足时按剩余任务批量补充；未知消费结果保留预扣并拒绝重复。"""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import time

import cv2
import numpy as np

from .song_title_ocr import recognize_song_title

from .vision_io import imread_unicode, imwrite_unicode
try:
    from ..screen_refresh import capture_image, ScreenRefreshCancelled
    from ..foreground_guard import require_game_foreground
except ImportError:
    from screen_refresh import capture_image, ScreenRefreshCancelled
    from foreground_guard import require_game_foreground

ROOT = Path(__file__).resolve().parents[2]
REFILL_PLAN_CAP = 24
REFILL_BAND_MINIMUM = 18
REFILL_SOFT_TARGET = 20
TEMPLATE_REGIONS = {
    "home_live": (1100, 610, 145, 82),
    "home_band": (802, 623, 105, 76),
    "top_icon": (947, 26, 23, 25),
    "top_plus": (1135, 24, 29, 29),
    "choose_title": (366, 138, 288, 31),
    "choose_items": (380, 283, 234, 111),
    "choose_item_button": (397, 424, 199, 33),
    "items_title": (347, 82, 205, 28),
    "small_identity": (378, 156, 111, 84),
    "small_label": (497, 156, 206, 27),
    "large_identity": (378, 305, 111, 83),
    "large_label": (497, 304, 170, 27),
    "items_limit": (423, 518, 282, 23),
    "confirm_title": (366, 235, 290, 29),
    "confirm_body": (509, 312, 255, 54),
    "confirm_yes": (658, 420, 224, 53),
    "success_close": (526, 419, 225, 54),
}
NUMBER_REGIONS = {
    "top": (979, 23, 32, 33),
    "small_stock": (390, 245, 98, 24),
    "large_stock": (390, 393, 98, 24),
    "small_count": (647, 209, 34, 41),
    "large_count": (647, 357, 34, 41),
    "current": (716, 473, 48, 37),
    "preview": (825, 476, 38, 30),
    "success_points": (500, 326, 290, 43),
}


def parse_number(text, maximum):
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]{1,4}", text.strip()):
        raise ValueError("体力数字必须是完整非负整数")
    value = int(text.strip())
    if value > maximum:
        raise ValueError("体力数字超出允许范围")
    return value


def validate_refill_options(options):
    if not isinstance(options, dict):
        raise ValueError("任务体力补充选项必须是对象")
    result = {}
    for key, default in (("stamina_refill_enabled", False),
                         ("stamina_refill_small_enabled", True),
                         ("stamina_refill_large_enabled", True)):
        value = options.get(key, default)
        if type(value) is not bool:
            raise ValueError(f"{key} 必须是布尔值")
        result[key] = value
    return result


def validate_remaining_rounds(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("剩余局数必须是非负整数，未知或无限使用 None")
    return value


def home_refill_target(required, remaining_rounds, minimum_required=None):
    """返回是否需要补充的最低门槛；组曲声明完整下一组三首，不决定批量目标。"""
    if type(required) is not int or not 0 <= required <= 99:
        raise ValueError("每局所需体力必须是 0..99 的整数")
    validate_remaining_rounds(remaining_rounds)
    minimum = required if minimum_required is None else minimum_required
    if type(minimum) is not int or not 0 <= minimum <= 99:
        raise ValueError("本次最小体力需求必须是 0..99 的整数")
    return 0 if required == 0 or remaining_rounds == 0 else max(required, minimum)


def plan_refill(current, required, small, large, remaining_rounds=None, *, small_enabled=True, large_enabled=True, minimum_required=None):
    """不足下一局/整组时批量补充；尾段优先覆盖，长期目标约20且不超过24。"""
    if any(type(v) is not int or v < 0 for v in (current, required, small, large)):
        raise ValueError("体力计划参数无效")
    if not 0 <= current <= 99:
        raise ValueError("体力必须在 0..99")
    minimum = home_refill_target(required, remaining_rounds, minimum_required)
    if current >= minimum:
        return (0, 0)
    small = min(small if small_enabled else 0, REFILL_PLAN_CAP)
    large = min(large if large_enabled else 0, REFILL_PLAN_CAP//10)
    candidates = [(current+s+10*l, s, l) for l in range(large+1) for s in range(small+1)
                  if minimum <= current+s+10*l <= REFILL_PLAN_CAP]
    if not candidates:
        raise RuntimeError("体力道具不足以覆盖下一局或完整组曲")
    demand = None if remaining_rounds is None else max(minimum,required*remaining_rounds)
    if demand is not None and demand <= REFILL_PLAN_CAP:
        covering = [item for item in candidates if item[0] >= demand]
        # 尾段可一次补完时，先减少结束后的多余体力；大罐颗粒度造成的最小超量仍允许。
        if covering:
            _, s, l = min(covering, key=lambda item:(item[0]-demand,item[1]+item[2],-item[2],item[1]))
        else:
            # 库存不够覆盖尾段也继续使用可行补给，不能把软目标升级为失败门禁。
            _, s, l = min(candidates, key=lambda item:(-item[0],item[1]+item[2],-item[2],item[1]))
        return s, l

    def rank(item):
        after, s, l = item
        future = ()
        if demand is not None:
            # 组曲按完整组计算容量，不能把无法演完一组的零头当作覆盖首数。
            units = (demand+minimum-1)//minimum
            remaining_units = max(0,units-after//minimum)
            capacity = REFILL_PLAN_CAP//minimum
            future_refills = (remaining_units+capacity-1)//capacity
            remaining_songs = remaining_units*minimum//required
            future = (future_refills, int(0 < remaining_songs <= 2))
        # 目标带可达时优先少用道具，避免为精确20点反复添加小罐；不可达则尽量靠近18。
        return future + (max(0,REFILL_BAND_MINIMUM-after),s+l,
                         abs(after-REFILL_SOFT_TARGET),after%minimum,-l,s)

    _, s, l = min(candidates, key=rank)
    return s, l


class StaminaDetector:
    def __init__(self):
        self.templates = {}
        for name in TEMPLATE_REGIONS:
            for base in (ROOT / "resource/image", ROOT / "resource/resource/image"):
                value = imread_unicode(base / "stamina_refill" / f"{name}.png")
                if value is not None:
                    self.templates[name] = value
                    break
            else:
                raise RuntimeError(f"体力补充模板缺失：{name}")

    def matches(self, image, name):
        if getattr(image, "shape", ())[:2] != (720, 1280):
            return False
        x, y, w, h = TEMPLATE_REGIONS[name]
        region = image[y:y+h, x:x+w, :3]
        template = self.templates[name]
        # 相关系数对遮罩变暗不敏感，额外核对亮度，禁止把弹窗后的菜单当可点击菜单。
        difference = float(np.abs(region.astype(np.int16) - template.astype(np.int16)).mean())
        return difference <= 20 and float(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED).max()) >= .94

    def page(self, image, name):
        groups = {
            "menu": ("top_icon", "top_plus"),
            "choose": ("choose_title", "choose_items", "choose_item_button"),
            "items": ("items_title", "small_identity", "small_label", "large_identity", "large_label", "items_limit"),
            "confirm": ("confirm_title", "confirm_body", "confirm_yes"),
            "success": ("confirm_title", "success_close"),
        }
        if not all(self.matches(image, key) for key in groups[name]):
            return False
        if name == "menu":
            return all(self.matches(image, key) for key in ("home_live", "home_band"))
        return True


@lru_cache(maxsize=1)
def detector():
    return StaminaDetector()


class RefillJournal:
    def __init__(self, root, task_id, *, session_id=None):
        if type(task_id) is not int or task_id <= 0:
            raise ValueError("自动补充体力缺少有效 task_detail.task_id")
        session_id = session_id if session_id is not None else os.environ.get("MAABANGDREAM_MFA_SESSION_ID")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("自动补充体力缺少可信 MFA 会话 ID")
        self.task_id = task_id
        self.session_id = session_id
        self.root = Path(root)
        identity = json.dumps([session_id, task_id], ensure_ascii=False)
        self.path = self.root / (hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json")
        self.pending_path = self.root / "unresolved.json"
        self.lock_path = self.root / "refill.lock"

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.lock_path
        # 锁文件不自动清理陈旧锁：进程中断后宁可拒绝补充，也不重复消费。
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            yield self
        finally:
            os.close(fd)
            lock.unlink()

    def load(self):
        if not self.path.exists():
            return {"version": 1, "task_id": self.task_id, "session_id": self.session_id, "reserved_points": 0, "status": "ready"}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (data.get("version") != 1 or data.get("task_id") != self.task_id or data.get("session_id") != self.session_id
                or type(data.get("reserved_points")) is not int or data["reserved_points"] < 0
                or data.get("status") not in {"ready", "reserved", "confirmed"}):
            raise RuntimeError("体力消费记录损坏，拒绝消费")
        return data

    def check_unresolved(self):
        if self.pending_path.exists():
            raise RuntimeError("存在未决体力消费，禁止通过更换任务或 MFA 会话绕过未决消费保护")

    @staticmethod
    def atomic_write(path, data):
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def save(self, data):
        if data["status"] == "reserved":
            # 未决凭据独立于会话，MFA 重启或 task ID 重用不能绕过一次不明消费。
            self.check_unresolved()
            self.atomic_write(self.pending_path, {"journal": self.path.name, "points": data["reserved_points"]})
        self.atomic_write(self.path, data)
        if data["status"] == "confirmed" and self.pending_path.exists():
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            if pending.get("journal") != self.path.name:
                raise RuntimeError("未决体力消费所有者不一致")
            self.pending_path.unlink()


class StaminaRefill:
    def __init__(self, context, *, detector=None, capture=capture_image,
                 foreground=require_game_foreground, sleep=time.sleep, ocr=None):
        self.context = context
        self.detector = detector or globals()["detector"]()
        self.capture = capture
        self.foreground = foreground
        self.sleep = sleep
        self.ocr = ocr or self.read_number
        self.stage = "initial"
        self.last_frame = None
        self.readings = {}
        self.history = []
        self.last_known_current = None
        self.navigation = None

    def stopped(self):
        if self.context.tasker.stopping:
            raise ScreenRefreshCancelled("用户已停止体力补充")

    def click(self, x, y):
        self.stopped()
        self.foreground(self.context.tasker.controller)
        self.stopped()
        job = self.context.tasker.controller.post_click(x, y).wait()
        self.stopped()
        if not job.succeeded:
            raise RuntimeError("体力补充点击失败")

    def read_number(self, image, key):
        x, y, w, h = NUMBER_REGIONS[key]
        crop = image[y:y+h, x:x+w, :3]
        if key in {"top", "preview"}:
            mask_crop = image[473:508, 817:862, :3]
            hsv = cv2.cvtColor(mask_crop, cv2.COLOR_BGR2HSV)
            ink = (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 120)
            mask = cv2.cvtColor(np.where(ink, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
            mask = cv2.copyMakeBorder(mask, 5, 5, 5, 5, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            variants = []
            values = set()
            # 原色与粉色掩膜只做有界交叉校验，冲突时不猜测消费数量。
            # 收紧原色裁剪，避免已复现的双 1 漏位。
            candidates = (("raw-tight", crop, NUMBER_REGIONS[key]),
                          ("pink-mask", mask, (817, 473, 45, 35))) if key == "preview" else (
                ("raw-expanded", crop, NUMBER_REGIONS[key]),
                ("raw-original", image[24:55, 979:1008, :3], (979, 24, 29, 31)))
            for name, candidate, roi in candidates:
                reading = recognize_song_title(candidate, roi=(0, 0, candidate.shape[1], candidate.shape[0]))
                detail = {"variant": name, "roi": list(roi), "text": reading.text if reading else None,
                          "confidence": float(reading.confidence) if reading else None}
                variants.append(detail)
                if reading is not None and reading.confidence >= .90:
                    text = reading.text.strip()
                    if re.fullmatch(r"(?:[0-9]|'[0-9]'|\"[0-9]\")+", text):
                        text = text.replace("'", "").replace('"', "")
                    try:
                        detail["value"] = parse_number(text, 99)
                        values.add(detail["value"])
                    except ValueError:
                        pass
            self.readings[key] = {"roi": list(NUMBER_REGIONS[key]), "variants": variants}
            if len(values) != 1:
                raise ValueError(f"体力读数冲突：{key}" if values else f"体力读数置信度不足：{key}")
            value = values.pop()
            self.readings[key]["value"] = value
            return value
        if key in {"top", "current", "success_points"}:
            reading = recognize_song_title(image, roi=NUMBER_REGIONS[key])
        else:
            if key in {"small_stock", "large_stock"}:
                # 完整保留乘号和最多四位库存；窄框会漏千位，残留乘号还可能被读成数字。
                # 只提取白字，避免绿色道具底图降低置信度，不放宽消费所需的读数门槛。
                ink = (crop.min(axis=2) >= 180) & (crop.max(axis=2) - crop.min(axis=2) < 60)
                crop = cv2.cvtColor(np.where(ink, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
            elif key == "preview":
                hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
                ink = (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 120)
                crop = cv2.cvtColor(np.where(ink, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
            crop = cv2.copyMakeBorder(crop, 5, 5, 5, 5, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            reading = recognize_song_title(crop, roi=(0, 0, crop.shape[1], crop.shape[0]))
        detail = {"roi": list(NUMBER_REGIONS[key]), "text": reading.text if reading else None,
                  "confidence": float(reading.confidence) if reading else None}
        self.readings[key] = detail
        if key in {"small_stock", "large_stock"}:
            detail["variant"] = "stock-white-mask"
        if reading is None or reading.confidence < .90:
            raise ValueError(f"体力读数置信度不足：{key}")
        text = reading.text.strip()
        if key == "success_points":
            match = re.fullmatch(r"LIVE\s*BOOST已回复((?:[0-9]|'[0-9]'|\"[0-9]\")+)[!！]", text)
            if match is None:
                raise ValueError("体力回复成功正文不符合预期")
            text = match.group(1)
        elif key in {"small_stock", "large_stock"}:
            match = re.fullmatch(r"[x×]((?:[0-9]|'[0-9]'|\"[0-9]\"){1,4})", text)
            if match is None:
                raise ValueError(f"体力库存必须包含完整乘号与数字：{key}")
            text = match.group(1)
        # 本地字典的数字字符保留 YAML 引号；只接受逐个数字的字典包装，不纠正字母。
        if re.fullmatch(r"(?:[0-9]|'[0-9]'|\"[0-9]\")+", text):
            text = text.replace("'", "").replace('"', "")
        value = parse_number(text, 9999 if key.endswith("stock") else 99)
        detail["value"] = value
        return value

    def template_scores(self, image):
        if getattr(image, "shape", ())[:2] != (720, 1280):
            return {}
        templates = getattr(self.detector, "templates", {})
        result = {}
        for name, template in templates.items():
            x,y,w,h = TEMPLATE_REGIONS[name]
            region = image[y:y+h,x:x+w,:3]
            result[name] = {"score": float(cv2.matchTemplate(region,template,cv2.TM_CCOEFF_NORMED).max()),
                            "difference": float(np.abs(region.astype(np.int16)-template.astype(np.int16)).mean())}
        return result

    def stable(self, page, keys=(), *, attempts=10):
        self.stage = page
        previous = None
        differences = []
        for attempt in range(attempts):
            self.stopped()
            image = self.capture(self.context)
            self.last_frame = image
            self.stopped()
            values = []
            self.readings = {}
            matched = self.detector.page(image, page)
            valid = matched
            if matched:
                for key in keys:
                    try:
                        value = self.ocr(image,key)
                        self.readings.setdefault(key,{})["value"] = value
                        values.append(value)
                    except (ValueError,TypeError) as exc:
                        self.readings.setdefault(key,{})["error"] = str(exc)
                        valid = False
            value = tuple(values) if valid else None
            self.history.append({"stage":page,"failure_kind":None if valid else "ocr" if matched else "page_delivery","attempt":attempt,"time":time.time(),"matched":matched,
                                 "readings":dict(self.readings),"templates":self.template_scores(image)})
            self.history = self.history[-16:]
            if value is not None and value == previous:
                if page == "menu" and keys == ("top",):
                    self.last_known_current = value[0]
                return value
            differences = ([f"{key}={before}->{after}" for key, before, after in zip(keys, previous, value)
                            if before != after] if value is not None and previous is not None else [])
            previous = value
            self.sleep(.12)
        details = []
        if matched:
            for key in keys:
                reading = self.readings.get(key, {})
                if "error" not in reading:
                    continue
                variants = reading.get("variants", [reading])
                evidence = ", ".join(
                    f"{variant.get('variant', 'raw')} text={variant.get('text')!r} "
                    f"confidence={variant.get('confidence')} roi={variant.get('roi')}"
                    for variant in variants
                )
                details.append(f"{key}: {reading['error']} ({evidence})")
            if not details and differences:
                details.append("连续帧读数变化 " + ", ".join(differences))
        suffix = "；" + "；".join(details) if details else ""
        raise RuntimeError(f"体力补充{'数字未稳定' if matched else '页面未送达'}：{page}{suffix}")

    def deliver_items_page(self):
        """仅重试非消费导航；已送达物品页或未知页面均不重复点击。"""
        for attempt in range(3):
            self.navigation = {"action": "choose->items", "target": [497,440], "attempt": attempt+1, "delivery_status": "checking"}
            self.stopped()
            image = self.capture(self.context)
            self.last_frame = image
            if self.detector.page(image, "items"):
                self.stable("items", attempts=3)
                self.navigation["delivery_status"] = "already-delivered"
                return
            if not self.detector.page(image, "choose"):
                self.stage = "page_delivery:choose->items"
                self.navigation["delivery_status"] = "unknown-page"
                raise RuntimeError("体力物品页未送达，当前页面未知")
            self.stable("choose", attempts=3)
            self.navigation["delivery_status"] = "clicked-awaiting-items"
            self.click(497, 440)
            try:
                self.stable("items")
                self.navigation["delivery_status"] = "delivered"
                return
            except RuntimeError:
                self.stopped()
                if not self.detector.page(self.last_frame, "choose"):
                    self.stage = "page_delivery:choose->items"
                    raise
        self.stage = "page_delivery:choose->items"
        self.navigation["delivery_status"] = "not-delivered"
        raise RuntimeError("体力道具选择到物品页未送达，导航重试耗尽")

    def save_failure(self, error, *, root=None):
        self.stopped()
        folder = Path(root or ROOT / "debug/stamina-refill-diagnostics") / f"{time.time_ns()}-{os.getpid()}"
        try:
            folder.mkdir(parents=True,exist_ok=True)
            if self.last_frame is not None:
                imwrite_unicode(folder / "failure.png",self.last_frame)
            RefillJournal.atomic_write(folder / "failure.json", {
                "stage":self.stage,"error":str(error),"last_known_current":self.last_known_current,
                "history":self.history,"navigation":self.navigation})
            return str(folder)
        except Exception as exc:
            print(f"StaminaRefill diagnostics_warning={type(exc).__name__}: {exc}",flush=True)
            return None

    def return_home_safely(self):
        # 只消费完整页面签名，绝不点击任何恢复/确认消费按钮；未知页面交通用导航处理。
        actions = {"confirm":(508,449),"items":(505,603),"choose":(640,544),"success":(640,449)}
        for _ in range(6):
            self.stopped()
            image = self.capture(self.context)
            self.last_frame = image
            self.stopped()
            if self.detector.page(image,"menu"):
                self.stable("menu")
                return True
            pages = [page for page in actions if self.detector.page(image,page)]
            if len(pages) != 1:
                return False
            page = pages[0]
            self.stable(page)
            self.click(*actions[page])
        return False

    def ensure(self, required, options, journal, *, remaining_rounds=1, minimum_required=None):
        options = validate_refill_options(options)
        if type(required) is not int or not 0 <= required <= 99:
            raise ValueError("本局所需体力必须是 0..99 的整数")
        target = home_refill_target(required, remaining_rounds, minimum_required)
        self.stopped()
        if required == 0 or remaining_rounds == 0:
            return True
        with journal.locked():
            journal.check_unresolved()
            data = journal.load()
            if data["status"] == "reserved":
                raise RuntimeError("上次体力补充结果不明，已保留消费预扣，拒绝重复消费")
            current, = self.stable("menu", ("top",))
            print(f"StaminaRefill current={current} next_required={target} required={required} remaining_rounds={remaining_rounds} recovered_total={data['reserved_points']}", flush=True)
            if current >= target:
                data["available_points"] = current
                journal.save(data)
                return True
            self.click(1149, 39)
            self.stable("choose")
            self.deliver_items_page()
            keys = ("small_stock", "large_stock", "small_count", "large_count", "current", "preview")
            stock_s, stock_l, count_s, count_l, actual, preview = self.stable("items", keys)
            if (count_s, count_l) != (0, 0) or preview != actual or actual < current:
                raise RuntimeError("体力选择初始读数冲突，拒绝消费")
            # 切页期间可能自然恢复；选择页完整读数是本次计划的新基线，下降仍拒绝。
            current = actual
            if current >= target:
                self.click(505, 603)
                observed, = self.stable("menu", ("top",))
                if observed < target:
                    raise RuntimeError("取消补充后体力读数冲突")
                data["available_points"] = observed
                journal.save(data)
                return True
            small, large = plan_refill(current, required, stock_s, stock_l, remaining_rounds,
                small_enabled=options["stamina_refill_small_enabled"], large_enabled=options["stamina_refill_large_enabled"], minimum_required=minimum_required)
            planned_target = current+small+10*large
            remaining_required = None if remaining_rounds is None else max(target,required*remaining_rounds)
            print(f"StaminaRefill plan next_required={target} planned_target={planned_target} remaining_required={remaining_required} remaining_rounds={remaining_rounds} small={small} large={large}", flush=True)
            if small + large == 0:
                self.click(505, 603)
                observed, = self.stable("menu", ("top",))
                if observed < current:
                    raise RuntimeError("取消补充后体力下降，拒绝更新可用点数")
                data["available_points"] = observed
                journal.save(data)
                return True
            selected_s = selected_l = 0
            for amount, y, kind in ((small, 228, "small"), (large, 376, "large")):
                for _ in range(amount):
                    self.click(766, y)
                    selected_s += kind == "small"
                    selected_l += kind == "large"
                    expected = (stock_s, stock_l, selected_s, selected_l, current, current+selected_s+10*selected_l)
                    if self.stable("items", keys) != expected:
                        raise RuntimeError("体力数量或预览与计划冲突，拒绝消费")
            points = small + 10*large
            self.click(770, 603)
            self.stable("confirm")
            self.stopped()
            # 唯一消费输入前持久化预扣；任何异常均保留 reserved，绝不自动重试确认。
            data.update(status="reserved", reserved_points=data["reserved_points"]+points,
                        before=current, expected=current+points, small=small, large=large)
            journal.save(data)
            self.click(770, 449)
            recovered, = self.stable("success", ("success_points",), attempts=84)
            if recovered != points:
                raise RuntimeError("体力回复成功提示与预扣计划冲突，保留未决消费并阻止再次补充")
            # 成功提示只在本次完整预扣链之后关闭，不处理启动时遗留的成功弹窗。
            self.click(640, 449)
            observed, = self.stable("menu", ("top",), attempts=84)
            if observed < current+points:
                raise RuntimeError("体力消费结果无法确认，保留未决消费并阻止再次补充")
            # 读回只证明所需体力已达标，不能排除期间自然恢复；审计累计仍按完整计划保留。
            data["success_points"] = recovered
            data["available_points"] = observed
            data["observed"] = observed
            data["status"] = "confirmed"
            journal.save(data)
            print(f"StaminaRefill target_verified=true before={current} after={observed} small={small} large={large} reserved={data['reserved_points']}", flush=True)
            return True


def ensure_stamina(context, argv, required=None, *, remaining_rounds=None, options=None, journal_root=None, session_id=None, minimum_required=None, **kwargs):
    options = validate_refill_options(options if options is not None else {})
    if not options["stamina_refill_enabled"]:
        return True
    if required is None:
        raise ValueError("任务体力补充必须明确提供本局所需体力")
    if type(required) is not int or not 0 <= required <= 99:
        raise ValueError("本局所需体力必须是 0..99 的整数")
    validate_remaining_rounds(remaining_rounds)
    if required == 0 or remaining_rounds == 0:
        return True
    task_id = getattr(getattr(argv, "task_detail", None), "task_id", None)
    journal = RefillJournal(journal_root or ROOT / "debug/stamina-refill", task_id, session_id=session_id)
    return StaminaRefill(context, **kwargs).ensure(required, options, journal, remaining_rounds=remaining_rounds, minimum_required=minimum_required)
