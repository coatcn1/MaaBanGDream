// Native 滚动播放会话的确定性测试：使用假时钟与假传输，避免依赖真机。

#include <cmath>
#include <filesystem>
#include <string>
#include <utility>
#include <vector>

#include "maabangdream/playback_session.hpp"
#include "maabangdream/pure_chart.hpp"
#include "maabangdream/touch_script.hpp"
#include "test_macros.hpp"

namespace {

using namespace mbdr;

ScheduledAction tap(double due_s, uint8_t lane = 0) {
    ScheduledAction result;
    result.kind = ActionKind::Tap;
    result.due_s = due_s;
    result.lane = lane;
    return result;
}

ScheduledAction counted_action(
    ActionKind kind,
    double due_s,
    int note_index,
    int8_t contact = -1) {
    ScheduledAction result;
    result.kind = kind;
    result.due_s = due_s;
    result.note_index = note_index;
    result.contact = contact;
    return result;
}

MinitouchLogEvent log_event(double start_ms, double end_ms,
                            const std::string& command) {
    MinitouchLogEvent result;
    result.start_ms = start_ms;
    result.end_ms = end_ms;
    result.cost_ms = end_ms - start_ms;
    result.command = command;
    return result;
}

std::vector<int> down_times_ms(const std::vector<std::string>& scripts) {
    std::vector<int> result;
    int cursor_ms = 0;
    for (const std::string& script : scripts) {
        std::size_t begin = 0;
        while (begin < script.size()) {
            const std::size_t end = script.find('\n', begin);
            const std::string line = script.substr(
                begin,
                end == std::string::npos
                    ? std::string::npos
                    : end - begin);
            if (line.rfind("w ", 0) == 0) {
                cursor_ms += std::stoi(line.substr(2));
            } else if (line.rfind("d ", 0) == 0) {
                result.push_back(cursor_ms);
            }
            if (end == std::string::npos) {
                break;
            }
            begin = end + 1;
        }
    }
    return result;
}

std::vector<ScheduledAction> materialize_future_down_reservations(
    const PlaybackChunk& chunk) {
    std::vector<ScheduledAction> result;
    result.reserve(chunk.future_down_reservations.size());
    for (const TimedPlaybackAction& timed
         : chunk.future_down_reservations) {
        ScheduledAction action = timed.action;
        action.due_s = timed.engine_due_s;
        result.push_back(std::move(action));
    }
    return result;
}

struct Fixture {
    double now_s = 0.0;
    bool publish_ok = true;
    bool reset_ack = false;
    bool fallback_ok = true;
    int reset_requests = 0;
    int fallback_requests = 0;
    std::vector<PlaybackChunk> chunks;

    PlaybackCallbacks callbacks() {
        PlaybackCallbacks result;
        result.clock = [this]() { return now_s; };
        result.publish = [this](const PlaybackChunk& chunk) {
            chunks.push_back(chunk);
            return publish_ok;
        };
        result.request_reset = [this]() {
            ++reset_requests;
            return reset_ack;
        };
        result.fallback_stop = [this]() {
            ++fallback_requests;
            return fallback_ok;
        };
        return result;
    }
};

void test_absolute_windows_respect_watermarks_and_final_marker() {
    Fixture fixture;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(0.00), tap(0.49), tap(0.80), tap(1.00)},
                      EngineConfig{}));
    CHECK(session.state() == PlaybackState::Armed);
    CHECK(session.start(0.0));

    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(1));
    CHECK(std::abs(fixture.chunks[0].window_start_s - 0.0) < 1e-9);
    // 中间块保持名义 500ms 边界，0.49s TAP 的 UP 由编译器留到下一块。
    CHECK(std::abs(fixture.chunks[0].window_end_s - 0.5) < 1e-9);
    CHECK_EQ(fixture.chunks[0].actions.size(), static_cast<std::size_t>(2));
    CHECK(!fixture.chunks[0].final_chunk);
    CHECK(!session.publish());  // 队列仍高于 200ms 低水位。

    fixture.now_s = 0.31;
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(2));
    CHECK(std::abs(fixture.chunks[1].window_start_s - 0.5) < 1e-9);
    CHECK(std::abs(fixture.chunks[1].window_end_s - 0.81) < 1e-9);
    CHECK_EQ(fixture.chunks[1].actions.size(), static_cast<std::size_t>(1));
    CHECK(!fixture.chunks[1].final_chunk);

    fixture.now_s = 0.62;
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(3));
    CHECK(std::abs(fixture.chunks[2].window_start_s - 0.81) < 1e-9);
    CHECK(std::abs(fixture.chunks[2].window_end_s - 1.05) < 1e-9);
    CHECK_EQ(fixture.chunks[2].actions.size(), static_cast<std::size_t>(1));
    CHECK(fixture.chunks[2].final_chunk);

    const PlaybackReport report = session.report();
    CHECK_EQ(report.planned_actions, static_cast<uint64_t>(4));
    CHECK_EQ(report.sent_actions, static_cast<uint64_t>(4));
    CHECK_EQ(report.chunks, static_cast<uint64_t>(3));
    CHECK_EQ(report.queue_underflows, static_cast<uint64_t>(0));
    CHECK(report.max_queue_depth_ms <= 750.0 + 1e-6);
    CHECK(session.finish("song completed"));
    CHECK(session.state() == PlaybackState::Finished);
}

void test_sparse_timeline_emits_empty_wait_window() {
    Fixture fixture;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(0.0), tap(2.0)}, EngineConfig{}));
    CHECK(session.start(0.0));
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(1));
    CHECK_EQ(fixture.chunks[0].actions.size(), static_cast<std::size_t>(1));
    CHECK(!fixture.chunks[0].final_chunk);
    CHECK(std::abs(fixture.chunks[0].window_end_s - 0.5) < 1e-9);
    fixture.now_s = 0.31;
    CHECK(session.publish());
    CHECK(fixture.chunks[1].actions.empty());
}

void test_chunk_reserves_future_hold_down_without_sending_it() {
    Fixture fixture;
    PlaybackSession session(fixture.callbacks());
    const ScheduledAction anchor = counted_action(ActionKind::Down, 0.0, 1, 0);
    const ScheduledAction anchor_up = counted_action(ActionKind::Up, 0.1, 1, 0);
    const ScheduledAction flick = counted_action(ActionKind::Flick, 0.49, 2);
    const ScheduledAction future_down = counted_action(
        ActionKind::Down, 0.55, 3, 7);
    const ScheduledAction future_up = counted_action(
        ActionKind::Up, 0.75, 3, 7);
    CHECK(session.arm(
        {anchor, anchor_up, flick, future_down, future_up},
        EngineConfig{}));
    CHECK(session.start(0.0));
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(1));
    CHECK_EQ(fixture.chunks[0].actions.size(), static_cast<std::size_t>(3));
    CHECK_EQ(fixture.chunks[0].future_down_reservations.size(),
        static_cast<std::size_t>(1));
    CHECK(fixture.chunks[0].future_down_reservations[0].action.kind
        == ActionKind::Down);
    CHECK_EQ(fixture.chunks[0].future_down_reservations[0].action.contact, 7);
    CHECK(std::abs(
        fixture.chunks[0].future_down_reservations[0].engine_due_s - 0.55)
        < 1e-9);
    CHECK_EQ(session.report().sent_actions, static_cast<uint64_t>(3));

    fixture.now_s = 0.31;
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(2));
    CHECK_EQ(fixture.chunks[1].actions.size(), static_cast<std::size_t>(2));
    CHECK(fixture.chunks[1].actions[0].action.kind == ActionKind::Down);
    CHECK_EQ(session.report().sent_actions, static_cast<uint64_t>(5));
}

void test_start_anchor_maps_first_action_and_preserves_relative_timing() {
    Fixture fixture;
    fixture.now_s = 9.5;
    PlaybackSession session(fixture.callbacks());
    EngineConfig config;
    config.song_offset_s = -6.0;
    config.press_bias_ms = 30;
    config.max_wait_ms = 120;
    CHECK(session.arm({tap(4.0), tap(6.0)}, config));
    CHECK(session.start(9.97));
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks[0].actions.size(), static_cast<std::size_t>(1));
    CHECK(std::abs(fixture.chunks[0].actions[0].engine_due_s - 9.97) < 1e-9);
    CHECK(std::abs(fixture.chunks[0].touch_config.song_offset_s) < 1e-9);
    CHECK_EQ(fixture.chunks[0].touch_config.press_bias_ms, 0);
    CHECK_EQ(fixture.chunks[0].touch_config.max_wait_ms, 120);
    CHECK(!fixture.chunks[0].final_chunk);
    fixture.now_s = 11.6;
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks[1].actions.size(), static_cast<std::size_t>(1));
    CHECK(std::abs(fixture.chunks[1].actions[0].engine_due_s - 11.97) < 1e-9);
}

void test_large_monotonic_clock_does_not_make_relative_chart_overdue() {
    Fixture fixture;
    fixture.now_s = 1000.0;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(3.5), tap(4.0), tap(10.0)}, EngineConfig{}));
    CHECK(session.start(1000.03));
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(1));
    CHECK_EQ(fixture.chunks[0].actions.size(), static_cast<std::size_t>(1));
    CHECK(std::abs(fixture.chunks[0].actions[0].engine_due_s - 1000.03)
          < 1e-9);
    CHECK(!fixture.chunks[0].final_chunk);
    CHECK(session.report().sent_actions < session.report().planned_actions);
    CHECK(std::abs(session.report().chart_first_due_s - 3.5) < 1e-9);
    CHECK(std::abs(session.report().first_action_engine_s - 1000.03) < 1e-9);
}

void test_probe_samples_are_reset_at_start() {
    Fixture fixture;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(0.1)}, EngineConfig{}));
    session.observe_minitouch_log(log_event(0.0, 0.2, "d 0 1 2 50"));
    session.observe_minitouch_log(log_event(0.3, 0.4, "c"));
    CHECK_EQ(session.calibration_event_count(), 2);
    CHECK(session.start(0.0));
    CHECK_EQ(session.calibration_event_count(), 0);
    CHECK_EQ(session.report().probe_events, static_cast<uint64_t>(2));
}

void test_underflow_is_counted_once_per_gap() {
    Fixture fixture;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(0.0), tap(2.0)}, EngineConfig{}));
    CHECK(session.start(0.0));
    CHECK(session.publish());  // [0.0, 0.5]

    fixture.now_s = 0.8;
    CHECK(session.publish());  // 队列已经断粮，补到 1.3。
    CHECK_EQ(session.report().queue_underflows, static_cast<uint64_t>(1));
    CHECK(!session.publish());
    CHECK_EQ(session.report().queue_underflows, static_cast<uint64_t>(1));

    fixture.now_s = 1.6;
    CHECK(session.publish());
    CHECK_EQ(session.report().queue_underflows, static_cast<uint64_t>(2));
}

void test_publish_failure_is_terminal_and_does_not_count_sent_actions() {
    Fixture fixture;
    fixture.publish_ok = false;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(0.1)}, EngineConfig{}));
    CHECK(session.start(0.0));
    CHECK(!session.publish());
    CHECK(session.state() == PlaybackState::Failed);
    CHECK_EQ(session.report().sent_actions, static_cast<uint64_t>(0));
    CHECK(session.report().terminal_reason == "chunk publish failed");
}

void test_execution_report_uses_absolute_drift_percentiles() {
    Fixture fixture;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(0.1), tap(0.2), tap(0.3)}, EngineConfig{}));
    CHECK(session.start(0.1));
    CHECK(session.publish());
    CHECK(session.observe_execution(0.1, 0.098));
    CHECK(session.observe_execution(0.2, 0.204));
    CHECK(session.observe_execution(0.3, 0.308));
    CHECK(!session.observe_execution(0.4, 0.4));
    const PlaybackReport report = session.report();
    CHECK_EQ(report.executed_actions, static_cast<uint64_t>(3));
    CHECK(std::abs(report.drift_p50_ms - 4.0) < 1e-9);
    CHECK(std::abs(report.drift_p95_ms - 8.0) < 1e-9);
    CHECK(std::abs(report.drift_max_ms - 8.0) < 1e-9);
}

void test_cancel_acknowledgement_finishes_without_fallback() {
    Fixture fixture;
    fixture.reset_ack = true;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(1.0)}, EngineConfig{}));
    CHECK(session.start(0.0));
    CHECK(session.cancel("user stop"));
    CHECK(session.state() == PlaybackState::Cancelled);
    CHECK_EQ(fixture.reset_requests, 1);
    CHECK_EQ(fixture.fallback_requests, 0);
    CHECK(session.report().terminal_reason == "user stop");
    CHECK(session.report().stop_latency_ms <= 1e-9);
}

void test_cancel_uses_fallback_after_100ms() {
    Fixture fixture;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(1.0)}, EngineConfig{}));
    CHECK(session.start(0.0));
    CHECK(session.cancel("life abort"));
    CHECK(session.state() == PlaybackState::Cancelling);

    fixture.now_s = 0.099;
    CHECK(session.poll() == PlaybackState::Cancelling);
    CHECK_EQ(fixture.fallback_requests, 0);
    fixture.now_s = 0.101;
    CHECK(session.poll() == PlaybackState::Cancelled);
    CHECK_EQ(fixture.fallback_requests, 1);
    CHECK(session.report().fallback_used);
    CHECK(session.report().stop_latency_ms <= 500.0);
}

void test_cancel_deadline_failure_is_bounded() {
    Fixture fixture;
    fixture.fallback_ok = false;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm({tap(1.0)}, EngineConfig{}));
    CHECK(session.start(0.0));
    CHECK(session.cancel("transport stalled"));
    fixture.now_s = 0.101;
    CHECK(session.poll() == PlaybackState::Cancelling);
    CHECK_EQ(fixture.fallback_requests, 1);
    fixture.now_s = 0.501;
    CHECK(session.poll() == PlaybackState::Failed);
    CHECK(session.report().terminal_reason == "cancel deadline exceeded");
    CHECK(session.report().stop_latency_ms >= 500.0);
}

void test_120_second_virtual_run_has_no_underflow() {
    Fixture fixture;
    fixture.now_s = 1000.0;
    std::vector<ScheduledAction> actions;
    actions.reserve(1201);
    for (int index = 0; index <= 1200; ++index) {
        actions.push_back(tap(static_cast<double>(index) / 10.0,
                              static_cast<uint8_t>(index % 7)));
    }
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm(std::move(actions), EngineConfig{}));
    CHECK(session.start(1000.03));

    // 10ms 主机轮询覆盖完整 120 秒；无论动作密度如何，设备前瞻保持在
    // 200--500ms 范围内，不应依赖真实 sleep 或调度器运气。
    for (int tick = 0; tick <= 12050; ++tick) {
        fixture.now_s = 1000.0 + static_cast<double>(tick) / 100.0;
        session.publish();
    }
    const PlaybackReport report = session.report();
    CHECK_EQ(report.planned_actions, static_cast<uint64_t>(1201));
    CHECK_EQ(report.sent_actions, static_cast<uint64_t>(1201));
    CHECK_EQ(report.queue_underflows, static_cast<uint64_t>(0));
    CHECK(report.max_queue_depth_ms <= 500.0 + 1e-6);
    CHECK(session.finish("virtual song completed"));
}

void test_chunks_feed_touch_compiler_with_absolute_windows() {
    double now_s = 0.0;
    TouchScriptCompiler compiler;
    std::vector<PlaybackChunk> chunks;
    std::vector<std::string> scripts;
    PlaybackCallbacks callbacks;
    callbacks.clock = [&]() { return now_s; };
    callbacks.publish = [&](const PlaybackChunk& chunk) {
        chunks.push_back(chunk);
        const auto lines = compiler.compile(
            materialize_playback_actions(chunk),
            chunk.touch_config,
            chunk.window_start_s,
            chunk.final_chunk,
            chunk.window_end_s,
            materialize_future_down_reservations(chunk));
        std::string text;
        for (const std::string& line : lines) {
            text += line;
        }
        scripts.push_back(std::move(text));
        return true;
    };

    ScheduledAction down;
    down.kind = ActionKind::Down;
    down.contact = 2;
    down.lane = 1;
    down.due_s = 0.0;
    ScheduledAction move = down;
    move.kind = ActionKind::Move;
    move.lane = 3;
    move.due_s = 1.0;
    ScheduledAction up = move;
    up.kind = ActionKind::Up;
    up.due_s = 2.0;

    PlaybackSession session(std::move(callbacks));
    CHECK(session.arm({down, move, up}, EngineConfig{}));
    CHECK(session.start(0.03));
    CHECK(session.publish());
    for (int step = 1; step <= 8
         && session.report().sent_actions < session.report().planned_actions;
         ++step) {
        now_s = static_cast<double>(step) * 0.31;
        session.publish();
    }
    CHECK_EQ(session.report().sent_actions, static_cast<uint64_t>(3));
    CHECK(!chunks.empty());
    CHECK(chunks.back().final_chunk);

    std::string combined;
    bool saw_empty_wait_window = false;
    for (std::size_t index = 0; index < chunks.size(); ++index) {
        combined += scripts[index];
        if (chunks[index].actions.empty()
            && scripts[index].find("w ") != std::string::npos
            && scripts[index].find("d ") == std::string::npos
            && scripts[index].find("m ") == std::string::npos
            && scripts[index].find("u ") == std::string::npos) {
            saw_empty_wait_window = true;
        }
    }
    CHECK(saw_empty_wait_window);
    const std::size_t down_pos = combined.find("d 2 ");
    const std::size_t move_pos = combined.find("m 2 ");
    const std::size_t up_pos = combined.find("u 2\n");
    CHECK(down_pos != std::string::npos);
    CHECK(move_pos != std::string::npos);
    CHECK(up_pos != std::string::npos);
    CHECK(down_pos < move_pos && move_pos < up_pos);
}

void test_cross_boundary_transient_does_not_delay_next_note() {
    double now_s = 0.0;
    TouchScriptCompiler compiler;
    std::vector<PlaybackChunk> chunks;
    std::vector<std::string> scripts;
    PlaybackCallbacks callbacks;
    callbacks.clock = [&]() { return now_s; };
    callbacks.publish = [&](const PlaybackChunk& chunk) {
        chunks.push_back(chunk);
        const auto lines = compiler.compile(
            materialize_playback_actions(chunk),
            chunk.touch_config,
            chunk.window_start_s,
            chunk.final_chunk,
            chunk.window_end_s,
            materialize_future_down_reservations(chunk));
        std::string text;
        for (const std::string& line : lines) {
            text += line;
        }
        scripts.push_back(std::move(text));
        return true;
    };

    ScheduledAction first = counted_action(ActionKind::Tap, 0.49, 1);
    first.lane = 1;
    ScheduledAction second = counted_action(ActionKind::Tap, 0.51, 2);
    second.lane = 5;
    PlaybackSession session(std::move(callbacks));
    CHECK(session.arm({first, second}, EngineConfig{}));
    CHECK(session.start(0.49));
    CHECK(session.publish());
    CHECK_EQ(chunks.size(), static_cast<std::size_t>(1));
    CHECK(std::abs(chunks[0].window_end_s - 0.50) < 1e-9);
    CHECK(!chunks[0].final_chunk);

    now_s = 0.31;
    CHECK(session.publish());
    CHECK_EQ(chunks.size(), static_cast<std::size_t>(2));
    CHECK(chunks[1].final_chunk);
    CHECK(std::abs(chunks[1].window_start_s - 0.50) < 1e-9);
    CHECK(std::abs(chunks[1].window_end_s - 0.56) < 1e-9);
    const std::vector<int> downs = down_times_ms(scripts);
    CHECK_EQ(downs.size(), static_cast<std::size_t>(2));
    CHECK_EQ(downs[0], 490);
    CHECK_EQ(downs[1], 510);
}

void test_report_counts_action_kinds_and_logical_chords() {
    Fixture fixture;
    PlaybackSession session(fixture.callbacks());
    CHECK(session.arm(
        {
            counted_action(ActionKind::Tap, 0.0, 10),
            counted_action(ActionKind::Flick, 0.0, 11),
            counted_action(ActionKind::Down, 1.0, 20, 0),
            counted_action(ActionKind::Down, 1.0, 21, 1),
            counted_action(ActionKind::Move, 1.0, 20, 0),
            counted_action(ActionKind::Move, 1.0, 21, 1),
            counted_action(ActionKind::Up, 2.0, 20, 0),
            counted_action(ActionKind::Up, 2.0, 21, 1),
            // 同一个逻辑 note 的重复开始不能被误报成双押。
            counted_action(ActionKind::Tap, 3.0, 30),
            counted_action(ActionKind::Tap, 3.0, 30),
        },
        EngineConfig{}));
    const PlaybackReport report = session.report();
    CHECK_EQ(report.tap_actions, static_cast<uint64_t>(3));
    CHECK_EQ(report.flick_actions, static_cast<uint64_t>(1));
    CHECK_EQ(report.hold_starts, static_cast<uint64_t>(2));
    CHECK_EQ(report.hold_moves, static_cast<uint64_t>(2));
    CHECK_EQ(report.hold_releases, static_cast<uint64_t>(2));
    CHECK_EQ(report.chord_groups, static_cast<uint64_t>(2));
}

void test_final_tap_tail_extends_window_and_queue_depth() {
    Fixture fixture;
    fixture.now_s = 1000.0;
    PlaybackSession session(fixture.callbacks());
    EngineConfig config;
    config.tap_duration_ms = 50;
    CHECK(session.arm({tap(3.5)}, config));
    CHECK(session.start(1000.03));
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(1));
    CHECK(fixture.chunks[0].final_chunk);
    CHECK(std::abs(fixture.chunks[0].window_end_s - 1000.08) < 1e-9);
    CHECK(std::abs(session.report().max_queue_depth_ms - 80.0) < 1e-6);
}

void test_group_beyond_hard_cap_waits_without_zero_length_chunk() {
    Fixture fixture;
    PlaybackSessionConfig session_config;
    session_config.lookahead_s = 0.750;
    session_config.low_water_s = 0.200;
    session_config.max_queue_s = 0.750;
    PlaybackSession session(fixture.callbacks(), session_config);
    EngineConfig engine_config;
    engine_config.flick_duration_ms = 80;
    CHECK(session.arm(
        {
            counted_action(ActionKind::Tap, 0.0, 1),
            counted_action(ActionKind::Flick, 0.751, 2),
        },
        engine_config));
    CHECK(session.start(0.0));
    CHECK(session.publish());
    CHECK_EQ(fixture.chunks.size(), static_cast<std::size_t>(1));
    CHECK(!fixture.chunks[0].final_chunk);
    CHECK_EQ(fixture.chunks[0].actions.size(), static_cast<std::size_t>(1));
    CHECK(fixture.chunks[0].window_end_s
          > fixture.chunks[0].window_start_s);
    CHECK(fixture.chunks[0].window_end_s <= 0.750 + 1e-9);
    CHECK(session.report().max_queue_depth_ms <= 750.0 + 1e-6);

    fixture.now_s = 0.56;
    CHECK(session.publish());
    CHECK(fixture.chunks[1].final_chunk);
    CHECK_EQ(fixture.chunks[1].actions.size(), static_cast<std::size_t>(1));
    CHECK(std::abs(fixture.chunks[1].window_end_s - 0.831) < 1e-9);
    CHECK(session.report().max_queue_depth_ms <= 750.0 + 1e-6);
}

void test_real_chart_48_streams_to_final_tail_without_underflow() {
    const std::filesystem::path repo_root =
        std::filesystem::path(__FILE__).parent_path()
            .parent_path().parent_path().parent_path();
    const std::filesystem::path chart_path = repo_root
        / "resource" / "charts" / "bestdori" / "48" / "expert.json";
    const ChartTimeline timeline = ChartTimeline::from_json_file(
        chart_path.string());
    EngineConfig engine_config;
    const std::vector<ScheduledAction> actions =
        compile_pure_chart_actions(timeline, engine_config);

    double now_s = 1000.0;
    TouchScriptCompiler compiler;
    bool final_seen = false;
    uint64_t compiled_lines = 0;
    PlaybackCallbacks callbacks;
    callbacks.clock = [&]() { return now_s; };
    callbacks.publish = [&](const PlaybackChunk& chunk) {
        const auto lines = compiler.compile(
            materialize_playback_actions(chunk),
            chunk.touch_config,
            chunk.window_start_s,
            chunk.final_chunk,
            chunk.window_end_s,
            materialize_future_down_reservations(chunk));
        compiled_lines += static_cast<uint64_t>(lines.size());
        final_seen = final_seen || chunk.final_chunk;
        return true;
    };

    PlaybackSession session(std::move(callbacks));
    CHECK(session.arm(actions, engine_config));
    CHECK(session.start(1000.03));
    for (int tick = 0; tick <= 30000 && !final_seen; ++tick) {
        now_s = 1000.0 + static_cast<double>(tick) / 100.0;
        session.publish();
    }
    const PlaybackReport report = session.report();
    CHECK(final_seen);
    CHECK_EQ(report.sent_actions, report.planned_actions);
    CHECK(report.planned_actions > 600);
    CHECK(report.tap_actions > 0);
    CHECK(report.flick_actions > 0);
    CHECK(report.hold_starts > 0);
    CHECK(report.hold_moves > 0);
    CHECK(report.hold_releases > 0);
    CHECK(report.chord_groups > 0);
    CHECK_EQ(report.queue_underflows, static_cast<uint64_t>(0));
    CHECK(report.max_queue_depth_ms <= 750.0 + 1e-6);
    CHECK(compiled_lines > report.planned_actions);
    CHECK(session.finish("real chart streamed"));
}

}  // namespace

int run_playback_session_tests() {
    test_absolute_windows_respect_watermarks_and_final_marker();
    test_sparse_timeline_emits_empty_wait_window();
    test_chunk_reserves_future_hold_down_without_sending_it();
    test_start_anchor_maps_first_action_and_preserves_relative_timing();
    test_large_monotonic_clock_does_not_make_relative_chart_overdue();
    test_probe_samples_are_reset_at_start();
    test_underflow_is_counted_once_per_gap();
    test_publish_failure_is_terminal_and_does_not_count_sent_actions();
    test_execution_report_uses_absolute_drift_percentiles();
    test_cancel_acknowledgement_finishes_without_fallback();
    test_cancel_uses_fallback_after_100ms();
    test_cancel_deadline_failure_is_bounded();
    test_120_second_virtual_run_has_no_underflow();
    test_chunks_feed_touch_compiler_with_absolute_windows();
    test_cross_boundary_transient_does_not_delay_next_note();
    test_report_counts_action_kinds_and_logical_chords();
    test_final_tap_tail_extends_window_and_queue_depth();
    test_group_beyond_hard_cap_waits_without_zero_length_chunk();
    test_real_chart_48_streams_to_final_tail_without_underflow();
    return 0;
}
