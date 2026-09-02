#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "maabangdream/types.hpp"

namespace mbdr {

// 分动作类型的延迟补偿，与参考仓库 autodori 的 up/down/move/wait/interval
// 对齐：单位毫秒、可亚毫秒。数值由 minitouch 的 jlog 回读（LatencyCalibrator）
// 统计得到，表示该类型命令在设备端实际多花的执行时间；编译时通过缩短后续
// w(ait) 来抵偿，不直接移动单个动作的时刻。
struct TouchLatencyOffsets {
    double down_ms = 0.0;
    double up_ms = 0.0;
    double move_ms = 0.0;
    double wait_ms = 0.0;
    double interval_ms = 0.0;
};

// 定时 minitouch 脚本编译器。
//
// 把已分配触点的调度动作编译成带相对 w(ait) 的 minitouch v1 脚本，整曲
// 一次推给 minitouch 进程，由它在模拟器内部按毫秒执行。这样按下时刻不再
// 受逐条跨进程派发的延迟与抖动影响。
//
// 补偿模型与 autodori 的 actions_to_MNTcmd 一致：
// - 每条命令先累加 interval_ms + 该类型 offset 到未清偿残差；
// - 每个 w 前先发 c(ommit) 冲刷触点状态，再按残差缩短该 w（每次限 ±1ms），
//   未清偿部分留给后续 w，避免整数毫秒丢精度；
// - 每次 w 四舍五入的损失累计进下一个 w（绝对值限 ±2ms），保证长时间线不漂移；
// - 残差与取整损失跨 compile() 调用保留，供分切片流式发布时逐片校准。
class TouchScriptCompiler {
public:
    TouchScriptCompiler() = default;
    explicit TouchScriptCompiler(TouchLatencyOffsets offsets) noexcept
        : offsets_(offsets) {}

    void set_offsets(TouchLatencyOffsets offsets) noexcept {
        offsets_ = offsets;
    }
    TouchLatencyOffsets offsets() const noexcept { return offsets_; }

    // 切片边界追加补偿（例如 LatencyCalibrator 统计出的上一切片欠账）。
    void add_residual_ms(double ms) noexcept {
        residual_offset_ms_ += ms;
    }

    // start_engine_time：脚本开始执行的引擎单调秒；早于它的动作立即执行。
    // contact==-1 的瞬态动作按 7,8,9,0..6 轮转分配，并避开该时刻仍按住的
    // hold 触点。
    std::vector<std::string> compile(
        std::vector<ScheduledAction> actions,
        const EngineConfig& config,
        double start_engine_time);

private:
    TouchLatencyOffsets offsets_;
    // 尚未通过缩短 w 清偿的补偿，跨 compile() 调用保留。
    double residual_offset_ms_ = 0.0;
    // 上次 w 取整后的欠账，跨 compile() 调用保留。
    double rounding_loss_ms_ = 0.0;
};

}  // namespace mbdr
