#include <cmath>
#include <string>

#include "maabangdream/pure_chart.hpp"
#include "maabangdream/types.hpp"
#include "test_macros.hpp"

using namespace mbdr;

namespace {

const char* kChart = R"json(
[
  {"type": "BPM", "bpm": 120, "beat": 0},
  {"type": "Single", "lane": 0, "beat": 4.0},
  {"type": "Single", "lane": 6, "beat": 4.0},
  {"type": "Single", "lane": 3, "beat": 4.5},
  {"type": "Directional", "lane": 2, "beat": 5.0, "direction": "Left"},
  {"type": "Long", "connections": [
    {"lane": 1, "beat": 6.0},
    {"lane": 1, "beat": 10.0},
    {"lane": 1, "beat": 10.0, "flick": true}
  ]},
  {"type": "Slide", "connections": [
    {"lane": 5, "beat": 8.0},
    {"lane": 4, "beat": 10.0},
    {"lane": 3, "beat": 14.0}
  ]}
]
)json";

ChartTimeline make_chart() {
    return ChartTimeline::from_json_string(kChart);
}

void test_taps_flicks_and_double_press() {
    const ChartTimeline timeline = make_chart();
    const auto actions = compile_pure_chart_actions(timeline, EngineConfig{});
    int taps = 0;
    int flicks = 0;
    int double_press_pairs = 0;
    for (std::size_t index = 0; index < actions.size(); ++index) {
        const ScheduledAction& action = actions[index];
        if (action.kind == ActionKind::Tap) {
            ++taps;
        }
        if (action.kind == ActionKind::Flick && action.contact < 0) {
            ++flicks;
        }
        if (index > 0 &&
            actions[index - 1].due_s == action.due_s &&
            actions[index - 1].lane != action.lane &&
            action.kind == ActionKind::Tap) {
            ++double_press_pairs;
        }
    }
    CHECK_EQ(taps, 3);       // lane 0/6/3 三个 Single。
    CHECK_EQ(flicks, 1);     // Directional。
    CHECK_EQ(double_press_pairs, 1);  // 4.0s 双押。
}

void test_hold_lifecycle_and_tail_flick() {
    const ChartTimeline timeline = make_chart();
    const auto actions = compile_pure_chart_actions(timeline, EngineConfig{});
    int downs = 0;
    int ups = 0;
    int tail_flicks = 0;
    int moves = 0;
    for (const ScheduledAction& action : actions) {
        if (action.kind == ActionKind::Down) {
            ++downs;
            CHECK(action.contact >= 0);
        }
        if (action.kind == ActionKind::Up) {
            ++ups;
        }
        if (action.kind == ActionKind::Flick && action.contact >= 0) {
            ++tail_flicks;
        }
        if (action.kind == ActionKind::Move) {
            ++moves;
        }
    }
    CHECK_EQ(downs, 2);
    CHECK_EQ(ups, 1);         // Slide 尾普通 UP。
    CHECK_EQ(tail_flicks, 1); // Long 尾 FLICK。
    CHECK_EQ(moves, 1);       // Slide 仅一个内部 lane 变化连接点。

    // hold 触点确定性：Long 先占 0，Slide 后占 1。
    int long_contact = -1;
    int slide_contact = -1;
    for (const ScheduledAction& action : actions) {
        if (action.kind == ActionKind::Down && action.lane == 1) {
            long_contact = action.contact;
        }
        if (action.kind == ActionKind::Down && action.lane == 5) {
            slide_contact = action.contact;
        }
    }
    CHECK_EQ(long_contact, 0);
    CHECK_EQ(slide_contact, 1);
}

void test_contact_reuse_and_exhaustion() {
    // 前一个 hold 结束后触点应被复用。
    const char* reuse_chart = R"json(
[
  {"type": "BPM", "bpm": 120, "beat": 0},
  {"type": "Long", "connections": [
    {"lane": 1, "beat": 2.0}, {"lane": 1, "beat": 3.0}
  ]},
  {"type": "Long", "connections": [
    {"lane": 2, "beat": 4.0}, {"lane": 2, "beat": 5.0}
  ]}
]
)json";
    const ChartTimeline timeline =
        ChartTimeline::from_json_string(reuse_chart);
    const auto actions = compile_pure_chart_actions(timeline, EngineConfig{});
    int first_contact = -1;
    int second_contact = -1;
    for (const ScheduledAction& action : actions) {
        if (action.kind != ActionKind::Down) {
            continue;
        }
        if (action.lane == 1) {
            first_contact = action.contact;
        } else {
            second_contact = action.contact;
        }
    }
    CHECK_EQ(first_contact, 0);
    CHECK_EQ(second_contact, 0);  // 复用。

    // 超过 10 个同时 hold 必须 fail-closed。
    std::string exhaustion = R"json([{"type":"BPM","bpm":120,"beat":0})json";
    for (int lane = 0; lane < 11; ++lane) {
        exhaustion += ",{\"type\":\"Long\",\"connections\":[";
        exhaustion += "{\"lane\":" + std::to_string(lane % 7) + ",\"beat\":10},";
        exhaustion += "{\"lane\":" + std::to_string(lane % 7) + ",\"beat\":20}]}";
    }
    exhaustion += "]";
    const ChartTimeline overload =
        ChartTimeline::from_json_string(exhaustion);
    bool threw = false;
    try {
        compile_pure_chart_actions(overload, EngineConfig{});
    } catch (const std::runtime_error&) {
        threw = true;
    }
    if (!threw) {
        std::fprintf(stderr, "NOTE: exhaustion did not throw\n");
    }
    CHECK(threw);
}

void test_determinism() {
    const ChartTimeline timeline = make_chart();
    const auto first = compile_pure_chart_actions(timeline, EngineConfig{});
    const auto second = compile_pure_chart_actions(timeline, EngineConfig{});
    CHECK_EQ(first.size(), second.size());
    for (std::size_t index = 0; index < first.size(); ++index) {
        CHECK(first[index].kind == second[index].kind);
        CHECK_EQ(first[index].lane, second[index].lane);
        CHECK_EQ(first[index].contact, second[index].contact);
        CHECK(std::abs(first[index].due_s - second[index].due_s) < 1e-12);
    }
}

}  // namespace

int run_pure_chart_tests() {
    test_taps_flicks_and_double_press();
    test_hold_lifecycle_and_tail_flick();
    test_contact_reuse_and_exhaustion();
    test_determinism();
    return 0;
}
