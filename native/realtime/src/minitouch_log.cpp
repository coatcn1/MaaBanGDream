#include "maabangdream/minitouch_log.hpp"

#include <cstdlib>
#include <cstring>

namespace mbdr {

bool parse_minitouch_log(std::string_view view, MinitouchLogEvent* out) {
    if (out == nullptr || view.rfind("jlog ", 0) != 0) {
        return false;
    }
    const std::string text(view);
    auto field = [&](const char* key, double* value) -> bool {
        const size_t pos = text.find(key);
        if (pos == std::string::npos) {
            return false;
        }
        const size_t start = pos + std::strlen(key);
        char* end = nullptr;
        *value = std::strtod(text.c_str() + start, &end);
        return end != text.c_str() + start;
    };
    if (!field("\"st\":", &out->start_ms)
        || !field("\"et\":", &out->end_ms)
        || !field("\"c\":", &out->cost_ms)) {
        return false;
    }
    constexpr const char* kCmdKey = "\"cmd\": \"";
    const size_t cmd_pos = text.find(kCmdKey);
    if (cmd_pos == std::string::npos) {
        return false;
    }
    const size_t value_pos = cmd_pos + std::strlen(kCmdKey);
    const size_t cmd_end = text.find('"', value_pos);
    if (cmd_end == std::string::npos) {
        return false;
    }
    out->command = text.substr(value_pos, cmd_end - value_pos);
    return true;
}

void LatencyCalibrator::observe(const MinitouchLogEvent& event) {
    ++event_count_;
    if (last_end_ms_ >= 0.0) {
        interval_.total_cost_ms += event.start_ms - last_end_ms_;
        ++interval_.count;
    }
    last_end_ms_ = event.end_ms;

    if (event.command.empty()) {
        return;
    }
    const char kind = event.command[0];
    if (kind == 'w') {
        // 名义等待毫秒 = "w <ms>" 的整数参数。
        const double nominal = std::strtod(event.command.c_str() + 2, nullptr);
        wait_.total_cost_ms += event.cost_ms - nominal;
        ++wait_.count;
    } else if (kind == 'd') {
        down_.total_cost_ms += event.cost_ms;
        ++down_.count;
        ++uncommitted_down_;
    } else if (kind == 'm') {
        move_.total_cost_ms += event.cost_ms;
        ++move_.count;
        ++uncommitted_move_;
    } else if (kind == 'u') {
        up_.total_cost_ms += event.cost_ms;
        ++up_.count;
        ++uncommitted_up_;
    } else if (kind == 'c') {
        const int total = uncommitted_down_ + uncommitted_up_
            + uncommitted_move_;
        if (total > 0) {
            down_.total_cost_ms
                += event.cost_ms * uncommitted_down_ / total;
            up_.total_cost_ms += event.cost_ms * uncommitted_up_ / total;
            move_.total_cost_ms
                += event.cost_ms * uncommitted_move_ / total;
            uncommitted_down_ = 0;
            uncommitted_up_ = 0;
            uncommitted_move_ = 0;
        }
    }
}

TouchLatencyOffsets LatencyCalibrator::offsets() const {
    TouchLatencyOffsets result;
    if (down_.count > 0) {
        result.down_ms = down_.total_cost_ms / down_.count;
    }
    if (up_.count > 0) {
        result.up_ms = up_.total_cost_ms / up_.count;
    }
    if (move_.count > 0) {
        result.move_ms = move_.total_cost_ms / move_.count;
    }
    if (wait_.count > 0) {
        result.wait_ms = wait_.total_cost_ms / wait_.count;
    }
    if (interval_.count > 0) {
        result.interval_ms = interval_.total_cost_ms / interval_.count;
    }
    return result;
}

double LatencyCalibrator::correction_ms(
    const TouchLatencyOffsets& previous) const {
    double total = 0.0;
    auto add = [&](const Stats& stats, double old_average) {
        if (stats.count > 0) {
            total += stats.total_cost_ms - old_average * stats.count;
        }
    };
    add(down_, previous.down_ms);
    add(up_, previous.up_ms);
    add(move_, previous.move_ms);
    add(wait_, previous.wait_ms);
    add(interval_, previous.interval_ms);
    return total;
}

void LatencyCalibrator::reset() {
    down_ = {};
    up_ = {};
    move_ = {};
    wait_ = {};
    interval_ = {};
    uncommitted_down_ = 0;
    uncommitted_up_ = 0;
    uncommitted_move_ = 0;
    last_end_ms_ = -1.0;
    event_count_ = 0;
}

}  // namespace mbdr
