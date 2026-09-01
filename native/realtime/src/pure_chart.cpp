#include "maabangdream/pure_chart.hpp"

#include <algorithm>
#include <bitset>
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
                const double tail_time_s = path->points.back().time_s;
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

    // 中间连接点生成 MOVE：仅可见点，且目标 lane 与上一已发射位置不同。
    for (const HoldPath& path : timeline.hold_paths) {
        if (path.points.size() < 3) {
            continue;
        }
        const int contact =
            hold_contacts[static_cast<std::size_t>(path.note_index)];
        if (contact < 0) {
            continue;
        }
        int previous_lane = static_cast<int>(path.points.front().lane);
        for (std::size_t index = 1; index + 1 < path.points.size(); ++index) {
            const PathPoint& point = path.points[index];
            if (point.hidden) {
                continue;
            }
            const int lane = static_cast<int>(point.lane);
            if (lane == previous_lane) {
                continue;
            }
            ScheduledAction action;
            action.kind = ActionKind::Move;
            action.lane = static_cast<uint8_t>(lane);
            action.contact = static_cast<int8_t>(contact);
            action.target_x = lane_center_x(config, lane);
            action.due_s = point.time_s;
            action.note_index = path.note_index;
            actions.push_back(action);
            previous_lane = lane;
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
            return static_cast<int>(lhs.kind) < static_cast<int>(rhs.kind);
        });
    return actions;
}

}  // namespace mbdr
