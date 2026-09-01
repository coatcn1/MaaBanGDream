from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import numpy as np


# The canonical game surface is 1280x720.  Use its literal bottom-right pixel
# so the animation-skip tap cannot overlap the home-page Live button (whose
# hit box reaches x=1265, y=710).  The previous inset point (1220, 690) sat
# inside that button and could accidentally start another navigation flow.
RESULT_ANIMATION_SKIP_POINT = (1279, 719)


class ResultNavigationStatus(str, Enum):
    IDENTIFIED = "identified"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ResultNavigationOutcome:
    status: ResultNavigationStatus
    image: np.ndarray | None = None
    page_state: str = "unknown"
    elapsed_seconds: float = 0.0
    back_attempts: int = 0
    reason: str | None = None


def _wait_unless_stopping(
    seconds: float,
    stopping: Callable[[], bool],
    *,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> bool:
    deadline = clock() + max(0.0, seconds)
    while clock() < deadline:
        if stopping():
            return False
        sleeper(min(0.1, deadline - clock()))
    return not stopping()


def click_result_surface(
    controller,
    *,
    before_input: Callable[[], None] = lambda: None,
    phase: str,
    log_prefix: str = "ResultNavigation",
) -> None:
    before_input()
    controller.post_click(*RESULT_ANIMATION_SKIP_POINT).wait()
    print(
        f"{log_prefix} action=animation-skip phase={phase} "
        f"point={RESULT_ANIMATION_SKIP_POINT[0]},"
        f"{RESULT_ANIMATION_SKIP_POINT[1]}",
        flush=True,
    )


def back_then_click(
    controller,
    *,
    before_input: Callable[[], None] = lambda: None,
    phase: str,
    log_prefix: str = "ResultNavigation",
) -> None:
    before_input()
    controller.post_click_key(4).wait()
    print(
        f"{log_prefix} action=back phase={phase} key=4",
        flush=True,
    )
    click_result_surface(
        controller,
        before_input=before_input,
        phase=f"{phase}-after-back",
        log_prefix=log_prefix,
    )


def accelerated_back(
    controller,
    *,
    before_input: Callable[[], None] = lambda: None,
    phase: str,
    log_prefix: str = "ResultNavigation",
) -> None:
    click_result_surface(
        controller,
        before_input=before_input,
        phase=f"{phase}-before-back",
        log_prefix=log_prefix,
    )
    back_then_click(
        controller,
        before_input=before_input,
        phase=phase,
        log_prefix=log_prefix,
    )


def navigate_result_pages(
    controller,
    stopping: Callable[[], bool],
    identify: Callable[[np.ndarray], str | None],
    *,
    before_input: Callable[[], None] = lambda: None,
    timeout_seconds: float = 180.0,
    settle_seconds: float = 0.15,
    retry_interval_seconds: float = 0.85,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    log_prefix: str = "ResultNavigation",
) -> ResultNavigationOutcome:
    """Run the shared result-page loop until a terminal identity is visible.

    The loop intentionally does not classify every intermediate reward, rank,
    score, loading, or network frame.  Those pages all support Android Back,
    so the robust transition is exactly:

        safe click -> recognise -> Back -> safe click -> recognise -> ...

    A Back sent while a page is still loading is harmless and the next cycle
    retries.  The loop is bounded by elapsed time rather than a small page- or
    Back-attempt limit.
    """
    started_at = clock()
    deadline = started_at + max(0.0, timeout_seconds)
    last_image: np.ndarray | None = None
    attempts = 0

    if stopping():
        return ResultNavigationOutcome(
            ResultNavigationStatus.STOPPED,
            elapsed_seconds=clock() - started_at,
            reason="user stopped before result navigation",
        )

    click_result_surface(
        controller,
        before_input=before_input,
        phase="entry",
        log_prefix=log_prefix,
    )

    while clock() < deadline:
        if not _wait_unless_stopping(
            settle_seconds,
            stopping,
            clock=clock,
            sleeper=sleeper,
        ):
            return ResultNavigationOutcome(
                ResultNavigationStatus.STOPPED,
                image=last_image,
                elapsed_seconds=clock() - started_at,
                back_attempts=attempts,
                reason="user stopped during result navigation",
            )

        image = controller.post_screencap().wait().get()
        last_image = image
        page_state = identify(image)
        if page_state is not None:
            print(
                f"{log_prefix} state={page_state} action=identified "
                f"back_attempts={attempts}",
                flush=True,
            )
            return ResultNavigationOutcome(
                ResultNavigationStatus.IDENTIFIED,
                image=image,
                page_state=page_state,
                elapsed_seconds=clock() - started_at,
                back_attempts=attempts,
            )

        back_then_click(
            controller,
            before_input=before_input,
            phase="unidentified",
            log_prefix=log_prefix,
        )
        attempts += 1
        print(
            f"{log_prefix} state=unidentified action=retry "
            f"attempt={attempts}",
            flush=True,
        )
        if not _wait_unless_stopping(
            retry_interval_seconds,
            stopping,
            clock=clock,
            sleeper=sleeper,
        ):
            return ResultNavigationOutcome(
                ResultNavigationStatus.STOPPED,
                image=last_image,
                elapsed_seconds=clock() - started_at,
                back_attempts=attempts,
                reason="user stopped during result navigation",
            )

    return ResultNavigationOutcome(
        ResultNavigationStatus.TIMED_OUT,
        image=last_image,
        elapsed_seconds=clock() - started_at,
        back_attempts=attempts,
        reason=(
            f"result terminal page was not identified within "
            f"{timeout_seconds:.1f}s after {attempts} Back attempts"
        ),
    )
