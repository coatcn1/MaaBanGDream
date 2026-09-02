#pragma once

#include <string>
#include <string_view>

#include "maabangdream/touch_script.hpp"

namespace mbdr {

// minitouch EvATive7 的逐命令耗时回读事件：
// jlog {"st": 12.3, "et": 12.5, "c": 0.2, "cmd": "d 0 100 200 50"}
// st/et/c 均为设备端单调毫秒；c 是单条命令（含 w 的 usleep）实际耗时。
struct MinitouchLogEvent {
    double start_ms = 0.0;
    double end_ms = 0.0;
    double cost_ms = 0.0;
    std::string command;
};

// 从一行文本解析 jlog；握手行（v/^/$ 等）不是 jlog，返回 false。
bool parse_minitouch_log(std::string_view line, MinitouchLogEvent* out);

// 分类型延迟统计器。归因模型与 autodori 的 mnt_callback/_adjust_offset 一致：
// - interval：相邻命令 start 与上一条 end 的间隙（PC 侧传输/派发间隙）；
// - w：实际耗时 - 名义毫秒（等待超出的执行开销）；
// - d/m/u：各自命令的实际耗时；
// - c：commit 耗时按未提交的 d/m/u 数量分摊。
//
// offsets() 输出各类型平均耗时，直接作为下一切片 TouchScriptCompiler 的
// 分类型 offset；correction_ms() 输出相对上一轮均值的整段欠账（毫秒），
// 由编译器 add_residual_ms() 吸收，补偿旧均值已派发命令的偏差。
class LatencyCalibrator {
public:
    void observe(const MinitouchLogEvent& event);
    TouchLatencyOffsets offsets() const;
    double correction_ms(const TouchLatencyOffsets& previous) const;
    void reset();
    int event_count() const noexcept { return event_count_; }

private:
    struct Stats {
        double total_cost_ms = 0.0;
        int count = 0;
    };

    Stats down_;
    Stats up_;
    Stats move_;
    Stats wait_;
    Stats interval_;
    int uncommitted_down_ = 0;
    int uncommitted_up_ = 0;
    int uncommitted_move_ = 0;
    double last_end_ms_ = -1.0;
    int event_count_ = 0;
};

}  // namespace mbdr
