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
    start_x: int
    direction: str | None = None
    next_phase: int = 0


@dataclass
class _PendingTap:
    started_at: float


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
        self._pending_taps: dict[int, _PendingTap] = {}
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
        self._tap_release_after = .024

    def trace_state(self) -> dict[str, object]:
        """Return a cheap immutable snapshot of submitted physical contacts.

        Planner actions use stable logical contact ids, while MTouch may receive
        rotated physical ids.  Persisting both views lets trace-only sessions
        reveal an alias collision or a contact that remained active after UP.
        """
        return {
            "active_contacts": sorted(self.active_contacts),
            "active_positions": {
                str(contact): x
                for contact, x in sorted(self.active_positions.items())
            },
            "contact_aliases": {
                str(planned): actual
                for planned, actual in sorted(self._contact_alias.items())
            },
            "pending_taps": sorted(self._pending_taps),
            "pending_flicks": sorted(self._pending_flicks),
        }

    def _ensure_running(self) -> None:
        if self.stopping():
            self.emergency_release_all()
            raise InterruptedError("任务正在停止，已释放全部触点")

    def _x(self, action: TouchAction) -> int:
        if action.target_x is None:
            return self.LANE_CENTERS[action.lane]
        return max(120, min(1160, int(action.target_x)))

    def _actual(self, planned: int) -> int:
        return self._contact_alias.get(planned, planned)

    def _physical_contact_owned_by_other(
        self,
        actual: int,
        planned: int,
    ) -> bool:
        """Return whether ``actual`` belongs to a different live gesture.

        Logical hold ids and physical MTouch ids share the same 0-9 range.
        Once a hold rotates, another logical id can numerically equal that
        physical id.  Treating the number alone as ownership releases the
        first hold and leaves its alias pointing at an inactive contact.
        """
        if any(
            owner != planned and physical == actual
            for owner, physical in self._contact_alias.items()
        ):
            return True
        # Transient aliases are removed after DOWN; their physical ownership
        # is represented by the pending gesture maps until advance() lifts it.
        return actual in self._pending_taps or actual in self._pending_flicks

    def _pick_fallback_contact(
        self,
        planned: int,
        *,
        reserved_contacts: set[int] | None = None,
    ) -> int:
        reserved_contacts = reserved_contacts or set()
        alias_targets = set(self._contact_alias.values())
        for contact in range(7, 10):
            if (
                contact not in self.active_contacts
                and contact not in alias_targets
                and contact not in reserved_contacts
            ):
                return contact
        for contact in range(10):
            if (
                contact not in self.active_contacts
                and contact not in alias_targets
                and contact not in reserved_contacts
                and contact != planned
            ):
                return contact
        if (
            planned not in self.active_contacts
            and planned not in alias_targets
            and planned not in reserved_contacts
        ):
            return planned
        raise RuntimeError("MaaFramework 可用触点不足")

    def _pick_hold_contact(
        self,
        planned: int,
        *,
        reserved_contacts: set[int] | None = None,
    ) -> int:
        """Allocate a touch id for a hold, preferring one not recently used.

        Reusing a hold contact shortly after its release lets the backend's
        stale "active" state swallow the press.  Rotate through 0-9, skipping
        active contacts, alias targets and contacts released in the last 2 s.
        """
        reserved_contacts = reserved_contacts or set()
        now = time.monotonic()
        recent = {
            contact
            for contact, released_at in self._last_released.items()
            if now - released_at < 2.0
        }
        alias_targets = set(self._contact_alias.values())
        first_choices = (
            [planned]
            + [contact for contact in range(7) if contact != planned]
            + [7, 8, 9]
        )
        for contact in first_choices:
            if (
                contact not in self.active_contacts
                and contact not in alias_targets
                and contact not in reserved_contacts
                and contact not in recent
            ):
                return contact
        # Fall back to the least recently released free contact.
        best: int | None = None
        best_released = float("inf")
        for contact in range(10):
            if (
                contact in self.active_contacts
                or contact in alias_targets
                or contact in reserved_contacts
            ):
                continue
            released_at = self._last_released.get(contact, float("-inf"))
            if released_at < best_released:
                best = contact
                best_released = released_at
        if best is None:
            raise RuntimeError("MaaFramework 可用触点不足")
        return best

    def _release(self, planned: int, *, wait_for_job: bool = True) -> None:
        aliased_actual = self._contact_alias.pop(planned, None)
        actual = planned if aliased_actual is None else aliased_actual
        if self._physical_contact_owned_by_other(actual, planned):
            # A stale logical UP must never lift another hold merely because
            # its id equals that hold's rotated physical contact.
            return
        job = self.controller.post_touch_up(actual)
        if wait_for_job:
            self._wait(job)
        self.active_contacts.discard(actual)
        self.active_positions.pop(actual, None)
        self._pending_taps.pop(actual, None)
        self._pending_flicks.pop(actual, None)
        # Cool down the physical id that MTouch actually received.  When a
        # planned lane id was aliased, cooling the planned id allowed the
        # just-released physical id to be reused immediately while its UP was
        # still queued, intermittently swallowing a dense-chart DOWN.
        self._last_released[actual] = time.monotonic()

    @staticmethod
    def _is_active_contact_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return "contact" in message and "already active" in message

    def _down(
        self,
        action: TouchAction,
        planned: int,
        *,
        wait_for_job: bool = True,
        reserved_contacts: set[int] | None = None,
    ) -> None:
        self._ensure_running()
        owned_actual = self._contact_alias.get(planned)
        stale_actual = planned if owned_actual is None else owned_actual
        if (
            stale_actual in self.active_contacts
            and not self._physical_contact_owned_by_other(
                stale_actual,
                planned,
            )
        ):
            self._release(planned, wait_for_job=wait_for_job)
            self.recovered_contacts += 1
            self.down_recoveries += 1
            # The game's input thread may not have consumed the UP yet; a
            # re-press within the same millisecond can be coalesced and leave
            # the contact stuck again.  Yield briefly so the UP lands before
            # the DOWN.  This is the exceptional desync-recovery path, not
            # the normal hot path.
            if wait_for_job:
                self.sleeper(0.015)
        actual = self._pick_hold_contact(
            planned,
            reserved_contacts=reserved_contacts,
        )
        self._contact_alias[planned] = actual
        if (
            actual in self._last_released
            and time.monotonic() - self._last_released[actual] >= 0.02
            and time.monotonic() - self._last_released[actual] < 0.5
        ):
            # Fallback allocation had to reuse a recently released contact;
            # proactively clear the backend state before the DOWN.
            clear_job = self.controller.post_touch_up(actual)
            if wait_for_job:
                self._wait(clear_job)
        x = self._x(action)
        try:
            job = self.controller.post_touch_down(x, 590, actual, 1)
            if wait_for_job:
                self._wait(job)
        except Exception as exc:
            if not self._is_active_contact_error(exc):
                raise
            self._release(planned, wait_for_job=wait_for_job)
            self.recovered_contacts += 1
            self.down_recoveries += 1
            actual = self._pick_fallback_contact(
                planned,
                reserved_contacts=reserved_contacts,
            )
            self._contact_alias[planned] = actual
            retry_job = self.controller.post_touch_down(x, 590, actual, 1)
            if wait_for_job:
                self._wait(retry_job)
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
        self._pending_taps.clear()
        self._pending_flicks.clear()
        self._contact_alias.clear()
        self._last_used.clear()
        self._last_released.clear()

    def force_release_all(self) -> None:
        """Release every touch id to clear silently stuck backend state.

        The emulator/game input stream can stop accepting presses even though
        MaaFramework reports every DOWN as successful: a pointer stuck inside
        the game blocks all later touches, so taps disappear as MISS with no
        tap effect.  Posting UP for all ten ids clears that state; MTouch
        accepts UP for an inactive contact, so this is safe between notes.
        """
        for contact in range(10):
            try:
                self._wait(self.controller.post_touch_up(contact))
            except Exception:
                pass
        self.active_contacts.clear()
        self.active_positions.clear()
        self._pending_taps.clear()
        self._pending_flicks.clear()
        self._contact_alias.clear()
        self._last_used.clear()
        self._last_released.clear()

    def emergency_release_all(self) -> None:
        """Post an all-contact release without waiting in the hot path.

        A severe life drop can mean that the device-side touch stream is
        stuck even though MaaFramework accepted every command.  Queueing UP
        for all ids can recover that stream, but synchronously waiting for
        ten jobs stalls capture and skips the very notes this safeguard is
        meant to protect.  Song-boundary cleanup still uses the synchronous
        ``synchronize``/``force_release_all`` paths.
        """
        for contact in range(10):
            try:
                self.controller.post_touch_up(contact)
            except Exception:
                pass
        self.active_contacts.clear()
        self.active_positions.clear()
        self._pending_taps.clear()
        self._pending_flicks.clear()
        self._contact_alias.clear()
        self._last_used.clear()
        self._last_released.clear()

    def advance(self, now: float) -> None:
        """Advance pending flick gestures without sleeping in the capture loop."""
        self._ensure_running()
        for contact, pending in list(self._pending_taps.items()):
            if float(now) - pending.started_at < self._tap_release_after:
                continue
            self.controller.post_touch_up(contact)
            self.active_contacts.discard(contact)
            self.active_positions.pop(contact, None)
            self._pending_taps.pop(contact, None)
            self._last_released[contact] = time.monotonic()
            self._contact_alias = {
                planned: actual
                for planned, actual in self._contact_alias.items()
                if actual != contact
            }
        for contact, pending in list(self._pending_flicks.items()):
            elapsed = float(now) - pending.started_at
            while (
                pending.next_phase < len(self._flick_phases)
                and elapsed >= self._flick_phases[pending.next_phase][0]
            ):
                _phase_time, y = self._flick_phases[pending.next_phase]
                # MaaFramework preserves the post order.  Waiting here can
                # block capture for 150-250 ms when the emulator input thread
                # hiccups, which skips an entire dense phrase.  Hold MOVE is
                # already queued the same way in dispatch().
                if pending.direction in {"Left", "Right"}:
                    sign = -1 if pending.direction == "Left" else 1
                    distance = (55, 105, 150)[pending.next_phase]
                    x = max(120, min(1160, pending.start_x + sign * distance))
                    move_y = 590
                else:
                    x = pending.start_x
                    move_y = y
                self.controller.post_touch_move(
                    x,
                    move_y,
                    contact,
                    1,
                )
                pending.next_phase += 1
            if elapsed < self._flick_release_after:
                continue
            # The contact cooldown prevents immediate reuse, so the release
            # can remain asynchronous without racing the next DOWN.
            self.controller.post_touch_up(contact)
            self.active_contacts.discard(contact)
            self.active_positions.pop(contact, None)
            self._pending_flicks.pop(contact, None)
            self._last_released[contact] = time.monotonic()
            # A hold converted into a flick keeps the planned alias until the
            # swipe completes; the release above frees the actual contact, so
            # forget every planned id that pointed at it.
            self._contact_alias = {
                planned: actual
                for planned, actual in self._contact_alias.items()
                if actual != contact
            }

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
        # Prefer the high contacts (7-9) for transient taps so a tap never
        # occupies a lane contact (0-6) that the next hold on that lane will
        # immediately re-press; reusing a just-released tap contact is what
        # makes the backend report "already active".
        transient_order = [7, 8, 9, 0, 1, 2, 3, 4, 5, 6]
        available = [
            contact for contact in transient_order if contact not in reserved
        ]
        if len(transients) > len(available):
            raise RuntimeError("MaaFramework 可用触点不足")
        transient_contacts = list(zip(transients, available))
        # Reserve every logical contact in this frame before dispatching any
        # DOWN.  Contact rotation observes the cooldown map and may otherwise
        # steal an id that a later chord member is about to use.  That later
        # DOWN then interprets the stolen id as its own stale contact, posts an
        # UP, and silently removes the first chord member from the game while
        # leaving it in ``pending_taps``.
        batch_contacts = [
            *(0 if action.contact is None else action.contact for action in persistent),
            *(contact for _action, contact in transient_contacts),
        ]
        try:
            for action in pre_releases:
                self._ensure_running()
                contact = 0 if action.contact is None else action.contact
                actual = self._actual(contact)
                if actual in self.active_contacts:
                    self._release(contact, wait_for_job=False)
            for action in persistent:
                planned = 0 if action.contact is None else action.contact
                self._down(
                    action,
                    planned,
                    wait_for_job=False,
                    reserved_contacts={
                        contact for contact in batch_contacts if contact != planned
                    },
                )
            for action, contact in transient_contacts:
                # A transient is deliberately held across frames.  Posting
                # DOWN and immediately waiting for UP produced sub-millisecond
                # pulses that LDPlayer/BanG Dream intermittently discarded.
                # MaaFramework preserves command order, so keep the hot path
                # nonblocking and let advance() release the contact later.
                self._down(
                    action,
                    contact,
                    wait_for_job=False,
                    reserved_contacts={
                        candidate
                        for candidate in batch_contacts
                        if candidate != contact
                    },
                )
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
                        interpolated_x, 590, actual, 1
                    )
                self.active_positions[actual] = target_x
            for action in deferred_releases:
                self._ensure_running()
                contact = 0 if action.contact is None else action.contact
                self._release(contact, wait_for_job=False)
            for action, contact in transient_contacts:
                self._ensure_running()
                actual = self._contact_alias.pop(contact, contact)
                if action.kind == ActionKind.FLICK:
                    self._pending_flicks[actual] = _PendingFlick(
                        action.lane,
                        float(action.timestamp),
                        self.active_positions.get(actual, self._x(action)),
                        action.flick_direction,
                    )
                    continue
                self._pending_taps[actual] = _PendingTap(
                    float(action.timestamp),
                )
            for action in conversions:
                self._pending_flicks[self._actual(action.contact)] = _PendingFlick(
                    action.lane,
                    float(action.timestamp),
                    self.active_positions.get(
                        self._actual(action.contact), self._x(action)
                    ),
                    action.flick_direction,
                )
        except BaseException:
            self.emergency_release_all()
            raise

    def reset(self) -> None:
        self.synchronize()

    def close(self) -> None:
        self.synchronize()
