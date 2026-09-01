#pragma once

#include <cstdint>
#include <vector>

#include "maabangdream/types.hpp"

namespace mbdr {

// 调度器统计：lateness = now - engine_due，正值表示派发晚了。
struct SchedulerStats {
    uint64_t dispatched = 0;
    uint64_t late_count = 0;       // lateness > 0 的动作数。
    double late_max_ms = 0.0;
    double late_p50_ms = 0.0;
    double late_p95_ms = 0.0;
    double scheduled_total = 0.0;
};

// 截止时间调度器。
//
// 第一轮只做确定性批处理：tick(now) 返回 (last_tick, now] 内到期的动作，
// 并累计 lateness 指标；触点与瞬态分配仍由 Python ControllerTouchDispatcher
// 完成。stop() 时对仍按住的 hold 释放触点（fail-closed），并返回释放动作。
class ActionScheduler {
public:
    ActionScheduler() = default;
    explicit ActionScheduler(
        std::vector<ScheduledAction> actions,
        EngineConfig config);

    void reset(
        std::vector<ScheduledAction> actions,
        EngineConfig config);

    // 推进调度时钟；重复调用 now 必须单调不减。
    std::vector<ScheduledAction> tick(double now_s);

    // 释放所有仍在生命周期内的 hold，返回 UP 动作（供 Python 派发）。
    std::vector<ScheduledAction> stop();

    bool stopped() const noexcept { return stopped_; }
    const SchedulerStats& stats() const noexcept { return stats_; }

private:
    struct Entry {
        ScheduledAction action;
        double engine_due_s = 0.0;
        std::size_t order = 0;
    };

    std::vector<Entry> entries_;
    std::size_t cursor_ = 0;
    double last_tick_s_ = 0.0;
    EngineConfig config_;
    SchedulerStats stats_;
    std::vector<double> lateness_ms_;
    bool stopped_ = true;
};

}  // namespace mbdr
