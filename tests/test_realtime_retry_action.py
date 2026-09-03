import json
from types import SimpleNamespace

from agent.realtime import runtime_options
from agent.realtime.runtime_options import (
    RealtimePlayRetryControl,
    retryable_play_failure,
)


def argv(operation: str):
    return SimpleNamespace(
        custom_action_param=json.dumps({"operation": operation})
    )


def context():
    return SimpleNamespace(tasker=SimpleNamespace(stopping=False))


def configure_transient_failure(monkeypatch, *, retry_count: int = 1):
    monkeypatch.setattr(
        runtime_options,
        "RealtimeProfileStore",
        lambda _root: SimpleNamespace(
            runtime_options=lambda: {
                "play_failure_retry_count": retry_count,
            }
        ),
    )
    monkeypatch.setattr(
        runtime_options,
        "current_live_run",
        lambda: SimpleNamespace(mode="formal"),
    )
    monkeypatch.setattr(
        runtime_options,
        "latest_failure_reason",
        lambda: "RuntimeError: temporary capture failure",
    )
    monkeypatch.setattr(
        runtime_options,
        "append_current_run_event",
        lambda *_args, **_kwargs: None,
    )


def test_single_play_retry_is_bounded_and_discards_native_prearm(monkeypatch):
    configure_transient_failure(monkeypatch, retry_count=1)
    discarded = []
    monkeypatch.setattr(
        runtime_options,
        "discard_prearmed_backend",
        discarded.append,
    )
    action = RealtimePlayRetryControl()
    ctx = context()

    assert action.run(ctx, argv("reset")) is True
    assert action.run(ctx, argv("check")) is True
    assert action.run(ctx, argv("check")) is False
    assert discarded == ["single-play-retry"]


def test_single_play_retry_rejects_identity_and_configuration_failures(
    monkeypatch,
):
    configure_transient_failure(monkeypatch, retry_count=3)
    monkeypatch.setattr(
        runtime_options,
        "latest_failure_reason",
        lambda: "ValueError: preparation song level conflicts with selected chart",
    )
    discarded = []
    monkeypatch.setattr(
        runtime_options,
        "discard_prearmed_backend",
        discarded.append,
    )
    action = RealtimePlayRetryControl()
    ctx = context()

    assert action.run(ctx, argv("reset")) is True
    assert action.run(ctx, argv("check")) is False
    assert discarded == []


def test_calibration_does_not_enter_the_nested_single_retry(monkeypatch):
    configure_transient_failure(monkeypatch, retry_count=3)
    monkeypatch.setattr(
        runtime_options,
        "current_live_run",
        lambda: SimpleNamespace(mode="calibration-rehearsal"),
    )
    action = RealtimePlayRetryControl()
    ctx = context()

    assert action.run(ctx, argv("reset")) is True
    assert action.run(ctx, argv("check")) is False


def test_retryable_failure_classifier_keeps_hard_conflicts_out():
    assert retryable_play_failure("RuntimeError: capture timed out") is True
    assert retryable_play_failure("ValueError: invalid option") is False
    assert retryable_play_failure("准备页难度冲突") is False
