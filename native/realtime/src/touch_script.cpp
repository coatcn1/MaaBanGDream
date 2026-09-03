#include "maabangdream/touch_script.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace mbdr {
namespace {

constexpr double kMillis = 1000.0;
constexpr double kNanos = 1000000000.0;
constexpr double kMaxOffsetPerWaitMs = 1.0;
constexpr double kMaxLossMs = 2.0;
constexpr double kTimeEpsilon = 1e-12;

using LowKind = detail::PendingTouchKind;
using LowEvent = detail::PendingTouchEvent;

struct TimedAction {
    ScheduledAction action;
    double due = 0.0;
    int64_t due_ns = 0;
    uint64_t action_token = 0;
};

struct ContactInterval {
    int contact = -1;
    int64_t start_ns = 0;
    int64_t end_ns = 0;
};

struct ContactAnalysis {
    std::array<bool, kMaxContacts> active{};
    std::array<int64_t, kMaxContacts> available_after{};
    std::vector<ContactInterval> intervals;
};

int64_t time_key(double seconds) {
    constexpr double kMaxSeconds =
        static_cast<double>(std::numeric_limits<int64_t>::max()) / kNanos;
    constexpr double kMinSeconds =
        static_cast<double>(std::numeric_limits<int64_t>::min()) / kNanos;
    if (!std::isfinite(seconds) || seconds > kMaxSeconds
        || seconds < kMinSeconds) {
        throw std::runtime_error("touch script: time is outside nanosecond range");
    }
    return static_cast<int64_t>(std::llround(seconds * kNanos));
}

double canonical_time(int64_t key) {
    return static_cast<double>(key) / kNanos;
}

double engine_due(const ScheduledAction& action, const EngineConfig& config) {
    // press_bias_ms 正值 = 提前输入，沿用 types.hpp 的换算约定。
    return action.due_s - config.song_offset_s
        - config.press_bias_ms / kMillis;
}

int rounded_x(float target_x, uint8_t lane, const EngineConfig& config) {
    if (target_x > 0.0F) {
        return static_cast<int>(std::lround(target_x));
    }
    return static_cast<int>(std::lround(
        config.lane_centers[std::min<std::size_t>(lane, kLaneCount - 1)]));
}

std::string line(std::initializer_list<std::string> parts) {
    std::ostringstream out;
    for (const std::string& part : parts) {
        out << part;
    }
    out << '\n';
    return out.str();
}

double clamp(double value, double lo, double hi) {
    return std::max(lo, std::min(hi, value));
}

bool low_event_less(const LowEvent& left, const LowEvent& right) {
    if (left.due_ns != right.due_ns) {
        return left.due_ns < right.due_ns;
    }
    if (left.kind != right.kind) {
        // 复用同一触点时必须先 MOVE 到旧尾点、再 UP、最后才允许新 DOWN。
        return static_cast<int>(left.kind) < static_cast<int>(right.kind);
    }
    return left.order < right.order;
}

void append_event(
    std::vector<LowEvent>& events,
    LowKind kind,
    int contact,
    int x,
    int y,
    double due,
    std::size_t& order,
    uint64_t action_token = 0,
    double planned_engine_s = 0.0,
    bool emits_receipt = false) {
    LowEvent event;
    event.kind = kind;
    event.contact = contact;
    event.x = x;
    event.y = y;
    event.due_ns = time_key(due);
    event.due = canonical_time(event.due_ns);
    event.order = order++;
    event.action_token = action_token;
    event.planned_engine_s = canonical_time(time_key(planned_engine_s));
    event.emits_receipt = emits_receipt;
    events.push_back(event);
}

void append_smooth_move(
    std::vector<LowEvent>& events,
    int contact,
    int from_x,
    int from_y,
    int to_x,
    int to_y,
    double from_due,
    double duration_s,
    double step_s,
    std::size_t& order,
    uint64_t action_token,
    bool receipt_on_first_move) {
    const std::size_t regular_steps = static_cast<std::size_t>(std::floor(
        std::max(0.0, duration_s - kTimeEpsilon) / step_s));
    for (std::size_t step = 1; step <= regular_steps + 1; ++step) {
        const bool endpoint = step > regular_steps;
        const double due = endpoint
            ? from_due + duration_s
            : from_due + static_cast<double>(step) * step_s;
        if (!endpoint && due >= from_due + duration_s - kTimeEpsilon) {
            continue;
        }
        const double progress = duration_s <= 0.0
            ? 1.0
            : (due - from_due) / duration_s;
        const int x = static_cast<int>(std::lround(
            from_x + (to_x - from_x) * progress));
        const int y = static_cast<int>(std::lround(
            from_y + (to_y - from_y) * progress));
        append_event(events, LowKind::Move, contact, x, y, due, order,
            action_token, due, receipt_on_first_move && step == 1);
    }
}

void expand_action(
    const TimedAction& timed,
    int contact,
    bool continuing_flick,
    const EngineConfig& config,
    std::vector<LowEvent>& events,
    std::size_t& order) {
    const ScheduledAction& action = timed.action;
    const int x = rounded_x(action.target_x, action.lane, config);
    const int y = static_cast<int>(std::lround(config.judgement_y));
    switch (action.kind) {
        case ActionKind::Tap:
            append_event(events, LowKind::Down, contact, x, y, timed.due,
                order, timed.action_token, timed.due, true);
            append_event(events, LowKind::Up, contact, 0, 0,
                timed.due + config.tap_duration_ms / kMillis, order,
                timed.action_token,
                timed.due + config.tap_duration_ms / kMillis, false);
            break;
        case ActionKind::Flick: {
            if (!continuing_flick) {
                append_event(
                    events, LowKind::Down, contact, x, y, timed.due, order,
                    timed.action_token, timed.due, true);
            }
            const int swipe_x = action.flick_direction == -1
                ? x - 120
                : (action.flick_direction == 1 ? x + 120 : x);
            const int swipe_y = action.flick_direction == 0 ? y - 120 : y + 8;
            const double duration_s = config.flick_duration_ms / kMillis;
            append_smooth_move(events, contact, x, y, swipe_x, swipe_y,
                timed.due, duration_s, config.slide_step_s, order,
                timed.action_token, continuing_flick);
            append_event(events, LowKind::Up, contact, 0, 0,
                timed.due + duration_s, order, timed.action_token,
                timed.due + duration_s, false);
            break;
        }
        case ActionKind::Down:
            append_event(events, LowKind::Down, contact, x, y, timed.due,
                order, timed.action_token, timed.due, true);
            break;
        case ActionKind::Move:
            append_event(events, LowKind::Move, contact, x, y, timed.due,
                order, timed.action_token, timed.due, true);
            break;
        case ActionKind::Up:
            append_event(events, LowKind::Up, contact, 0, 0, timed.due,
                order, timed.action_token, timed.due, true);
            break;
    }
}

ContactAnalysis analyse_contacts(
    std::vector<LowEvent> events,
    const std::array<bool, kMaxContacts>& initial_active,
    const std::array<int64_t, kMaxContacts>& initial_available_after) {
    std::stable_sort(events.begin(), events.end(), low_event_less);
    ContactAnalysis result;
    result.active = initial_active;
    result.available_after = initial_available_after;
    std::array<int64_t, kMaxContacts> active_since{};
    active_since.fill(std::numeric_limits<int64_t>::min());

    for (const LowEvent& event : events) {
        if (event.contact < 0 || event.contact >= kMaxContacts) {
            throw std::runtime_error("touch script: contact out of range");
        }
        const std::size_t contact = static_cast<std::size_t>(event.contact);
        switch (event.kind) {
            case LowKind::Move:
                if (!result.active[contact]) {
                    throw std::runtime_error(
                        "touch script: MOVE on inactive contact");
                }
                break;
            case LowKind::Up:
                if (!result.active[contact]) {
                    throw std::runtime_error(
                        "touch script: UP on inactive contact");
                }
                result.intervals.push_back(ContactInterval{
                    event.contact, active_since[contact], event.due_ns});
                result.active[contact] = false;
                break;
            case LowKind::Down:
                if (result.active[contact]) {
                    throw std::runtime_error(
                        "touch script: duplicate DOWN on active contact");
                }
                if (event.due_ns < result.available_after[contact]) {
                    throw std::runtime_error(
                        "touch script: contact reused before queued UP");
                }
                result.active[contact] = true;
                active_since[contact] = event.due_ns;
                break;
        }
        result.available_after[contact] = std::max(
            result.available_after[contact], event.due_ns);
    }

    for (std::size_t contact = 0; contact < kMaxContacts; ++contact) {
        if (result.active[contact]) {
            result.intervals.push_back(ContactInterval{
                static_cast<int>(contact), active_since[contact],
                std::numeric_limits<int64_t>::max()});
        }
    }
    return result;
}

bool overlaps(int64_t start_ns, int64_t end_ns,
              const ContactInterval& interval) {
    // 触点在恰好 UP 的时刻可以复用；其余重叠全部拒绝。
    return start_ns < interval.end_ns && end_ns > interval.start_ns;
}

void validate_unique_same_time_operations(
    const std::vector<LowEvent>& events) {
    std::size_t begin = 0;
    while (begin < events.size()) {
        std::size_t end = begin + 1;
        while (end < events.size()
               && events[end].due_ns == events[begin].due_ns) {
            ++end;
        }
        bool seen[kMaxContacts][3] = {};
        for (std::size_t index = begin; index < end; ++index) {
            const LowEvent& event = events[index];
            if (event.contact < 0 || event.contact >= kMaxContacts) {
                throw std::runtime_error("touch script: contact out of range");
            }
            const std::size_t contact =
                static_cast<std::size_t>(event.contact);
            const std::size_t kind = static_cast<std::size_t>(event.kind);
            if (seen[contact][kind]) {
                throw std::runtime_error(
                    "touch script: duplicate operation for contact at one timestamp");
            }
            seen[contact][kind] = true;
        }
        begin = end;
    }
}

}  // namespace

TouchScriptCompiler::TouchScriptCompiler() noexcept {
    contact_available_after_.fill(std::numeric_limits<int64_t>::min());
}

TouchScriptCompiler::TouchScriptCompiler(TouchLatencyOffsets offsets) noexcept
    : offsets_(offsets) {
    contact_available_after_.fill(std::numeric_limits<int64_t>::min());
}

void TouchScriptCompiler::reset_contacts() noexcept {
    active_contacts_.fill(false);
    contact_available_after_.fill(std::numeric_limits<int64_t>::min());
    pending_events_.clear();
    next_event_order_ = 0;
    next_action_token_ = 1;
    last_execution_receipts_.clear();
    transient_cursor_ = 0;
}

std::vector<std::string> TouchScriptCompiler::compile(
    std::vector<ScheduledAction> actions,
    const EngineConfig& config,
    double start_engine_time,
    bool final_chunk,
    double end_engine_time,
    std::vector<ScheduledAction> future_down_reservations) {
    if (!std::isfinite(start_engine_time)) {
        throw std::runtime_error("touch script: start_engine_time must be finite");
    }
    if (config.tap_duration_ms <= 0 || config.flick_duration_ms <= 0) {
        throw std::runtime_error(
            "touch script: gesture durations must be positive");
    }
    if (!std::isfinite(config.slide_step_s) || config.slide_step_s <= 0.0) {
        throw std::runtime_error("touch script: slide_step_s must be positive");
    }

    std::vector<TimedAction> timed;
    timed.reserve(actions.size());
    for (ScheduledAction& action : actions) {
        const double due = engine_due(action, config);
        const int64_t due_ns = time_key(due);
        timed.push_back(TimedAction{
            action, canonical_time(due_ns), due_ns, 0});
    }
    std::stable_sort(timed.begin(), timed.end(),
        [](const TimedAction& left, const TimedAction& right) {
            return left.due_ns < right.due_ns;
        });
    uint64_t action_token = next_action_token_;
    for (TimedAction& item : timed) {
        item.action_token = action_token++;
    }

    // 前一窗口留下的隐式 MOVE/UP 先进入本次时间轴；新动作只加入一次，
    // 未落入当前窗口的低层事件继续留待下一次 compile()。
    std::vector<LowEvent> explicit_events = pending_events_;
    std::size_t event_order = next_event_order_;
    for (const TimedAction& item : timed) {
        if (item.action.contact < 0) {
            if (item.action.kind != ActionKind::Tap
                && item.action.kind != ActionKind::Flick) {
                throw std::runtime_error(
                    "touch script: lifecycle action requires a contact");
            }
            continue;
        }
        const bool continuing_flick = item.action.kind == ActionKind::Flick;
        expand_action(item, item.action.contact, continuing_flick, config,
            explicit_events, event_order);
    }

    const ContactAnalysis explicit_analysis = analyse_contacts(
        explicit_events, active_contacts_, contact_available_after_);
    std::vector<ContactInterval> reservations = explicit_analysis.intervals;
    for (const ScheduledAction& action : future_down_reservations) {
        if (action.kind != ActionKind::Down || action.contact < 0
            || action.contact >= kMaxContacts) {
            throw std::runtime_error(
                "touch script: reservation must be a fixed contact DOWN");
        }
        const double due = engine_due(action, config);
        const int64_t due_ns = time_key(due);
        reservations.push_back(ContactInterval{
            action.contact,
            due_ns,
            std::numeric_limits<int64_t>::max(),
        });
    }

    int transient_cursor = transient_cursor_;
    constexpr int transient_order[kMaxContacts] = {
        7, 8, 9, 0, 1, 2, 3, 4, 5, 6,
    };
    std::vector<LowEvent> all_events = explicit_events;
    for (const TimedAction& item : timed) {
        if (item.action.contact >= 0) {
            continue;
        }
        const double duration_s = item.action.kind == ActionKind::Tap
            ? config.tap_duration_ms / kMillis
            : config.flick_duration_ms / kMillis;
        const int64_t end_ns = time_key(item.due + duration_s);
        int contact = -1;
        for (int attempt = 0; attempt < kMaxContacts; ++attempt) {
            const int candidate = transient_order[
                (transient_cursor + attempt) % kMaxContacts];
            if (item.due_ns
                < contact_available_after_[static_cast<std::size_t>(candidate)]) {
                continue;
            }
            bool busy = false;
            for (const ContactInterval& reservation : reservations) {
                if (reservation.contact == candidate
                    && overlaps(item.due_ns, end_ns, reservation)) {
                    busy = true;
                    break;
                }
            }
            if (!busy) {
                contact = candidate;
                transient_cursor = (transient_cursor + attempt + 1)
                    % kMaxContacts;
                break;
            }
        }
        if (contact < 0) {
            throw std::runtime_error(
                "touch script: no free contact for transient gesture");
        }
        reservations.push_back(ContactInterval{
            contact, item.due_ns, end_ns});
        expand_action(item, contact, false, config, all_events, event_order);
    }

    std::stable_sort(all_events.begin(), all_events.end(), low_event_less);
    validate_unique_same_time_operations(all_events);
    const ContactAnalysis final_analysis = analyse_contacts(
        all_events, active_contacts_, contact_available_after_);
    if (final_chunk && std::any_of(
            final_analysis.active.begin(), final_analysis.active.end(),
            [](bool active) { return active; })) {
        throw std::runtime_error(
            "touch script: final chunk leaves active contacts");
    }

    std::vector<LowEvent> emitted_events;
    std::vector<LowEvent> deferred_events;
    emitted_events.reserve(all_events.size());
    deferred_events.reserve(all_events.size());
    if (final_chunk || !std::isfinite(end_engine_time)) {
        emitted_events = all_events;
    } else {
        const int64_t end_engine_time_ns = time_key(end_engine_time);
        for (const LowEvent& event : all_events) {
            if (event.due_ns <= end_engine_time_ns) {
                emitted_events.push_back(event);
            } else {
                deferred_events.push_back(event);
            }
        }
    }
    const ContactAnalysis emitted_analysis = analyse_contacts(
        emitted_events, active_contacts_, contact_available_after_);

    std::vector<std::string> script;
    script.reserve(emitted_events.size() * 2 + 1);
    std::vector<TouchExecutionReceipt> receipts;
    receipts.reserve(emitted_events.size());
    double cursor = canonical_time(time_key(start_engine_time));
    double residual = residual_offset_ms_;
    double loss = rounding_loss_ms_;

    auto account = [&](double type_offset_ms) {
        residual += offsets_.interval_ms + type_offset_ms;
    };
    auto emit_commit = [&]() {
        if (script.empty() || script.back() != "c\n") {
            script.push_back("c\n");
        }
    };
    auto emit_wait = [&](double wait_s) {
        if (wait_s <= 0.0) {
            return;
        }
        account(offsets_.wait_ms);
        const double ideal_wait_ms = wait_s * kMillis;
        double compensated_wait_ms = ideal_wait_ms;
        // 正补偿只能吃掉本段确实存在的等待；不足 1ms 的短段把剩余欠账
        // 留给后续窗口，不能凭空生成负等待。
        const double max_offset_adjust = std::min(
            kMaxOffsetPerWaitMs, compensated_wait_ms);
        const double offset_adjust = clamp(
            residual, -kMaxOffsetPerWaitMs, max_offset_adjust);
        compensated_wait_ms -= offset_adjust;
        residual -= offset_adjust;
        const double previous_loss = loss;
        const double loss_adjust = clamp(
            previous_loss, -kMaxLossMs, kMaxLossMs);
        double wait_ms = std::max(
            0.0, compensated_wait_ms - loss_adjust);
        double emitted_wait_ms = 0.0;
        const double chunk_ms = std::max(1, config.max_wait_ms);
        while (wait_ms > 0.0) {
            const double piece = std::min(wait_ms, chunk_ms);
            const long rounded = std::lround(piece);
            if (rounded > 0) {
                emit_commit();
                script.push_back(line({"w ", std::to_string(rounded)}));
                emitted_wait_ms += static_cast<double>(rounded);
                wait_ms -= piece;
            } else {
                wait_ms = 0.0;
            }
        }
        // cursor 只描述理想绝对时间轴；设备实际发出的整数等待与理想值
        // 之差由 loss 单独跨块携带。若同时按 rounded 推进 cursor，会在
        // 下一事件的 due-cursor 中再次补同一误差，长曲会产生随机游走。
        cursor += wait_s;
        loss = previous_loss + emitted_wait_ms - compensated_wait_ms;
    };
    auto emit_event = [&](const LowEvent& event) {
        const std::string contact = std::to_string(event.contact);
        const std::size_t line_index = script.size();
        TouchCommandKind command = TouchCommandKind::Down;
        switch (event.kind) {
            case LowKind::Move:
                command = TouchCommandKind::Move;
                account(offsets_.move_ms);
                script.push_back(line({"m ", contact, " ",
                    std::to_string(event.x), " ", std::to_string(event.y),
                    " 50"}));
                break;
            case LowKind::Up:
                command = TouchCommandKind::Up;
                account(offsets_.up_ms);
                script.push_back(line({"u ", contact}));
                break;
            case LowKind::Down:
                command = TouchCommandKind::Down;
                account(offsets_.down_ms);
                script.push_back(line({"d ", contact, " ",
                    std::to_string(event.x), " ", std::to_string(event.y),
                    " 50"}));
                break;
        }
        if (event.emits_receipt) {
            receipts.push_back(TouchExecutionReceipt{
                line_index,
                event.planned_engine_s,
                event.action_token,
                command,
            });
        }
    };

    std::size_t begin = 0;
    while (begin < emitted_events.size()) {
        std::size_t end = begin + 1;
        while (end < emitted_events.size()
            && emitted_events[begin].due_ns == emitted_events[end].due_ns) {
            ++end;
        }
        emit_wait(emitted_events[begin].due - cursor);

        std::array<std::vector<const LowEvent*>, 3> phases;
        std::array<std::size_t, kMaxContacts> contact_depth{};
        for (std::size_t index = begin; index < end; ++index) {
            const LowEvent& event = emitted_events[index];
            const std::size_t contact =
                static_cast<std::size_t>(event.contact);
            const std::size_t phase = contact_depth[contact]++;
            if (phase >= phases.size()) {
                throw std::runtime_error(
                    "touch script: too many operations for contact at one timestamp");
            }
            phases[phase].push_back(&event);
        }
        for (auto& phase : phases) {
            std::stable_sort(phase.begin(), phase.end(),
                [](const LowEvent* left, const LowEvent* right) {
                    return left->order < right->order;
                });
            for (const LowEvent* event : phase) {
                emit_event(*event);
            }
            if (!phase.empty()) {
                emit_commit();
            }
        }
        begin = end;
    }

    if (std::isfinite(end_engine_time)) {
        const double canonical_end = canonical_time(time_key(end_engine_time));
        if (canonical_end > cursor) {
            emit_wait(canonical_end - cursor);
        }
    }
    emit_commit();

    // 只有完整编译成功后才提交跨切片状态，异常不会污染后续重试。
    active_contacts_ = emitted_analysis.active;
    contact_available_after_ = emitted_analysis.available_after;
    pending_events_ = std::move(deferred_events);
    next_event_order_ = event_order;
    next_action_token_ = action_token;
    last_execution_receipts_ = std::move(receipts);
    transient_cursor_ = transient_cursor;
    residual_offset_ms_ = residual;
    rounding_loss_ms_ = loss;
    return script;
}

}  // namespace mbdr
