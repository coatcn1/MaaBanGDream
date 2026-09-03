#include <cmath>

#include "maabangdream/scheduler.hpp"
#include "maabangdream/types.hpp"
#include "test_macros.hpp"

using namespace mbdr;

namespace {

std::vector<ScheduledAction> make_actions() {
    // 谱面时间 4.0s 双押 + 6.0s hold head + 8.0s hold tail。
    std::vector<ScheduledAction> actions;
    ScheduledAction first;
    first.kind = ActionKind::Tap;
    first.lane = 0;
    first.contact = -1;
    first.due_s = 4.0;
    actions.push_back(first);

    ScheduledAction second = first;
    second.lane = 6;
    actions.push_back(second);

    ScheduledAction down;
    down.kind = ActionKind::Down;
    down.lane = 2;
    down.contact = 0;
    down.due_s = 6.0;
    actions.push_back(down);

    ScheduledAction up;
    up.kind = ActionKind::Up;
    up.lane = 2;
    up.contact = 0;
    up.due_s = 8.0;
    actions.push_back(up);
    return actions;
}

void test_due_time_conversion() {
    EngineConfig config;
    config.song_offset_s = -6.0;  // chart = engine - 6
    config.press_bias_ms = 30;    // 提前 30ms 输入。
    ActionScheduler scheduler(make_actions(), config);

    // engine_due = due_s - offset + press_bias = 4 + 6 - 0.03 = 9.97。
    auto due = scheduler.tick(9.0);
    CHECK(due.empty());
    due = scheduler.tick(9.98);
    CHECK_EQ(due.size(), static_cast<std::size_t>(2));
    CHECK(due[0].kind == ActionKind::Tap);
    CHECK(due[1].kind == ActionKind::Tap);
}

void test_lateness_metrics() {
    EngineConfig config;
    config.song_offset_s = -6.0;
    ActionScheduler scheduler(make_actions(), config);
    // 全部动作在到期后 100ms 才派发。
    const auto due = scheduler.tick(100.0);
    CHECK_EQ(due.size(), static_cast<std::size_t>(4));
    const SchedulerStats stats = scheduler.stats();
    CHECK_EQ(stats.dispatched, static_cast<uint64_t>(4));
    CHECK_EQ(stats.late_count, static_cast<uint64_t>(4));
    CHECK(stats.late_max_ms > 0.0);
    CHECK(stats.late_p50_ms > 0.0);
    CHECK(stats.late_p95_ms >= stats.late_p50_ms);
}

void test_stop_releases_active_holds() {
    EngineConfig config;
    config.song_offset_s = 0.0;
    ActionScheduler scheduler(make_actions(), config);
    // 派发前 3 个动作（双押 + DOWN），hold 尚未 UP。
    const auto due = scheduler.tick(6.5);
    CHECK_EQ(due.size(), static_cast<std::size_t>(3));
    const auto releases = scheduler.stop();
    CHECK_EQ(releases.size(), static_cast<std::size_t>(1));
    CHECK(releases[0].kind == ActionKind::Up);
    CHECK_EQ(releases[0].contact, static_cast<int8_t>(0));
    CHECK_EQ(releases[0].lane, static_cast<uint8_t>(2));
    CHECK(scheduler.stopped());
}

void test_monotonic_guard() {
    ActionScheduler scheduler(make_actions(), EngineConfig{});
    scheduler.tick(5.0);
    bool threw = false;
    try {
        scheduler.tick(4.0);
    } catch (const std::runtime_error&) {
        threw = true;
    }
    CHECK(threw);
}

void test_large_batch_lateness_buffer_is_bounded() {
    // 回归：一次性派发超过 1024 个动作时延迟样本缓冲必须安全裁剪。
    std::vector<ScheduledAction> actions;
    for (int index = 0; index < 1200; ++index) {
        ScheduledAction action;
        action.kind = ActionKind::Tap;
        action.lane = static_cast<uint8_t>(index % 7);
        action.contact = -1;
        action.due_s = 0.1 * index;
        actions.push_back(action);
    }
    ActionScheduler scheduler(std::move(actions), EngineConfig{});
    const auto due = scheduler.tick(1000.0);
    CHECK_EQ(due.size(), static_cast<std::size_t>(1200));
    const SchedulerStats stats = scheduler.stats();
    CHECK_EQ(stats.dispatched, static_cast<uint64_t>(1200));
    CHECK(stats.late_p95_ms >= stats.late_p50_ms);
}

}  // namespace

int run_scheduler_tests() {
    test_due_time_conversion();
    test_lateness_metrics();
    test_stop_releases_active_holds();
    test_monotonic_guard();
    test_large_batch_lateness_buffer_is_bounded();
    return 0;
}
