#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include "maabangdream/types.hpp"

namespace mbdr {

namespace detail {

// 低层事件可能跨滚动窗口；保存在编译器实例内，直到其绝对时刻落入当前
// 窗口。该类型只承载内部状态，不是 Python/设备协议接口。
enum class PendingTouchKind : uint8_t {
    Move = 0,
    Up = 1,
    Down = 2,
};

struct PendingTouchEvent {
    PendingTouchKind kind = PendingTouchKind::Down;
    int contact = -1;
    int x = 0;
    int y = 0;
    double due = 0.0;
    int64_t due_ns = 0;
    std::size_t order = 0;
    uint64_t action_token = 0;
    double planned_engine_s = 0.0;
    bool emits_receipt = false;
};

}  // namespace detail

enum class TouchCommandKind : uint8_t {
    Down = 0,
    Move = 1,
    Up = 2,
};

// 高层动作判定关键命令在本次脚本中的精确位置。line_index 包含 c/w 行；
// 调用方需继续关联该相位随后的 commit，后者才是触控对系统可见的时刻。
struct TouchExecutionReceipt {
    std::size_t line_index = 0;
    double planned_engine_s = 0.0;
    uint64_t action_token = 0;
    TouchCommandKind command = TouchCommandKind::Down;
};

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
// 把已分配触点的调度动作编译成带相对 w(ait) 的 minitouch v1 脚本；
// 编译器保留跨切片触点和补偿状态，供滚动窗口持续推给设备端执行。
//
// 补偿模型与 autodori 的 actions_to_MNTcmd 一致：
// - 每条命令先累加 interval_ms + 该类型 offset 到未清偿残差；
// - 每个 w 前先发 c(ommit) 冲刷触点状态，再按残差缩短该 w（每次限 ±1ms），
//   未清偿部分留给后续 w，避免整数毫秒丢精度；
// - 每次 w 四舍五入的损失累计进下一个 w（绝对值限 ±2ms），保证长时间线不漂移；
// - 残差与取整损失跨 compile() 调用保留，供分切片流式发布时逐片校准。
class TouchScriptCompiler {
public:
    TouchScriptCompiler() noexcept;
    explicit TouchScriptCompiler(TouchLatencyOffsets offsets) noexcept;

    void set_offsets(TouchLatencyOffsets offsets) noexcept {
        offsets_ = offsets;
    }
    TouchLatencyOffsets offsets() const noexcept { return offsets_; }

    // 切片边界追加补偿（例如 LatencyCalibrator 统计出的上一切片欠账）。
    void add_residual_ms(double ms) noexcept {
        residual_offset_ms_ += ms;
    }

    // 设备已执行 panic reset 或新建会话后，清除跨切片触点状态。
    void reset_contacts() noexcept;

    // 仅返回最近一次成功 compile() 中到达判定关键命令的高层动作；
    // TAP/普通 FLICK 记在 DOWN，尾部 FLICK 记在首个 MOVE，HOLD 生命周期
    // 动作分别记在自身 DOWN/MOVE/UP。隐式收尾仍由完整脚本队列验证。
    const std::vector<TouchExecutionReceipt>&
    last_execution_receipts() const noexcept {
        return last_execution_receipts_;
    }

    // start_engine_time：脚本开始执行的引擎单调秒；早于它的动作立即执行。
    // contact==-1 的瞬态动作按 7,8,9,0..6 轮转分配，并避开该时刻仍按住的
    // hold 触点。中间切片传 final_chunk=false；最后一片默认要求所有触点
    // 已释放。非最终切片只发射不晚于 end_engine_time 的低层事件，未来的
    // 瞬态 MOVE/UP 留到下一片；窗口尾若晚于最后事件则补等待，空切片也可
    // 据此推进设备时间。future_down_reservations 只允许固定触点 DOWN，
    // 仅阻止跨边界瞬态手势占用该触点，不生成命令、receipt 或生命周期状态。
    std::vector<std::string> compile(
        std::vector<ScheduledAction> actions,
        const EngineConfig& config,
        double start_engine_time,
        bool final_chunk = true,
        double end_engine_time =
            std::numeric_limits<double>::quiet_NaN(),
        std::vector<ScheduledAction> future_down_reservations = {});

private:
    TouchLatencyOffsets offsets_;
    // 尚未通过缩短 w 清偿的补偿，跨 compile() 调用保留。
    double residual_offset_ms_ = 0.0;
    // 上次 w 取整后的欠账，跨 compile() 调用保留。
    double rounding_loss_ms_ = 0.0;
    // 触点状态跨滚动切片保留；available_after 防止下一片把仍在设备队列中
    // 的瞬态手势触点提前复用。
    std::array<bool, kMaxContacts> active_contacts_{};
    std::array<int64_t, kMaxContacts> contact_available_after_{};
    std::vector<detail::PendingTouchEvent> pending_events_;
    std::size_t next_event_order_ = 0;
    uint64_t next_action_token_ = 1;
    std::vector<TouchExecutionReceipt> last_execution_receipts_;
    int transient_cursor_ = 0;
};

}  // namespace mbdr
