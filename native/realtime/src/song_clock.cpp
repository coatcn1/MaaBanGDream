#include "maabangdream/song_clock.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>

namespace mbdr {

namespace {

// 观测 kind 与谱面判定 kind 的兼容规则。
bool kinds_compatible(NoteKind observed, JudgementKind chart_kind) {
    if (chart_kind == JudgementKind::HoldHead) {
        return observed == NoteKind::Hold;
    }
    // TAP/FLICK/SKILL 与谱面 tap 判定兼容；hold-tail 由释放语义处理，
    // 开场相位不依赖释放事件。
    return observed != NoteKind::Hold;
}

// 单调有序匹配（DP，带插入/删除罚分）。返回匹配残差与 lane 集合。
struct MatchResult {
    int matches = 0;
    int lanes = 0;
    double median_residual_s = 0.0;
    double mad_s = 0.0;
    double first_matched_obs_s = 0.0;
    double last_matched_obs_s = 0.0;
    bool valid = false;
};

double median(std::vector<double> values) {
    if (values.empty()) {
        return 0.0;
    }
    const std::size_t middle = values.size() / 2;
    std::nth_element(values.begin(), values.begin() + middle, values.end());
    const double upper = values[middle];
    if (values.size() % 2 == 1) {
        return upper;
    }
    std::nth_element(values.begin(), values.begin() + middle - 1,
        values.begin() + middle);
    return (values[middle - 1] + upper) / 2.0;
}

MatchResult ordered_match(
    const std::vector<SyncObservation>& observations,
    const std::vector<ChartJudgement>& judgements,
    double offset_s,
    double tol_s) {
    const std::size_t n = observations.size();
    const std::size_t m = judgements.size();
    MatchResult result;
    if (n == 0 || m == 0) {
        return result;
    }
    const double gap_penalty = -0.6;
    const double match_reward = 1.0;
    std::vector<std::vector<double>> dp(
        n + 1, std::vector<double>(m + 1, 0.0));
    for (std::size_t i = 1; i <= n; ++i) {
        dp[i][0] = dp[i - 1][0] + gap_penalty;
    }
    for (std::size_t j = 1; j <= m; ++j) {
        dp[0][j] = dp[0][j - 1] + gap_penalty;
    }
    for (std::size_t i = 1; i <= n; ++i) {
        const SyncObservation& obs = observations[i - 1];
        for (std::size_t j = 1; j <= m; ++j) {
            const ChartJudgement& judgement = judgements[j - 1];
            double best = std::max(dp[i - 1][j], dp[i][j - 1]) + gap_penalty;
            if (obs.lane == judgement.lane &&
                kinds_compatible(obs.kind, judgement.kind) &&
                std::abs(judgement.time_s - (obs.engine_time_s + offset_s)) <= tol_s) {
                best = std::max(best, dp[i - 1][j - 1] + match_reward);
            }
            dp[i][j] = best;
        }
    }
    // 回溯收集残差与 lane 集合。
    std::vector<double> residuals;
    std::set<int> matched_lanes;
    double first_matched_obs = 0.0;
    double last_matched_obs = 0.0;
    bool have_first = false;
    std::size_t i = n;
    std::size_t j = m;
    while (i > 0 && j > 0) {
        const SyncObservation& obs = observations[i - 1];
        const ChartJudgement& judgement = judgements[j - 1];
        const double residual =
            judgement.time_s - (obs.engine_time_s + offset_s);
        if (obs.lane == judgement.lane &&
            kinds_compatible(obs.kind, judgement.kind) &&
            std::abs(residual) <= tol_s &&
            dp[i][j] == dp[i - 1][j - 1] + match_reward) {
            residuals.push_back(residual);
            matched_lanes.insert(obs.lane);
            if (!have_first || obs.engine_time_s < first_matched_obs) {
                first_matched_obs = obs.engine_time_s;
                have_first = true;
            }
            if (!have_first || obs.engine_time_s > last_matched_obs) {
                last_matched_obs = obs.engine_time_s;
            }
            --i;
            --j;
        } else if (dp[i][j] == dp[i - 1][j] + gap_penalty) {
            --i;
        } else {
            --j;
        }
    }
    if (!residuals.empty()) {
        std::reverse(residuals.begin(), residuals.end());
        const double center = median(residuals);
        std::vector<double> absolute;
        absolute.reserve(residuals.size());
        for (const double residual : residuals) {
            absolute.push_back(std::abs(residual - center));
        }
        result.median_residual_s = center;
        result.mad_s = median(std::move(absolute));
        result.matches = static_cast<int>(residuals.size());
        result.lanes = static_cast<int>(matched_lanes.size());
        result.first_matched_obs_s = first_matched_obs;
        result.last_matched_obs_s = last_matched_obs;
        result.valid = true;
    }
    return result;
}

}  // namespace

SongClockSynchronizer::SongClockSynchronizer(
    ChartTimeline chart,
    SyncConfig config)
    : chart_(std::move(chart)), config_(std::move(config)) {
    if (config_.match_tol_s <= 0.0 || config_.max_mad_s <= 0.0) {
        throw std::invalid_argument("sync tolerances must be positive");
    }
}

void SongClockSynchronizer::set_anchor(
    double engine_time_s,
    double uncertainty_s) {
    if (!std::isfinite(engine_time_s) || engine_time_s < 0.0) {
        throw std::invalid_argument("anchor time must be finite and non-negative");
    }
    if (!std::isfinite(uncertainty_s) || uncertainty_s <= 0.0) {
        throw std::invalid_argument("anchor uncertainty must be positive");
    }
    state_.has_anchor = true;
    state_.anchor_time_s = engine_time_s;
    state_.anchor_uncertainty_s = uncertainty_s;
    if (state_.status != SyncState::Status::Locked) {
        reevaluate();
    }
}

void SongClockSynchronizer::observe(const SyncObservation& observation) {
    if (!std::isfinite(observation.engine_time_s)) {
        throw std::invalid_argument("observation time must be finite");
    }
    if (observation.lane >= kLaneCount) {
        throw std::invalid_argument("observation lane must be within 0..6");
    }
    if (state_.status != SyncState::Status::Pending) {
        return;
    }
    observations_.push_back(observation);
    reevaluate();
}

void SongClockSynchronizer::reject(const std::string& reason) {
    state_.status = SyncState::Status::Rejected;
    state_.reason = reason;
}

SyncSolution SongClockSynchronizer::match_offset(double offset_s) const {
    SyncSolution solution;
    solution.offset_s = offset_s;
    const MatchResult result =
        ordered_match(observations_, chart_.judgements, offset_s, config_.match_tol_s);
    solution.matches = result.matches;
    solution.lanes = result.lanes;
    solution.median_residual_s = result.median_residual_s;
    solution.mad_s = result.mad_s;
    solution.first_matched_obs_s = result.first_matched_obs_s;
    solution.last_matched_obs_s = result.last_matched_obs_s;
    return solution;
}

void SongClockSynchronizer::reevaluate() {
    state_.best = SyncSolution{};
    state_.second = SyncSolution{};
    state_.best_second_offset_gap_s = 0.0;
    state_.second_matches = 0;
    if (observations_.empty()) {
        state_.reason = "insufficient evidence: no observations yet";
        return;
    }

    // 候选偏移网格：有锚点时限定在锚点窗口；否则全范围扫描。
    const double lower = state_.has_anchor
        ? -state_.anchor_time_s - state_.anchor_uncertainty_s
        : config_.min_offset_s;
    const double upper = state_.has_anchor
        ? -state_.anchor_time_s + state_.anchor_uncertainty_s
        : config_.max_offset_s;
    const int steps = static_cast<int>(
        std::ceil((upper - lower) / config_.offset_step_s));

    struct Ranked {
        SyncSolution solution;
    };
    std::vector<Ranked> ranked;
    ranked.reserve(static_cast<std::size_t>(steps + 1));
    for (int step = 0; step <= steps; ++step) {
        const double offset =
            lower + static_cast<double>(step) * config_.offset_step_s;
        SyncSolution solution = match_offset(offset);
        if (solution.matches > 0) {
            ranked.push_back(Ranked{std::move(solution)});
        }
    }
    std::sort(ranked.begin(), ranked.end(),
        [](const Ranked& lhs, const Ranked& rhs) {
            if (lhs.solution.matches != rhs.solution.matches) {
                return lhs.solution.matches > rhs.solution.matches;
            }
            if (lhs.solution.mad_s != rhs.solution.mad_s) {
                return lhs.solution.mad_s < rhs.solution.mad_s;
            }
            return lhs.solution.offset_s < rhs.solution.offset_s;
        });

    // 按聚类宽度找出最优簇与次优簇。两个格点相距小于 ~2 倍匹配容差时，
    // 它们只是同一相位的相邻采样，不能算两个独立解。
    const double cluster_width =
        std::max(config_.min_margin_s, 2.2 * config_.match_tol_s);
    SyncSolution* best = ranked.empty() ? nullptr : &ranked.front().solution;
    const SyncSolution* second = nullptr;
    if (best != nullptr) {
        for (const Ranked& item : ranked) {
            if (std::abs(item.solution.offset_s - best->offset_s) >=
                cluster_width) {
                second = &item.solution;
                break;
            }
        }
    }

    if (best == nullptr) {
        state_.reason = "no plausible phase candidate matched the chart opening";
        return;
    }
    // 用匹配残差中位数细化格点：把残差中心归零，避免 5ms 网格引入系统偏置。
    const double refined_offset = best->offset_s + best->median_residual_s;
    best->offset_s = refined_offset;
    state_.best = *best;
    state_.second_matches = second == nullptr ? 0 : second->matches;
    state_.best_second_offset_gap_s =
        second == nullptr ? 0.0
                          : std::abs(second->offset_s - best->offset_s);
    if (second != nullptr) {
        state_.second = *second;
    }

    const int required_samples = state_.has_anchor
        ? config_.min_samples_with_anchor
        : config_.min_samples;
    if (best->matches < required_samples) {
        state_.reason = "insufficient matched samples: " +
            std::to_string(best->matches) + " < " +
            std::to_string(required_samples);
        return;
    }
    if (best->lanes < config_.min_lanes) {
        state_.reason = "lane diversity below threshold";
        return;
    }
    if (best->mad_s > config_.max_mad_s) {
        state_.reason = "matched residual MAD exceeded " +
            std::to_string(config_.max_mad_s) + "s";
        return;
    }
    // 前置保护：最早匹配到的观测必须晚于 GO 锚点 + 保护窗。
    const double prelude_floor = state_.has_anchor
        ? state_.anchor_time_s + config_.prelude_grace_s
        : config_.prelude_grace_s;
    if (best->first_matched_obs_s < prelude_floor) {
        state_.reason = "opening evidence appeared inside the prelude grace window";
        return;
    }
    if (!state_.has_anchor) {
        if (second != nullptr &&
            second->matches > best->matches - config_.min_margin_samples) {
            state_.reason =
                "ambiguous phase: second-best solution within margin";
            return;
        }
    }

    state_.status = SyncState::Status::Locked;
    state_.offset_s = best->offset_s;
    state_.samples = best->matches;
    state_.lanes = best->lanes;
    state_.mad_s = best->mad_s;
    state_.median_residual_s = best->median_residual_s;
    state_.locked_at_s = best->last_matched_obs_s;
    state_.reason = "locked";
}

}  // namespace mbdr
