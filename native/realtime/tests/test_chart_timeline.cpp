#include <cmath>
#include <string>

#include "maabangdream/chart_timeline.hpp"
#include "maabangdream/types.hpp"
#include "test_macros.hpp"

using namespace mbdr;

namespace {

const char* kBasicChart = R"json(
[
  {"type": "BPM", "bpm": 192, "beat": 0},
  {"type": "Single", "lane": 0, "beat": 6.5},
  {"type": "Single", "lane": 2, "beat": 7.0},
  {"type": "Long", "connections": [
    {"lane": 1, "beat": 9.0},
    {"lane": 1, "beat": 10.0}
  ]},
  {"type": "Slide", "connections": [
    {"lane": 5, "beat": 12.0},
    {"lane": 4, "beat": 13.0}
  ]}
]
)json";

void test_tempo_map() {
    TempoMap map = TempoMap::from_events({{0.0, 192.0}});
    CHECK(std::abs(map.seconds_at(6.5) - 6.5 * 60.0 / 192.0) < 1e-9);

    // 首个 BPM 事件 beat > 0 时补 0。
    TempoMap late = TempoMap::from_events({{8.0, 120.0}});
    CHECK(std::abs(late.seconds_at(4.0) - 2.0) < 1e-9);

    // 分段 BPM：前 4 拍 120，后 4 拍 240。
    TempoMap piecewise =
        TempoMap::from_events({{0.0, 120.0}, {4.0, 240.0}});
    CHECK(std::abs(piecewise.seconds_at(4.0) - 2.0) < 1e-9);
    CHECK(std::abs(piecewise.seconds_at(8.0) - 3.0) < 1e-9);
}

void test_basic_compile() {
    ChartTimeline timeline = ChartTimeline::from_json_string(kBasicChart);
    CHECK_EQ(timeline.judgements.size(), static_cast<std::size_t>(6));
    CHECK_EQ(timeline.hold_paths.size(), static_cast<std::size_t>(2));
    // 排序后：2 taps + 2 hold head/tail + 2 slide head/tail。
    CHECK(timeline.judgements[0].kind == JudgementKind::Tap);
    CHECK(timeline.judgements[0].lane == 0);
    CHECK(timeline.judgements[1].kind == JudgementKind::Tap);
    CHECK(timeline.judgements[1].lane == 2);
}

void test_schema_v1_and_metadata() {
    const char* schema_v1 = R"json(
{
  "schema_version": 1,
  "source": {"provider": "bestdori"},
  "song": {"bestdori_id": 125},
  "difficulty": {"name": "hard", "level": 20},
  "chart": [
    {"type": "BPM", "bpm": 153, "beat": 0},
    {"type": "Single", "lane": 2, "beat": 8}
  ]
}
)json";
    ChartTimeline timeline = ChartTimeline::from_json_string(schema_v1);
    CHECK_EQ(timeline.bestdori_song_id, 125);
    CHECK(timeline.difficulty == "hard");
    CHECK_EQ(timeline.level, 20);
    CHECK_EQ(timeline.judgements.size(), static_cast<std::size_t>(1));
    CHECK(std::abs(timeline.judgements[0].time_s - 8.0 * 60.0 / 153.0) < 1e-9);
}

void test_hidden_trim_and_single_point() {
    const char* chart = R"json(
[
  {"type": "BPM", "bpm": 120, "beat": 0},
  {"type": "Long", "connections": [
    {"lane": 1, "beat": 8.0, "hidden": true},
    {"lane": 2, "beat": 9.0},
    {"lane": 3, "beat": 10.0},
    {"lane": 4, "beat": 11.0, "hidden": true}
  ]},
  {"type": "Slide", "connections": [
    {"lane": 5, "beat": 20.0}
  ]},
  {"type": "Single", "lane": 0, "beat": 23.5, "skill": true},
  {"type": "Single", "lane": 1, "beat": 24.0, "flick": true},
  {"type": "Directional", "lane": 6, "beat": 25.0, "direction": "Right"}
]
)json";
    ChartTimeline timeline = ChartTimeline::from_json_string(chart);
    CHECK_EQ(timeline.hold_paths.size(), static_cast<std::size_t>(1));
    const HoldPath& path = timeline.hold_paths.front();
    CHECK_EQ(path.points.size(), static_cast<std::size_t>(2));
    CHECK(path.points.front().lane == 2.0);
    CHECK(path.points.back().lane == 3.0);

    // 单点 Slide 修复成 tap。
    bool has_single_point_tap = false;
    for (const ChartJudgement& judgement : timeline.judgements) {
        if (judgement.kind == JudgementKind::Tap &&
            std::abs(judgement.time_s - 10.0) < 1e-9) {
            has_single_point_tap = true;
        }
    }
    CHECK(has_single_point_tap);

    // flick 与方向 FLICK 语义。
    bool has_plain_flick = false;
    bool has_directional_flick = false;
    for (const ChartJudgement& judgement : timeline.judgements) {
        if (judgement.flick && std::abs(judgement.time_s - 12.0) < 1e-9) {
            has_plain_flick = true;
        }
        if (judgement.flick && judgement.direction == 1 &&
            std::abs(judgement.time_s - 12.5) < 1e-9) {
            has_directional_flick = true;
        }
    }
    CHECK(has_plain_flick);
    CHECK(has_directional_flick);
}

void test_invalid_inputs() {
    bool threw = false;
    try {
        ChartTimeline::from_json_string(
            R"json([{"type":"BPM","bpm":0,"beat":0}])json");
    } catch (const ChartParseError&) {
        threw = true;
    }
    CHECK(threw);

    threw = false;
    try {
        ChartTimeline::from_json_string(
            R"json([{"type":"BPM","bpm":120,"beat":0},
                    {"type":"Single","lane":9,"beat":1}])json");
    } catch (const ChartParseError&) {
        threw = true;
    }
    CHECK(threw);
}

}  // namespace

int run_chart_timeline_tests() {
    test_tempo_map();
    test_basic_compile();
    test_schema_v1_and_metadata();
    test_hidden_trim_and_single_point();
    test_invalid_inputs();
    return 0;
}
