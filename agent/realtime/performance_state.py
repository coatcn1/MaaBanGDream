"""向开发界面提供演奏器占用区间，不参与演奏调度。"""

from contextlib import contextmanager
import json
import uuid


PREFIX = "MBD_PERFORMANCE_STATE "


def _emit(task_id: int, run_token: str, active: bool) -> None:
    try:
        print(PREFIX + json.dumps({
            "version": 1, "task_id": task_id, "run_token": run_token,
            "active": active,
        }, separators=(",", ":")), flush=True)
    except Exception:
        # 状态通知是附加功能，坏管道不能打断输入或覆盖原异常。
        pass


@contextmanager
def performance_window(argv):
    try:
        task_id = getattr(getattr(argv, "task_detail", None), "task_id", None)
        valid = not isinstance(task_id, bool) and isinstance(task_id, int) and task_id > 0
        run_token = uuid.uuid4().hex if valid else None
    except Exception:
        valid = False
    if not valid:
        yield
        return
    _emit(task_id, run_token, True)
    try:
        yield
    finally:
        _emit(task_id, run_token, False)
