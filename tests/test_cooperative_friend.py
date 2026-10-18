from __future__ import annotations

import pytest

from agent.realtime.cooperative_friend import (
    CooperativeFriendFlow,
    FriendInviteUncertain,
    FriendObservation,
    FriendPage,
    FriendRoomCancelled,
    FriendRoomError,
    InviteLedger,
    InviteStatus,
)


def page(state, **kwargs):
    return FriendObservation(state, **kwargs)


class Adapter:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.last = page(FriendPage.UNKNOWN)
        self.clicks = []
        self.time = 0.0
        self.stop = False
        self.observations = 0
        self.after_observe = None
        self.after_click = None

    def observe(self):
        self.observations += 1
        self.last = next(self.frames, self.last)
        if self.after_observe:
            self.after_observe(self)
        return self.last

    def click(self, point):
        self.clicks.append(point)
        if self.after_click:
            self.after_click(self)

    def stopped(self):
        return self.stop

    def monotonic(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds


def flow(frames, *, ledger=None, task_id="task-1"):
    adapter = Adapter(frames)
    ledger = ledger if ledger is not None else InviteLedger()
    machine = CooperativeFriendFlow(adapter, task_id=task_id, ledger=ledger)
    return machine, adapter, ledger


def host_room(**kwargs):
    return page(FriendPage.HOST_ROOM, room_key="123456", **kwargs)


def panel(**kwargs):
    return page(FriendPage.INVITE_PANEL, room_key="123456", **kwargs)


def test_host_creates_invites_all_once_and_waits_for_actual_room_members():
    machine, adapter, ledger = flow([
        page(FriendPage.ROOM_SELECTION, actions={"create_room": (100, 100)}),
        page(FriendPage.CREATE_CONFIRM, actions={"confirm_create": (200, 200)}),
        host_room(member_count=1, actions={"open_invites": (300, 300)}),
        panel(actions={"invite_all": (400, 400)}),
        panel(actions={"invite_all": (400, 400)}),  # 发送延迟不得重按。
        page(FriendPage.INVITE_CONFIRM, room_key="123456", actions={"confirm_invite": (500, 500)}),
        panel(invitation_sent=True, actions={"close_invites": (600, 600)}),
        host_room(member_count=1),
        host_room(member_count=2),
    ])
    result = machine.host_invite_all(timeout_s=10)
    assert result.member_count == 2
    assert adapter.clicks == [(100, 100), (200, 200), (300, 300), (400, 400), (500, 500), (600, 600)]
    assert ledger.status("task-1", "123456") == InviteStatus.SENT


def test_real_host_invite_button_opens_confirmation_then_grey_inviting_button_proves_sent():
    machine, adapter, ledger = flow([
        host_room(member_count=1, actions={"open_invites": (585, 647)}),
        page(FriendPage.INVITE_CONFIRM, actions={"confirm_invite": (770, 447)}),
        page(FriendPage.INVITE_CONFIRM, actions={"confirm_invite": (770, 447)}),
        host_room(member_count=1, invitation_sent=True),
        host_room(member_count=2, invitation_sent=True),
    ])
    result = machine.host_invite_all(timeout_s=3)
    assert result.member_count == 2
    assert adapter.clicks == [(585, 647), (770, 447)]
    assert ledger.status("task-1", "123456") == InviteStatus.SENT


def test_cancel_real_invite_confirmation_never_records_a_send_or_clicks_cancel():
    machine, adapter, ledger = flow([
        host_room(actions={"open_invites": (585, 647)}),
        page(FriendPage.CANCELLED, actions={"cancel": (510, 447)}),
    ])
    with pytest.raises(FriendRoomCancelled):
        machine.host_invite_all()
    assert adapter.clicks == [(585, 647)]
    assert ledger.status("task-1", "123456") == InviteStatus.NOT_SENT


def test_an_existing_invite_confirmation_cannot_invent_a_send_intent():
    machine, adapter, ledger = flow([
        page(FriendPage.INVITE_CONFIRM, room_key="123456", actions={"confirm_invite": (770, 447)}),
    ])
    with pytest.raises(FriendInviteUncertain, match="来源不明"):
        machine.host_invite_all()
    assert adapter.clicks == []
    assert ledger.status("task-1", "123456") == InviteStatus.NOT_SENT


def test_send_ledger_is_reserved_before_real_confirmation_input():
    machine, adapter, ledger = flow([
        host_room(actions={"open_invites": (585, 647)}),
        page(FriendPage.INVITE_CONFIRM, actions={"confirm_invite": (770, 447)}),
        host_room(member_count=2, invitation_sent=True),
    ])
    def inspect_send(adapter):
        expected = InviteStatus.NOT_SENT if adapter.clicks[-1] == (585, 647) else InviteStatus.SENDING
        assert ledger.status("task-1", "123456") == expected
    adapter.after_click = inspect_send
    machine.host_invite_all()


def test_sending_confirmation_cannot_be_inherited_by_another_flow():
    ledger = InviteLedger()
    first, adapter, _ = flow([
        host_room(actions={"open_invites": (585,647)}),
        page(FriendPage.INVITE_CONFIRM, actions={"confirm_invite": (770,447)}),
        host_room(member_count=2, invitation_sent=True),
    ], ledger=ledger)
    second, other, _ = flow([
        page(FriendPage.INVITE_CONFIRM, room_key="123456", actions={"confirm_invite": (770,447)}),
        host_room(member_count=2, invitation_sent=True),
    ], ledger=ledger)
    def reenter(current):
        if current.clicks[-1] == (770,447):
            with pytest.raises(FriendInviteUncertain):
                second.host_invite_all(timeout_s=1)
            assert ledger.status("task-1","123456") == InviteStatus.SENDING
    adapter.after_click = reenter
    first.host_invite_all()
    assert adapter.clicks.count((770,447)) == 1
    assert not other.clicks


def test_same_flow_reentry_cannot_repeat_confirmation_or_consume_outer_state():
    machine, adapter, ledger = flow([
        host_room(actions={"open_invites": (585,647)}),
        page(FriendPage.INVITE_CONFIRM, actions={"confirm_invite": (770,447)}),
        host_room(member_count=2, invitation_sent=True),
    ])
    def reenter(current):
        if current.clicks[-1] == (770,447):
            with pytest.raises(FriendInviteUncertain):
                machine.host_invite_all(timeout_s=1)
    adapter.after_click = reenter
    machine.host_invite_all()
    assert adapter.clicks.count((770,447)) == 1
    assert ledger.status("task-1","123456") == InviteStatus.SENT


def test_start_host_room_waits_for_member_and_delivers_start_to_song_selection():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    machine, adapter, _ = flow([
        host_room(member_count=1, actions={"start_room": (1050, 647)}),
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
        page(FriendPage.START_CONFIRM, actions={"confirm_start": (770, 447)}),
        page(FriendPage.UNKNOWN),
        page(FriendPage.ROOM_STARTED),
    ], ledger=ledger)
    result = machine.start_host_room(timeout_s=3)
    assert result.state == FriendPage.ROOM_STARTED
    assert adapter.clicks == [(1050, 647), (770, 447)]


def test_start_host_room_does_not_repeat_unknown_start_across_flow_instances():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    frames = [host_room(member_count=2, actions={"start_room": (1050, 647)})]
    first, adapter, _ = flow(frames, ledger=ledger)
    with pytest.raises(FriendRoomError, match="未确认进入选曲"):
        first.start_host_room(timeout_s=1)
    assert adapter.clicks == [(1050, 647)]
    second, again, _ = flow(frames, ledger=ledger)
    with pytest.raises(FriendRoomError, match="不会重复点击"):
        second.start_host_room(timeout_s=1)
    assert again.clicks == []


def test_two_successful_rounds_can_start_same_room_without_reinviting():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    machine, adapter, _ = flow([
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
        page(FriendPage.ROOM_STARTED),
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
        page(FriendPage.START_CONFIRM, actions={"confirm_start": (770, 447)}),
        page(FriendPage.ROOM_STARTED),
    ], ledger=ledger)
    machine.start_host_room()
    assert ledger.room_round("task-1", "123456") == 0
    assert machine.begin_next_round(completed_round_token="task-1:completed:1") == 1
    machine.start_host_room()
    assert adapter.clicks == [(1050, 647), (1050, 647), (770, 447)]
    assert ledger.status("task-1", "123456") == InviteStatus.SENT
    assert machine.begin_next_round(completed_round_token="task-1:completed:2") == 2


def test_next_round_start_confirmation_is_not_deduplicated_against_previous_round():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    frames = []
    for _ in range(2):
        frames.extend([
            host_room(member_count=2, actions={"start_room": (1050, 647)}),
            page(FriendPage.START_CONFIRM, actions={"confirm_start": (770, 447)}),
            page(FriendPage.ROOM_STARTED),
        ])
    machine, adapter, _ = flow(frames, ledger=ledger)
    machine.start_host_room()
    machine.begin_next_round(completed_round_token="task-1:completed:1")
    machine.start_host_room()
    assert adapter.clicks == [(1050, 647), (770, 447)] * 2


def test_completed_round_token_is_idempotent_and_cannot_skip_an_unstarted_round():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    machine, _, _ = flow([
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
        page(FriendPage.ROOM_STARTED),
    ], ledger=ledger)
    machine.start_host_room()
    assert machine.begin_next_round(completed_round_token="task-1:completed:1") == 1
    assert machine.begin_next_round(completed_round_token="task-1:completed:1") == 1
    with pytest.raises(FriendRoomError, match="本轮尚未确认"):
        machine.begin_next_round(completed_round_token="task-1:completed:2")
    assert ledger.room_round("task-1", "123456") == 1


def test_room_started_does_not_unlock_another_start_without_completed_round_token():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    machine, adapter, _ = flow([
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
        page(FriendPage.ROOM_STARTED),
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
    ], ledger=ledger)
    machine.start_host_room()
    with pytest.raises(FriendRoomError, match="不会重复点击"):
        machine.start_host_room()
    assert adapter.clicks == [(1050, 647)]


def test_uncertain_start_cannot_use_completion_token_to_release_failed_round():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    machine, adapter, _ = flow([
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
    ], ledger=ledger)
    with pytest.raises(FriendRoomError, match="未确认进入选曲"):
        machine.start_host_room(timeout_s=1)
    with pytest.raises(FriendRoomError, match="本轮尚未确认"):
        machine.begin_next_round(completed_round_token="task-1:completed:1")
    assert ledger.room_round("task-1", "123456") == 0
    assert adapter.clicks == [(1050, 647)]


def test_stop_before_next_round_keeps_old_round_start_reserved():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    machine, adapter, _ = flow([
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
        page(FriendPage.ROOM_STARTED),
    ], ledger=ledger)
    machine.start_host_room()
    adapter.stop = True
    with pytest.raises(InterruptedError):
        machine.begin_next_round(completed_round_token="task-1:completed:1")
    assert ledger.room_round("task-1", "123456") == 0
    assert ledger.room_start_attempted("task-1", "123456")


def test_round_token_does_not_unlock_another_room_or_task():
    ledger = InviteLedger()
    ledger.begin_room_start("task-1", "123456")
    ledger.mark_room_started("task-1", "123456")
    assert ledger.advance_room_round("task-1", "123456", "completion-1") == 1
    for task_id, room_key in [("task-2", "123456"), ("task-1", "654321")]:
        with pytest.raises(FriendRoomError, match="本轮尚未确认"):
            ledger.advance_room_round(task_id, room_key, "completion-1")
        assert ledger.room_round(task_id, room_key) == 0


@pytest.mark.parametrize("completed_round_token", ["", "  ", None, 1])
def test_next_round_requires_nonempty_completion_token(completed_round_token):
    machine, _, _ = flow([])
    machine.room_key = "123456"
    with pytest.raises(ValueError, match="完成凭据"):
        machine.begin_next_round(completed_round_token=completed_round_token)


def test_start_requires_confirmed_invite_and_cannot_adopt_unrelated_song_page():
    machine, adapter, _ = flow([host_room(member_count=2, actions={"start_room": (1050, 647)})])
    with pytest.raises(FriendRoomError, match="尚未确认"):
        machine.start_host_room()
    assert adapter.clicks == []
    machine, adapter, _ = flow([page(FriendPage.ROOM_STARTED)])
    with pytest.raises(FriendRoomError, match="缺少本任务"):
        machine.start_host_room()


def test_start_member_wait_is_stoppable_without_pressing_start_or_cancel():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    machine, adapter, _ = flow([
        host_room(member_count=1, actions={"start_room": (1050, 647), "cancel": (510, 447)}),
    ], ledger=ledger)
    adapter.sleep = lambda _seconds: setattr(adapter, "stop", True)
    with pytest.raises(InterruptedError):
        machine.start_host_room()
    assert adapter.clicks == []


def test_stop_after_start_click_never_presses_start_confirmation():
    ledger = InviteLedger()
    ledger.mark_sent("task-1", "123456")
    machine, adapter, _ = flow([
        host_room(member_count=2, actions={"start_room": (1050, 647)}),
        page(FriendPage.START_CONFIRM, actions={"confirm_start": (770, 447), "cancel": (510, 447)}),
    ], ledger=ledger)
    adapter.after_click = lambda adapter: setattr(adapter, "stop", True)
    with pytest.raises(InterruptedError):
        machine.start_host_room()
    assert adapter.clicks == [(1050, 647)]


def test_all_friends_is_not_restricted_to_a_single_friend():
    machine, adapter, _ = flow([
        panel(actions={"invite_all": (400, 400)}),
        panel(invitation_sent=True, actions={"close_invites": (600, 600)}),
        host_room(member_count=4),
    ])
    assert machine.host_invite_all(expected_members=4).member_count == 4
    assert adapter.clicks == [(400, 400), (600, 600)]


def test_already_sent_ui_does_not_send_again():
    machine, adapter, ledger = flow([
        panel(invitation_sent=True, actions={"invite_all": (400, 400), "close_invites": (600, 600)}),
        host_room(member_count=2),
    ])
    machine.host_invite_all()
    assert adapter.clicks == [(600, 600)]
    assert ledger.status("task-1", "123456") == InviteStatus.SENT


def test_sent_ledger_survives_flow_reentry_and_guest_timeout():
    ledger = InviteLedger()
    first, _, _ = flow([
        panel(invitation_sent=True, actions={"close_invites": (600, 600)}),
        host_room(member_count=1),
    ], ledger=ledger)
    with pytest.raises(FriendRoomError, match="等待好友加入"):
        first.host_invite_all(timeout_s=1)
    assert ledger.status("task-1", "123456") == InviteStatus.SENT
    second, adapter, _ = flow([host_room(member_count=2, actions={"open_invites": (300, 300)})], ledger=ledger)
    second.host_invite_all()
    assert adapter.clicks == []


def test_send_timeout_is_uncertain_and_reentry_cannot_repeat():
    ledger = InviteLedger()
    first, adapter, _ = flow([panel(actions={"invite_all": (400, 400)})], ledger=ledger)
    with pytest.raises(FriendInviteUncertain, match="结果不明"):
        first.host_invite_all(timeout_s=1)
    assert adapter.clicks == [(400, 400)]
    assert ledger.status("task-1", "123456") == InviteStatus.UNCERTAIN
    second, again, _ = flow([panel(actions={"invite_all": (400, 400)})], ledger=ledger)
    with pytest.raises(FriendInviteUncertain):
        second.host_invite_all()
    assert again.clicks == []


def test_uncertain_send_can_only_be_resolved_by_explicit_sent_evidence():
    ledger = InviteLedger()
    ledger.begin("task-1", "123456")
    ledger.finish("task-1", "123456", InviteStatus.UNCERTAIN)
    machine, adapter, _ = flow([
        panel(invitation_sent=True, actions={"close_invites": (600, 600)}),
        host_room(member_count=2),
    ], ledger=ledger)
    machine.host_invite_all()
    assert adapter.clicks == [(600, 600)]
    assert ledger.status("task-1", "123456") == InviteStatus.SENT


def test_disappearing_invite_panel_is_not_send_success():
    machine, adapter, ledger = flow([
        panel(actions={"invite_all": (400, 400)}),
        host_room(member_count=2, actions={"open_invites": (300, 300)}),
    ])
    with pytest.raises(FriendInviteUncertain):
        machine.host_invite_all(timeout_s=1)
    assert adapter.clicks == [(400, 400)]
    assert ledger.status("task-1", "123456") == InviteStatus.UNCERTAIN


def test_send_click_exception_does_not_allow_retry():
    machine, adapter, ledger = flow([panel(actions={"invite_all": (400, 400)})])
    adapter.after_click = lambda _adapter: (_ for _ in ()).throw(RuntimeError("controller disconnected"))
    with pytest.raises(RuntimeError, match="controller disconnected"):
        machine.host_invite_all()
    assert ledger.status("task-1", "123456") == InviteStatus.UNCERTAIN


@pytest.mark.parametrize("state,status,exception", [
    (FriendPage.ERROR, InviteStatus.FAILED, FriendRoomError),
    (FriendPage.CANCELLED, InviteStatus.CANCELLED, FriendRoomCancelled),
])
def test_explicit_send_failure_or_cancel_is_terminal(state, status, exception):
    machine, adapter, ledger = flow([
        panel(actions={"invite_all": (400, 400)}),
        page(state, error="邀请已取消或发送失败", actions={"invite_all": (400, 400), "cancel": (900, 500)}),
    ])
    with pytest.raises(exception, match="取消或发送失败"):
        machine.host_invite_all()
    assert adapter.clicks == [(400, 400)]
    assert ledger.status("task-1", "123456") == status
    with pytest.raises(FriendRoomError):
        ledger.begin("task-1", "123456")


def test_room_key_must_be_known_before_inviting():
    machine, adapter, _ = flow([page(FriendPage.INVITE_PANEL, actions={"invite_all": (400, 400)})])
    with pytest.raises(FriendRoomError, match="明确房间标识"):
        machine.host_invite_all()
    assert adapter.clicks == []


def test_changing_room_during_send_cannot_invite_the_new_room():
    machine, adapter, ledger = flow([
        panel(actions={"invite_all": (400, 400)}),
        page(FriendPage.INVITE_PANEL, room_key="654321", actions={"invite_all": (400, 400)}),
    ])
    with pytest.raises(FriendRoomError, match="房间标识改变"):
        machine.host_invite_all()
    assert adapter.clicks == [(400, 400)]
    assert ledger.status("task-1", "123456") == InviteStatus.UNCERTAIN
    assert ledger.status("task-1", "654321") == InviteStatus.NOT_SENT


def test_host_room_closes_without_automatically_creating_another():
    machine, adapter, _ = flow([
        host_room(actions={"open_invites": (300, 300)}),
        page(FriendPage.ROOM_SELECTION, actions={"create_room": (100, 100)}),
    ])
    with pytest.raises(FriendRoomError, match="房间已关闭"):
        machine.host_invite_all()
    assert adapter.clicks == [(300, 300)]


def test_stable_page_never_repeats_create_or_open_invites():
    machine, adapter, _ = flow([page(FriendPage.ROOM_SELECTION, actions={"create_room": (100, 100)})])
    with pytest.raises(FriendRoomError, match="超时"):
        machine.host_invite_all(timeout_s=1)
    assert adapter.clicks == [(100, 100)]


def test_guest_enters_unique_invitation_and_requires_real_lobby():
    machine, adapter, _ = flow([
        page(FriendPage.ROOM_SELECTION, actions={"open_search": (100, 100)}),
        page(FriendPage.SEARCH_METHOD, actions={"open_friend_list": (200, 200)}),
        page(FriendPage.GUEST_INVITES, valid_invitation_count=0),
        page(FriendPage.GUEST_INVITES, room_key="123456", valid_invitation_count=1, actions={"join_invitation": (300, 300)}),
        page(FriendPage.UNKNOWN),
        page(FriendPage.JOIN_CONFIRM, actions={"confirm_join": (400, 400)}),
        page(FriendPage.GUEST_ROOM, room_key="123456", member_count=2),
    ])
    result = machine.join_friend_invitation(timeout_s=5)
    assert result.room_key == "123456"
    assert adapter.clicks == [(100, 100), (200, 200), (300, 300), (400, 400)]


def test_guest_multiple_valid_invitations_fails_without_clicking_first_row():
    machine, adapter, _ = flow([
        page(FriendPage.GUEST_INVITES, valid_invitation_count=2, actions={"join_invitation": (300, 300)}),
    ])
    with pytest.raises(FriendRoomError, match="多条有效好友邀请"):
        machine.join_friend_invitation()
    assert adapter.clicks == []


def test_guest_invalid_or_disabled_row_does_not_join():
    machine, adapter, _ = flow([
        page(FriendPage.GUEST_INVITES, valid_invitation_count=0, actions={"join_invitation": (300, 300)}),
    ])
    with pytest.raises(FriendRoomError, match="未出现唯一有效"):
        machine.join_friend_invitation(timeout_s=1)
    assert adapter.clicks == []


def test_guest_unique_count_without_same_row_button_does_not_join():
    machine, adapter, _ = flow([page(FriendPage.GUEST_INVITES, valid_invitation_count=1)])
    with pytest.raises(FriendRoomError, match="未出现唯一有效"):
        machine.join_friend_invitation(timeout_s=1)
    assert adapter.clicks == []


def test_guest_departure_from_list_alone_cannot_report_success_or_reclick():
    machine, adapter, _ = flow([
        page(FriendPage.GUEST_INVITES, valid_invitation_count=1, actions={"join_invitation": (300, 300)}),
        page(FriendPage.UNKNOWN),
    ])
    with pytest.raises(FriendRoomError, match="未确认进入房间"):
        machine.join_friend_invitation(timeout_s=1)
    assert adapter.clicks == [(300, 300)]


@pytest.mark.parametrize("state,error", [
    (FriendPage.ERROR, "房间已满"),
    (FriendPage.CANCELLED, "用户取消加入"),
    (FriendPage.ROOM_SELECTION, ""),
])
def test_guest_failed_expired_or_cancelled_invite_never_retries(state, error):
    machine, adapter, _ = flow([
        page(FriendPage.GUEST_INVITES, valid_invitation_count=1, actions={"join_invitation": (300, 300)}),
        page(state, error=error, actions={"open_search": (100, 100), "confirm_join": (400, 400)}),
    ])
    with pytest.raises(FriendRoomError):
        machine.join_friend_invitation()
    assert adapter.clicks == [(300, 300)]


@pytest.mark.parametrize("room_key", ["", "654321"])
def test_guest_lobby_requires_matching_room_identity(room_key):
    machine, adapter, _ = flow([
        page(FriendPage.GUEST_INVITES, room_key="123456", valid_invitation_count=1, actions={"join_invitation": (300, 300)}),
        page(FriendPage.GUEST_ROOM, room_key=room_key),
    ])
    with pytest.raises(FriendRoomError):
        machine.join_friend_invitation()
    assert adapter.clicks == [(300, 300)]


def test_guest_does_not_adopt_unrelated_existing_room():
    machine, adapter, _ = flow([page(FriendPage.GUEST_ROOM, room_key="123456")])
    with pytest.raises(FriendRoomError, match="未确认接受好友邀请"):
        machine.join_friend_invitation()
    assert adapter.clicks == []


@pytest.mark.parametrize("method", ["host_invite_all", "join_friend_invitation"])
@pytest.mark.parametrize("stop_point", ["before", "after_observe", "after_click", "during_wait"])
def test_stop_has_priority_and_never_clicks_cancel_or_continues(method, stop_point):
    frame = (panel(actions={"invite_all": (400, 400), "cancel": (900, 500)})
             if method == "host_invite_all" else
             page(FriendPage.GUEST_INVITES, valid_invitation_count=1,
                  actions={"join_invitation": (300, 300), "cancel": (900, 500)}))
    machine, adapter, ledger = flow([frame])
    if stop_point == "before":
        adapter.stop = True
    elif stop_point == "after_observe":
        adapter.after_observe = lambda adapter: setattr(adapter, "stop", True)
    elif stop_point == "after_click":
        adapter.after_click = lambda adapter: setattr(adapter, "stop", True)
    else:
        def sleep(_seconds):
            adapter.stop = True
        adapter.sleep = sleep
    with pytest.raises(InterruptedError):
        getattr(machine, method)(timeout_s=1)
    assert len(adapter.clicks) == (1 if stop_point in {"after_click", "during_wait"} else 0)
    assert (900, 500) not in adapter.clicks
    if stop_point == "before":
        assert adapter.observations == 0
    if method == "host_invite_all" and adapter.clicks:
        assert ledger.status("task-1", "123456") == InviteStatus.UNCERTAIN


def test_ledger_scopes_invites_to_both_task_and_actual_room():
    ledger = InviteLedger()
    assert ledger.begin("task-1", "123456") is True
    with pytest.raises(FriendInviteUncertain):
        ledger.begin("task-1", "123456")
    assert ledger.begin("task-2", "123456") is True
    assert ledger.begin("task-1", "654321") is True
    ledger.mark_sent("task-1", "123456")
    assert ledger.begin("task-1", "123456") is False


@pytest.mark.parametrize("point", [(1280, 20), (10, 720), (-1, 20), (1.2, 20), (True, 20), (10,)])
def test_invalid_action_coordinates_cannot_generate_input(point):
    machine, adapter, _ = flow([panel(actions={"invite_all": point})])
    with pytest.raises(FriendRoomError, match="按钮证据无效"):
        machine.host_invite_all()
    assert adapter.clicks == []


@pytest.mark.parametrize("task_id", ["", " ", None, 1])
def test_host_requires_real_task_identity(task_id):
    with pytest.raises(ValueError, match="任务 ID"):
        flow([], task_id=task_id)
