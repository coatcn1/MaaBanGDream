from __future__ import annotations

from collections.abc import Callable
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
    ) -> None:
        self.controller = controller
        self.stopping = stopping
        self.sleeper = sleeper
        self.active_contacts: set[int] = set()

    def _ensure_running(self) -> None:
        if self.stopping():
            self.reset()
            raise InterruptedError("任务正在停止，已释放全部触点")

    def _down(self, action: TouchAction, contact: int) -> None:
        self._ensure_running()
        self.controller.post_touch_down(
            self.LANE_CENTERS[action.lane], 590, contact, 50
        ).wait()
        self.active_contacts.add(contact)

    def dispatch(self, actions: list[TouchAction]) -> None:
        persistent = [action for action in actions if action.kind == ActionKind.DOWN]
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
            for action in actions:
                if action.kind != ActionKind.UP:
                    continue
                self._ensure_running()
                contact = 0 if action.contact is None else action.contact
                self.controller.post_touch_up(contact).wait()
                self.active_contacts.discard(contact)
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

    def close(self) -> None:
        self.reset()
