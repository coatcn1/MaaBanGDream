#include "maabangdream/playback_session.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>
#include <utility>

namespace mbdr {
namespace {

constexpr double kMillis = 1000.0;
constexpr double kTimeEpsilon = 1e-9;

double steady_now_s() {
    const auto elapsed = std::chrono::steady_clock::now().time_since_epoch();
    return std::chrono::duration<double>(elapsed).count();
}

double percentile(std::vector<double> values, double fraction) {
    if (values.empty()) {
        return 0.0;
    }
    std::sort(values.begin(), values.end());
    const double rank = std::ceil(
        fraction * static_cast<double>(values.size()));
    const std::size_t index = static_cast<std::size_t>(
        std::max(1.0, rank) - 1.0);
    return values[std::min(index, values.size() - 1)];
}

bool valid_config(const PlaybackSessionConfig& config) {
    return std::isfinite(config.lookahead_s)
        && std::isfinite(config.low_water_s)
        && std::isfinite(config.max_queue_s)
        && std::isfinite(config.reset_timeout_s)
        && std::isfinite(config.cancel_deadline_s)
        && config.low_water_s > 0.0
        && config.lookahead_s > config.low_water_s
        && config.max_queue_s >= config.lookahead_s
        && config.reset_timeout_s > 0.0
        && config.cancel_deadline_s >= config.reset_timeout_s;
}

double implicit_duration_s(
    ActionKind kind,
    const EngineConfig& config) {
    if (kind == ActionKind::Tap) {
        return std::max(0, config.tap_duration_ms) / kMillis;
    }
    if (kind == ActionKind::Flick) {
        return std::max(0, config.flick_duration_ms) / kMillis;
    }
    return 0.0;
}

bool starts_logical_note(ActionKind kind) {
    return kind == ActionKind::Tap
        || kind == ActionKind::Flick
        || kind == ActionKind::Down;
}

}  // namespace

std::vector<ScheduledAction> materialize_playback_actions(
    const PlaybackChunk& chunk) {
    std::vector<ScheduledAction> result;
    result.reserve(chunk.actions.size());
    for (const TimedPlaybackAction& timed : chunk.actions) {
        ScheduledAction action = timed.action;
        action.due_s = timed.engine_due_s;
        result.push_back(std::move(action));
    }
    return result;
}

PlaybackSession::PlaybackSession(
    PlaybackCallbacks callbacks,
    PlaybackSessionConfig config)
    : callbacks_(std::move(callbacks)), config_(config) {
    if (!valid_config(config_)) {
        throw std::invalid_argument("invalid playback session config");
    }
    if (!callbacks_.clock) {
        callbacks_.clock = steady_now_s;
    }
}

bool PlaybackSession::arm(
    std::vector<ScheduledAction> actions,
    EngineConfig engine_config) {
    if (state_ == PlaybackState::Running
        || state_ == PlaybackState::Cancelling) {
        return false;
    }
    if (!callbacks_.publish || actions.empty()) {
        state_ = PlaybackState::Failed;
        report_ = {};
        report_.terminal_reason = callbacks_.publish
            ? "no playback actions"
            : "chunk publisher is not configured";
        return false;
    }

    engine_config_ = std::move(engine_config);
    // photogate anchor 已经是最终首拍执行时刻，后续编译不得重复套用旧的
    // planner song_offset 或 press_bias。
    engine_config_.song_offset_s = 0.0;
    engine_config_.press_bias_ms = 0;
    entries_.clear();
    entries_.reserve(actions.size());
    for (std::size_t index = 0; index < actions.size(); ++index) {
        const double due_s = actions[index].due_s;
        if (!std::isfinite(due_s)) {
            state_ = PlaybackState::Failed;
            report_ = {};
            report_.terminal_reason = "non-finite action deadline";
            entries_.clear();
            return false;
        }
        Entry entry;
        entry.timed.action = actions[index];
        entry.timed.engine_due_s = due_s;
        entry.completion_s = due_s;
        entry.order = index;
        entries_.push_back(std::move(entry));
    }
    std::stable_sort(entries_.begin(), entries_.end(),
        [](const Entry& left, const Entry& right) {
            if (left.timed.engine_due_s != right.timed.engine_due_s) {
                return left.timed.engine_due_s < right.timed.engine_due_s;
            }
            return left.order < right.order;
        });
    const double first_due_s = entries_.front().timed.engine_due_s;
    chart_first_due_s_ = first_due_s;
    for (Entry& entry : entries_) {
        // arm 阶段先保存相对首拍时间；绝对单调时刻只有 photogate 成功后
        // 才能确定，不能再依赖 Python planner 的 song_offset。
        entry.timed.engine_due_s -= first_due_s;
        entry.completion_s = entry.timed.engine_due_s
            + implicit_duration_s(entry.timed.action.kind, engine_config_);
    }

    cursor_ = 0;
    next_sequence_ = 1;
    queue_tail_s_ = 0.0;
    last_clock_s_ = 0.0;
    clock_started_ = false;
    published_once_ = false;
    final_chunk_published_ = false;
    underflow_latched_ = false;
    fallback_requested_ = false;
    cancel_started_s_ = 0.0;
    playback_end_engine_s_ = 0.0;
    cancel_reason_.clear();
    report_ = {};
    report_.planned_actions = static_cast<uint64_t>(entries_.size());
    report_.chart_first_due_s = chart_first_due_s_;
    for (const Entry& entry : entries_) {
        switch (entry.timed.action.kind) {
            case ActionKind::Tap:
                ++report_.tap_actions;
                break;
            case ActionKind::Flick:
                ++report_.flick_actions;
                break;
            case ActionKind::Down:
                ++report_.hold_starts;
                break;
            case ActionKind::Move:
                ++report_.hold_moves;
                break;
            case ActionKind::Up:
                ++report_.hold_releases;
                break;
        }
    }
    // 同时刻只有 TAP/FLICK/DOWN 才表示判定开始；MOVE/UP 即使同帧也
    // 只是既有手势生命周期，不能把 Slide 插值误报成双押。
    for (std::size_t begin = 0; begin < entries_.size();) {
        std::size_t end = begin + 1;
        while (end < entries_.size()
               && std::abs(entries_[end].timed.engine_due_s
                    - entries_[begin].timed.engine_due_s)
                    <= kTimeEpsilon) {
            ++end;
        }
        std::set<std::pair<int, long long>> logical_notes;
        for (std::size_t index = begin; index < end; ++index) {
            const Entry& entry = entries_[index];
            const ScheduledAction& action = entry.timed.action;
            if (!starts_logical_note(action.kind)) {
                continue;
            }
            if (action.note_index >= 0) {
                logical_notes.emplace(0, action.note_index);
            } else if (action.contact >= 0) {
                logical_notes.emplace(1, action.contact);
            } else {
                // 匿名瞬态动作没有稳定 note/contact，只能以编译顺序区分；
                // 仍要求同一 due，避免把普通连续音符合并。
                logical_notes.emplace(
                    2, static_cast<long long>(entry.order));
            }
        }
        if (logical_notes.size() >= 2) {
            ++report_.chord_groups;
        }
        begin = end;
    }
    absolute_drift_ms_.clear();
    calibrator_.reset();
    state_ = PlaybackState::Armed;
    return true;
}

bool PlaybackSession::start(double first_action_engine_s) {
    if (state_ != PlaybackState::Armed) {
        return false;
    }
    const double current_s = now();
    if (!clock_is_valid(current_s) || !std::isfinite(first_action_engine_s)) {
        if (!std::isfinite(first_action_engine_s)) {
            fail("first action anchor is not finite");
        }
        return false;
    }
    report_.probe_events = static_cast<uint64_t>(calibrator_.event_count());
    // 开演前握手和探测存在长空闲间隔，必须与正式播放统计隔离。
    calibrator_.reset();
    for (Entry& entry : entries_) {
        entry.timed.engine_due_s += first_action_engine_s;
        entry.completion_s += first_action_engine_s;
    }
    playback_end_engine_s_ = entries_.front().completion_s;
    for (const Entry& entry : entries_) {
        playback_end_engine_s_ = std::max(
            playback_end_engine_s_, entry.completion_s);
    }
    report_.first_action_engine_s = first_action_engine_s;
    queue_tail_s_ = current_s;
    state_ = PlaybackState::Running;
    return true;
}

bool PlaybackSession::publish() {
    if (state_ != PlaybackState::Running) {
        return false;
    }
    const double current_s = now();
    if (!clock_is_valid(current_s)) {
        return false;
    }
    detect_underflow(current_s);
    if (final_chunk_published_) {
        return false;
    }

    const double queue_depth_s = std::max(0.0, queue_tail_s_ - current_s);
    if (published_once_
        && queue_depth_s > config_.low_water_s + kTimeEpsilon) {
        return false;
    }

    const double window_start_s = std::max(queue_tail_s_, current_s);
    const double queue_cap_s = current_s + config_.max_queue_s;
    const double desired_end_s = std::min(
        current_s + config_.lookahead_s, queue_cap_s);
    const double base_window_end_s = std::max(
        window_start_s,
        std::min(desired_end_s, playback_end_engine_s_));

    std::size_t next_cursor = cursor_;
    while (next_cursor < entries_.size()) {
        const double group_due_s =
            entries_[next_cursor].timed.engine_due_s;
        if (group_due_s > base_window_end_s + kTimeEpsilon) {
            break;
        }
        std::size_t group_end = next_cursor + 1;
        while (group_end < entries_.size()
               && std::abs(entries_[group_end].timed.engine_due_s
                    - group_due_s) <= kTimeEpsilon) {
            ++group_end;
        }
        next_cursor = group_end;
    }

    // 中间块必须停在名义窗口边界：TouchScriptCompiler 会把越界的隐式
    // MOVE/UP 留在 pending_events，下一块仍从相同绝对时间轴继续。若把
    // queue_tail 延到 TAP/FLICK 的 completion，边界后的音符会整体变晚。
    const bool all_actions_selected = next_cursor == entries_.size();
    const bool final_tail_fits =
        playback_end_engine_s_ <= queue_cap_s + kTimeEpsilon;
    const bool final_chunk = all_actions_selected && final_tail_fits;
    const double window_end_s = final_chunk
        ? std::max(base_window_end_s, playback_end_engine_s_)
        : base_window_end_s;
    if (!final_chunk
        && next_cursor == cursor_
        && window_end_s <= window_start_s + kTimeEpsilon) {
        // 不发布无法推进设备时间的空块；等待时钟前进后再尝试。
        return false;
    }

    PlaybackChunk chunk;
    chunk.sequence = next_sequence_;
    chunk.window_start_s = window_start_s;
    chunk.window_end_s = window_end_s;
    chunk.touch_config = engine_config_;
    chunk.actions.reserve(next_cursor - cursor_);
    for (std::size_t index = cursor_; index < next_cursor; ++index) {
        chunk.actions.push_back(entries_[index].timed);
    }
    const double reservation_horizon_s = window_end_s
        + std::max(
            std::max(0, engine_config_.tap_duration_ms),
            std::max(0, engine_config_.flick_duration_ms)) / kMillis;
    for (std::size_t index = next_cursor; index < entries_.size(); ++index) {
        const TimedPlaybackAction& timed = entries_[index].timed;
        if (timed.engine_due_s > reservation_horizon_s + kTimeEpsilon) {
            break;
        }
        if (timed.engine_due_s > window_end_s + kTimeEpsilon
            && timed.action.kind == ActionKind::Down
            && timed.action.contact >= 0) {
            chunk.future_down_reservations.push_back(timed);
        }
    }
    chunk.final_chunk = final_chunk;

    bool published = false;
    try {
        published = callbacks_.publish(chunk);
    } catch (...) {
        published = false;
    }
    if (!published) {
        fail("chunk publish failed");
        return false;
    }

    cursor_ = next_cursor;
    ++next_sequence_;
    queue_tail_s_ = window_end_s;
    published_once_ = true;
    final_chunk_published_ = chunk.final_chunk;
    underflow_latched_ = false;
    report_.sent_actions += static_cast<uint64_t>(chunk.actions.size());
    ++report_.chunks;
    report_.max_queue_depth_ms = std::max(
        report_.max_queue_depth_ms,
        std::max(0.0, queue_tail_s_ - current_s) * kMillis);
    return true;
}

bool PlaybackSession::cancel(std::string reason) {
    if (state_ != PlaybackState::Armed
        && state_ != PlaybackState::Running) {
        return false;
    }
    const double current_s = now();
    if (!clock_is_valid(current_s)) {
        return false;
    }
    cancel_started_s_ = current_s;
    cancel_reason_ = reason.empty() ? "cancelled" : std::move(reason);
    fallback_requested_ = false;
    state_ = PlaybackState::Cancelling;

    bool acknowledged = false;
    if (callbacks_.request_reset) {
        try {
            acknowledged = callbacks_.request_reset();
        } catch (...) {
            acknowledged = false;
        }
    }
    if (acknowledged) {
        complete_cancel(current_s);
    }
    return true;
}

bool PlaybackSession::acknowledge_reset() {
    if (state_ != PlaybackState::Cancelling) {
        return false;
    }
    const double current_s = now();
    if (!clock_is_valid(current_s)) {
        return false;
    }
    complete_cancel(current_s);
    return true;
}

PlaybackState PlaybackSession::poll() {
    if (state_ != PlaybackState::Running
        && state_ != PlaybackState::Cancelling) {
        return state_;
    }
    const double current_s = now();
    if (!clock_is_valid(current_s)) {
        return state_;
    }
    if (state_ == PlaybackState::Running) {
        detect_underflow(current_s);
        return state_;
    }

    const double elapsed_s = std::max(0.0, current_s - cancel_started_s_);
    if (!fallback_requested_
        && elapsed_s + kTimeEpsilon >= config_.reset_timeout_s) {
        fallback_requested_ = true;
        report_.fallback_used = true;
        bool stopped = false;
        if (callbacks_.fallback_stop) {
            try {
                stopped = callbacks_.fallback_stop();
            } catch (...) {
                stopped = false;
            }
        }
        if (stopped) {
            complete_cancel(current_s);
            return state_;
        }
    }
    if (elapsed_s + kTimeEpsilon >= config_.cancel_deadline_s) {
        fail("cancel deadline exceeded", elapsed_s * kMillis);
    }
    return state_;
}

bool PlaybackSession::finish(std::string reason) {
    if (state_ != PlaybackState::Running || !final_chunk_published_) {
        return false;
    }
    state_ = PlaybackState::Finished;
    report_.terminal_reason = reason.empty()
        ? "finished"
        : std::move(reason);
    return true;
}

void PlaybackSession::observe_minitouch_log(
    const MinitouchLogEvent& event) {
    calibrator_.observe(event);
}

int PlaybackSession::calibration_event_count() const noexcept {
    return calibrator_.event_count();
}

TouchLatencyOffsets PlaybackSession::latency_offsets() const {
    return calibrator_.offsets();
}

double PlaybackSession::latency_correction_ms(
    const TouchLatencyOffsets& previous) const {
    return calibrator_.correction_ms(previous);
}

void PlaybackSession::reset_calibration_window() {
    calibrator_.reset();
}

bool PlaybackSession::observe_execution(
    double planned_engine_s,
    double actual_engine_s,
    uint64_t count) {
    if (count == 0
        || !std::isfinite(planned_engine_s)
        || !std::isfinite(actual_engine_s)
        || report_.executed_actions + count > report_.sent_actions) {
        return false;
    }
    const double drift_ms = std::abs(
        actual_engine_s - planned_engine_s) * kMillis;
    absolute_drift_ms_.insert(
        absolute_drift_ms_.end(),
        static_cast<std::size_t>(count),
        drift_ms);
    report_.executed_actions += count;
    update_drift_metrics();
    return true;
}

double PlaybackSession::now() {
    try {
        return callbacks_.clock();
    } catch (...) {
        return std::numeric_limits<double>::quiet_NaN();
    }
}

bool PlaybackSession::clock_is_valid(double value) {
    if (!std::isfinite(value)) {
        fail("playback clock is not finite");
        return false;
    }
    if (clock_started_ && value + kTimeEpsilon < last_clock_s_) {
        fail("playback clock moved backwards");
        return false;
    }
    last_clock_s_ = value;
    clock_started_ = true;
    return true;
}

void PlaybackSession::detect_underflow(double current_s) {
    if (!published_once_ || final_chunk_published_) {
        return;
    }
    const bool underflow = current_s > queue_tail_s_ + kTimeEpsilon;
    if (underflow && !underflow_latched_) {
        ++report_.queue_underflows;
        underflow_latched_ = true;
    } else if (!underflow) {
        underflow_latched_ = false;
    }
}

void PlaybackSession::complete_cancel(double current_s) {
    state_ = PlaybackState::Cancelled;
    report_.terminal_reason = cancel_reason_;
    report_.stop_latency_ms = std::max(
        0.0, current_s - cancel_started_s_) * kMillis;
}

void PlaybackSession::fail(std::string reason, double stop_latency_ms) {
    state_ = PlaybackState::Failed;
    report_.terminal_reason = std::move(reason);
    report_.stop_latency_ms = std::max(0.0, stop_latency_ms);
}

void PlaybackSession::update_drift_metrics() {
    report_.drift_p50_ms = percentile(absolute_drift_ms_, 0.50);
    report_.drift_p95_ms = percentile(absolute_drift_ms_, 0.95);
    report_.drift_max_ms = absolute_drift_ms_.empty()
        ? 0.0
        : *std::max_element(
            absolute_drift_ms_.begin(), absolute_drift_ms_.end());
}

}  // namespace mbdr
