from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from agent.manual_flow_recording import (
    ManualFlowRecording,
    ManualFlowVideoWriter,
    RuntimeAdbDevice,
    TouchIndicatorGuard,
    _build_adb_shell,
    _load_runtime_adb_device,
    capture_manual_flow,
    configure_manual_flow_settings,
    current_manual_flow_settings,
)


ROOT = Path(__file__).parents[1]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeRecorder:
    def __init__(self) -> None:
        self.timestamps: list[float] = []

    def record(self, image: np.ndarray, timestamp: float) -> None:
        assert image.shape == (8, 16, 3)
        self.timestamps.append(timestamp)


def test_capture_manual_flow_is_paced_and_stops_neutrally() -> None:
    clock = FakeClock()
    recorder = FakeRecorder()

    reason = capture_manual_flow(
        lambda: np.zeros((8, 16, 3), dtype=np.uint8),
        lambda: len(recorder.timestamps) >= 3,
        recorder,  # type: ignore[arg-type]
        fps=10,
        max_duration_seconds=60,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert reason == "user_stop"
    assert recorder.timestamps == [0.0, 0.1, 0.2]


def test_manual_flow_video_is_seekable_and_has_timestamp_index(
    tmp_path: Path,
) -> None:
    recorder = ManualFlowVideoWriter(tmp_path, fps=10, label="Activity Test")
    for index in range(8):
        image = np.full((72, 128, 3), index * 20, dtype=np.uint8)
        recorder.record(image, 100.0 + index * 0.1)

    video_path = recorder.close(stop_reason="user_stop", touch_indicators=True)

    assert video_path.name == "screen.mkv"
    assert video_path.is_file()
    assert "activity-test" in video_path.parent.name
    summary = json.loads(
        (video_path.parent / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["kind"] == "manual-ui-flow"
    assert summary["stop_reason"] == "user_stop"
    assert summary["touch_indicators"] is True
    assert summary["frame_count"] == 8
    assert summary["video_seek_verified"] is True
    assert summary["width"] == 128
    assert summary["height"] == 72
    assert len(
        (video_path.parent / "frames.jsonl").read_text(encoding="utf-8").splitlines()
    ) == 8
    capture = cv2.VideoCapture(str(video_path))
    try:
        assert capture.isOpened()
        assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 8
    finally:
        capture.release()


class FakeShell:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...]) -> str:
        self.commands.append(command)
        return "0" if command == ("settings", "get", "system", "show_touches") else ""


class FakeScreenshotJob:
    def __init__(self, tasker: "FakeTasker") -> None:
        self.tasker = tasker

    def wait(self) -> "FakeScreenshotJob":
        return self

    def get(self) -> np.ndarray:
        self.tasker.captures += 1
        return np.full((72, 128, 3), self.tasker.captures, dtype=np.uint8)


class FakeRecordingController:
    def __init__(self, tasker: "FakeTasker") -> None:
        self.tasker = tasker

    def post_screencap(self) -> FakeScreenshotJob:
        return FakeScreenshotJob(self.tasker)


class FakeTasker:
    def __init__(self) -> None:
        self.captures = 0
        self.controller = FakeRecordingController(self)

    @property
    def stopping(self) -> bool:
        return self.captures >= 4


def test_touch_indicator_guard_restores_original_setting() -> None:
    shell = FakeShell()

    with TouchIndicatorGuard(shell, enabled=True) as guard:
        assert guard.active is True

    assert shell.commands == [
        ("settings", "get", "system", "show_touches"),
        ("settings", "put", "system", "show_touches", "1"),
        ("settings", "put", "system", "show_touches", "0"),
    ]


def test_runtime_adb_device_is_loaded_from_ignored_mfa_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    adb_path = tmp_path / "adb.exe"
    adb_path.touch()
    instance = tmp_path / "config" / "instances" / "default.json"
    instance.parent.mkdir(parents=True)
    instance.write_text(
        json.dumps(
            {
                "AdbDevice": {
                    "AdbPath": str(adb_path),
                    "AdbSerial": "private-device-address",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAABANGDREAM_MFA_ROOT", str(tmp_path))

    device = _load_runtime_adb_device()

    assert device.adb_path == adb_path
    assert device.serial == "private-device-address"
    assert "private-device-address" not in repr(device)


def test_adb_shell_uses_argument_list_and_returns_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    adb_path = tmp_path / "adb.exe"
    adb_path.touch()
    captured: list[list[str]] = []

    def fake_run(arguments, **kwargs):
        captured.append(arguments)
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    monkeypatch.setattr("agent.manual_flow_recording.subprocess.run", fake_run)
    shell = _build_adb_shell(RuntimeAdbDevice(adb_path, "test-address"))

    assert shell(("settings", "get", "system", "show_touches")) == "1"
    assert captured == [
        [
            str(adb_path),
            "-s",
            "test-address",
            "shell",
            "settings",
            "get",
            "system",
            "show_touches",
        ]
    ]


def test_custom_action_finalizes_video_when_mfa_stops(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "agent.manual_flow_recording.DEFAULT_RECORDING_ROOT",
        tmp_path,
    )
    fake_shell = FakeShell()
    monkeypatch.setattr(
        "agent.manual_flow_recording._load_runtime_adb_device",
        lambda: RuntimeAdbDevice(tmp_path / "adb.exe", "redacted-test-device"),
    )
    monkeypatch.setattr(
        "agent.manual_flow_recording._build_adb_shell",
        lambda device: fake_shell,
    )
    tasker = FakeTasker()
    context = SimpleNamespace(tasker=tasker)
    argv = SimpleNamespace(
        custom_action_param=json.dumps(
            {
                "fps": 15,
                "max_duration_seconds": 60,
                "show_touches": True,
            }
        )
    )

    assert ManualFlowRecording()._run(context, argv) is True

    sessions = list(tmp_path.glob("manual-flow-general-*"))
    assert len(sessions) == 1
    summary = json.loads((sessions[0] / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop_reason"] == "user_stop"
    assert summary["frame_count"] == 4
    assert summary["video_seek_verified"] is True
    assert fake_shell.commands[-1] == (
        "settings",
        "put",
        "system",
        "show_touches",
        "0",
    )


def test_manual_flow_task_is_generic_and_configurable() -> None:
    interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))
    task = next(
        task for task in interface["task"] if task["name"] == "ManualFlowRecording"
    )
    assert task["entry"] == "ManualFlowRecording"
    assert task["option"] == [
        "ManualFlowRecordingQuality",
        "ManualFlowRecordingDuration",
        "ManualFlowRecordingTouches",
    ]
    pipeline = json.loads(
        (ROOT / "resource/pipeline/manual_flow_recording.json").read_text(
            encoding="utf-8"
        )
    )
    assert pipeline["ManualFlowRecordingQualityGate"]["custom_action_param"] == {
        "reset": True,
        "fps": 10,
    }
    assert pipeline["ManualFlowRecordingDurationGate"]["custom_action_param"] == {
        "max_duration_seconds": 900,
    }
    assert pipeline["ManualFlowRecordingTouchesGate"]["custom_action_param"] == {
        "show_touches": True,
    }
    capture = pipeline["ManualFlowRecordingCapture"]
    assert capture["custom_action"] == "ManualFlowRecording"
    assert "custom_action_param" not in capture
    assert "协力" not in task["label"]


def test_recording_options_merge_across_independent_pipeline_nodes() -> None:
    configure_manual_flow_settings({"reset": True, "fps": 5})
    configure_manual_flow_settings({"max_duration_seconds": 300})
    configure_manual_flow_settings({"show_touches": False})

    assert current_manual_flow_settings() == {
        "fps": 5,
        "max_duration_seconds": 300,
        "show_touches": False,
    }

    configure_manual_flow_settings({"reset": True})
