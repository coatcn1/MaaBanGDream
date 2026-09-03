#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "maabangdream/minitouch_log.hpp"
#include "maabangdream/types.hpp"

namespace mbdr {

// 滚动播放会话只负责绝对时间分窗和生命周期，不直接依赖 socket 或 Python。
// 上层收到 PlaybackChunk 后负责把窗口编译成设备脚本并发布，因此测试可以用
// 假时钟和假传输覆盖断粮、停止超时等真实时间边界。
// 会话采用单所有者模型：异步 jlog 读取线程应把事件排队，再由会话线程调用
// observe_minitouch_log，不能并发调用这些方法。
enum class PlaybackState : uint8_t {
    Idle = 0,
    Armed = 1,
    Running = 2,
    Cancelling = 3,
    Finished = 4,
    Cancelled = 5,
    Failed = 6,
};

struct PlaybackSessionConfig {
    double lookahead_s = 0.500;
    double low_water_s = 0.200;
    double max_queue_s = 0.750;
    double reset_timeout_s = 0.100;
    double cancel_deadline_s = 0.500;
};

struct TimedPlaybackAction {
    ScheduledAction action;
    // start(anchor) 映射后的绝对引擎单调时刻。
    double engine_due_s = 0.0;
};

struct PlaybackChunk {
    uint64_t sequence = 0;
    // 绝对引擎单调时间窗口；允许 actions 为空，此时适配器仍应生成等待窗。
    double window_start_s = 0.0;
    double window_end_s = 0.0;
    // 只保留坐标/触控参数；时间偏移已经折入 engine_due_s，因此这里的
    // song_offset_s 与 press_bias_ms 固定清零，避免适配器重复补偿。
    EngineConfig touch_config;
    std::vector<TimedPlaybackAction> actions;
    // 仅供当前块瞬态触点分配：这些固定触点 HOLD DOWN 位于窗口之后，
    // 不属于本块发送动作，也不得产生命令、receipt 或推进会话 cursor。
    std::vector<TimedPlaybackAction> future_down_reservations;
    // 只有最后一个动作所在窗口为 true；TouchScriptCompiler 必须据此决定
    // 是否结束跨块触点状态。
    bool final_chunk = false;
};

// 把 chunk 中的绝对截止时刻写回 ScheduledAction::due_s，供时间偏移已清零
// 的 TouchScriptCompiler 直接消费，避免各适配器重复实现并误用谱面 due_s。
std::vector<ScheduledAction> materialize_playback_actions(
    const PlaybackChunk& chunk);

struct PlaybackReport {
    uint64_t planned_actions = 0;
    uint64_t sent_actions = 0;
    uint64_t executed_actions = 0;
    uint64_t tap_actions = 0;
    uint64_t flick_actions = 0;
    uint64_t hold_starts = 0;
    uint64_t hold_moves = 0;
    uint64_t hold_releases = 0;
    uint64_t chord_groups = 0;
    uint64_t chunks = 0;
    uint64_t queue_underflows = 0;
    uint64_t probe_events = 0;
    double chart_first_due_s = 0.0;
    double first_action_engine_s = 0.0;
    double max_queue_depth_ms = 0.0;
    // 漂移统计使用 |actual_engine_s - planned_engine_s|，便于直接做门禁。
    double drift_p50_ms = 0.0;
    double drift_p95_ms = 0.0;
    double drift_max_ms = 0.0;
    double stop_latency_ms = 0.0;
    bool fallback_used = false;
    std::string terminal_reason;
};

struct PlaybackCallbacks {
    // clock 必须返回单调秒；为空时使用 std::chrono::steady_clock。
    std::function<double()> clock;
    // publish 必须完整接收一个窗口；返回 false 会使会话 fail-closed。
    std::function<bool(const PlaybackChunk&)> publish;
    // request_reset 必须非阻塞；返回 true 表示 reset 已经确认生效。
    std::function<bool()> request_reset;
    // reset 100ms 未确认时调用；通常终止 minitouch 进程并标记下次重连。
    std::function<bool()> fallback_stop;
};

class PlaybackSession {
public:
    explicit PlaybackSession(
        PlaybackCallbacks callbacks,
        PlaybackSessionConfig config = {});

    // arm 只做排序和相对首拍换算，不发布任何输入。
    bool arm(std::vector<ScheduledAction> actions, EngineConfig engine_config);
    // first_action_engine_s 是 photogate 确认后的首拍绝对执行时刻；会话把
    // 最小 due_s 映射到该锚点，并保持后续动作的谱面相对间隔。
    bool start(double first_action_engine_s);
    // 低于 low_water 时至多发布一个窗口；队列充足时返回 false 但不失败。
    bool publish();

    // cancel 先发非阻塞 reset；poll 在 100ms 后触发 fallback，并在 500ms
    // 截止时间处 fail-closed。设备异步确认 reset 时调用 acknowledge_reset。
    bool cancel(std::string reason);
    bool acknowledge_reset();
    PlaybackState poll();

    // 仅在全部动作已经成功发送后允许正常结束。
    bool finish(std::string reason);

    // jlog 只有显式交给会话才会参与校准；start 会把探测样本与正式样本隔离。
    void observe_minitouch_log(const MinitouchLogEvent& event);
    int calibration_event_count() const noexcept;
    TouchLatencyOffsets latency_offsets() const;
    double latency_correction_ms(const TouchLatencyOffsets& previous) const;
    void reset_calibration_window();

    // 适配器完成 jlog→动作关联后回报绝对执行时刻；超出已发送数量时拒绝。
    bool observe_execution(
        double planned_engine_s,
        double actual_engine_s,
        uint64_t count = 1);

    PlaybackState state() const noexcept { return state_; }
    PlaybackReport report() const { return report_; }
    const PlaybackSessionConfig& config() const noexcept { return config_; }

private:
    struct Entry {
        TimedPlaybackAction timed;
        // arm 时为相对首拍的隐式手势结束时刻，start 后改为绝对单调时刻。
        double completion_s = 0.0;
        std::size_t order = 0;
    };

    double now();
    bool clock_is_valid(double value);
    void detect_underflow(double current_s);
    void complete_cancel(double current_s);
    void fail(std::string reason, double stop_latency_ms = 0.0);
    void update_drift_metrics();

    PlaybackCallbacks callbacks_;
    PlaybackSessionConfig config_;
    EngineConfig engine_config_;
    PlaybackState state_ = PlaybackState::Idle;
    std::vector<Entry> entries_;
    std::size_t cursor_ = 0;
    uint64_t next_sequence_ = 1;
    double queue_tail_s_ = 0.0;
    double last_clock_s_ = 0.0;
    bool clock_started_ = false;
    bool published_once_ = false;
    bool final_chunk_published_ = false;
    bool underflow_latched_ = false;
    bool fallback_requested_ = false;
    double cancel_started_s_ = 0.0;
    double chart_first_due_s_ = 0.0;
    double playback_end_engine_s_ = 0.0;
    std::string cancel_reason_;
    PlaybackReport report_;
    std::vector<double> absolute_drift_ms_;
    LatencyCalibrator calibrator_;
};

}  // namespace mbdr
