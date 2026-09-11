import json
from types import SimpleNamespace

from agent.realtime import runtime_options
from agent.realtime.runtime_options import (
    RealtimePlayRetryControl,
    retryable_play_failure,
)


def argv(operation: str, task_id: int | None = 1):
    return SimpleNamespace(
        custom_action_param=json.dumps({"operation": operation}),
        task_detail=(
            None
            if task_id is None
            else SimpleNamespace(task_id=task_id)
        ),
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

    assert action.run(ctx, argv("reset", 101)) is True
    assert action.run(ctx, argv("check", 101)) is True
    assert action.run(ctx, argv("check", 101)) is False
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

    assert action.run(ctx, argv("reset", 102)) is True
    assert action.run(ctx, argv("check", 102)) is False
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

    assert action.run(ctx, argv("reset", 103)) is True
    assert action.run(ctx, argv("check", 103)) is False


def test_retryable_failure_classifier_keeps_hard_conflicts_out():
    assert retryable_play_failure("RuntimeError: capture timed out") is True
    assert retryable_play_failure("ValueError: invalid option") is False
    assert retryable_play_failure("准备页难度冲突") is False


def test_retry_budget_survives_callback_wrapper_recreation(monkeypatch):
    configure_transient_failure(monkeypatch, retry_count=1)
    monkeypatch.setattr(runtime_options, "discard_prearmed_backend", lambda _: None)
    action = RealtimePlayRetryControl()
    contexts = [context() for _ in range(5)]
    assert action.run(contexts[0], argv("reset", 104))
    assert action.run(contexts[1], argv("check", 104))
    assert not action.run(contexts[2], argv("check", 104))
    assert not action.run(contexts[3], argv("check", 104))
    assert action.run(contexts[4], argv("reset", 104))


def test_retry_budget_isolated_by_maafw_task_id(monkeypatch):
    configure_transient_failure(monkeypatch, retry_count=1)
    monkeypatch.setattr(runtime_options, "discard_prearmed_backend", lambda _: None)
    action = RealtimePlayRetryControl()

    assert action.run(context(), argv("reset", 105))
    assert action.run(context(), argv("check", 105))
    assert not action.run(context(), argv("check", 105))
    assert action.run(context(), argv("reset", 106))
    assert action.run(context(), argv("check", 106))


def test_retry_fails_closed_without_stable_task_id(monkeypatch):
    configure_transient_failure(monkeypatch, retry_count=1)
    discarded = []
    monkeypatch.setattr(runtime_options, "discard_prearmed_backend", discarded.append)
    action = RealtimePlayRetryControl()

    assert action.run(context(), argv("reset", None))
    assert not action.run(context(), argv("check", None))
    assert discarded == []


def test_stopping_is_neutral_and_does_not_mutate_retry_budget(monkeypatch):
    configure_transient_failure(monkeypatch, retry_count=1)
    discarded = []
    monkeypatch.setattr(runtime_options, "discard_prearmed_backend", discarded.append)
    action = RealtimePlayRetryControl()
    stopped = SimpleNamespace(tasker=SimpleNamespace(stopping=True))

    assert action.run(context(), argv("reset", 107))
    assert action.run(stopped, argv("check", 107))
    assert action.run(context(), argv("check", 107))
    assert discarded == ["single-play-retry"]
