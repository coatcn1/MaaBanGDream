#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "maabangdream/types.hpp"

namespace mbdr {

// 分动作类型的延迟补偿，与参考仓库 autodori 的 up/down/move/wait/interval
// 对齐：正值表示该类型动作提前，用于抵消逐类型输入链路延迟差异。
struct TouchLatencyOffsets {
    int down_ms = 0;
    int up_ms = 0;
    int move_ms = 0;
    int tap_ms = 0;
    int flick_ms = 0;
};

// 定时 minitouch 脚本编译器。
//
// 把已分配触点的调度动作编译成带相对 w(ait) 的 minitouch v1 脚本，整曲
// 一次推给 minitouch 进程，由它在模拟器内部按毫秒执行。这样按下时刻不再
// 受逐条跨进程派发的延迟与抖动影响；每次 w 四舍五入的损失按参考实现
// 累计进下一个 w（绝对值限 ±2ms），保证长时间线不漂移。
class TouchScriptCompiler {
public:
    TouchScriptCompiler() = default;
    explicit TouchScriptCompiler(TouchLatencyOffsets offsets) noexcept
        : offsets_(offsets) {}

    // start_engine_time：脚本开始执行的引擎单调秒；早于它的动作立即执行。
    // contact==-1 的瞬态动作按 7,8,9,0..6 轮转分配，并避开该时刻仍按住的
    // hold 触点。
    std::vector<std::string> compile(
        std::vector<ScheduledAction> actions,
        const EngineConfig& config,
        double start_engine_time) const;

private:
    TouchLatencyOffsets offsets_;
};

}  // namespace mbdr
