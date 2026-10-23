"""房间选曲输入门禁与独立于 LiveRun 的有界早期证据。"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from uuid import uuid4

import cv2

try:
    from ..foreground_guard import require_game_foreground
except ImportError:
    from foreground_guard import require_game_foreground

from .startup_diagnostics import CooperativeStartupRecorder
from .optional_diagnostics import safe_print


class RoomEntryTransient(RuntimeError):
    """有明确未开演来源的匹配或选曲未送达，不占演出失败预算。"""


def check_preplay_connection_failed(flow, image):
    check_stopping(flow)
    # “错误”标题也用于连接失败，必须先识别正文，不能点击成员退出的居中按钮。
    if flow.visible(image, "connect_failed_body", 0.90):
        raise RoomEntryTransient("开演前连接失败，重新确认安全页面后重入")


def retry_connection_failed(flow, attempts=3):
    for attempt in range(max(1, int(attempts))):
        check_stopping(flow)
        image = flow.capture()
        check_stopping(flow)
        if not flow.visible(image, "connect_failed_body", 0.90):
            return True
        safe_print(f"RoomEntry connection_failed=retry attempt={attempt + 1}", flush=True)
        check_stopping(flow)
        flow.click((748, 527))
        check_stopping(flow)
        time.sleep(0.8)
    check_stopping(flow)
    image = flow.capture()
    check_stopping(flow)
    return not flow.visible(image, "connect_failed_body", 0.90)


def rematch_backoff(flow, failures):
    # 累计次数只影响重匹配节奏；安全页面已经恢复时不能据此结束任务。
    deadline = time.monotonic() + min(2.0, max(0, failures - 3) * 0.25)
    while time.monotonic() < deadline:
        check_stopping(flow)
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    check_stopping(flow)


def wait_for_preplay_recovery_page(flow, room_marker, home_node):
    # 超时发生的位置不能证明游戏仍未开演；未知或黑场只观察，禁止盲目退出。
    deadline = time.monotonic() + 60.0
    connection_retried = False
    connection_cancels = 0
    while time.monotonic() < deadline:
        check_stopping(flow)
        image = flow.capture()
        check_stopping(flow)
        if flow.visible(image, "connect_failed_body", 0.90):
            if not connection_retried:
                connection_retried = True
                retry_connection_failed(flow, attempts=3)
                continue
            # 重试无进展时只取消仍有明确正文的双按钮弹窗；每次输入后重新观察。
            if connection_cancels < 3:
                safe_print(f"RoomEntry connection_failed=cancel attempt={connection_cancels + 1}", flush=True)
                check_stopping(flow)
                flow.click((510, 527))
                check_stopping(flow)
                connection_cancels += 1
                time.sleep(0.8)
            else:
                time.sleep(0.1)
            continue
        if any(flow.visible(image, name)
               for name in ("room_search", room_marker,
                            "song_unspecified", "ready_button", "live_entry")):
            return
        if flow.pipeline_box(image, home_node) is not None:
            return
        # 服务器更新可能把游戏送回登录或资源下载页，由 CommonRecover 接管下载。
        if any(flow.pipeline_box(image, node) is not None for node in (
            "AutoLiveLoginScreenMarker", "ResourceDownloadConfirm",
            "ResourceDownloadPageMarker", "ResourceDownloadProgressMarker",
        )):
            return
        time.sleep(0.1)
    check_stopping(flow)
    raise RuntimeError("开演前恢复无法确认安全页面，保留当前现场并停止")


def check_stopping(flow):
    if flow.stopped():
        raise InterruptedError("用户已停止任务")


def guarded_song_choice_pipeline(mode, point, template_shape, template_position):
    height, width = template_shape[:2]
    x, y = template_position
    left, top = max(0, x - 8), max(0, y - 8)
    node = "RoomSongChoiceGuardedClick"
    # 与 Python 模板检测使用相同局部窗口、算法和阈值；框架识别与点击在同一节点执行。
    return node, {node: {
        "recognition": "TemplateMatch",
        "template": [f"{mode}/song_unspecified.png"],
        "roi": [left, top, min(1280, x + width + 8) - left,
                min(720, y + height + 8) - top],
        "threshold": [0.90], "method": 5, "green_mask": False,
        "action": "Click", "target": [point[0], point[1], 1, 1],
        "timeout": 0, "rate_limit": 0, "pre_delay": 0, "post_delay": 0,
        "next": [], "on_error": [],
    }}


def guarded_song_choice_click(flow, point, mode, template_position):
    check_stopping(flow)
    require_game_foreground(flow.context.tasker.controller)
    check_stopping(flow)
    node, pipeline = guarded_song_choice_pipeline(
        mode, point, flow.templates["song_unspecified"].shape, template_position,
    )
    detail = flow.context.run_task(node, pipeline)
    check_stopping(flow)
    # 选曲页在保护查询或截图期间消失是正常竞争结果，由外层重新观察准备页。
    return bool(detail and detail.status.succeeded)


def select_song_and_wait(flow, song_choice, *, random_point, unspecified_point,
                         confirm_point, timeout, label):
    deadline = time.monotonic() + timeout
    option = {"random": random_point, "unspecified": unspecified_point}.get(song_choice)
    confirms = 0
    option_sent = option is None
    option_attempts = 0
    next_confirm_at = time.monotonic()
    if option is not None:
        option_attempts += 1
        _phase(flow, "before-option", option)
        check_stopping(flow)
        option_sent = flow.guarded_song_choice_click(option)
        check_stopping(flow)
        _phase(flow, "after-option", option,
               input_result="sent" if option_sent else "page-gate-miss")
    while time.monotonic() < deadline:
        check_stopping(flow)
        state, _ = flow.wait_for(("ready_button", "song_unspecified"),
            timeout=min(0.5, max(0.0, deadline - time.monotonic())))
        check_stopping(flow)
        if state == "ready_button":
            return
        if state == "song_unspecified" and not option_sent and option_attempts < 3:
            option_attempts += 1
            _phase(flow, f"before-option-{option_attempts}", option)
            option_sent = flow.guarded_song_choice_click(option)
            check_stopping(flow)
            _phase(flow, f"after-option-{option_attempts}", option,
                   input_result="sent" if option_sent else "page-gate-miss")
        elif (state == "song_unspecified" and option_sent and confirms < 3
              and time.monotonic() >= next_confirm_at):
            confirms += 1
            _phase(flow, f"before-confirm-{confirms}", confirm_point)
            sent = flow.guarded_song_choice_click(confirm_point)
            check_stopping(flow)
            _phase(flow, f"after-confirm-{confirms}", confirm_point,
                   input_result="sent" if sent else "page-gate-miss")
            next_confirm_at = time.monotonic() + 0.5
        # 即使 wait_for 立即命中选曲页，确认也只做三次且不会忙等。
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    check_stopping(flow)
    _phase(flow, "ready-timeout")
    raise RoomEntryTransient(f"确认{label}选曲后60秒内未进入演出准备页")


def _phase(flow, phase, target=None, **details):
    evidence = getattr(flow, "_room_entry_evidence", None)
    if evidence is not None:
        try:
            evidence.phase = phase
            evidence.target = target
            evidence.mark(**details)
        except Exception as exc:
            print(f"RoomEntry diagnostics_warning={type(exc).__name__}: {exc}", flush=True)


class RoomEntryEvidence:
    def __init__(self, settings, mode, task_id, positions):
        self.enabled = any(settings.get(key, False) for key in
                           ("debug_recording", "opening_recording", "diagnostic_trace"))
        self.keep_frames = bool(settings.get("debug_recording") or settings.get("opening_recording"))
        self.attempt_id = f"room-entry-{mode}-{datetime.now():%Y%m%d-%H%M%S-%f}-{uuid4().hex[:8]}"
        self.mode, self.task_id, self.positions = mode, task_id, positions
        self.phase, self.target = "room-entry", None
        self.events, self.frames = [], {}
        self.last_image = self.last_event = None
        self._last_at = float("-inf")
        self._last_phase = None
        self._closed = False
        self.active = True
        self.observations_dropped = 0

    def mark(self, **details):
        if not self.enabled or self.last_event is None:
            return
        event = {**self.last_event, "phase": self.phase, "input_target": self.target,
                 "event_monotonic": time.monotonic(), **details}
        self._append(event)
        if (self.keep_frames and self.phase.startswith("before-")
                and self.phase not in self.frames and len(self.frames) < 22):
            self.frames[self.phase] = (self.last_image, event)

    def _append(self, event):
        if len(self.events) < 511:
            self.events.append(event)
        else:
            self.events[-1] = event
            self.observations_dropped += 1

    def observe(self, image, captured_at, templates):
        if not self.enabled or not self.active:
            return
        try:
            scores = {}
            for name in ("song_unspecified", "ready_button", "member_exit_title", "connect_failed_body"):
                template = templates.get(name)
                if template is None:
                    continue
                height, width = template.shape[:2]
                x, y = self.positions[name]
                left, top = max(0, x - 8), max(0, y - 8)
                search = image[top:min(image.shape[0], y + height + 8),
                               left:min(image.shape[1], x + width + 8)]
                if search.shape[0] < height or search.shape[1] < width:
                    continue
                _, score, _, location = cv2.minMaxLoc(
                    cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED))
                threshold = 0.93 if name == "member_exit_title" else 0.90
                scores[name] = {"score": float(score), "threshold": threshold,
                    "box": [left + location[0], top + location[1], width, height],
                    "hit": float(score) >= threshold}
            event = {"phase": self.phase, "input_target": self.target,
                     "captured_at": captured_at, "event_monotonic": time.monotonic(),
                     "timestamp_kind": "capture-completed-upper-bound",
                     "attempt_id": self.attempt_id, "task_id": self.task_id,
                     "mode": self.mode, "templates": scores}
            # 高频重复画面只留低频状态；阶段首帧和最后一帧始终有界保留。
            self.last_image, self.last_event = image.copy(), event
            if self.phase != self._last_phase or captured_at - self._last_at >= 1.0:
                self._append(event)
                self._last_at, self._last_phase = captured_at, self.phase
            if self.keep_frames and self.phase not in self.frames and len(self.frames) < 22:
                self.frames[self.phase] = (self.last_image, event)
        except Exception as exc:
            print(f"RoomEntry diagnostics_warning={type(exc).__name__}: {exc}", flush=True)

    def close(self, root, outcome):
        if self._closed:
            return
        self._closed = True
        self.active = False
        if not self.enabled or self.last_event is None:
            return
        terminal = {**self.last_event, "phase": "entry-terminal", "outcome": outcome,
                    "event_monotonic": time.monotonic(),
                    "observations_dropped": self.observations_dropped}
        frames = {id(event): image for image, event in self.frames.values()}
        events = list(self.events)
        # 失败和停止只落盘已缓存末帧；不从这里截图或访问 LiveRun。
        events.append(terminal)
        last_image = self.last_image
        destination = root / self.attempt_id

        def write_cached():
            recorder = None
            try:
                recorder = CooperativeStartupRecorder(destination, None, queue_capacity=24,
                    max_frames=23, max_events=512)
                for event in events:
                    image = last_image if event is terminal else frames.get(id(event))
                    recorder.record(image, event["event_monotonic"], event)
                print(f"RoomEntry recording={recorder.output_dir} attempt_id={self.attempt_id}", flush=True)
            except Exception as exc:
                print(f"RoomEntry diagnostics_warning={type(exc).__name__}: {exc}", flush=True)
            finally:
                if recorder is not None:
                    try:
                        recorder.close(outcome)
                    except Exception as exc:
                        print(f"RoomEntry diagnostics_warning={type(exc).__name__}: {exc}", flush=True)

        threading.Thread(target=write_cached, daemon=True, name="room-entry-evidence").start()
