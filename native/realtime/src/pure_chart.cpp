#include "maabangdream/pure_chart.hpp"

#include <algorithm>
#include <bitset>
#include <cmath>
#include <stdexcept>

namespace mbdr {

namespace {

// hold 触点分配器：按 head 时间顺序扫描，任何 hold 的 tail 时间不晚于
// 当前 head 时间时释放其触点；始终取编号最小的空闲触点。
// 结果确定，与 Python 参考实现一致。
class ContactAllocator {
public:
    ContactAllocator() {
        free_.set();  // 初始 0..9 全部可用。
    }

    int acquire(double head_time_s, double tail_time_s) {
        std::vector<PendingRelease> remaining;
        for (const auto& release : pending_releases_) {
            if (release.tail_time_s <= head_time_s) {
                free_.set(static_cast<std::size_t>(release.contact));
            } else {
                remaining.push_back(release);
            }
        }
        pending_releases_.swap(remaining);
        for (std::size_t contact = 0; contact < kMaxContacts; ++contact) {
            if (free_.test(contact)) {
                free_.reset(contact);
                pending_releases_.push_back(
                    PendingRelease{static_cast<int8_t>(contact), tail_time_s});
                return static_cast<int8_t>(contact);
            }
        }
        throw std::runtime_error(
            "pure chart: hold contact exhaustion (more than 10 simultaneous holds)");
    }

private:
    struct PendingRelease {
        int8_t contact = -1;
        double tail_time_s = 0.0;
    };

    std::bitset<kMaxContacts> free_{};
    std::vector<PendingRelease> pending_releases_;
};

float lane_center_x(const EngineConfig& config, int lane) {
    if (lane < 0 || lane >= static_cast<int>(kLaneCount)) {
        throw std::runtime_error("pure chart: lane out of range");
    }
    return config.lane_centers[static_cast<std::size_t>(lane)];
}

float lane_position_x(const EngineConfig& config, double lane) {
    if (!std::isfinite(lane) || lane < -0.5 || lane > 6.5) {
        throw std::runtime_error("pure chart: path lane out of range");
    }
    if (lane <= 0.0) {
        const double delta = config.lane_centers[1] - config.lane_centers[0];
        return static_cast<float>(config.lane_centers[0] + lane * delta);
    }
    if (lane >= static_cast<double>(kLaneCount - 1)) {
        const std::size_t last = kLaneCount - 1;
        const double delta = config.lane_centers[last]
            - config.lane_centers[last - 1];
        return static_cast<float>(config.lane_centers[last]
            + (lane - static_cast<double>(last)) * delta);
    }
    const std::size_t left = static_cast<std::size_t>(std::floor(lane));
    const double fraction = lane - static_cast<double>(left);
    return static_cast<float>(config.lane_centers[left]
        + fraction * (config.lane_centers[left + 1]
            - config.lane_centers[left]));
}

int action_rank(ActionKind kind) {
    // 同一条 HOLD 的尾点 MOVE 必须先于 FLICK/UP；否则编译器会在触点
    // 尚未到达尾轨时结束手势。
    switch (kind) {
        case ActionKind::Move: return 0;
        case ActionKind::Flick: return 1;
        case ActionKind::Up: return 2;
        case ActionKind::Down: return 3;
        case ActionKind::Tap: return 4;
    }
    return 5;
}

const HoldPath* find_path(
    const ChartTimeline& timeline,
    int note_index) {
    for (const HoldPath& candidate : timeline.hold_paths) {
        if (candidate.note_index == note_index) {
            return &candidate;
        }
    }
    return nullptr;
}

}  // namespace

std::vector<ScheduledAction> compile_pure_chart_actions(
    const ChartTimeline& timeline,
    const EngineConfig& config) {
    if (!std::isfinite(config.slide_step_s) || config.slide_step_s <= 0.0) {
        throw std::runtime_error("pure chart: slide_step_s must be positive");
    }
    if (config.flick_duration_ms <= 0) {
        throw std::runtime_error(
            "pure chart: flick_duration_ms must be positive");
    }
    std::vector<ScheduledAction> actions;
    actions.reserve(timeline.judgements.size() * 2);

    // hold 的触点分配只看 head 时间，因此按判定顺序即可；每次先释放
    // tail 时间已过的触点。
    ContactAllocator allocator;
    // note_index 是全局音符计数（tap 也占号），其上界不超过判定数量。
    std::vector<int> hold_contacts(timeline.judgements.size() + 1, -1);

    for (const ChartJudgement& judgement : timeline.judgements) {
        ScheduledAction action;
        action.due_s = judgement.time_s;
        action.lane = judgement.lane;
        action.note_index = judgement.note_index;
        switch (judgement.kind) {
            case JudgementKind::Tap: {
                action.kind =
                    judgement.flick ? ActionKind::Flick : ActionKind::Tap;
                action.contact = -1;  // 瞬态：由 Python 派发时分配。
                action.target_x = lane_center_x(config, judgement.lane);
                action.flick_direction = judgement.direction;
                actions.push_back(action);
                break;
            }
            case JudgementKind::HoldHead: {
                const HoldPath* path =
                    find_path(timeline, judgement.note_index);
                if (path == nullptr || path->points.size() < 2) {
                    throw std::runtime_error(
                        "pure chart: hold head without a valid hold path");
                }
                const double tail_time_s = path->points.back().time_s
                    + (path->points.back().flick
                        ? static_cast<double>(config.flick_duration_ms) / 1000.0
                        : 0.0);
                const int contact =
                    allocator.acquire(judgement.time_s, tail_time_s);
                hold_contacts[static_cast<std::size_t>(judgement.note_index)] =
                    contact;
                action.kind = ActionKind::Down;
                action.contact = static_cast<int8_t>(contact);
                action.target_x = lane_center_x(
                    config, static_cast<int>(path->points.front().lane));
                actions.push_back(action);
                break;
            }
            case JudgementKind::HoldTail: {
                const HoldPath* path =
                    find_path(timeline, judgement.note_index);
                if (path == nullptr || path->points.size() < 2) {
                    throw std::runtime_error(
                        "pure chart: hold tail without a valid hold path");
                }
                const int contact =
                    hold_contacts[static_cast<std::size_t>(judgement.note_index)];
                if (contact < 0) {
                    throw std::runtime_error(
                        "pure chart: hold tail reached before its head");
                }
                if (judgement.tail_flick) {
                    action.kind = ActionKind::Flick;
                    action.flick_direction = judgement.direction;
                } else {
                    action.kind = ActionKind::Up;
                }
                action.contact = static_cast<int8_t>(contact);
                action.target_x = lane_center_x(
                    config, static_cast<int>(path->points.back().lane));
                actions.push_back(action);
                break;
            }
        }
    }

    // 沿每段连接线按固定步长生成 MOVE。隐藏点虽然不是判定点，但仍定义
    // Slide 几何；每段都排除起点、包含终点，保证触点精确到达最终尾轨。
    for (const HoldPath& path : timeline.hold_paths) {
        if (path.points.size() < 2) {
            continue;
        }
        const int contact =
            hold_contacts[static_cast<std::size_t>(path.note_index)];
        if (contact < 0) {
            continue;
        }
        for (std::size_t index = 1; index < path.points.size(); ++index) {
            const PathPoint& from = path.points[index - 1];
            const PathPoint& to = path.points[index];
            if (to.time_s < from.time_s) {
                throw std::runtime_error(
                    "pure chart: hold path time must be monotonic");
            }
            const float from_x = lane_position_x(config, from.lane);
            const float to_x = lane_position_x(config, to.lane);
            if (std::abs(to_x - from_x) < 1e-6F) {
                continue;
            }

            const double duration_s = to.time_s - from.time_s;
            std::size_t regular_steps = 0;
            if (duration_s > 0.0) {
                regular_steps = static_cast<std::size_t>(std::floor(
                    std::max(0.0, duration_s - 1e-12)
                    / config.slide_step_s));
            }
            for (std::size_t step = 1; step <= regular_steps + 1; ++step) {
                const bool endpoint = step > regular_steps;
                const double due_s = endpoint
                    ? to.time_s
                    : from.time_s
                        + static_cast<double>(step) * config.slide_step_s;
                if (!endpoint && due_s >= to.time_s - 1e-12) {
                    continue;
                }
                const double progress = duration_s <= 0.0
                    ? 1.0
                    : (due_s - from.time_s) / duration_s;
                const double lane = from.lane + (to.lane - from.lane) * progress;
                const int rounded_lane = std::max(0,
                    std::min(static_cast<int>(kLaneCount) - 1,
                        static_cast<int>(std::lround(lane))));
                ScheduledAction action;
                action.kind = ActionKind::Move;
                action.lane = static_cast<uint8_t>(rounded_lane);
                action.contact = static_cast<int8_t>(contact);
                action.target_x = static_cast<float>(
                    from_x + (to_x - from_x) * progress);
                action.due_s = due_s;
                action.note_index = path.note_index;
                actions.push_back(action);
            }
        }
    }

    // 稳定排序：按 due_s；同一时刻保持 note_index 顺序，保证双押确定性。
    std::stable_sort(actions.begin(), actions.end(),
        [](const ScheduledAction& lhs, const ScheduledAction& rhs) {
            if (lhs.due_s != rhs.due_s) {
                return lhs.due_s < rhs.due_s;
            }
            if (lhs.note_index != rhs.note_index) {
                return lhs.note_index < rhs.note_index;
            }
            return action_rank(lhs.kind) < action_rank(rhs.kind);
        });
    return actions;
}

}  // namespace mbdr
