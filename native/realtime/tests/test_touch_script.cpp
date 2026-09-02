// TouchScriptCompiler 的 C++ 单元测试：验证定时脚本的毫秒时序、
// commit-before-wait、分类型延迟补偿与取整损失补偿。

#include <cmath>
#include <cstdlib>
#include <string>
#include <vector>

#include "maabangdream/touch_script.hpp"
#include "test_macros.hpp"

namespace {

using namespace mbdr;

ScheduledAction action(ActionKind kind, uint8_t lane, double due_s,
                       int8_t contact = -1, int note_index = 0) {
    ScheduledAction result;
    result.kind = kind;
    result.lane = lane;
    result.due_s = due_s;
    result.contact = contact;
    result.note_index = note_index;
    return result;
}

std::string join(const std::vector<std::string>& lines) {
    std::string out;
    for (const std::string& item : lines) {
        out += item;
    }
    return out;
}

int sum_waits(const std::vector<std::string>& lines) {
    int total = 0;
    for (const std::string& item : lines) {
        if (item.rfind("w ", 0) == 0) {
            total += std::stoi(item.substr(2));
        }
    }
    return total;
}

int count_wait_lines(const std::vector<std::string>& lines) {
    int total = 0;
    for (const std::string& item : lines) {
        if (item.rfind("w ", 0) == 0) {
            ++total;
        }
    }
    return total;
}

void test_basic_hold_lifecycle_ordering() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    std::vector<ScheduledAction> actions = {
        action(ActionKind::Down, 2, 1.0, 2),
        action(ActionKind::Tap, 4, 1.5),
        action(ActionKind::Up, 2, 2.0, 2),
    };
    auto script = compiler.compile(actions, config, 0.0);
    std::string text = join(script);

    CHECK(text.find("w 1000\nd 2") != std::string::npos);
    CHECK(text.find("w 500\nd ") != std::string::npos);
    // tap 按下 12ms 计入时间线，up 的 wait 相应缩短为 488ms。
    CHECK(text.find("w 488\nu 2") != std::string::npos);
    // tap 使用轮转触点 7，不与 hold 触点 2 冲突。
    CHECK(text.find("\nd 7 ") != std::string::npos);
}

void test_commit_precedes_every_wait() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    std::vector<ScheduledAction> actions = {
        action(ActionKind::Down, 0, 0.1, 1),
        action(ActionKind::Tap, 3, 1.0),
    };
    auto script = compiler.compile(actions, config, 0.0);
    const std::string text = join(script);
    // 每个 w 行前都必须有 c 行：minitouch 在睡眠前冲刷触点状态，
    // 否则按压会被推迟到下一个 commit 才写入设备。
    size_t pos = 0;
    while ((pos = text.find("\nw ", pos)) != std::string::npos) {
        CHECK(pos > 0 && text[pos - 1] == 'c');
        ++pos;
    }
    CHECK(count_wait_lines(script) > 0);
}

void test_per_type_offset_shortens_waits_with_clamp() {
    TouchLatencyOffsets offsets;
    offsets.down_ms = 5.0;
    TouchScriptCompiler compiler(offsets);
    EngineConfig config;
    std::vector<ScheduledAction> actions = {
        action(ActionKind::Down, 0, 0.5, 1),
        action(ActionKind::Down, 1, 1.0, 2),
        action(ActionKind::Down, 2, 1.5, 3),
    };
    auto script = compiler.compile(actions, config, 0.0);
    const std::string text = join(script);
    // down 5ms 按 ±1ms 上限缩短后续 w：第二段 500ms 变 499ms。
    CHECK(text.find("w 499\n") != std::string::npos);
    CHECK(sum_waits(script) == 1499);
}

void test_rounding_loss_is_compensated_and_bounded() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    std::vector<ScheduledAction> actions = {
        action(ActionKind::Down, 0, 0.0166, 1),
        action(ActionKind::Down, 1, 0.0332, 2),
        action(ActionKind::Down, 2, 0.0498, 3),
    };
    auto script = compiler.compile(actions, config, 0.0);
    const int total = sum_waits(script);
    // 精确总等待 49.8ms，补偿后四舍五入误差 ≤2ms。
    CHECK(total >= 48);
    CHECK(total <= 52);
    CHECK(std::abs(total - 50) <= 2);
}

void test_transient_contact_avoids_active_hold() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    std::vector<ScheduledAction> actions = {
        action(ActionKind::Down, 0, 0.1, 7),
        action(ActionKind::Tap, 3, 1.0),
        action(ActionKind::Up, 0, 5.0, 7),
    };
    auto script = compiler.compile(actions, config, 0.0);
    std::string text = join(script);
    // 触点 7 被 hold 占用，tap 必须落到 8。
    CHECK(text.find("w 900\nd 8 ") != std::string::npos);
}

void test_song_offset_and_press_bias_map_to_engine_time() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    config.song_offset_s = 0.5;
    config.press_bias_ms = 4;
    std::vector<ScheduledAction> actions = {
        action(ActionKind::Tap, 1, 2.0),
    };
    auto script = compiler.compile(actions, config, 0.0);
    // 2.0 - 0.5 - 0.004 = 1.496s -> 1496ms；w 前必有 c 行。
    const std::string text = join(script);
    CHECK(text.find("w 1496\n") != std::string::npos);
}

void test_flick_emits_down_move_up_swipe() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    ScheduledAction flick = action(ActionKind::Flick, 3, 1.0);
    flick.flick_direction = -1;
    auto script = compiler.compile({flick}, config, 0.0);
    std::string text = join(script);
    CHECK(text.find("\nd 7 ") != std::string::npos);
    CHECK(text.find("\nm 7 ") != std::string::npos);
    CHECK(text.find("\nu 7") != std::string::npos);
}

}  // namespace

int run_touch_script_tests() {
    test_basic_hold_lifecycle_ordering();
    test_commit_precedes_every_wait();
    test_per_type_offset_shortens_waits_with_clamp();
    test_rounding_loss_is_compensated_and_bounded();
    test_transient_contact_avoids_active_hold();
    test_song_offset_and_press_bias_map_to_engine_time();
    test_flick_emits_down_move_up_swipe();
    return 0;
}
