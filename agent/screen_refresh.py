from __future__ import annotations

from typing import Any

from maa.context import Context


class ScreenRefreshCancelled(RuntimeError):
    pass


def capture_image(context: Context) -> Any:
    """Ask MaaFramework to refresh once, then read the cached screenshot.

    Agent callbacks must not call ``post_screencap`` directly on this runtime:
    that reverse controller call can remain unresolved and prevent task stop.
    The dedicated pipeline node has no ``max_hit``, so nested runs cannot
    consume a business node's retained hit counter.
    """

    if context.tasker.stopping:
        raise ScreenRefreshCancelled("task is stopping")
    detail = context.run_task("CommonRefreshScreen")
    if context.tasker.stopping:
        raise ScreenRefreshCancelled("task stopped during screen refresh")
    if not detail or not detail.status.succeeded:
        raise RuntimeError("CommonRefreshScreen did not complete")
    image = context.tasker.controller.cached_image
    if image is None or getattr(image, "size", 1) == 0:
        raise RuntimeError("CommonRefreshScreen returned an empty image")
    return image
