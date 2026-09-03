"""Pure Chart 动作编译器的 Python 参考实现（仅用于离线差分测试）。

本模块不接入实时演奏路径。它与 native/realtime/src/pure_chart.cpp 的语义
逐条对齐，供 scripts/diff_native_timeline.py 和 pytest 比较 Native 输出：

- TAP / FLICK（contact=-1 表示由 Python 派发时分配）；
- Long / Slide 的 DOWN + MOVE + UP 生命周期；
- 尾部 FLICK 携带触点替换 UP；
- 最早空闲触点分配，尾时间 <= 头时间时释放；
- 中间连接点仅在 lane 变化且可见时生成 MOVE。

两边的排序键同为 (due_s, note_index, kind_rank)。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.realtime.chart_timeline import ChartTimeline


LANE_CENTERS = (190.0, 340.0, 490.0, 640.0, 790.0, 940.0, 1090.0)

_KIND_RANK = {
    "tap": 0,
    "flick": 1,
    "down": 2,
    "move": 3,
    "up": 4,
}


@dataclass
class _PendingRelease:
    contact: int
    tail_time_s: float


class _ContactAllocator:
    """与 C++ ContactAllocator 一致的确定性触点分配。"""

    def __init__(self) -> None:
        self.free = set(range(10))
        self.pending: list[_PendingRelease] = []

    def acquire(self, head_time_s: float, tail_time_s: float) -> int:
        remaining: list[_PendingRelease] = []
        for release in self.pending:
            if release.tail_time_s <= head_time_s:
                self.free.add(release.contact)
            else:
                remaining.append(release)
        self.pending = remaining
        if not self.free:
            raise RuntimeError(
                "pure chart: hold contact exhaustion "
                "(more than 10 simultaneous holds)"
            )
        contact = min(self.free)
        self.free.remove(contact)
        self.pending.append(_PendingRelease(contact, tail_time_s))
        return contact


def compile_actions(
    timeline: ChartTimeline,
    *,
    lane_centers: tuple[float, ...] = LANE_CENTERS,
) -> list[dict[str, object]]:
    """编译确定性动作序列；字段与 Native binding 输出一致。"""
    actions: list[dict[str, object]] = []
    allocator = _ContactAllocator()
    contacts: dict[int, int] = {}

    def _append(
        kind: str,
        lane: int,
        due_s: float,
        note_index: int,
        *,
        contact: int = -1,
        flick_direction: str | None = None,
    ) -> None:
        actions.append({
            "kind": kind,
            "lane": lane,
            "contact": contact,
            "target_x": lane_centers[lane],
            "due_s": due_s,
            "note_index": note_index,
            "flick_direction": flick_direction,
        })

    for judgement in timeline.judgements:
        if judgement.kind == "tap":
            _append(
                "flick" if judgement.flick else "tap",
                judgement.lane,
                judgement.time_s,
                judgement.note_index,
                flick_direction=judgement.direction,
            )
            continue
        path = next(
            (
                candidate for candidate in timeline.hold_paths
                if candidate.note_index == judgement.note_index
            ),
            None,
        )
        if path is None or len(path.points) < 2:
            raise RuntimeError("pure chart: hold head without a valid hold path")
        if judgement.kind == "hold-head":
            contact = allocator.acquire(judgement.time_s, path.tail.time_s)
            contacts[judgement.note_index] = contact
            _append(
                "down",
                int(path.head.lane),
                judgement.time_s,
                judgement.note_index,
                contact=contact,
            )
            continue
        contact = contacts[judgement.note_index]
        if judgement.tail_flick:
            _append(
                "flick",
                int(path.tail.lane),
                judgement.time_s,
                judgement.note_index,
                contact=contact,
                flick_direction=judgement.direction,
            )
        else:
            _append(
                "up",
                int(path.tail.lane),
                judgement.time_s,
                judgement.note_index,
                contact=contact,
            )

    for path in timeline.hold_paths:
        if len(path.points) < 3:
            continue
        contact = contacts[path.note_index]
        previous_lane = int(path.head.lane)
        for point in path.points[1:-1]:
            if point.hidden:
                continue
            lane = int(point.lane)
            if lane == previous_lane:
                continue
            _append(
                "move",
                lane,
                point.time_s,
                path.note_index,
                contact=contact,
            )
            previous_lane = lane

    actions.sort(key=lambda item: (
        item["due_s"],
        item["note_index"],
        _KIND_RANK[str(item["kind"])],
    ))
    return actions
