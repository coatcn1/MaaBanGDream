from __future__ import annotations

from typing import BinaryIO

from .touch_planner import ActionKind, TouchAction


class TouchProtocolError(RuntimeError):
    pass


class MaaTouchProtocol:
    """Encode planned actions for an already-connected MaaTouch stream."""

    LANE_CENTERS = (190, 340, 490, 640, 790, 940, 1090)

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.active_contacts: set[int] = set()

    def _send(self, lines: list[str]) -> None:
        if not lines:
            return
        try:
            self.stream.write(("\n".join(lines) + "\n").encode("ascii"))
            self.stream.flush()
        except (OSError, ValueError) as exc:
            raise TouchProtocolError(f"MaaTouch 发送失败: {exc}") from exc

    def _x(self, action: TouchAction) -> int:
        if action.target_x is None:
            return self.LANE_CENTERS[action.lane]
        return max(120, min(1160, round(action.target_x)))

    def dispatch(self, actions: list[TouchAction]) -> None:
        persistent_down = [a for a in actions if a.kind == ActionKind.DOWN]
        transients = [a for a in actions if a.kind in (ActionKind.TAP, ActionKind.FLICK)]
        reserved = set(self.active_contacts)
        reserved.update(0 if a.contact is None else a.contact for a in persistent_down)
        available = [contact for contact in range(10) if contact not in reserved]
        if len(transients) > len(available):
            raise TouchProtocolError("MaaTouch 可用触点不足")
        transient_contacts = list(zip(transients, available))

        downs: list[str] = []
        for action in persistent_down:
            contact = 0 if action.contact is None else action.contact
            downs.append(f"d {contact} {self._x(action)} 590 50")
            self.active_contacts.add(contact)
        downs.extend(
            f"d {contact} {self._x(action)} 590 50"
            for action, contact in transient_contacts
        )
        persistent_ups: list[str] = []
        for action in actions:
            if action.kind != ActionKind.UP:
                continue
            contact = 0 if action.contact is None else action.contact
            persistent_ups.append(f"u {contact}")
            self.active_contacts.discard(contact)
        if downs or persistent_ups:
            self._send([*downs, *persistent_ups, "c"])

        moves = [
            f"m {0 if a.contact is None else a.contact} {self._x(a)} 590 50"
            for a in actions
            if a.kind == ActionKind.MOVE
        ]
        moves.extend(
            (
                f"m {contact} "
                f"{max(120, min(1160, self._x(action) + (-150 if action.flick_direction == 'Left' else 150)))} "
                "590 50"
                if action.flick_direction in {"Left", "Right"}
                else f"m {contact} {self._x(action)} 490 50"
            )
            for action, contact in transient_contacts
            if action.kind == ActionKind.FLICK
        )
        if moves:
            self._send([*moves, "c"])
        if transient_contacts:
            self._send([*(f"u {contact}" for _, contact in transient_contacts), "c"])

    def reset(self) -> None:
        if self.active_contacts:
            self._send([*(f"u {c}" for c in sorted(self.active_contacts)), "c"])
        self._send(["r", "c"])
        self.active_contacts.clear()

    def close(self) -> None:
        self.reset()
