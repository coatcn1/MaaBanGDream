import math

import pytest

from agent.realtime.native_feedback_ticket import FeedbackTicketStream
from agent.realtime import native_engine
from agent.realtime.native_play import NativeMinitouchBackend
from test_native_random_great import ENABLED, write_chart
from types import SimpleNamespace


def receipt(token, at, *, intentional=False, trusted=True, eligible=True):
    return dict(action_token=token, planned_engine_s=at, command="d", action_kind="tap",
        feedback_single_tap=eligible and not intentional, feedback_identity_trusted=trusted,
        intentional_random_great=intentional)


def commit(stream, row, device_s):
    stream.publish_receipts([row], final_chunk=True)
    stream.observe_commit([row], 100., 1, 0., device_actual_s=device_s)
    return stream.snapshot().ticket


@pytest.mark.parametrize("elapsed", [.11797, .16575, .26509, .299999])
def test_actual_intentional_execution_blocks_ordinary_tickets_for_less_than_300ms(elapsed):
    stream = FeedbackTicketStream("guard", random_great_feedback_guard_enabled=True)
    assert commit(stream, receipt(1, 10., intentional=True), 1000.) is None
    assert commit(stream, receipt(2, 11.), 1000. + elapsed) is None
    assert stream.events[-1]["event"] == "random-great-feedback-guard"
    assert stream.snapshot().judgement_sequence == 2


@pytest.mark.parametrize("elapsed", [.3, .300001, 1.29])
def test_exact_300ms_and_later_allow_tickets_despite_host_receipt_time(elapsed):
    stream = FeedbackTicketStream("guard", random_great_feedback_guard_enabled=True)
    commit(stream, receipt(1, 10., intentional=True), 1000.)
    assert commit(stream, receipt(2, 10.), 1000. + elapsed) is not None


def test_published_random_plan_is_insufficient_until_actual_execution():
    stream = FeedbackTicketStream("guard", random_great_feedback_guard_enabled=True)
    stream.publish_receipts([receipt(1, 10., intentional=True)])
    assert commit(stream, receipt(2, 10.1), 1000.1) is not None
    commit(stream, receipt(1, 10., intentional=True), 1000.2)
    assert commit(stream, receipt(3, 10.6), 1000.3) is None
    assert commit(stream, receipt(4, 10.7), 1000.5) is not None


@pytest.mark.parametrize("enabled", [False, True])
def test_untrusted_intentional_marker_and_default_false_eligibility_never_start_guard(enabled):
    stream = FeedbackTicketStream("guard", random_great_feedback_guard_enabled=enabled)
    commit(stream, receipt(1, 10., intentional=True, trusted=False), 1000.)
    commit(stream, receipt(2, 10.1, eligible=False), 1000.1)
    assert commit(stream, receipt(3, 10.2), 1000.2) is not None
    assert stream.report()["random_great_feedback_guard"]["last_intentional_device_us"] is None


def test_disabled_guard_keeps_original_tickets_and_new_stream_resets_guard():
    disabled = FeedbackTicketStream("old")
    commit(disabled, receipt(1, 10., intentional=True), None)
    assert commit(disabled, receipt(2, 10.01), None) is not None
    enabled = FeedbackTicketStream("guard", random_great_feedback_guard_enabled=True)
    commit(enabled, receipt(1, 10., intentional=True), 1000.)
    assert commit(enabled, receipt(2, 10.01), 1000.01) is None
    reset = FeedbackTicketStream("next", random_great_feedback_guard_enabled=True)
    assert commit(reset, receipt(1, 10.01), 10.01) is not None


@pytest.mark.parametrize("invalid", [None, math.nan, math.inf, True, "1000", 999.9])
def test_missing_or_backward_device_time_stops_feedback_until_new_stream(invalid):
    stream = FeedbackTicketStream("guard", random_great_feedback_guard_enabled=True)
    commit(stream, receipt(1, 10., intentional=True), 1000.)
    assert commit(stream, receipt(2, 11.), invalid) is None
    assert stream.events[-1]["event"].startswith("random-great-feedback-clock-")
    assert commit(stream, receipt(3, 12.), 1002.) is None
    assert stream.report()["random_great_feedback_guard"]["clock_error"] is not None


def test_a_later_intentional_execution_extends_only_future_guard_window():
    stream = FeedbackTicketStream("guard", random_great_feedback_guard_enabled=True)
    commit(stream, receipt(1, 10., intentional=True), 1000.)
    assert commit(stream, receipt(2, 10.3), 1000.3)
    commit(stream, receipt(3, 10.4, intentional=True), 1000.4)
    assert commit(stream, receipt(4, 10.6), 1000.6) is None
    assert commit(stream, receipt(5, 10.7), 1000.7)
    assert stream.summary["ticket-created"] == 2


@pytest.mark.parametrize("elapsed, allowed", [(.299999, False), (.3, True), (.300001, True)])
def test_exact_guard_boundary_at_real_jlog_epoch(elapsed, allowed):
    actual = 1794928068.0550568
    stream = FeedbackTicketStream("epoch", random_great_feedback_guard_enabled=True)
    commit(stream, receipt(1, 10., intentional=True), actual)
    assert (commit(stream, receipt(2, 11.), actual + elapsed) is not None) is allowed


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_guard_configuration_requires_an_explicit_boolean(value):
    with pytest.raises(ValueError, match="布尔值"):
        FeedbackTicketStream("invalid", random_great_feedback_guard_enabled=value)


@pytest.mark.skipif(not native_engine.available(), reason="Native 未构建")
@pytest.mark.parametrize("old_choice", [False, True, "ignored", None])
@pytest.mark.parametrize("mode", ["cooperative", "versus"])
def test_direct_backend_automatically_enables_guard_and_ignores_old_choice(tmp_path, old_choice, mode):
    options = ENABLED | {"native_random_great_feedback_guard_enabled": old_choice}
    backend = NativeMinitouchBackend(write_chart(tmp_path), adb_path="unused", serial="unused",
        device=SimpleNamespace(request_reset=lambda: True), life_feedback_enabled=True,
        random_great_options=options, start_gate_mode=mode, expected_notes=100, run_id="frozen")
    report = backend._feedback_tickets.report()["random_great_feedback_guard"]
    assert report["enabled"] is True
    plan = backend._random_great_report.copy()
    options["native_random_great_feedback_guard_enabled"] = False
    assert backend._feedback_tickets.report()["random_great_feedback_guard"] == report
    assert backend._random_great_report == plan
