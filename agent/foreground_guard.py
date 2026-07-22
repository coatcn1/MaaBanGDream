from __future__ import annotations

import re
import subprocess
from typing import Protocol


GAME_PACKAGE = "com.bilibili.star.bili"
_FOCUS_RE = re.compile(r"(?:mCurrentFocus|mFocusedApp).*?\s([A-Za-z0-9_.]+)/(?:[A-Za-z0-9_.$]+)")


class _Job(Protocol):
    def wait(self) -> "_Job": ...
    def get(self) -> object: ...


class _Controller(Protocol):
    def post_shell(self, command: str, timeout: int = 20000) -> _Job: ...


class ForegroundAppMismatch(RuntimeError):
    pass


def foreground_package(controller: _Controller) -> str | None:
    try:
        output = controller.post_shell("dumpsys window windows", 5000).wait().get()
    except Exception:
        info = controller.info
        adb_path = str(info["adb_path"])
        adb_serial = str(info["adb_serial"])
        completed = subprocess.run(
            [
                adb_path,
                "-s",
                adb_serial,
                "shell",
                "dumpsys",
                "window",
                "windows",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = completed.stdout
    match = _FOCUS_RE.search(str(output or ""))
    return match.group(1) if match else None


def require_game_foreground(controller: _Controller, package: str = GAME_PACKAGE) -> None:
    actual = foreground_package(controller)
    if actual != package:
        raise ForegroundAppMismatch(
            f"unsafe controller input blocked: expected foreground={package}, actual={actual or 'unknown'}"
        )
