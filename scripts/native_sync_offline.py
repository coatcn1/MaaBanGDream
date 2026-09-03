"""离线 SongClockSynchronizer 证据前端（不进实时路径）。

从 trace.jsonl 重建两类证据：
1. 观测序列：每帧每个 lane 只保留最靠近判定线的检测（最大 y），再做跨 kind
   的连续性跟踪；"近线局部速度"外推 crossing；静态 GO/前奏误检（无持续
   下落运动）会被运动门禁排除；
2. photogate 粗锚点：第一条可信轨迹的 crossing 减去谱面首判定时间。

跟踪假设（基于两份失败 trace 的实测）：
- hold 头接近判定线时检测器会同时报出"头(近线)/残体(上方)/新生音符(顶部)"
  多个检测，因此逐样本关联不可靠，必须按帧取最大 y；
- 判定线附近存在透视加速，必须用跨过参考线的相邻样本外推，不能用整段回归；
- 音符过线后会有 y≈564 的击打残影，因此只取时间轴上第一个穿过参考线的上升段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


JUDGEMENT_Y = 565.0
TRACK_GAP_S = 0.40
ATTACH_MIN_TOLERANCE = 70.0
MIN_SAMPLES = 4
MIN_ABOVE_LINE_SAMPLES = 3
MIN_NEAR_LINE_Y = 480.0
CROSSING_REFERENCE_Y = 545.0
MIN_VELOCITY = 250.0
MAX_VELOCITY = 8000.0

NOTE_KINDS = {"tap", "flick", "skill", "hold"}


@dataclass(frozen=True)
class Observation:
    """一条已通过运动门禁的 crossing 投影。"""

    time_s: float
    lane: int
    kind: str
    confidence: float


class _Track:
    def __init__(self, lane: int, first_time: float, first_y: float, kind: str):
        self.lane = lane
        self.kind = kind
        self.samples: list[tuple[float, float]] = [(first_time, first_y)]
        self.last_time = first_time
        self.last_y = first_y

    def attach(self, time_s: float, y: float) -> None:
        self.samples.append((time_s, y))
        self.last_time = time_s
        self.last_y = y

    def predicted_y(self, time_s: float) -> tuple[float, float]:
        """速度感知预测：返回 (预测 y, 吸附容差)。"""
        # 容差以"近线最大合理速度 × 时间间隔"为上限：透视加速下相邻帧可
        # 位移上百像素，但静态残影的瞬时大跳（如 255px/16ms）会被拒绝。
        tolerance = max(
            ATTACH_MIN_TOLERANCE,
            MAX_VELOCITY * (time_s - self.last_time),
        )
        if len(self.samples) >= 2:
            (t1, y1), (t2, y2) = self.samples[-2:]
            if t2 > t1:
                velocity = (y2 - y1) / (t2 - t1)
                return y2 + velocity * (time_s - t2), tolerance
        return self.last_y, tolerance

    def crossing(self) -> tuple[float, float] | None:
        """返回 (crossing 引擎时间, 置信度)；不合格返回 None。"""
        if len(self.samples) < MIN_SAMPLES:
            return None
        above = [(t, y) for t, y in self.samples if y <= JUDGEMENT_Y]
        if len(above) < MIN_ABOVE_LINE_SAMPLES:
            return None
        max_y = max(y for _t, y in above)
        if max_y < MIN_NEAR_LINE_Y:
            return None

        # 取时间轴上第一个穿过参考线的上升段，用其局部速度外推判定线。
        crossing: float | None = None
        velocity = 0.0
        for index in range(1, len(above)):
            t0, y0 = above[index - 1]
            t1, y1 = above[index]
            if y0 < CROSSING_REFERENCE_Y <= y1 and t1 > t0:
                velocity = (y1 - y0) / (t1 - t0)
                crossing = t0 + (JUDGEMENT_Y - y0) / velocity
                break
        if crossing is None:
            return None
        if not MIN_VELOCITY <= velocity <= MAX_VELOCITY:
            return None
        if not self.samples[0][0] - 0.5 <= crossing <= self.last_time + 1.0:
            return None
        confidence = min(
            1.0,
            len(self.samples) / 8.0,
            max(0.0, (max_y - self.samples[0][1]) / 60.0),
        )
        if confidence <= 0:
            return None
        return crossing, confidence


def _iter_frames(path: Path, until_s: float | None) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            elapsed = float(row["elapsed_ms"]) / 1000.0
            if until_s is not None and elapsed > until_s:
                break
            yield row


def _frame_maximums(row: dict) -> dict[int, tuple[float, str]]:
    """每帧每个 lane 取最靠近判定线的检测（最大 y），返回 {lane: (y, kind)}。"""
    maximums: dict[int, tuple[float, str]] = {}
    for note in row.get("notes", []):
        kind = str(note.get("kind"))
        if kind not in NOTE_KINDS:
            continue
        lane = int(note["lane"])
        y = float(note["y"])
        current = maximums.get(lane)
        if current is None or y > current[0]:
            maximums[lane] = (y, kind)
    return maximums


def extract_observations(
    path: Path,
    *,
    until_s: float | None = None,
) -> list[Observation]:
    """重建观测序列；同一 lane 上 80ms 内的重复投影只保留置信度最高者。"""
    tracks: list[_Track] = []
    observations: list[Observation] = []

    def close_stale(now: float) -> None:
        nonlocal tracks
        alive: list[_Track] = []
        for track in tracks:
            if now - track.last_time > TRACK_GAP_S:
                projected = track.crossing()
                if projected is not None:
                    crossing, confidence = projected
                    observations.append(Observation(
                        crossing,
                        track.lane,
                        track.kind,
                        confidence,
                    ))
            else:
                alive.append(track)
        tracks = alive

    for row in _iter_frames(path, until_s):
        elapsed = float(row["elapsed_ms"]) / 1000.0
        for lane, (y, _kind) in sorted(_frame_maximums(row).items()):
            candidates: list[tuple[float, _Track]] = []
            for track in tracks:
                if (
                    track.lane != lane
                    or elapsed - track.last_time > TRACK_GAP_S
                ):
                    continue
                if y < track.last_y - ATTACH_MIN_TOLERANCE:
                    # 新音符：y 大幅上跳（更靠近屏幕顶部），不属于当前轨迹。
                    continue
                predicted, tolerance = track.predicted_y(elapsed)
                distance = abs(y - predicted)
                if distance <= tolerance:
                    candidates.append((distance, track))
            if candidates:
                target = min(candidates, key=lambda pair: pair[0])[1]
                target.attach(elapsed, y)
            else:
                tracks.append(_Track(lane, elapsed, y, _kind))
        close_stale(elapsed)

    for track in tracks:
        projected = track.crossing()
        if projected is not None:
            crossing, confidence = projected
            observations.append(Observation(
                crossing, track.lane, track.kind, confidence,
            ))

    observations.sort(key=lambda item: item.time_s)
    deduplicated: list[Observation] = []
    for observation in observations:
        if deduplicated and (
            observation.lane == deduplicated[-1].lane
            and observation.time_s - deduplicated[-1].time_s < 0.08
        ):
            if observation.confidence > deduplicated[-1].confidence:
                deduplicated[-1] = observation
            continue
        deduplicated.append(observation)
    return deduplicated


def first_hp_loss_ms(path: Path) -> float | None:
    for row in _iter_frames(path, None):
        if float(row.get("life_value", 1000)) < 1000:
            return float(row["elapsed_ms"])
    return None


def derive_go_anchor(
    observations: list[Observation],
    chart_start_time_s: float,
    *,
    uncertainty_s: float = 0.6,
) -> tuple[float, float] | None:
    """photogate 粗锚点：第一条可信轨迹 crossing 减去谱面首判定时间。"""
    trusted = [
        observation for observation in observations
        if observation.confidence >= 0.5
    ]
    if not trusted:
        return None
    first = trusted[0]
    anchor = first.time_s - chart_start_time_s
    if anchor < 0:
        return None
    return anchor, uncertainty_s
