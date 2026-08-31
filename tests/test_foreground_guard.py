from types import SimpleNamespace

from agent import foreground_guard


class UnsupportedShellController:
    info = {
        "adb_path": "C:/tools/adb.exe",
        "adb_serial": "emulator-7554",
    }

    def post_shell(self, _command, _timeout=20000):
        raise RuntimeError("Agent controller proxy does not support shell")


def test_foreground_query_falls_back_to_bound_adb_when_agent_shell_is_unsupported(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            stdout="mCurrentFocus=Window{123 u0 com.bilibili.star.bili/.MainActivity}"
        )

    monkeypatch.setattr(foreground_guard.subprocess, "run", run)

    assert foreground_guard.foreground_package(UnsupportedShellController()) == foreground_guard.GAME_PACKAGE
    assert calls[0][0] == [
        "C:/tools/adb.exe",
        "-s",
        "emulator-7554",
        "shell",
        "dumpsys",
        "window",
        "windows",
    ]
    assert calls[0][1]["timeout"] == 5


def test_foreground_query_retries_when_mumu_window_windows_has_no_focus_marker(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-2:] == ["window", "windows"]:
            return SimpleNamespace(
                stdout=(
                    "Window #0 Window{123 u0 ScreenDecorOverlay}:\n"
                    "Window #1 Window{456 u0 com.bilibili.star.bili/"
                    "com.bilibili.star.bili.MainActivity}:"
                )
            )
        return SimpleNamespace(
            stdout=(
                "mCurrentFocus=Window{456 u0 com.bilibili.star.bili/"
                "com.bilibili.star.bili.MainActivity}"
            )
        )

    monkeypatch.setattr(foreground_guard.subprocess, "run", run)

    assert (
        foreground_guard.foreground_package(UnsupportedShellController())
        == foreground_guard.GAME_PACKAGE
    )
    assert [call[0][-3:] for call in calls] == [
        ["dumpsys", "window", "windows"],
        ["shell", "dumpsys", "window"],
    ]
