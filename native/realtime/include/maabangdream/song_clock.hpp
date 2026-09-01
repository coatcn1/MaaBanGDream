#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "maabangdream/types.hpp"

namespace mbdr {

// 同步观测：一条已通过运动/几何门禁的轨迹的 crossing 投影。
struct SyncObservation {
    double engine_time_s = 0.0;
    uint8_t lane = 0;
    NoteKind kind = NoteKind::Tap;
};

// 同步门禁参数。默认值面向离线验证与 fail-closed 原则。
struct SyncConfig {
    double match_tol_s = 0.15;          // 单点匹配容差。
    double max_mad_s = 0.05;            // 锁定允许的最大 MAD。
    double min_margin_s = 0.20;         // 两个相位簇的最小偏移距离。
    int min_samples = 6;                // 无锚点锁定的最小样本数。
    int min_samples_with_anchor = 2;    // 有锚点锁定的最小样本数。
    int min_margin_samples = 2;         // 无锚点最优须领先次优的样本数。
    int min_lanes = 2;                  // 最小 lane 多样性。
    double prelude_grace_s = 2.0;       // 引擎起始/GO 锚点后的静默保护窗。
    double anchor_default_uncertainty_s = 0.6;
    double min_offset_s = -30.0;        // 无锚点扫描下界。
    double max_offset_s = 10.0;         // 无锚点扫描上界。
    double offset_step_s = 0.005;       // 候选网格步长。
    double sync_chart_window_s = 30.0;  // 只匹配开场判定（之后交给运行时相位维护）。
};

struct SyncSolution {
    double offset_s = 0.0;
    int matches = 0;
    int lanes = 0;
    double median_residual_s = 0.0;
    double mad_s = 0.0;
    double first_matched_obs_s = 0.0;
    double last_matched_obs_s = 0.0;
};

struct SyncState {
    enum class Status : uint8_t {
        Pending = 0,
        Locked = 1,
        Rejected = 2,
    };

    Status status = Status::Pending;
    double offset_s = 0.0;
    int samples = 0;
    int lanes = 0;
    double mad_s = 0.0;
    double median_residual_s = 0.0;
    double locked_at_s = 0.0;
    bool has_anchor = false;
    double anchor_time_s = 0.0;
    double anchor_uncertainty_s = 0.0;
    SyncSolution best;
    SyncSolution second;
    double best_second_offset_gap_s = 0.0;
    int second_matches = 0;
    std::string reason;
};

// SongClockSynchronizer：把观测到的有序 lane/kind 序列与谱面开场做带偏移
// 的单调有序匹配，只在唯一解满足样本数、lane 数、MAD、前置保护与锚点
// 约束时锁定；否则 fail-closed 保持 Pending/Rejected，绝不允许盲打。
class SongClockSynchronizer {
public:
    SongClockSynchronizer(ChartTimeline chart, SyncConfig config);

    // GO/演出开场锚点：anchor 是"谱面 beat 0"对应的引擎时间。
    void set_anchor(double engine_time_s, double uncertainty_s);

    // 喂入一条观测并重新评估；状态可通过 state() 读取。
    void observe(const SyncObservation& observation);

    // 硬拒绝：用于上层确认谱面身份不匹配等不可恢复条件。
    void reject(const std::string& reason);

    const SyncState& state() const noexcept { return state_; }

private:
    ChartTimeline chart_;
    SyncConfig config_;
    std::vector<SyncObservation> observations_;
    SyncState state_;

    void reevaluate();
    SyncSolution match_offset(double offset_s) const;
};

}  // namespace mbdr
