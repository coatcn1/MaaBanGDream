from __future__ import annotations

from collections.abc import Callable
import math
import time
from typing import Protocol

from .touch_planner import ActionKind, TouchAction


class _Job(Protocol):
    def wait(self) -> "_Job": ...


class _Controller(Protocol):
    def post_touch_down(self, x: int, y: int, contact: int = 0, pressure: int = 1) -> _Job: ...
    def post_touch_move(self, x: int, y: int, contact: int = 0, pressure: int = 1) -> _Job: ...
    def post_touch_up(self, contact: int = 0) -> _Job: ...


class ControllerTouchDispatcher:
    """Dispatch planner output through MaaFramework's native multi-touch API."""

    LANE_CENTERS = (190, 340, 490, 640, 790, 940, 1090)

    def __init__(
        self,
        controller: _Controller,
        stopping: Callable[[], bool],
        *,
        sleeper: Callable[[float], None] = time.sleep,
        maximum_move_step: int = 80,
    ) -> None:
        self.controller = controller
        self.stopping = stopping
        self.sleeper = sleeper
        self.maximum_move_step = max(20, int(maximum_move_step))
        self.active_contacts: set[int] = set()
        self.active_positions: dict[int, int] = {}

    def _ensure_running(self) -> None:
        if self.stopping():
            self.reset()
            raise InterruptedError("任务正在停止，已释放全部触点")

    def _x(self, action: TouchAction) -> int:
        if action.target_x is None:
            return self.LANE_CENTERS[action.lane]
        return max(120, min(1160, int(action.target_x)))

    def _down(self, action: TouchAction, contact: int) -> None:
        self._ensure_running()
        x = self._x(action)
        self.controller.post_touch_down(
            x, 590, contact, 50
        ).wait()
        self.active_contacts.add(contact)
        self.active_positions[contact] = x

    def dispatch(self, actions: list[TouchAction]) -> None:
        persistent = [action for action in actions if action.kind == ActionKind.DOWN]
        moves = [action for action in actions if action.kind == ActionKind.MOVE]
        transients = [
            action for action in actions if action.kind in (ActionKind.TAP, ActionKind.FLICK)
        ]
        reserved = set(self.active_contacts)
        reserved.update(0 if action.contact is None else action.contact for action in persistent)
        available = [contact for contact in range(10) if contact not in reserved]
        if len(transients) > len(available):
            raise RuntimeError("MaaFramework 可用触点不足")
        transient_contacts = list(zip(transients, available))
        try:
            for action in persistent:
                self._down(action, 0 if action.contact is None else action.contact)
            for action, contact in transient_contacts:
                self._down(action, contact)
            for action in moves:
                self._ensure_running()
                contact = 0 if action.contact is None else action.contact
                if contact not in self.active_contacts:
                    raise RuntimeError(
                        f"cannot move inactive touch contact {contact}"
                    )
                target_x = self._x(action)
                previous_x = self.active_positions.get(contact, target_x)
                steps = max(
                    1,
                    math.ceil(abs(target_x - previous_x) / self.maximum_move_step),
                )
                for step in range(1, steps + 1):
                    interpolated_x = round(
                        previous_x + (target_x - previous_x) * step / steps
                    )
                    self.controller.post_touch_move(
                        interpolated_x, 590, contact, 50
                    ).wait()
                self.active_positions[contact] = target_x
            for action in actions:
                if action.kind != ActionKind.UP:
                    continue
                self._ensure_running()
                contact = 0 if action.contact is None else action.contact
                self.controller.post_touch_up(contact).wait()
                self.active_contacts.discard(contact)
                self.active_positions.pop(contact, None)
            flick_contacts = [
                (action, contact) for action, contact in transient_contacts
                if action.kind == ActionKind.FLICK
            ]
            # A zero-duration Down/Move/Up sequence can collapse into a tap at
            # the game's input sampling boundary. Move all members of a flick
            # chord together over three short phases so at least two game
            # frames observe a genuine upward gesture.
            for y in (545, 490, 455):
                if not flick_contacts:
                    break
                self.sleeper(.012)
                for action, contact in flick_contacts:
                    self._ensure_running()
                    self.controller.post_touch_move(
                        self.LANE_CENTERS[action.lane], y, contact, 50
                    ).wait()
            for _, contact in transient_contacts:
                self._ensure_running()
                self.controller.post_touch_up(contact).wait()
                self.active_contacts.discard(contact)
                self.active_positions.pop(contact, None)
        except BaseException:
            self.reset()
            raise

    def reset(self) -> None:
        for contact in sorted(self.active_contacts):
            try:
                self.controller.post_touch_up(contact).wait()
            except Exception:
                pass
        self.active_contacts.clear()
        self.active_positions.clear()

    def close(self) -> None:
        self.reset()
