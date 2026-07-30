from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionKind(str, Enum):
    TAP = "tap"
    DOWN = "down"
    MOVE = "move"
    UP = "up"
    FLICK = "flick"


def sliding_holds_enabled(difficulty: str) -> bool:
    return str(difficulty).strip().lower() in {"hard", "expert", "special"}


@dataclass(frozen=True)
class TouchAction:
    kind: ActionKind
    lane: int
    timestamp: float
    contact: int | None = None
    reason: str = ""
    track_id: int | None = None
    target_x: int | None = None
