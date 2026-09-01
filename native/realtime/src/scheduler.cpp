#include "maabangdream/scheduler.hpp"

#include <algorithm>
#include <stdexcept>

namespace mbdr {

namespace {

double percentile(const std::vector<double>& values, double fraction) {
    if (values.empty()) {
        return 0.0;
    }
    std::vector<double> sorted = values;
    std::sort(sorted.begin(), sorted.end());
    const std::size_t index = std::min(
        sorted.size() - 1,
        static_cast<std::size_t>(fraction * static_cast<double>(sorted.size())));
    return sorted[index];
}

}  // namespace

ActionScheduler::ActionScheduler(
    std::vector<ScheduledAction> actions,
    EngineConfig config) {
    reset(std::move(actions), std::move(config));
}

void ActionScheduler::reset(
    std::vector<ScheduledAction> actions,
    EngineConfig config) {
    config_ = std::move(config);
    const double press_bias_s = -static_cast<double>(config_.press_bias_ms) / 1000.0;
    entries_.clear();
    entries_.reserve(actions.size());
    for (std::size_t index = 0; index < actions.size(); ++index) {
        Entry entry;
        entry.action = actions[index];
        entry.engine_due_s =
            entry.action.due_s - config_.song_offset_s + press_bias_s;
        entry.order = index;
        entries_.push_back(entry);
    }
    // 按引擎到期时间稳定排序；同一时刻保留编译器顺序。
    std::stable_sort(entries_.begin(), entries_.end(),
        [](const Entry& lhs, const Entry& rhs) {
            if (lhs.engine_due_s != rhs.engine_due_s) {
                return lhs.engine_due_s < rhs.engine_due_s;
            }
            return lhs.order < rhs.order;
        });
    cursor_ = 0;
    last_tick_s_ = 0.0;
    stats_ = SchedulerStats{};
    lateness_ms_.clear();
    stopped_ = false;
}

std::vector<ScheduledAction> ActionScheduler::tick(double now_s) {
    if (stopped_) {
        throw std::runtime_error("scheduler already stopped");
    }
    if (now_s < last_tick_s_) {
        throw std::runtime_error("scheduler clock must be monotonic");
    }
    last_tick_s_ = now_s;
    std::vector<ScheduledAction> due;
    while (cursor_ < entries_.size() &&
           entries_[cursor_].engine_due_s <= now_s) {
        const Entry& entry = entries_[cursor_];
        due.push_back(entry.action);
        const double lateness_ms = (now_s - entry.engine_due_s) * 1000.0;
        lateness_ms_.push_back(lateness_ms);
        if (lateness_ms > 0.0) {
            ++stats_.late_count;
            stats_.late_max_ms = std::max(stats_.late_max_ms, lateness_ms);
        }
        ++stats_.dispatched;
        ++cursor_;
    }
    stats_.scheduled_total = static_cast<double>(entries_.size());
    // 延迟样本只保留最近 1024 条，避免整场演奏后内存无界增长；
    // 注意必须先从头部裁剪，偏移量不能为负。
    if (lateness_ms_.size() > 1024) {
        lateness_ms_.erase(
            lateness_ms_.begin(),
            lateness_ms_.begin() + static_cast<std::ptrdiff_t>(
                lateness_ms_.size() - 1024));
    }
    stats_.late_p50_ms = percentile(lateness_ms_, 0.50);
    stats_.late_p95_ms = percentile(lateness_ms_, 0.95);
    return due;
}

std::vector<ScheduledAction> ActionScheduler::stop() {
    stopped_ = true;
    std::vector<ScheduledAction> releases;
    // 收集所有已 DOWN 但尚未 UP/FLICK 的 hold 触点。
    std::vector<int8_t> active(kMaxContacts, -1);
    for (std::size_t index = 0; index < cursor_; ++index) {
        const ScheduledAction& action = entries_[index].action;
        if (action.kind == ActionKind::Down) {
            active[static_cast<std::size_t>(action.contact)] =
                static_cast<int8_t>(action.lane);
        } else if (action.kind == ActionKind::Up ||
                   (action.kind == ActionKind::Flick && action.contact >= 0)) {
            active[static_cast<std::size_t>(action.contact)] = -1;
        }
    }
    for (std::size_t contact = 0; contact < kMaxContacts; ++contact) {
        if (active[contact] >= 0) {
            ScheduledAction release;
            release.kind = ActionKind::Up;
            release.lane = static_cast<uint8_t>(active[contact]);
            release.contact = static_cast<int8_t>(contact);
            release.target_x =
                config_.lane_centers[static_cast<std::size_t>(active[contact])];
            releases.push_back(release);
        }
    }
    return releases;
}

}  // namespace mbdr
