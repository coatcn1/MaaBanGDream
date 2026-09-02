#include "maabangdream/touch_script.hpp"

#include <algorithm>
#include <cmath>
#include <sstream>

namespace mbdr {
namespace {

constexpr double kMillis = 1000.0;
constexpr double kMaxOffsetPerWaitMs = 1.0;
constexpr double kMaxLossMs = 2.0;

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

double clamp(double value, double lo, double hi) {
    return std::max(lo, std::min(hi, value));
}

}  // namespace

std::vector<std::string> TouchScriptCompiler::compile(
    std::vector<ScheduledAction> actions,
    const EngineConfig& config,
    double start_engine_time) {
    struct Timed {
        ScheduledAction action;
        double due = 0.0;
    };

    std::vector<Timed> timed;
    timed.reserve(actions.size());
    for (ScheduledAction& action : actions) {
        timed.push_back(Timed{action, engine_due(action, config)});
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
    // 未清偿补偿与取整损失是编译器跨切片状态：同一次分片发布过程中必须
    // 跨 compile() 调用保留，否则切片边界会丢补偿精度。
    double& residual = residual_offset_ms_;
    double& loss = rounding_loss_ms_;

    // 每条命令前累加 interval + 类型 offset；每次 w 前先 commit 冲刷触点，
    // 再按残差缩短 w（每次最多 ±1ms），未清偿部分留给后续 w。
    auto account = [&](double type_offset_ms) {
        residual += offsets_.interval_ms + type_offset_ms;
    };
    auto emit_wait = [&](double wait_s) {
        if (wait_s <= 0.0) {
            return;
        }
        account(offsets_.wait_ms);
        double wait_ms = wait_s * kMillis;
        const double offset_adjust = clamp(residual, -kMaxOffsetPerWaitMs,
            kMaxOffsetPerWaitMs);
        wait_ms -= offset_adjust;
        residual -= offset_adjust;
        const double loss_adjust = clamp(loss, -kMaxLossMs, kMaxLossMs);
        wait_ms -= loss_adjust;
        loss -= loss_adjust;
        wait_ms = std::max(0.0, wait_ms);
        // 分段等待：单个 w 阻塞设备端读循环，切小段可把停止/异常时
        // panic reset（r）的生效延迟限制在 max_wait_ms 内。
        const double chunk_ms = std::max(1, config.max_wait_ms);
        while (wait_ms > 0.0) {
            const double piece = std::min(wait_ms, chunk_ms);
            const long rounded = std::lround(piece);
            if (rounded > 0) {
                loss -= piece - static_cast<double>(rounded);
                script.push_back("c\n");
                script.push_back(line({"w ", std::to_string(rounded)}));
                cursor += static_cast<double>(rounded) / kMillis;
                wait_ms -= piece;
            } else {
                // piece < 0.5ms 取整为 0：直接丢弃该余量，损失记入
                // rounding_loss 由下一个 w 补偿，避免死循环。
                loss -= piece;
                wait_ms = 0.0;
            }
        }
    };
    auto emit_command = [&](const std::string& text) {
        script.push_back(text);
    };

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

        emit_wait(item.due - cursor);

        const int x = rounded_x(action.target_x, action.lane, config);
        const int y = static_cast<int>(std::lround(config.judgement_y));
        const std::string contact = std::to_string(action.contact);
        switch (action.kind) {
            case ActionKind::Tap:
                account(offsets_.down_ms);
                emit_command(line({"d ", contact, " ", std::to_string(x),
                    " ", std::to_string(y), " 50"}));
                emit_wait(12.0 / kMillis);
                account(offsets_.up_ms);
                emit_command(line({"u ", contact}));
                break;
            case ActionKind::Flick: {
                const int swipe_x = action.flick_direction == -1
                    ? x - 120
                    : (action.flick_direction == 1 ? x + 120 : x);
                const int swipe_y = action.flick_direction == 0 ? y - 120 : y + 8;
                account(offsets_.down_ms);
                emit_command(line({"d ", contact, " ", std::to_string(x),
                    " ", std::to_string(y), " 50"}));
                emit_wait(8.0 / kMillis);
                account(offsets_.move_ms);
                emit_command(line({"m ", contact, " ",
                    std::to_string(swipe_x), " ", std::to_string(swipe_y),
                    " 50"}));
                emit_wait(8.0 / kMillis);
                account(offsets_.up_ms);
                emit_command(line({"u ", contact}));
                break;
            }
            case ActionKind::Down:
                account(offsets_.down_ms);
                emit_command(line({"d ", contact, " ", std::to_string(x),
                    " ", std::to_string(y), " 50"}));
                break;
            case ActionKind::Move:
                account(offsets_.move_ms);
                emit_command(line({"m ", contact, " ", std::to_string(x),
                    " ", std::to_string(y), " 50"}));
                break;
            case ActionKind::Up:
                account(offsets_.up_ms);
                emit_command(line({"u ", contact}));
                break;
        }
    }
    // 切片末尾 commit，冲刷最后一批触点状态。
    script.push_back("c\n");
    return script;
}

}  // namespace mbdr
