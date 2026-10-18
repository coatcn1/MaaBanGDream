"""协力建房邀请和唯一好友邀请入房的独立状态机。

页面识别与坐标由适配器提供；本模块不推测好友、按钮或页面。邀请账本只在
当前进程保留，并按 MaaFramework task_id 和实际房间号隔离，重入不得重复发送。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import math
import threading
from typing import Protocol


class FriendPage(str, Enum):
    ROOM_SELECTION = "room_selection"
    CREATE_CONFIRM = "create_confirm"
    HOST_ROOM = "host_room"
    INVITE_PANEL = "invite_panel"
    INVITE_CONFIRM = "invite_confirm"
    SEARCH_METHOD = "search_method"
    GUEST_INVITES = "guest_invites"
    JOIN_CONFIRM = "join_confirm"
    GUEST_ROOM = "guest_room"
    START_CONFIRM = "start_confirm"
    ROOM_STARTED = "room_started"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FriendObservation:
    """单张新截图的证据；actions 只能包含该截图确认可操作的按钮。

    room_key 为房间号等稳定唯一标识，不能用“房主大厅”之类页面名代替。
    valid_invitation_count 是整张邀请列表上可加入邀请的数量；join_invitation
    必须绑定其中唯一有效的一行，失效行和灰色按钮不能计入。
    invitation_sent 只表示明确“已发送/已邀请”证据，离开面板不是发送成功。
    member_count 仅计算已经在该房间的成员（含房主），不能计算邀请名单。
    """

    state: FriendPage | str
    room_key: str = ""
    actions: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    invitation_sent: bool = False
    valid_invitation_count: int = 0
    member_count: int = 0
    error: str = ""


class FriendAdapter(Protocol):
    def observe(self) -> FriendObservation: ...
    def click(self, point: tuple[int, int]) -> None: ...
    def stopped(self) -> bool: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class FriendRoomError(RuntimeError):
    pass


class FriendInviteUncertain(FriendRoomError):
    """可能已经发送，必须保留现场，不能自动再次发送。"""


class FriendRoomCancelled(FriendRoomError):
    pass


class InviteStatus(str, Enum):
    NOT_SENT = "not_sent"
    SENDING = "sending"
    SENT = "sent"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InviteLedger:
    """只记录发送意图及结果；不存设备、账号或好友身份。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], InviteStatus] = {}
        self._room_rounds: dict[tuple[str, str], int] = {}
        self._room_start_attempts: set[tuple[str, str, int]] = set()
        self._room_start_delivered: set[tuple[str, str, int]] = set()
        self._completed_round_tokens: dict[tuple[str, str], set[str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(task_id: str, room_key: str) -> tuple[str, str]:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("好友邀请缺少有效任务 ID")
        if not isinstance(room_key, str) or not room_key.strip():
            raise ValueError("好友邀请缺少明确房间标识")
        return task_id.strip(), room_key.strip()

    def status(self, task_id: str, room_key: str) -> InviteStatus:
        with self._lock:
            return self._entries.get(self._key(task_id, room_key), InviteStatus.NOT_SENT)

    def begin(self, task_id: str, room_key: str) -> bool:
        """点击前占用；并发/重入只允许一次发送，已发送返回 False。"""
        key = self._key(task_id, room_key)
        with self._lock:
            status = self._entries.get(key, InviteStatus.NOT_SENT)
            if status == InviteStatus.SENT:
                return False
            if status in {InviteStatus.SENDING, InviteStatus.UNCERTAIN}:
                raise FriendInviteUncertain("本任务在该房间的好友邀请结果不明，不会重复发送")
            if status in {InviteStatus.FAILED, InviteStatus.CANCELLED}:
                raise FriendRoomError("本任务在该房间的邀请已失败或取消，不会重新发送")
            self._entries[key] = InviteStatus.SENDING
            return True

    def mark_sent(self, task_id: str, room_key: str) -> None:
        with self._lock:
            key = self._key(task_id, room_key)
            if self._entries.get(key) in {InviteStatus.FAILED, InviteStatus.CANCELLED}:
                raise FriendRoomError("已失败或取消的邀请不能恢复为发送成功")
            self._entries[key] = InviteStatus.SENT

    def finish(self, task_id: str, room_key: str, status: InviteStatus) -> None:
        if status not in {InviteStatus.UNCERTAIN, InviteStatus.FAILED, InviteStatus.CANCELLED}:
            raise ValueError("邀请终止状态无效")
        with self._lock:
            key = self._key(task_id, room_key)
            # 等待成员超时/用户停止不能撤销已经确认的发送成功。
            if self._entries.get(key) == InviteStatus.SENDING:
                self._entries[key] = status

    def begin_room_start(self, task_id: str, room_key: str) -> None:
        """开始点击同样不能跨实例重发；送达未知时保留该尝试。"""
        room = self._key(task_id, room_key)
        with self._lock:
            key = (*room, self._room_rounds.get(room, 0))
            if key in self._room_start_attempts:
                raise FriendRoomError("本任务已尝试开始该房间，送达结果不明时不会重复点击")
            self._room_start_attempts.add(key)

    def room_start_attempted(self, task_id: str, room_key: str) -> bool:
        room = self._key(task_id, room_key)
        with self._lock:
            return (*room, self._room_rounds.get(room, 0)) in self._room_start_attempts

    def room_round(self, task_id: str, room_key: str) -> int:
        with self._lock:
            return self._room_rounds.get(self._key(task_id, room_key), 0)

    def mark_room_started(self, task_id: str, room_key: str) -> None:
        room = self._key(task_id, room_key)
        with self._lock:
            key = (*room, self._room_rounds.get(room, 0))
            if key not in self._room_start_attempts:
                raise FriendRoomError("缺少本轮房间开始记录，不能确认开始送达")
            self._room_start_delivered.add(key)

    def advance_room_round(self, task_id: str, room_key: str, completed_round_token: str) -> int:
        """消费一次完成凭据，开启下一轮；调用方必须已验证整轮演出完成。

        ROOM_STARTED 仅证明起房送达，本身不能开启下一轮。旧轮的开始记录
        永久保留；不同完成凭据也不能跳过尚未开始的新轮。同一凭据重入幂等。
        """
        room = self._key(task_id, room_key)
        if not isinstance(completed_round_token, str) or not completed_round_token.strip():
            raise ValueError("开启下一轮缺少有效演出完成凭据")
        token = completed_round_token.strip()
        with self._lock:
            current = self._room_rounds.get(room, 0)
            consumed = self._completed_round_tokens.setdefault(room, set())
            if token in consumed:
                return current
            if (*room, current) not in self._room_start_delivered:
                raise FriendRoomError("本轮尚未确认开始送达，不能开启下一轮")
            consumed.add(token)
            self._room_rounds[room] = current + 1
            return current + 1


DEFAULT_INVITE_LEDGER = InviteLedger()


class CooperativeFriendFlow:
    """只处理入房前阶段，成功后交回既有协力选曲/准备/演奏流程。"""

    def __init__(
        self, adapter: FriendAdapter, *, task_id: str,
        ledger: InviteLedger | None = None, interval_s: float = 0.25,
    ) -> None:
        InviteLedger._key(task_id, "validation-only")
        if not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("好友邀请观察间隔必须为正数")
        self.adapter = adapter
        self.task_id = task_id.strip()
        self.ledger = ledger if ledger is not None else DEFAULT_INVITE_LEDGER
        self.interval_s = min(interval_s, 0.5)
        self.room_key = ""
        self._clicked: set[tuple[str, str, int]] = set()
        self._invite_send_owned = False
        self._host_invite_lock = threading.Lock()

    def _stop(self) -> None:
        if self.adapter.stopped():
            raise InterruptedError("用户已停止任务")

    def _observe(self) -> FriendObservation:
        self._stop()
        observation = self.adapter.observe()
        self._stop()
        if not isinstance(observation, FriendObservation):
            raise FriendRoomError("好友邀请页面识别未返回有效证据")
        if observation.state == FriendPage.CANCELLED:
            if self.room_key and self._invite_send_owned:
                self.ledger.finish(self.task_id, self.room_key, InviteStatus.CANCELLED)
            raise FriendRoomCancelled(observation.error or "好友邀请或入房已取消")
        if observation.state == FriendPage.ERROR or observation.error:
            if self.room_key and self._invite_send_owned:
                self.ledger.finish(self.task_id, self.room_key, InviteStatus.FAILED)
            raise FriendRoomError(observation.error or "好友房间发生错误")
        return observation

    def _action_key(self, action: str) -> tuple[str, str, int]:
        round_index = (
            self.ledger.room_round(self.task_id, self.room_key)
            if action in {"start_room", "confirm_start"} else -1
        )
        return self.room_key, action, round_index

    def _click_once(self, observation: FriendObservation, action: str) -> bool:
        self._stop()
        point = observation.actions.get(action)
        if point is None:
            return False
        if (
            not isinstance(point, (tuple, list)) or len(point) != 2
            or any(not isinstance(value, int) or isinstance(value, bool) for value in point)
            or not (0 <= point[0] < 1280 and 0 <= point[1] < 720)
        ):
            raise FriendRoomError(f"好友邀请按钮证据无效：{action}")
        key = self._action_key(action)
        if key in self._clicked:
            return False
        self._stop()
        # 点击抛异常也不能复用同一截图再次点击；发送另有跨实例账本保护。
        self._clicked.add(key)
        self.adapter.click((point[0], point[1]))
        self._stop()
        return True

    def _deadline(self, timeout_s: float) -> float:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("好友房间等待时限必须为正数")
        self._stop()
        return self.adapter.monotonic() + timeout_s

    def _pause(self, deadline: float) -> None:
        self._stop()
        remaining = deadline - self.adapter.monotonic()
        if remaining > 0:
            self.adapter.sleep(min(self.interval_s, remaining))
        self._stop()

    def _bind_room(self, observation: FriendObservation) -> None:
        key = observation.room_key.strip()
        if not self.room_key:
            if not key:
                raise FriendRoomError("未识别到明确房间标识，不能发送好友邀请")
            self.room_key = key
        elif key and key != self.room_key:
            raise FriendRoomError("好友邀请期间房间标识改变，已停止本轮操作")

    def host_invite_all(self, *, timeout_s: float = 90.0, expected_members: int = 2) -> FriendObservation:
        """建房→一次邀请所有好友→发送证据→关闭面板→等待成员到齐。

        不限制好友总数。expected_members 是本次入房所需成员数，默认等待一名
        好友和房主；适配器不能把好友名单人数当作已经加入房间的人数。
        """
        if not isinstance(expected_members, int) or isinstance(expected_members, bool) or expected_members < 2:
            raise ValueError("房主等待的房间成员数必须至少为2")
        deadline = self._deadline(timeout_s)
        if not self._host_invite_lock.acquire(blocking=False):
            raise FriendInviteUncertain("本流程正在发送或确认邀请，不允许重入消费同一现场")
        phase = "等待建房页面"
        try:
            while self.adapter.monotonic() < deadline:
                observation = self._observe()
                state = observation.state
                if state in {FriendPage.HOST_ROOM, FriendPage.INVITE_PANEL, FriendPage.INVITE_CONFIRM}:
                    self._bind_room(observation)
                    if observation.invitation_sent:
                        self.ledger.mark_sent(self.task_id, self.room_key)
                    status = self.ledger.status(self.task_id, self.room_key)
                    if status in {InviteStatus.UNCERTAIN, InviteStatus.FAILED, InviteStatus.CANCELLED}:
                        self.ledger.begin(self.task_id, self.room_key)
                    if state == FriendPage.HOST_ROOM:
                        if status == InviteStatus.SENT:
                            phase = "等待好友加入房间"
                            if observation.member_count >= expected_members:
                                return observation
                        elif status == InviteStatus.SENDING:
                            # 消失的面板不能证明发送成功，也不能重新打开后重发。
                            phase = "确认邀请发送结果"
                        else:
                            phase = "打开好友邀请面板"
                            self._click_once(observation, "open_invites")
                    elif state == FriendPage.INVITE_PANEL:
                        if status == InviteStatus.SENT:
                            phase = "关闭好友邀请面板"
                            self._click_once(observation, "close_invites")
                        elif status == InviteStatus.SENDING:
                            phase = "确认邀请发送结果"
                        elif "invite_all" in observation.actions:
                            self._stop()
                            self._invite_send_owned = self.ledger.begin(self.task_id, self.room_key)
                            phase = "确认邀请发送结果"
                            if self._invite_send_owned:
                                self._click_once(observation, "invite_all")
                    elif state == FriendPage.INVITE_CONFIRM:
                        if status == InviteStatus.NOT_SENT:
                            # 实测国服“邀请好友”直接弹确认框，不先显示名单面板。
                            # 只接受本流程从已识别大厅亲自打开的确认页。
                            if self._action_key("open_invites") not in self._clicked:
                                raise FriendInviteUncertain("出现来源不明的邀请确认页，不会继续发送")
                            if "confirm_invite" in observation.actions:
                                self._stop()
                                self._invite_send_owned = self.ledger.begin(self.task_id, self.room_key)
                                phase = "确认邀请发送结果"
                                if self._invite_send_owned:
                                    self._click_once(observation, "confirm_invite")
                        elif status == InviteStatus.SENDING:
                            # 账本中的发送中状态不是可继承权限，只有亲自占用的流程可以确认。
                            if not self._invite_send_owned:
                                raise FriendInviteUncertain("邀请由另一流程发送中，不会继承其确认按钮")
                            phase = "确认邀请发送结果"
                            self._click_once(observation, "confirm_invite")
                        else:
                            raise FriendInviteUncertain("出现来源不明的邀请确认页，不会继续发送")
                elif state == FriendPage.ROOM_SELECTION:
                    if self.room_key:
                        raise FriendRoomError("好友邀请期间房间已关闭或退出")
                    phase = "创建好友房间"
                    self._click_once(observation, "create_room")
                elif state == FriendPage.CREATE_CONFIRM:
                    if self.room_key:
                        raise FriendRoomError("好友邀请期间意外出现重新建房页面")
                    phase = "确认创建好友房间"
                    self._click_once(observation, "confirm_create")
                self._pause(deadline)
            self._stop()
            if self.room_key and self.ledger.status(self.task_id, self.room_key) == InviteStatus.SENDING:
                raise FriendInviteUncertain("好友邀请发送结果不明，不会重复发送")
            raise FriendRoomError(f"好友房间超时：{phase}")
        except BaseException:
            if self.room_key and self._invite_send_owned:
                self.ledger.finish(self.task_id, self.room_key, InviteStatus.UNCERTAIN)
            raise
        finally:
            self._host_invite_lock.release()

    def start_host_room(self, *, timeout_s: float = 30.0, expected_members: int = 2) -> FriendObservation:
        """邀请并等人成功后，单次起房并确认选曲/准备页已经到达。

        适配器仅在明确选曲或准备页标记出现时返回 ROOM_STARTED。大厅消失、
        黑屏、加载页均不能证明开始已送达。可选 START_CONFIRM 对应真正的
        开始确认框，并使用 confirm_start 动作，绝不复用邀请确认按钮。
        """
        if not isinstance(expected_members, int) or isinstance(expected_members, bool) or expected_members < 2:
            raise ValueError("房主等待的房间成员数必须至少为2")
        deadline = self._deadline(timeout_s)
        starting = False
        while self.adapter.monotonic() < deadline:
            observation = self._observe()
            if observation.state == FriendPage.HOST_ROOM:
                self._bind_room(observation)
                if self.ledger.status(self.task_id, self.room_key) != InviteStatus.SENT:
                    raise FriendRoomError("尚未确认好友邀请发送成功，不能开始房间")
                if not starting:
                    if self.ledger.room_start_attempted(self.task_id, self.room_key):
                        raise FriendRoomError("本任务已尝试开始该房间，送达结果不明时不会重复点击")
                    if observation.member_count >= expected_members and "start_room" in observation.actions:
                        self._stop()
                        self.ledger.begin_room_start(self.task_id, self.room_key)
                        starting = True
                        self._click_once(observation, "start_room")
            elif observation.state == FriendPage.START_CONFIRM:
                if not starting:
                    raise FriendRoomError("出现来源不明的开始确认页，不会开始房间")
                self._click_once(observation, "confirm_start")
            elif observation.state == FriendPage.ROOM_STARTED:
                if not self.room_key or not self.ledger.room_start_attempted(self.task_id, self.room_key):
                    raise FriendRoomError("缺少本任务的房间开始记录，不能采用已有选曲页面")
                if observation.room_key.strip() and observation.room_key.strip() != self.room_key:
                    raise FriendRoomError("开始后的房间标识与已确认房间不一致")
                self.ledger.mark_room_started(self.task_id, self.room_key)
                return observation
            elif observation.state == FriendPage.ROOM_SELECTION:
                raise FriendRoomError("房主开始前房间已关闭或退出")
            self._pause(deadline)
        self._stop()
        reason = "立刻开始点击后未确认进入选曲或准备页" if starting else "未确认成员到齐或开始按钮可用"
        raise FriendRoomError(f"好友房间开始超时：{reason}")

    def begin_next_round(self, *, completed_round_token: str) -> int:
        """同房续打的唯一轮次推进口，返回从零开始的下一轮编号。

        调用方必须在 play 成功、完成计数递增、stay 成功且确有下一轮后调用，
        例如传入 ``f'{task_id}:completed:{completed}'``。只看到选曲页、准备页、
        起房失败或用户停止均不能调用。本方法不重置邀请发送记录。
        """
        self._stop()
        if not self.room_key:
            raise FriendRoomError("缺少已确认房间标识，不能开启下一轮")
        return self.ledger.advance_room_round(
            self.task_id, self.room_key, completed_round_token,
        )

    def join_friend_invitation(self, *, timeout_s: float = 90.0) -> FriendObservation:
        """只点唯一有效邀请；加载页或离开列表不算已经入房。"""
        deadline = self._deadline(timeout_s)
        joining = False
        joined_room_key = ""
        while self.adapter.monotonic() < deadline:
            observation = self._observe()
            state = observation.state
            if state == FriendPage.GUEST_ROOM:
                if not joining:
                    raise FriendRoomError("未确认接受好友邀请，不能使用已有房间作为入房成功")
                if joined_room_key and observation.room_key != joined_room_key:
                    raise FriendRoomError("加入的房间与已确认的好友邀请不一致")
                if not observation.room_key.strip():
                    raise FriendRoomError("好友邀请入房后未识别到明确房间标识")
                return observation
            if not joining:
                if state == FriendPage.ROOM_SELECTION:
                    self._click_once(observation, "open_search")
                elif state == FriendPage.SEARCH_METHOD:
                    self._click_once(observation, "open_friend_list")
                elif state == FriendPage.GUEST_INVITES:
                    count = observation.valid_invitation_count
                    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                        raise FriendRoomError("好友邀请列表的有效邀请数量证据无效")
                    if count > 1:
                        raise FriendRoomError("存在多条有效好友邀请，无法确定要加入的房间")
                    if count == 1 and "join_invitation" in observation.actions:
                        joined_room_key = observation.room_key.strip()
                        joining = self._click_once(observation, "join_invitation")
            elif state == FriendPage.JOIN_CONFIRM:
                self._click_once(observation, "confirm_join")
            elif state == FriendPage.ROOM_SELECTION:
                raise FriendRoomError("好友邀请已失效、房间关闭或入房失败")
            self._pause(deadline)
        self._stop()
        reason = "接受好友邀请后未确认进入房间" if joining else "未出现唯一有效好友邀请"
        raise FriendRoomError(f"好友房间超时：{reason}")
