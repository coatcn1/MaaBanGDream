"""隔离附加诊断；每个入口失败一次后禁用，避免热路径重复报错。"""

from contextlib import contextmanager


def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except Exception:
        # stdout 断开也是附加日志故障，不能反向终止演出或清理。
        pass


def warning(message):
    safe_print(message, flush=True)


def safe_traceback():
    import traceback
    try:
        traceback.print_exc()
    except Exception:
        pass


class OptionalDiagnostics:
    def __init__(self, stopping=lambda: False):
        self.stopping = stopping
        self.disabled = set()

    @contextmanager
    def guard(self, key, *, during_cleanup=False):
        if key in self.disabled or (not during_cleanup and self.stopping()):
            yield False
            return
        try:
            yield True
        except Exception as exc:
            if isinstance(exc, InterruptedError) and self.stopping() and not during_cleanup:
                raise
            self.disabled.add(key)
            if not self.stopping():
                warning(f"RealtimeDiagnostics warning={key} error={type(exc).__name__}: {exc}")

    def call(self, key, callback, *, default=None, during_cleanup=False):
        with self.guard(key, during_cleanup=during_cleanup) as enabled:
            if enabled:
                return callback()
        return default
