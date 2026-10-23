"""任务独立的主页补充与保守消耗凭据；房间和演奏中不读取体力界面。"""

# 日志通道是附加功能，输出故障不得改变演出、停止或清理结果。
from .optional_diagnostics import safe_print as print, safe_traceback
from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from .optional_diagnostics import OptionalDiagnostics, warning
from .profile_store import RealtimeProfileStore
from .stamina_refill import (
    ROOT, RefillJournal, StaminaRefill, detector, validate_refill_options,
    ScreenRefreshCancelled, capture_image,
)
try:
    from ..foreground_click import ForegroundClick
    from ..task_reporting import remaining_task_rounds, record_failure_reason, report_task_warning, report_task_configuration
except ImportError:
    from foreground_click import ForegroundClick
    from task_reporting import remaining_task_rounds, record_failure_reason, report_task_warning, report_task_configuration


class StaminaTaskError(RuntimeError):
    pass


class StaminaNavigationError(StaminaTaskError):
    """补充界面已交互后无法确认安全页面，不能继续导航。"""


def _report_safely(report, context, label, message):
    if context.tasker.stopping:
        return
    try:
        report(context, label, message)
    except Exception as exc:
        if not context.tasker.stopping:
            warning(f"StaminaRefill report_warning={type(exc).__name__}: {exc}")


def _disable_optional_refill(context, argv, reason):
    state = task_state(argv)
    if state is not None:
        state.options = {**state.options, "stamina_refill_enabled": False}
        state.degraded = True
        state.degraded_reason = str(reason)
    _report_safely(report_task_warning, context, "演出任务", f"自动补充已隔离，继续原任务：{reason}")



@dataclass
class TaskStamina:
    options: dict
    mode: str
    cost: int
    available: int | None = None
    remaining: int | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    completed_epoch: int = 0
    home_attempt_epoch: int = -1


_TASKS = {}


def task_identity(argv):
    value = getattr(getattr(argv, "task_detail", None), "task_id", None)
    if type(value) is not int or value <= 0:
        raise StaminaTaskError("任务体力配置缺少可信 task_detail.task_id")
    return value


def task_state(argv):
    try:
        return _TASKS.get(task_identity(argv))
    except StaminaTaskError:
        return None


def enabled_state(argv):
    state = task_state(argv)
    return state if state and state.options["stamina_refill_enabled"] else None


def formal_single(context):
    node = context.get_node_data("RealtimeLiveFormalModeGate")
    if not isinstance(node, dict):
        raise StaminaTaskError("无法读取单人演奏模式，拒绝猜测体力消耗")
    # Maa 返回解析后的跳转对象，不能按原 Pipeline 的字符串数组直接比较。
    names = [item.get("name") if isinstance(item, dict) else item for item in node.get("next", ())]
    return names == ["RealtimeLiveRequireProfile"]


def allocate_home(context, argv, remaining=None, *, minimum_rounds=1):
    state = enabled_state(argv)
    if state is None:
        return
    if context.tasker.stopping:
        raise ScreenRefreshCancelled("用户已停止主页体力检查")
    state.remaining = remaining
    state.home_attempt_epoch = state.completed_epoch
    if remaining == 0:
        return
    # 单人排练不消耗普通体力，校准只为正式验证预留一首。
    cost = state.cost
    runner = None
    try:
        if state.mode == "RealtimeLive" and not formal_single(context):
            cost = 0
        journal = RefillJournal(ROOT / "debug/stamina-refill", task_identity(argv))
        runner = StaminaRefill(context)
        runner.ensure(cost, state.options, journal, remaining_rounds=remaining,
                      minimum_required=cost*minimum_rounds)
        if cost:
            state.available = journal.load().get("available_points")
            if type(state.available) is not int:
                raise StaminaTaskError("主页体力检查未产生可信可用点数")
        state.degraded = False
        state.degraded_reason = None
    except ScreenRefreshCancelled:
        raise
    except Exception as exc:
        if context.tasker.stopping:
            raise ScreenRefreshCancelled("用户已停止体力处理") from exc
        state.degraded = True
        state.degraded_reason = str(exc)
        state.available = getattr(runner,"last_known_current",None)
        evidence = OptionalDiagnostics(lambda: context.tasker.stopping).call(
            "stamina-failure-evidence", lambda: runner.save_failure(exc)) if runner is not None else None
        _report_safely(report_task_warning,context,"演出任务",f"本次体力补充已跳过，继续原任务；原因：{exc}；诊断：{evidence or '保存不可用'}")
        home = False
        if runner is not None:
            try:
                home = runner.return_home_safely()
            except ScreenRefreshCancelled:
                raise
            except Exception:
                home = False
        if not home:
            recover_stamina_home(context,state)


def recover_stamina_home(context,state):
    if context.tasker.stopping:
        raise ScreenRefreshCancelled("用户已停止主页恢复")
    try:
        try:
            from ..common_recover import CommonRecover
        except ImportError:
            from common_recover import CommonRecover
        nodes = {"CooperativeLive":"CooperativeRecover","VersusLive":"VersusRecover",
                 "AutoLive":"AutoLiveRecover","RealtimeLive":"RealtimeLiveRecover",
                 "RealtimeCalibration":"RealtimeCalibrationRecover"}
        node = context.get_node_data(nodes.get(state.mode,"RealtimeLiveRecover"))
        params = node.get("action",{}).get("param",{}).get("custom_action_param",{}) if node else {}
        if not params:
            raise StaminaNavigationError("通用主页恢复参数不可用，无法安全继续导航")
        recovered = CommonRecover().run(context,SimpleNamespace(custom_action_param=json.dumps(params)))
        if context.tasker.stopping:
            raise ScreenRefreshCancelled("用户已停止主页恢复")
        if not recovered:
            raise StaminaNavigationError("既有通用导航无法恢复主页")

    except ScreenRefreshCancelled:
        raise
    except StaminaNavigationError:
        raise
    except Exception as exc:
        if context.tasker.stopping:
            raise ScreenRefreshCancelled("用户已停止主页恢复") from exc
        raise StaminaNavigationError(f"体力处理后无法安全恢复主页：{type(exc).__name__}: {exc}") from exc


def reserve_attempt(argv, *, rounds=1):
    state = enabled_state(argv)
    if state is None or state.cost == 0:
        return
    needed = state.cost * rounds
    if state.degraded or state.available is None or state.available < needed:
        state.degraded = True
        if state.available is not None:
            state.available = max(0,state.available-needed)
        return
    # 每次尝试前保守预扣，失败不猜测游戏是否消费，也不自动退款。
    state.available -= needed
    print(f"StaminaAllocation reserved={needed} estimated_available={state.available}", flush=True)


def mark_uncertain(argv):
    state = enabled_state(argv)
    if state is not None:
        state.available = None


def allocate_flow_home(flow):
    argv = getattr(flow, "stamina_argv", None)
    if enabled_state(argv) is not None:
        try:
            allocate_home(flow.context, argv, getattr(flow, "_stamina_remaining", None))
        except ScreenRefreshCancelled as exc:
            raise InterruptedError("用户已停止任务") from exc
        except StaminaNavigationError:
            raise
        except Exception as exc:
            if flow.stopped():
                raise InterruptedError("用户已停止任务") from exc
            raise StaminaNavigationError(f"主页补充后未能确认安全页面：{exc}") from exc


def mark_stamina_settled(argv, completed):
    """只在上一局结算导航结束后进入下一轮时，开放一次主页重读机会。"""
    state = enabled_state(argv)
    if state is not None and type(completed) is int and completed > state.completed_epoch:
        state.completed_epoch = completed


def prepare_stamina_round(flow, remaining):
    argv = getattr(flow, "stamina_argv", None)
    state = enabled_state(argv)
    if state is None or state.cost == 0:
        return False
    flow._stamina_remaining = remaining
    returned_home = False
    if state.available is None:
        state.degraded = True
        reserve_attempt(argv)
        return False
    if state.available < state.cost and state.home_attempt_epoch >= state.completed_epoch:
        reserve_attempt(argv)
        return False
    if state.available is None or state.available < state.cost:
        if flow.stopped():
            raise InterruptedError("用户已停止任务")
        # 只在已结算返回的房间/选择/准备页恢复，开场或曲中绝不主动退出。
        templates = getattr(flow, "templates", None)
        names = ("room_search", "room_wait", "room_together", "ready_button", "song_unspecified")
        names = tuple(name for name in names if name in templates) if templates is not None else ("room_search", "ready_button", "song_unspecified")
        try:
            marker, _ = flow.wait_for(names, timeout=0.0, detect_member_exit=False)
        except (InterruptedError, ScreenRefreshCancelled):
            raise
        except Exception as exc:
            if flow.stopped():
                raise InterruptedError("用户已停止任务") from exc
            _disable_optional_refill(flow.context, argv, exc)
            return False
        if marker is None:
            state.degraded = True
            state.degraded_reason = "无法确认补充所需的安全房间边界"
            _report_safely(report_task_warning,flow.context,"演出任务","无法确认安全回主页边界，已跳过自动补充并继续原流程")
            return False
        state.home_attempt_epoch = state.completed_epoch
        try:
            flow.recover_after_play_failure("完整结算后主页补充体力")
        except InterruptedError:
            raise
        except Exception as exc:
            raise StaminaNavigationError(f"结算后主页补充失败：{exc}") from exc
        returned_home = True
    reserve_attempt(argv)
    return returned_home


@AgentServer.custom_action("StaminaRefillConfigure")
class StaminaRefillConfigure(CustomAction):
    def run(self, context, argv):
        if context.tasker.stopping:
            return True
        try:
            params = json.loads(argv.custom_action_param or "{}")
            options = validate_refill_options(params)
            mode = str(params["task_mode"])
            if mode not in {"AutoLive", "RealtimeLive", "CooperativeLive", "VersusLive", "MedleyLive", "RealtimeCalibration", "ContinuousRealtimeLive"}:
                raise StaminaTaskError("不支持的体力任务模式")
            cost = RealtimeProfileStore(ROOT / "profiles").runtime_options()["live_boost_target"]
            identity = task_identity(argv)
            previous = _TASKS.get(identity)
            if previous is not None:
                if (previous.options, previous.mode, previous.cost) != (options, mode, cost):
                    raise StaminaTaskError("同任务体力配置发生冲突，拒绝覆盖已有凭据")
            else:
                _TASKS[identity] = TaskStamina(options, mode, cost)
            if not options["stamina_refill_enabled"]:
                description = "已关闭"
            elif options["stamina_refill_small_enabled"] and options["stamina_refill_large_enabled"]:
                description = "大小火罐智能补充"
            elif options["stamina_refill_small_enabled"]:
                description = "仅小火罐（+1）"
            elif options["stamina_refill_large_enabled"]:
                description = "仅大火罐（+10）"
            else:
                description = "已启用但未允许任何道具"
            _report_safely(report_task_configuration,context,"演出任务",f"体力补充：{description}；每局消耗 {cost} 点；task_id={identity}；仅在主页处理")
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            _disable_optional_refill(context, argv, exc)
            return True


def home_remaining(context, argv, state):
    if state.mode == "CooperativeLive":
        from .cooperative_action import current_cooperative_settings
        count = int(current_cooperative_settings()["count"])
        return count or None
    if state.mode == "VersusLive":
        from .versus_action import current_versus_settings
        count = int(current_versus_settings()["count"])
        return count or None
    if state.mode in {"RealtimeCalibration", "ContinuousRealtimeLive"}:
        return 1
    return remaining_task_rounds(argv)


@AgentServer.custom_action("StaminaHomeEntry")
class StaminaHomeEntry(CustomAction):
    def run(self, context, argv):
        try:
            if context.tasker.stopping:
                return True
            state = enabled_state(argv)
            if state is not None:
                allocate_home(context, argv, home_remaining(context, argv, state))
                if context.tasker.stopping:
                    return True
                # 补充弹窗可能改变代理和页面，只在最新完整主页再次确认后沿原入口点击。
                if not detector().page(capture_image(context), "menu"):
                    raise StaminaTaskError("体力处理后无法重新确认主页演出入口")
                # 保留 Maa 已解析的 target/box 中心，只对新门禁路径使用保护后重取的代理。
                box = argv.box
                StaminaRefill(context).click(box.x + box.w//2, box.y + box.h//2)
                return True
            return ForegroundClick().run(context, argv)
        except ScreenRefreshCancelled:
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            record_failure_reason(f"主页体力检查失败：{exc}")
            return False


@AgentServer.custom_action("StaminaPassiveHomeGate")
class StaminaPassiveHomeGate(CustomAction):
    def run(self, context, argv):
        try:
            if context.tasker.stopping or enabled_state(argv) is None:
                return True
            # 被动一键已有现场时只跳过，不能为补充离开封面、准备页或演奏中现场。
            image = capture_image(context)
            if detector().page(image, "menu"):
                allocate_home(context, argv, 1)
            else:
                print("StaminaRefill passive_non_home=skipped", flush=True)
            return True
        except ScreenRefreshCancelled:
            return True
        except StaminaNavigationError as exc:
            if context.tasker.stopping:
                return True
            record_failure_reason(str(exc))
            return False
        except Exception as exc:
            if context.tasker.stopping:
                return True
            _disable_optional_refill(context, argv, exc)
            return True
