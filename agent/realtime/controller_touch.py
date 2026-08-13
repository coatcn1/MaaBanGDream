from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass
class _PendingFlick:
    lane: int
    started_at: float
    next_phase: int = 0


class ControllerTouchDispatcher:
    """Dispatch planner output through MaaFramework's native multi-touch API."""

    LANE_CENTERS = (190, 340, 490, 640, 790, 940, 1090)

    def __init__(
        self,
        controller: _Controller,
        stopping: Callable[[], bool],
        *,
        sleeper: Callable[[float], None] = time.sleep,
        maximum_move_step: int = 160,
    ) -> None:
        self.controller = controller
        self.stopping = stopping
        self.sleeper = sleeper
        self.maximum_move_step = max(20, int(maximum_move_step))
        self.active_contacts: set[int] = set()
        self.active_positions: dict[int, int] = {}
        self._pending_flicks: dict[int, _PendingFlick] = {}
        self._contact_alias: dict[int, int] = {}
        self._last_used: dict[int, float] = {}
        self._last_released: dict[int, float] = {}
        self.recovered_contacts = 0
        self.down_recoveries = 0
        self.stale_move_recoveries = 0
        self.wait_seconds_total = 0.0
        self.wait_count = 0
        self.wait_max_seconds = 0.0
        self._flick_phases = ((.012, 545), (.024, 490), (.036, 455))
        self._flick_release_after = .048

    def _ensure_running(self) -> None:
        if self.stopping():
            self.reset()
            raise InterruptedError("任务正在停止，已释放全部触点")

    def _x(self, action: TouchAction) -> int:
        if action.target_x is None:
            return self.LANE_CENTERS[action.lane]
        return max(120, min(1160, int(action.target_x)))

    def _actual(self, planned: int) -> int:
        return self._contact_alias.get(planned, planned)

    def _pick_fallback_contact(self, planned: int) -> int:
        alias_targets = set(self._contact_alias.values())
        for contact in range(7, 10):
            if contact not in self.active_contacts and contact not in alias_targets:
                return contact
        for contact in range(10):
            if (
                contact not in self.active_contacts
                and contact not in alias_targets
                and contact != planned
            ):
                return contact
        return planned

    def _release(self, planned: int) -> None:
        actual = self._contact_alias.pop(planned, planned)
        self._wait(self.controller.post_touch_up(actual))
        self.active_contacts.discard(actual)
        self.active_positions.pop(actual, None)
        self._pending_flicks.pop(actual, None)
        self._last_released[planned] = time.monotonic()

    @staticmethod
    def _is_active_contact_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return "contact" in message and "already active" in message

    def _down(self, action: TouchAction, planned: int) -> None:
        self._ensure_running()
        actual = self._actual(planned)
        if actual in self.active_contacts:
            self._release(planned)
            self.recovered_contacts += 1
            self.down_recoveries += 1
            # The game's input thread may not have consumed the UP yet; a
            # re-press within the same millisecond can be coalesced and leave
            # the contact stuck again.  Yield briefly so the UP lands before
            # the DOWN.  This is the exceptional desync-recovery path, not
            # the normal hot path.
            self.sleeper(0.015)
            # Use a fresh contact so the stale backend state for the old
            # contact cannot swallow this press.
            actual = self._pick_fallback_contact(planned)
            self._contact_alias[planned] = actual
        elif (
            planned in self._last_released
            and time.monotonic() - self._last_released[planned] >= 0.02
            and time.monotonic() - self._last_released[planned] < 0.5
        ):
            # The backend can still consider a recently released contact
            # active even though our bookkeeping cleared it.  MTouch accepts
            # UP for an inactive contact, so proactively clear the stale
            # backend state before the DOWN (no recovery accounting needed).
            self._wait(self.controller.post_touch_up(actual))
        x = self._x(action)
        try:
            self._wait(self.controller.post_touch_down(x, 590, actual, 50))
        except Exception as exc:
            if not self._is_active_contact_error(exc):
                raise
            self._release(planned)
            self.recovered_contacts += 1
            self.down_recoveries += 1
            actual = self._pick_fallback_contact(planned)
            self._contact_alias[planned] = actual
            self._wait(self.controller.post_touch_down(x, 590, actual, 50))
        self.active_contacts.add(actual)
        self.active_positions[actual] = x
        self._last_used[planned] = float(action.timestamp)

    def _wait(self, job: _Job) -> None:
        started = time.perf_counter()
        job.wait()
        elapsed = time.perf_counter() - started
        self.wait_seconds_total += elapsed
        self.wait_count += 1
        self.wait_max_seconds = max(self.wait_max_seconds, elapsed)

    def synchronize(self) -> None:
        """Release contacts owned by this dispatcher without desynchronizing MTouch.

        MaaFramework's MTouch backend accepts UP for an inactive contact, but a
        burst of such synthetic releases can leave the device-side gesture
        stream unable to register the next song's taps.  External stale
        contacts are therefore recovered lazily by ``_down`` when the backend
        reports ``already active``.
        """
        for contact in sorted(self.active_contacts):
            try:
                self._wait(self.controller.post_touch_up(contact))
            except Exception:
                pass
        self.active_contacts.clear()
        self.active_positions.clear()
        self._pending_flicks.clear()
        self._contact_alias.clear()
        self._last_used.clear()
        self._last_released.clear()

    def advance(self, now: float) -> None:
        """Advance pending flick gestures without sleeping in the capture loop."""
        self._ensure_running()
        for contact, pending in list(self._pending_flicks.items()):
            elapsed = float(now) - pending.started_at
            while (
                pending.next_phase < len(self._flick_phases)
                and elapsed >= self._flick_phases[pending.next_phase][0]
            ):
                _, y = self._flick_phases[pending.next_phase]
                self._wait(self.controller.post_touch_move(
                    self.LANE_CENTERS[pending.lane],
                    y,
                    contact,
                    50,
                ))
                pending.next_phase += 1
            if elapsed < self._flick_release_after:
                continue
            self._wait(self.controller.post_touch_up(contact))
            self.active_contacts.discard(contact)
            self.active_positions.pop(contact, None)
            self._pending_flicks.pop(contact, None)

    def dispatch(self, actions: list[TouchAction]) -> None:
        persistent = [action for action in actions if action.kind == ActionKind.DOWN]
        moves = [action for action in actions if action.kind == ActionKind.MOVE]
        persistent_contacts = {
            0 if action.contact is None else action.contact
            for action in persistent
        }
        # A hold can end and a new hold can acquire the same stable contact in
        # one planner frame. Preserve that causal order instead of grouping
        # every DOWN ahead of every UP, which would try to press an active
        # MaaFramework contact and abort the song.
        pre_releases = [
            action
            for action in actions
            if action.kind == ActionKind.UP
            and (0 if action.contact is None else action.contact)
            in persistent_contacts
        ]
        deferred_releases = [
            action
            for action in actions
            if action.kind == ActionKind.UP and action not in pre_releases
        ]
        transients = [
            action for action in actions if action.kind in (ActionKind.TAP, ActionKind.FLICK)
        ]
        # Hold-to-flick conversions keep their held contact: the finger
        # swipes up via advance() instead of lifting and re-pressing.
        conversions = [
            action for action in transients
            if action.kind == ActionKind.FLICK
            and action.contact is not None
            and self._actual(action.contact) in self.active_contacts
        ]
        if conversions:
            conversion_ids = {id(action) for action in conversions}
            transients = [
                action for action in transients if id(action) not in conversion_ids
            ]
        reserved = set(self.active_contacts)
        reserved.update(
            self._actual(0 if action.contact is None else action.contact)
            for action in persistent
        )
        available = [contact for contact in range(10) if contact not in reserved]
        if len(transients) > len(available):
            raise RuntimeError("MaaFramework 可用触点不足")
        transient_contacts = list(zip(transients, available))
        try:
            for action in pre_releases:
                self._ensure_running()
                contact = 0 if action.contact is None else action.contact
                actual = self._actual(contact)
                if actual in self.active_contacts:
                    self._release(contact)
            for action in persistent:
                self._down(action, 0 if action.contact is None else action.contact)
            for action, contact in transient_contacts:
                self._down(action, contact)
            for action in moves:
                self._ensure_running()
                contact = 0 if action.contact is None else action.contact
                actual = self._actual(contact)
                if actual not in self.active_contacts:
                    # A hold release can race with the planner's MOVE in the
                    # same frame, or the backend can drop a contact after a
                    # long hold. A stale MOVE is not a song failure: drop the
                    # state and let the next DOWN re-press.
                    self.recovered_contacts += 1
                    self.stale_move_recoveries += 1
                    self._contact_alias.pop(contact, None)
                    self.active_positions.pop(actual, None)
                    self._pending_flicks.pop(actual, None)
                    continue
                target_x = self._x(action)
                previous_x = self.active_positions.get(actual, target_x)
                steps = max(
                    1,
                    math.ceil(abs(target_x - previous_x) / self.maximum_move_step),
                )
                for step in range(1, steps + 1):
                    interpolated_x = round(
                        previous_x + (target_x - previous_x) * step / steps
                    )
                    self.controller.post_touch_move(
                        interpolated_x, 590, actual, 50
                    )
                self.active_positions[actual] = target_x
            for action in deferred_releases:
                self._ensure_running()
                contact = 0 if action.contact is None else action.contact
                self._release(contact)
            for action, contact in transient_contacts:
                if action.kind == ActionKind.FLICK:
                    self._pending_flicks[contact] = _PendingFlick(
                        action.lane,
                        float(action.timestamp),
                    )
                    continue
                self._ensure_running()
                self._wait(self.controller.post_touch_up(contact))
                self.active_contacts.discard(contact)
                self.active_positions.pop(contact, None)
            for action in conversions:
                self._pending_flicks[self._actual(action.contact)] = _PendingFlick(
                    action.lane,
                    float(action.timestamp),
                )
        except BaseException:
            self.reset()
            raise

    def reset(self) -> None:
        self.synchronize()

    def close(self) -> None:
        self.synchronize()
