"""在完整原谱规划后选择协力尾部漏键，不改写谱面身份或判定分母。"""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path


def plan_cooperative_jitter(actions, *, chart_path, run_id, enabled, mode,
                            protected_note_indices=()):
    active = bool(enabled) and str(mode).strip().lower() in {"cooperative", "versus"}
    report = dict(enabled=active, status="disabled", desired=0, selected=0,
                  eligible=0, shortfall=0, dropped_note_indices=[],
                  dropped_actions=0, policy="canonical-actions-tail-v1")
    if not active:
        return list(actions), report

    payload = json.loads(Path(chart_path).read_text(encoding="utf-8-sig"))
    chart = payload if isinstance(payload, list) else payload["chart"]
    singles, note_index = [], 0
    for note in chart:
        kind = note["type"]
        if kind in {"BPM", "System"}:
            continue
        if kind in {"Long", "Slide"}:
            if not any(not bool(point.get("hidden", False)) for point in note["connections"]):
                continue
        elif kind == "Single":
            singles.append(note_index)
        elif kind != "Directional":
            raise ValueError(f"漏键不支持的谱面事件：{kind}")
        # 与 Native parser 一致：不可见的退化长条不占音符编号。
        note_index += 1

    grouped = {}
    for row in actions:
        index, due = row["note_index"], float(row["due_s"])
        if type(index) is not int or not 0 <= index < note_index or not math.isfinite(due):
            raise ValueError("漏键动作与原谱编号或时间不一致")
        grouped.setdefault(index, []).append(row)
    first_due = min(float(row["due_s"]) for row in actions)
    first_group = {row["note_index"] for row in actions
                   if abs(float(row["due_s"]) - first_due) <= 1e-9}
    protected = first_group | set(protected_note_indices)
    candidates = []
    for index in singles[1:]:
        rows = grouped.get(index, ())
        if not rows or index in protected:
            continue
        # 仅整组删除独立 Single；任何长条触点或未知动作都保留原输入。
        if any(row["kind"] not in {"tap", "flick"} or row.get("contact", -1) >= 0 for row in rows):
            raise ValueError("漏键 Single 映射包含长条或未知触点")
        candidates.append(index)
    candidates.sort(key=lambda index: (min(float(row["due_s"]) for row in grouped[index]), index))
    rng = random.Random(hashlib.sha1(str(run_id).encode("utf-8")).digest())
    desired = rng.randint(1, 2)
    count = min(desired, len(candidates))
    dropped = set(candidates[-count:]) if count else set()
    result = [row for row in actions if row["note_index"] not in dropped]
    report.update(status="planned" if dropped else "no-candidates", desired=desired,
                  eligible=len(candidates), selected=len(dropped), shortfall=desired - len(dropped),
                  dropped_note_indices=sorted(dropped), dropped_actions=len(actions) - len(result))
    return result, report
