#pragma once

#include <vector>

#include "maabangdream/types.hpp"

namespace mbdr {

// Pure Chart 动作编译器。
//
// 从 ChartTimeline 生成确定性的 ScheduledAction 序列，覆盖：
// - TAP（含 skill）、方向 FLICK；
// - Long/Slide 的 DOWN + MOVE + UP 生命周期；
// - hold 尾部 FLICK（携带触点，替换 UP）；
// - 同时双押与多触点（最早空闲触点分配）。
//
// 语义与 scripts/pure_chart_reference.py 逐条对齐，供差分测试比对。
std::vector<ScheduledAction> compile_pure_chart_actions(
    const ChartTimeline& timeline,
    const EngineConfig& config);

}  // namespace mbdr
