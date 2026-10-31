"""在内存动作上规划有意的单 TAP 时间偏移，不推断游戏实际判定。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import random

from .chart_timeline import ChartTimeline
from .native_engine import LANE_CENTERS, TOUCH_Y


RANDOM_GREAT_DEFAULTS = {
    "native_random_great_enabled": False,
    "native_random_great_base_percent": 2.0,
    "native_random_great_level_start": 26,
    "native_random_great_per_level_percent": 1.0,
    "native_random_great_max_percent": 10.0,
    "native_random_great_offset_ms": 45,
}
RANDOM_GREAT_MODES = frozenset({"formal", "challenge", "cooperative", "versus", "continuous", "medley"})


def validate_random_great_options(options):
    result = {key: options.get(key, default) for key, default in RANDOM_GREAT_DEFAULTS.items()}
    if type(result["native_random_great_enabled"]) is not bool:
        raise ValueError("native_random_great_enabled 必须是布尔值")
    for key in ("native_random_great_base_percent", "native_random_great_per_level_percent", "native_random_great_max_percent"):
        value = result[key]
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or not 0 <= value <= 20):
            raise ValueError(f"{key} 必须是 0..20 的有限数字")
        result[key] = float(value)
    for key, low, high in (("native_random_great_level_start", 1, 40), ("native_random_great_offset_ms", 35, 60)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} 必须是 {low}..{high} 的整数")
    return result


def random_great_enabled(options, mode):
    return options.get("native_random_great_enabled", False) is True and str(mode).strip().lower() in RANDOM_GREAT_MODES


def chart_random_great_inputs(chart_path):
    payload = json.loads(Path(chart_path).read_text(encoding="utf-8-sig"))
    raw = payload if isinstance(payload, list) else payload["chart"]
    ordinary, total, note_index = set(), 0, 0
    for note in raw:
        kind = note["type"]
        if kind in {"BPM", "System"}:
            continue
        if kind in {"Long", "Slide"}:
            visible = sum(not bool(point.get("hidden", False)) for point in note["connections"])
            if not visible:
                continue
            total += visible
        elif kind in {"Single", "Directional"}:
            total += 1
            if kind == "Single" and not note.get("flick", False) and not note.get("direction"):
                ordinary.add(note_index)
        else:
            raise ValueError(f"不支持的谱面事件：{kind}")
        # 与 Native parser 同步：隐藏点全空的路径不占编号；单点退化路径仍占号。
        note_index += 1
    return ordinary, total


@dataclass(frozen=True, slots=True)
class RandomGreatHold:
    contact: int
    points: tuple[tuple[float, float], ...]
    sampled_points: tuple[tuple[float, float], ...]
    end_s: float


@dataclass(frozen=True, slots=True)
class RandomGreatSafetyContext:
    visible: tuple[tuple[float, int, bool], ...]
    ordinary: frozenset[int]
    holds: tuple[RandomGreatHold, ...]
    action_signature: tuple
    max_contacts: int
    judgement_y: float


def _signature(actions):
    return tuple(tuple(row.get(key) for key in ("kind", "due_s", "lane", "target_x", "contact", "note_index", "flick_direction")) for row in actions)


def _lane_x(lane, centers):
    if not math.isfinite(lane) or not -.5 <= lane <= 6.5:
        raise ValueError("随机 GREAT 长条轨迹范围未知")
    left = min(5, max(0, math.floor(lane)))
    return centers[left] + (lane - left) * (centers[left + 1] - centers[left])


def build_random_great_safety_context(chart_path, actions, *, lane_centers=LANE_CENTERS,
                                     judgement_y=TOUCH_Y, max_contacts=10):
    centers = tuple(float(value) for value in lane_centers)
    if (len(centers) != 7 or not all(math.isfinite(value) for value in centers)
            or any(b <= a for a, b in zip(centers, centers[1:])) or not math.isfinite(judgement_y)
            or type(max_contacts) is not int or not 1 <= max_contacts <= 10):
        raise ValueError("随机 GREAT 输入映射或触点容量未知")
    timeline = ChartTimeline.from_json(chart_path)
    payload = json.loads(Path(chart_path).read_text(encoding="utf-8-sig"))
    raw = payload if isinstance(payload, list) else payload["chart"]
    visible, ordinary, index = [], set(), 0
    for note in raw:
        kind = note["type"]
        if kind in {"BPM", "System"}:
            continue
        if kind in {"Long", "Slide"}:
            points = note["connections"]
            if not any(not point.get("hidden", False) for point in points):
                continue
            if (type(note.get("width", 1)) is not int or note.get("width", 1) != 1
                    or any(type(point.get("width", 1)) is not int or point.get("width", 1) != 1 for point in points)):
                raise ValueError("随机 GREAT 不支持未知宽度长条")
            visible.extend((timeline.tempo_map.seconds_at(point["beat"]), index, False)
                           for point in points if not point.get("hidden", False))
        else:
            plain = kind == "Single" and not note.get("flick", False) and not note.get("direction")
            if plain and (type(note.get("width", 1)) is not int or note.get("width", 1) != 1):
                raise ValueError("随机 GREAT 不支持未知宽度 TAP")
            visible.append((timeline.tempo_map.seconds_at(note["beat"]), index, plain))
            if plain:
                ordinary.add(index)
        index += 1
    paths = {path.note_index: path for path in timeline.hold_paths}
    tracks, contacts = [], []
    lifecycle = [row for row in actions if row["kind"] in {"down", "move", "up"}
                 or row["kind"] == "flick" and row.get("contact", -1) >= 0]
    if any(row.get("note_index") not in paths for row in lifecycle):
        raise ValueError("随机 GREAT 无法绑定长条生命周期")
    for note_index, path in paths.items():
        rows = sorted((row for row in lifecycle if row["note_index"] == note_index), key=lambda row: row["due_s"])
        heads = [row for row in rows if row["kind"] == "down"]
        tails = [row for row in rows if row["kind"] in {"up", "flick"}]
        if (len(heads) != 1 or len(tails) != 1 or rows[0] is not heads[0]
                or type(heads[0]["contact"]) is not int or not 0 <= heads[0]["contact"] < 10
                or any(row["contact"] != heads[0]["contact"] for row in rows)):
            raise ValueError("随机 GREAT 长条触点未闭合或编号未知")
        points = tuple((point.time_s, _lane_x(point.lane, centers)) for point in path.points)
        if (abs(heads[0]["due_s"] - points[0][0]) > 1e-8
                or abs(tails[0]["due_s"] - points[-1][0]) > 1e-8):
            raise ValueError("随机 GREAT 长条时间与原谱不一致")
        for row in rows:
            at, x = float(row["due_s"]), float(row["target_x"])
            expected = _path_x(points, at)
            if not math.isfinite(x) or abs(x - expected) > .001:
                raise ValueError("随机 GREAT 长条空间映射与 Native 不一致")
        end = points[-1][0]
        if tails[0]["kind"] == "flick":
            direction = tails[0].get("flick_direction")
            if direction not in {None, "Left", "Right"}:
                raise ValueError("随机 GREAT 长条尾粉键方向未知")
            end += .080
            # 粉键尾部的水平扫动也纳入保守包络；邻近判定门禁仍独立保留。
            points += ((end, points[-1][1] + (-120 if direction == "Left" else 120 if direction == "Right" else 0)),)
        sampled = tuple((float(row["due_s"]), float(math.floor(float(row["target_x"]) + .5)))
                        for row in rows if row["kind"] != "up")
        tracks.append(RandomGreatHold(heads[0]["contact"], points, sampled, end))
        contacts.append((heads[0]["contact"], points[0][0], end))
    for position, (contact, lo, hi) in enumerate(contacts):
        if any(other == contact and lo < end and hi > start for other, start, end in contacts[position + 1:]):
            raise ValueError("随机 GREAT 长条触点重复占用")
    for row in actions:
        if row["kind"] == "tap":
            if (row.get("contact") != -1 or not math.isfinite(float(row["target_x"]))
                    or abs(float(row["target_x"]) - _lane_x(float(row["lane"]), centers)) > .001):
                raise ValueError("随机 GREAT TAP 空间映射或触点表示未知")
    expected_taps = {row.note_index: row for row in timeline.judgements if row.kind == "tap"}
    for row in actions:
        if row["kind"] in {"tap", "flick"} and row.get("contact", -1) < 0:
            expected = expected_taps.get(row.get("note_index"))
            if (expected is None or abs(float(row["due_s"]) - expected.time_s) > 1e-8
                    or row["lane"] != expected.lane or (row["kind"] == "flick") != expected.flick):
                raise ValueError("随机 GREAT 原始判定与 Native 不一致")
    return RandomGreatSafetyContext(tuple(sorted(visible)), frozenset(ordinary), tuple(tracks),
                                   _signature(actions), max_contacts, float(judgement_y))


def _path_x(points, at):
    for a, b in zip(points, points[1:]):
        if a[0] <= at <= b[0]:
            fraction = 0 if b[0] == a[0] else (at - a[0]) / (b[0] - a[0])
            return a[1] + fraction * (b[1] - a[1])
    return points[0][1] if at <= points[0][0] else points[-1][1]


def _hold_window_clear(hold, lo, hi, tap_x):
    if hold.points[0][0] > hi or hold.end_s < lo:
        return True
    for a, b in zip(hold.points, hold.points[1:]):
        if b[0] < lo or a[0] > hi:
            continue
        # 连续原谱插值（包括隐藏几何点）必须全段远离 TAP，不能只看头尾。
        left, right = max(lo, a[0]), min(hi, b[0])
        xs = (a[1], b[1]) if a[0] == b[0] else (_path_x((a, b), left), _path_x((a, b), right))
        distance = max(min(xs) - tap_x, tap_x - max(xs), 0)
        if distance < 300 - 1e-7:
            return False
    # Native MOVE 之间保留上次像素；检查窗前一采样，避免上游位置被插值掩盖。
    rounded_tap = math.floor(tap_x + .5)
    for position, (at, x) in enumerate(hold.sampled_points):
        end = hold.sampled_points[position + 1][0] if position + 1 < len(hold.sampled_points) else hold.end_s
        if at <= hi and end >= lo and abs(x - rounded_tap) < 300:
            return False
    return True


def _within(times, at, radius):
    key = round(at * 1e9)
    margin = round(radius * 1e9)
    return bisect_left(times, key + margin) - bisect_right(times, key - margin)


def _balanced_sample(candidates, desired, rng):
    fast_only = [row for row in candidates if row[1] == (-1,)]
    slow_only = [row for row in candidates if row[1] == (1,)]
    both = [row for row in candidates if row[1] == (-1, 1)]
    for size in range(min(desired, len(candidates)), -1, -1):
        quotas = list({size // 2, size - size // 2})
        rng.shuffle(quotas)
        for fast in quotas:
            slow = size - fast
            minimum_fast_only = max(0, fast - len(both), size - len(both) - len(slow_only))
            if minimum_fast_only > min(fast, len(fast_only)):
                continue
            nf = rng.randint(minimum_fast_only, min(fast, len(fast_only)))
            minimum_slow_only = max(0, size - len(both) - nf)
            if minimum_slow_only > min(slow, len(slow_only)):
                continue
            ns = rng.randint(minimum_slow_only, min(slow, len(slow_only)))
            flexible = rng.sample(both, size - nf - ns)
            selected = [(row, -1) for row in rng.sample(fast_only, nf)] + [(row, 1) for row in rng.sample(slow_only, ns)]
            selected += [(row, -1 if index < fast - nf else 1) for index, row in enumerate(flexible)]
            rng.shuffle(selected)
            return selected
    return []


def _verify_selected_plan(actions, result, selected, visible):
    selected_indices = {row[0] for row, _ in selected}
    shifts = {actions[index]["note_index"]: result[index]["due_s"] for index in selected_indices}
    final_times = sorted(round(shifts.get(note, at) * 1e9) for at, note, _ in visible)
    for index, (before, after) in enumerate(zip(actions, result)):
        if index not in selected_indices:
            if before != after:
                raise ValueError("随机 GREAT 复核发现非目标动作变化")
        elif before["kind"] != "tap" or before | {"due_s": after["due_s"]} != after:
            raise ValueError("随机 GREAT 复核发现手势变化")
        elif _within(final_times, after["due_s"], .070) != 1:
            raise ValueError("随机 GREAT 复核发现判定间距不足")


def plan_random_great(actions, *, options, mode, run_id, level=None, total_judgements,
                      ordinary_single_note_indices, safety_context=None):
    config = validate_random_great_options(options)
    active = random_great_enabled(config, mode)
    trusted_level = type(level) is int and 1 <= level <= 40
    percent = min(config["native_random_great_max_percent"], config["native_random_great_base_percent"]
                  + max(0, level - config["native_random_great_level_start"]) * config["native_random_great_per_level_percent"]
                  if trusted_level else config["native_random_great_base_percent"])
    if type(total_judgements) is not int or total_judgements <= 0:
        raise ValueError("随机 GREAT 缺少有效整曲判定数")
    seed = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
    desired = int(math.floor(total_judgements * percent / 100 + .5)) if active else 0
    report = dict(enabled=active, status="planned" if active else "disabled", seed=seed,
                  total_judgements=total_judgements, level=level if trusted_level else None,
                  level_source="confirmed-chart" if trusted_level else "base-fallback",
                  target_percent=percent, desired=desired, eligible=0, selected=0,
                  planned_percent=0.0, fast=0, slow=0, shortfall=desired, selected_notes=[],
                  actual_great_measured=False, offset_ms=config["native_random_great_offset_ms"],
                  eligibility_policy="separated-hold-300px-v2" if safety_context is not None else "isolated-v1")
    if not active or not actions:
        return list(actions), report
    offset = config["native_random_great_offset_ms"] / 1000
    ordered = sorted(enumerate(actions), key=lambda pair: float(pair[1]["due_s"]))
    times = [float(row["due_s"]) for _, row in ordered]
    if not all(math.isfinite(value) for value in times):
        raise ValueError("随机 GREAT 动作时间必须有限")
    if safety_context is not None:
        if safety_context.action_signature != _signature(actions) or len(safety_context.visible) != total_judgements:
            raise ValueError("随机 GREAT 安全上下文与本局原谱动作不一致")
        visible = safety_context.visible
        ordinary = ordinary_single_note_indices & safety_context.ordinary
    else:
        visible = tuple((float(row["due_s"]), row.get("note_index"), row["kind"] == "tap")
                        for _, row in ordered if row["kind"] != "move")
        ordinary = ordinary_single_note_indices
    judgment_times = sorted(round(row[0] * 1e9) for row in visible)
    counts = Counter(judgment_times)
    non_tap_times = sorted(round(row[0] * 1e9) for row in visible if not row[2])
    candidates, active_contacts = [], set()
    for index, row in ordered:
        due, kind = float(row["due_s"]), row["kind"]
        if kind == "down":
            active_contacts.add(row.get("contact"))
        elif kind == "up" or kind == "flick" and row.get("contact", -1) >= 0:
            active_contacts.discard(row.get("contact"))
        if (kind != "tap" or row.get("note_index") not in ordinary or counts[round(due * 1e9)] != 1
                or round(due * 1e9) in (judgment_times[0], judgment_times[-1])):
            continue
        if _within(judgment_times, due, 2 * offset + .070) != 1:
            continue
        if safety_context is None:
            if active_contacts or _within(non_tap_times, due, .200 + offset):
                continue
            signs, overlap = (-1, 1), False
        else:
            lo, hi = due - offset, due + offset + .050
            overlapping = [hold for hold in safety_context.holds if hold.points[0][0] <= hi and hold.end_s >= lo]
            # 同一空闲槽必须覆盖整窗，兼容跨块未来 DOWN 的显式预留。
            reserved = {hold.contact for hold in overlapping}
            if (not set(range(safety_context.max_contacts)) - reserved
                    or any(not _hold_window_clear(hold, lo, hi, float(row["target_x"])) for hold in overlapping)):
                continue
            signs = tuple(sign for sign in (-1, 1) if not _within(non_tap_times, due + sign * offset, .200))
            overlap = bool(overlapping)
        if signs:
            candidates.append((index, signs, overlap))
    rng = random.Random(seed)
    selected = _balanced_sample(candidates, desired, rng)
    result, details = list(actions), []
    for (index, signs, overlap), sign in selected:
        row = actions[index]
        shifted = float(row["due_s"]) + sign * offset
        result[index] = dict(row, due_s=shifted)
        if len(details) < 256:
            details.append(dict(note_index=row["note_index"], original_s=float(row["due_s"]),
                                planned_s=shifted, direction="FAST" if sign < 0 else "SLOW",
                                during_separated_hold=overlap))
    fast = sum(sign < 0 for _, sign in selected)
    _verify_selected_plan(actions, result, selected, visible)
    report.update(eligible=len(candidates), eligible_both=sum(row[1] == (-1, 1) for row in candidates),
                  eligible_fast_only=sum(row[1] == (-1,) for row in candidates),
                  eligible_slow_only=sum(row[1] == (1,) for row in candidates),
                  selected=len(selected), planned_percent=len(selected) * 100 / total_judgements,
                  fast=fast, slow=len(selected) - fast, shortfall=max(0, desired - len(selected)),
                  selected_with_active_hold=sum(row[2] for row, _ in selected),
                  selected_notes=details, selected_notes_truncated=len(selected) > len(details))
    report.update(original_judgement_gap_ms=2 * config["native_random_great_offset_ms"] + 70,
                  shifted_non_tap_guard_ms=200, minimum_hold_distance_px=300,
                  contact_capacity_required=10 if report["selected_with_active_hold"] else None)
    return result, report
