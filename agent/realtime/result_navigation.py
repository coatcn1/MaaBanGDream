from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import numpy as np


# 标准游戏画面是 1280x720。固定使用字面意义上的最右下角像素，避免动画
# 加速点击落入主页“演出”按钮（其命中区域延伸到 x=1265, y=710）。旧坐标
# (1220, 690) 位于按钮内部，可能意外启动新的导航流程。
RESULT_ANIMATION_SKIP_POINT = (1279, 719)

STORY_NODES = (
    "AutoLiveStorySkipConfirmLarge", "AutoLiveStorySkipConfirm",
    "AutoLiveStorySkip", "AutoLiveStoryMenu",
)


def _current_controller(controller):
    """在每次输入前解析当前反向控制器，避免复用已释放的代理句柄。"""
    return controller() if callable(controller) else controller


def handle_story_page(image, *, recognise, click, stopping) -> bool:
    """只点击已识别的剧情控件；每次输入后交还外层重新截图。"""
    for node in STORY_NODES:
        if stopping():
            return False
        box = recognise(image, node)
        if box is None:
            continue
        if stopping():
            return False
        click((int(box.x + box.w // 2), int(box.y + box.h // 2)))
        print(f"ResultNavigation state=story action={node}", flush=True)
        return True
    return False


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
    while True:
        # 先取剩余时间再检查停止：stopping 的原生调用可能阻塞到越过
        # deadline，直接 sleep 负值会抛 ValueError 打断整个结算流程。
        remaining = deadline - clock()
        if remaining <= 0:
            break
        if stopping():
            return False
        sleeper(min(0.1, remaining))
    return not stopping()


def click_result_surface(
    controller,
    *,
    before_input: Callable[[], None] = lambda: None,
    phase: str,
    log_prefix: str = "ResultNavigation",
) -> None:
    before_input()
    _current_controller(controller).post_click(
        *RESULT_ANIMATION_SKIP_POINT
    ).wait()
    print(
        f"{log_prefix} action=animation-skip phase={phase} "
        f"point={RESULT_ANIMATION_SKIP_POINT[0]},"
        f"{RESULT_ANIMATION_SKIP_POINT[1]}",
        flush=True,
    )


def press_result_back(
    controller,
    *,
    before_input: Callable[[], None] = lambda: None,
    phase: str,
    log_prefix: str = "ResultNavigation",
) -> None:
    before_input()
    _current_controller(controller).post_click_key(4).wait()
    print(
        f"{log_prefix} action=back phase={phase} key=4",
        flush=True,
    )


def advance_result_cadence(
    controller,
    *,
    back_next: bool,
    before_input: Callable[[], None] = lambda: None,
    phase: str,
    log_prefix: str = "ResultNavigation",
) -> bool:
    """推进一次共享结算节拍，并返回下一步是否应发送 BACK。"""
    if back_next:
        press_result_back(
            controller,
            before_input=before_input,
            phase=phase,
            log_prefix=log_prefix,
        )
    else:
        click_result_surface(
            controller,
            before_input=before_input,
            phase=phase,
            log_prefix=log_prefix,
        )
    return not back_next


def back_then_click(
    controller,
    *,
    before_input: Callable[[], None] = lambda: None,
    phase: str,
    log_prefix: str = "ResultNavigation",
) -> None:
    press_result_back(
        controller,
        before_input=before_input,
        phase=phase,
        log_prefix=log_prefix,
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
    handle_intermediate: Callable[[np.ndarray], bool] = lambda _image: False,
    timeout_seconds: float = 180.0,
    settle_seconds: float = 0.15,
    retry_interval_seconds: float = 0.85,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    log_prefix: str = "ResultNavigation",
) -> ResultNavigationOutcome:
    """用共享结算节拍循环，直到识别到所需终点。

    中间的奖励、排名、分数、加载或网络画面一律不分类。输入序列持续为
    “安全像素 → BACK → 安全像素”；识别只发生在输入之间，不会把任何中间页
    的可见按钮当作推进目标。加载期间提前收到 BACK 也可由下一轮自然重试，
    整体按时间有界而不是按少量页面或 BACK 次数退出。
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

        image = _current_controller(controller).post_screencap().wait().get()
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

        if stopping():
            continue
        if handle_intermediate(image):
            # 剧情确认框不支持通用 BACK；点击后只重新采样，防止取消跳过。
            continue
        if stopping():
            continue
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
