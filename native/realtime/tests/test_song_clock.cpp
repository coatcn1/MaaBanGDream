#include <cmath>

#include "maabangdream/song_clock.hpp"
#include "maabangdream/types.hpp"
#include "test_macros.hpp"

using namespace mbdr;

namespace {

ChartTimeline make_chart() {
    // 开场：4.0s (0,4) hold heads；5.167s hold tails；6.0s lane1 tap；
    // 7.0s lane2 tap；7.5s lane5 tap；8.0s lane0 flick。
    const char* text = R"json(
[
  {"type": "BPM", "bpm": 120, "beat": 0},
  {"type": "Long", "connections": [
    {"lane": 0, "beat": 8.0}, {"lane": 0, "beat": 10.333}
  ]},
  {"type": "Long", "connections": [
    {"lane": 4, "beat": 8.0}, {"lane": 4, "beat": 10.333}
  ]},
  {"type": "Single", "lane": 1, "beat": 12.0},
  {"type": "Single", "lane": 2, "beat": 14.0},
  {"type": "Single", "lane": 5, "beat": 15.0},
  {"type": "Single", "lane": 0, "beat": 16.0, "flick": true}
]
)json";
    return ChartTimeline::from_json_string(text);
}

SyncConfig dense_config() {
    SyncConfig config;
    config.min_samples = 6;
    config.min_samples_with_anchor = 2;
    config.max_mad_s = 0.06;
    config.match_tol_s = 0.15;
    return config;
}

void test_locks_with_anchor_on_sparse_opening() {
    // 只有两个 hold head 的稀疏开场 + GO 锚点必须能锁。
    SongClockSynchronizer sync(make_chart(), dense_config());
    sync.set_anchor(5.44, 0.5);  // chart 0 在引擎 5.44s。
    sync.observe(SyncObservation{9.44, 0, NoteKind::Hold});
    sync.observe(SyncObservation{9.47, 4, NoteKind::Hold});
    const SyncState& state = sync.state();
    CHECK(state.status == SyncState::Status::Locked);
    CHECK(std::abs(state.offset_s - (-5.44)) < 0.2);
    CHECK_EQ(state.samples, 2);
    CHECK(state.lanes >= 2);
    CHECK(state.locked_at_s < 10.079);  // 早于首次掉血。
}

void test_wrong_chart_is_rejected_with_anchor() {
    // 换一张开场不是 (0,4) hold 双押的谱面：同偏移下只能配到 1 个样本。
    const char* other = R"json(
[
  {"type": "BPM", "bpm": 120, "beat": 0},
  {"type": "Long", "connections": [
    {"lane": 0, "beat": 8.0}, {"lane": 0, "beat": 10.0}
  ]},
  {"type": "Single", "lane": 6, "beat": 8.0},
  {"type": "Single", "lane": 4, "beat": 9.0}
]
)json";
    ChartTimeline other_chart = ChartTimeline::from_json_string(other);
    SongClockSynchronizer sync(std::move(other_chart), dense_config());
    sync.set_anchor(5.44, 0.5);
    sync.observe(SyncObservation{9.44, 0, NoteKind::Hold});
    sync.observe(SyncObservation{9.47, 4, NoteKind::Hold});
    const SyncState& state = sync.state();
    CHECK(state.status == SyncState::Status::Pending);
    CHECK(state.reason.find("samples") != std::string::npos ||
          state.reason.find("lane") != std::string::npos);
}

void test_dense_locks_without_anchor_and_requires_margin() {
    SongClockSynchronizer sync(make_chart(), dense_config());
    // 引擎提前 6s：chart 4.0 在引擎 10.0。
    sync.observe(SyncObservation{10.02, 0, NoteKind::Hold});
    sync.observe(SyncObservation{10.05, 4, NoteKind::Hold});
    sync.observe(SyncObservation{12.03, 1, NoteKind::Tap});
    sync.observe(SyncObservation{13.01, 2, NoteKind::Tap});
    sync.observe(SyncObservation{13.52, 5, NoteKind::Tap});
    sync.observe(SyncObservation{14.04, 0, NoteKind::Flick});
    const SyncState& state = sync.state();
    CHECK(state.status == SyncState::Status::Locked);
    CHECK(std::abs(state.offset_s - (-6.0)) < 0.05);
    CHECK_EQ(state.samples, 6);
}

void test_prelude_grace_blocks_go_ui_junk() {
    SongClockSynchronizer sync(make_chart(), dense_config());
    sync.set_anchor(5.44, 0.5);
    // GO 界面静态误检：引擎 0~1s 的"音符"不能触发锁定。
    sync.observe(SyncObservation{0.3, 1, NoteKind::Tap});
    sync.observe(SyncObservation{0.5, 2, NoteKind::Tap});
    sync.observe(SyncObservation{0.7, 3, NoteKind::Flick});
    const SyncState& state = sync.state();
    CHECK(state.status == SyncState::Status::Pending);
}

void test_ambiguous_periodic_opening_stays_pending() {
    // 周期 2s 重复同一和弦，2 个样本无法区分 → 无锚点时保持 Pending。
    const char* periodic = R"json(
[
  {"type": "BPM", "bpm": 120, "beat": 0},
  {"type": "Single", "lane": 0, "beat": 4.0},
  {"type": "Single", "lane": 0, "beat": 8.0},
  {"type": "Single", "lane": 0, "beat": 12.0},
  {"type": "Single", "lane": 0, "beat": 16.0}
]
)json";
    ChartTimeline chart = ChartTimeline::from_json_string(periodic);
    SyncConfig config = dense_config();
    config.min_lanes = 1;  // 单 lane 周期谱面，用于验证歧义拒绝。
    SongClockSynchronizer sync(std::move(chart), config);
    sync.observe(SyncObservation{8.0, 0, NoteKind::Tap});
    sync.observe(SyncObservation{12.0, 0, NoteKind::Tap});
    const SyncState& state = sync.state();
    CHECK(state.status == SyncState::Status::Pending);
}

}  // namespace

int run_song_clock_tests() {
    test_locks_with_anchor_on_sparse_opening();
    test_wrong_chart_is_rejected_with_anchor();
    test_dense_locks_without_anchor_and_requires_margin();
    test_prelude_grace_blocks_go_ui_junk();
    test_ambiguous_periodic_opening_stays_pending();
    return 0;
}
