#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

// Native Realtime Engine V2 公共数据类型。
//
// 设计约束（见项目交接文档）：
// - BanG Dream 固定 7 lane / 最多 10 触点，热路径全部用定长数组或紧凑
//   POD，避免逐帧动态分配；
// - 时间约定：`time_s` 一律为谱面秒（ChartTimeline / ScheduledAction.due_s），
//   `engine_time_s` 为引擎单调秒；二者通过 song_offset_s 关联：
//   chart_time = engine_time + song_offset_s。

namespace mbdr {

constexpr uint8_t kLaneCount = 7;
constexpr uint8_t kMaxContacts = 10;

// 视觉观测的音符语义。
enum class NoteKind : uint8_t {
    Tap = 0,
    Flick = 1,
    Skill = 2,
    Hold = 3,
};

// 谱面判定类型。
enum class JudgementKind : uint8_t {
    Tap = 0,
    HoldHead = 1,
    HoldTail = 2,
};

// 触控动作类型。Flick 与 Tap 是瞬态动作，由 Python 触点分配；
// Down/Move/Up 属于 hold 生命周期，必须携带确定性 contact。
enum class ActionKind : uint8_t {
    Tap = 0,
    Flick = 1,
    Down = 2,
    Move = 3,
    Up = 4,
};

const char* note_kind_name(NoteKind kind) noexcept;
const char* judgement_kind_name(JudgementKind kind) noexcept;
const char* action_kind_name(ActionKind kind) noexcept;

// 谱面判定：由 ChartTimeline 编译器从 Bestdori JSON 生成。
struct ChartJudgement {
    double time_s = 0.0;
    uint8_t lane = 0;
    JudgementKind kind = JudgementKind::Tap;
    // 同一物理音符的索引；hold 的 head/tail 共用同一 note_index。
    int note_index = -1;
    bool flick = false;
    // -1=Left，+1=Right，0=无方向。
    int8_t direction = 0;
    bool tail_flick = false;
};

// hold 路径上的连接点。
struct PathPoint {
    double time_s = 0.0;
    double lane = 0.0;  // 可见连接点取整数 lane，隐藏连接点可为半整数。
    bool hidden = false;
    bool flick = false;
    int8_t direction = 0;
};

struct HoldPath {
    int note_index = -1;
    std::string note_type;  // "Long" 或 "Slide"。
    std::vector<PathPoint> points;
};

struct TempoChange {
    double beat = 0.0;
    double bpm = 0.0;
    double time_s = 0.0;
};

// 分段恒定 BPM 的 beat→秒 换算表，与 agent/realtime/chart_timeline.py 对齐。
class TempoMap {
public:
    TempoMap() = default;
    explicit TempoMap(std::vector<TempoChange> changes);

    // 从 (beat, bpm) 事件构建；重复 beat 取最后值，首个 beat>0 时补 0。
    static TempoMap from_events(
        const std::vector<std::pair<double, double>>& beat_bpm);

    double seconds_at(double beat) const;
    const std::vector<TempoChange>& changes() const noexcept {
        return changes_;
    }

private:
    std::vector<TempoChange> changes_;
    std::vector<double> beats_;
};

// 编译后的谱面时间轴。
struct ChartTimeline {
    TempoMap tempo_map;
    std::vector<ChartJudgement> judgements;
    std::vector<HoldPath> hold_paths;
    double start_time_s = 0.0;
    double end_time_s = 0.0;
    // 身份元数据：仅用于诊断与上层门禁，不参与相位计算。
    int bestdori_song_id = -1;
    std::string difficulty;
    int level = -1;

    static ChartTimeline from_json_file(const std::string& path);
    static ChartTimeline from_json_string(const std::string& text);
};

// 编译后的调度动作。
struct ScheduledAction {
    ActionKind kind = ActionKind::Tap;
    uint8_t lane = 0;
    // -1 表示瞬态（TAP/FLICK），由 Python 在派发时分配触点；
    // hold 生命周期动作必须携带确定性 contact（0..9）。
    int8_t contact = -1;
    // 目标 x 像素；由 lane 中心换算。
    float target_x = 0.0F;
    // 谱面秒。调度时换算：engine_due = due_s - song_offset_s + press_bias_s。
    double due_s = 0.0;
    int note_index = -1;
    int8_t flick_direction = 0;
};

struct EngineConfig {
    double judgement_y = 565.0;
    // 正值 = 提前输入（沿用 Python 约定：press_bias_s = -ms/1000）。
    int press_bias_ms = 0;
    double song_offset_s = 0.0;
    std::array<float, kLaneCount> lane_centers = {
        190.0F, 340.0F, 490.0F, 640.0F, 790.0F, 940.0F, 1090.0F,
    };
};

}  // namespace mbdr
