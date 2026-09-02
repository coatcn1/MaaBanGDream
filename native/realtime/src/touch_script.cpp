#include "maabangdream/touch_script.hpp"

#include <algorithm>
#include <cmath>
#include <set>
#include <sstream>

namespace mbdr {
namespace {

constexpr double kMillis = 1000.0;
constexpr double kMaxLossMs = 2.0;

double engine_due(const ScheduledAction& action, const EngineConfig& config) {
    // press_bias_ms 正值 = 提前输入，沿用 types.hpp 的换算约定。
    return action.due_s - config.song_offset_s
        - config.press_bias_ms / kMillis;
}

int latency_offset_ms(ActionKind kind, const TouchLatencyOffsets& offsets) {
    switch (kind) {
        case ActionKind::Down:
            return offsets.down_ms;
        case ActionKind::Up:
            return offsets.up_ms;
        case ActionKind::Move:
            return offsets.move_ms;
        case ActionKind::Tap:
            return offsets.tap_ms;
        case ActionKind::Flick:
            return offsets.flick_ms;
    }
    return 0;
}

int rounded_x(float target_x, uint8_t lane, const EngineConfig& config) {
    if (target_x > 0.0F) {
        return static_cast<int>(std::lround(target_x));
    }
    return static_cast<int>(std::lround(
        config.lane_centers[std::min<size_t>(lane, kLaneCount - 1)]));
}

std::string line(std::initializer_list<std::string> parts) {
    std::ostringstream out;
    for (const std::string& part : parts) {
        out << part;
    }
    out << '\n';
    return out.str();
}

}  // namespace

std::vector<std::string> TouchScriptCompiler::compile(
    std::vector<ScheduledAction> actions,
    const EngineConfig& config,
    double start_engine_time) const {
    struct Timed {
        ScheduledAction action;
        double due = 0.0;
    };

    std::vector<Timed> timed;
    timed.reserve(actions.size());
    for (ScheduledAction& action : actions) {
        timed.push_back(Timed{
            action,
            engine_due(action, config)
                - latency_offset_ms(action.kind, offsets_) / kMillis,
        });
    }
    std::stable_sort(timed.begin(), timed.end(),
        [](const Timed& left, const Timed& right) {
            return left.due < right.due;
        });

    // hold 触点占用表：瞬态动作的轮转触点必须避开这些正在按住的触点。
    std::vector<std::pair<int, double>> occupied;
    for (const Timed& item : timed) {
        if (item.action.kind == ActionKind::Down && item.action.contact >= 0) {
            occupied.push_back({item.action.contact, item.due});
        }
    }
    std::vector<double> release_after;
    release_after.reserve(occupied.size());
    for (const Timed& item : timed) {
        if (item.action.kind == ActionKind::Up && item.action.contact >= 0) {
            for (size_t i = 0; i < occupied.size(); ++i) {
                if (occupied[i].first == item.action.contact) {
                    if (release_after.size() <= i) {
                        release_after.resize(i + 1, 0.0);
                    }
                    release_after[i] = item.due;
                }
            }
        }
    }

    int transient_cursor = 0;
    constexpr int transient_order[kMaxContacts] = {7, 8, 9, 0, 1, 2, 3, 4, 5, 6};
    auto busy = [&](int contact, double due) {
        for (size_t i = 0; i < occupied.size(); ++i) {
            if (occupied[i].first == contact
                && occupied[i].second <= due
                && (i >= release_after.size() || release_after[i] >= due)) {
                return true;
            }
        }
        return false;
    };

    std::vector<std::string> script;
    script.reserve(timed.size() * 3);
    double cursor = start_engine_time;
    double loss_ms = 0.0;

    for (Timed& item : timed) {
        ScheduledAction& action = item.action;
        if (action.contact < 0) {
            int contact = -1;
            for (int attempt = 0; attempt < kMaxContacts; ++attempt) {
                int candidate = transient_order[
                    (transient_cursor + attempt) % kMaxContacts];
                if (!busy(candidate, item.due)) {
                    contact = candidate;
                    break;
                }
            }
            action.contact = static_cast<int8_t>(
                contact >= 0 ? contact : transient_order[transient_cursor]);
            transient_cursor = (transient_cursor + 1) % kMaxContacts;
        }

        const double wait_s = std::max(0.0, item.due - cursor);
        const double compensated_ms = wait_s * kMillis + loss_ms;
        long wait_ms = std::lround(compensated_ms);
        if (wait_ms < 0) {
            wait_ms = 0;
        }
        loss_ms = std::max(-kMaxLossMs,
            std::min(kMaxLossMs, compensated_ms - static_cast<double>(wait_ms)));
        cursor += static_cast<double>(wait_ms) / kMillis;
        if (wait_ms > 0) {
            script.push_back(line({"w ", std::to_string(wait_ms)}));
        }

        const int x = rounded_x(action.target_x, action.lane, config);
        const int y = static_cast<int>(std::lround(config.judgement_y));
        const std::string contact = std::to_string(action.contact);
        switch (action.kind) {
            case ActionKind::Tap:
                script.push_back(line({"d ", contact, " ", std::to_string(x),
                    " ", std::to_string(y), " 50"}));
                script.push_back(line({"c"}));
                script.push_back(line({"w 12"}));
                cursor += 12.0 / kMillis;
                script.push_back(line({"u ", contact}));
                script.push_back(line({"c"}));
                break;
            case ActionKind::Flick: {
                const int swipe_x = action.flick_direction == -1
                    ? x - 120
                    : (action.flick_direction == 1 ? x + 120 : x);
                const int swipe_y = action.flick_direction == 0 ? y - 120 : y + 8;
                script.push_back(line({"d ", contact, " ", std::to_string(x),
                    " ", std::to_string(y), " 50"}));
                script.push_back(line({"c"}));
                script.push_back(line({"w 8"}));
                cursor += 8.0 / kMillis;
                script.push_back(line({"m ", contact, " ",
                    std::to_string(swipe_x), " ", std::to_string(swipe_y),
                    " 50"}));
                script.push_back(line({"c"}));
                script.push_back(line({"w 8"}));
                cursor += 8.0 / kMillis;
                script.push_back(line({"u ", contact}));
                script.push_back(line({"c"}));
                break;
            }
            case ActionKind::Down:
                script.push_back(line({"d ", contact, " ", std::to_string(x),
                    " ", std::to_string(y), " 50"}));
                script.push_back(line({"c"}));
                break;
            case ActionKind::Move:
                script.push_back(line({"m ", contact, " ", std::to_string(x),
                    " ", std::to_string(y), " 50"}));
                script.push_back(line({"c"}));
                break;
            case ActionKind::Up:
                script.push_back(line({"u ", contact}));
                script.push_back(line({"c"}));
                break;
        }
    }
    return script;
}

}  // namespace mbdr
