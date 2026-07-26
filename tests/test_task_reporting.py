from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import task_reporting


class Context:
    def __init__(self, hit_count: int = 0):
        self.hit_count = hit_count
        self.tasker = SimpleNamespace(stopping=False)
        self.visible = []

    def run_task(self, entry, override):
        assert entry == "TaskReportVisible"
        focus = override[entry]["focus"]["Node.Action.Succeeded"]
        self.visible.append(focus)
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))

    def get_hit_count(self, _node_name: str) -> int:
        return self.hit_count


def argv(task_id: int, node_name: str = "RoundGate", **params):
    return SimpleNamespace(
        task_detail=SimpleNamespace(task_id=task_id, entry="Entry"),
        node_name=node_name,
        custom_action_param=json.dumps(params, ensure_ascii=False),
    )


@pytest.fixture(autouse=True)
def clear_reporting_state():
    task_reporting.clear_states()
    yield
    task_reporting.clear_states()


def test_progress_reports_exact_round_and_completion(capsys):
    context = Context(hit_count=1)

    assert task_reporting.TaskProgress().run(
        context,
        argv(
            101,
            task_name="AutoLive",
            label="自动演出",
            total=3,
            phase="start",
        ),
    )
    assert task_reporting.TaskProgress().run(
        context,
        argv(
            101,
            node_name="RoundCompleted",
            task_name="AutoLive",
            label="自动演出",
            total=3,
            phase="completed",
        ),
    )

    output = capsys.readouterr().out
    assert "演奏次数：当前 1/3，已完成 0/3" in output
    assert "演奏次数：已完成 1/3" in output
    assert [item["content"] for item in context.visible] == [
        "自动演出演奏次数：当前 1/3，已完成 0/3",
        "自动演出演奏次数：已完成 1/3",
    ]
    assert all(item["display"] == ["log"] for item in context.visible)


def test_failure_returns_false_without_post_stop_and_includes_progress(capsys):
    context = Context(hit_count=2)
    assert task_reporting.TaskProgress().run(
        context,
        argv(
            202,
            task_name="ChallengeLive",
            label="挑战演出",
            total=5,
            phase="start",
        ),
    )

    assert not task_reporting.TaskOutcome().run(
        context,
        argv(
            202,
            node_name="ChallengeNoEvent",
            task_name="ChallengeLive",
            label="挑战演出",
            total=5,
            status="failure",
            reason="当前没有可用的挑战演出活动",
        ),
    )

    output = capsys.readouterr().out
    assert "任务失败" in output
    assert "已完成 0/5" in output
    assert "当前没有可用的挑战演出活动" in output
    assert context.visible[-1]["content"].startswith("任务失败")
    assert context.visible[-1]["display"] == ["log"]
    assert not hasattr(context.tasker, "post_stop")


def test_success_clears_only_its_own_task_state(capsys):
    context = Context(hit_count=1)
    for task_id in (301, 302):
        assert task_reporting.TaskProgress().run(
            context,
            argv(
                task_id,
                task_name="AutoLive",
                label="自动演出",
                total=1,
                phase="start",
            ),
        )

    assert task_reporting.TaskProgress().run(
        context,
        argv(
            301,
            node_name="RoundCompleted",
            task_name="AutoLive",
            label="自动演出",
            total=1,
            phase="completed",
        ),
    )
    assert task_reporting.TaskOutcome().run(
        context,
        argv(
            301,
            node_name="Complete",
            task_name="AutoLive",
            label="自动演出",
            total=1,
            status="success",
        ),
    )

    assert 301 not in task_reporting.active_task_ids()
    assert 302 in task_reporting.active_task_ids()
    assert "任务成功：已完成 1/1" in capsys.readouterr().out
    assert context.visible[-1]["display"] == ["log", "toast"]

def test_visible_log_failure_does_not_change_business_result(monkeypatch):
    context = Context(hit_count=1)
    monkeypatch.setattr(
        context,
        "run_task",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("client log unavailable")),
    )

    assert task_reporting.TaskProgress().run(
        context,
        argv(
            401,
            task_name="AutoLive",
            label="自动演出",
            total=1,
            phase="start",
        ),
    )
    assert task_reporting.TaskOutcome().run(
        context,
        argv(
            401,
            task_name="AutoLive",
            label="自动演出",
            total=1,
            status="success",
        ),
    )


def test_user_stop_is_reported_as_cancelled_not_failed(capsys):
    context = Context(hit_count=1)
    context.tasker.stopping = True

    assert task_reporting.TaskOutcome().run(
        context,
        argv(
            501,
            task_name="RealtimeLive",
            label="实时演奏",
            total=2,
            status="failure",
            reason="不应显示的失败",
        ),
    )

    output = capsys.readouterr().out
    assert "用户已停止任务" in output
    assert "任务失败" not in output


def test_failure_uses_recorded_runtime_reason_in_terminal_log(capsys):
    context = Context(hit_count=1)
    task_reporting.record_failure_reason(
        "演奏超过安全时限 600 秒，仍未识别到结算画面"
    )

    assert not task_reporting.TaskOutcome().run(
        context,
        argv(
            502,
            task_name="RealtimeLive",
            label="实时演奏",
            total=2,
            status="failure",
            reason_source="latest",
            reason="实时演奏流程未完成",
        ),
    )

    output = capsys.readouterr().out
    assert "演奏超过安全时限 600 秒，仍未识别到结算画面" in output
    assert "实时演奏流程未完成" not in output
