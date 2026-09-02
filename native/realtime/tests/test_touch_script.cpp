// TouchScriptCompiler 的 C++ 单元测试：验证定时脚本的毫秒时序、
// commit-before-wait、分类型延迟补偿与取整损失补偿。

#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "maabangdream/pure_chart.hpp"
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

int count_commands(const std::vector<std::string>& lines, char command) {
    int total = 0;
    for (const std::string& item : lines) {
        if (item.size() >= 2 && item[0] == command && item[1] == ' ') {
            ++total;
        }
    }
    return total;
}

std::vector<int> down_times_ms(const std::vector<std::string>& lines) {
    std::vector<int> times;
    int elapsed_ms = 0;
    for (const std::string& item : lines) {
        if (item.rfind("w ", 0) == 0) {
            elapsed_ms += std::stoi(item.substr(2));
        } else if (item.rfind("d ", 0) == 0) {
            times.push_back(elapsed_ms);
        }
    }
    return times;
}

struct CommitFrame {
    int elapsed_ms = 0;
    std::vector<std::pair<char, int>> operations;
};

std::vector<CommitFrame> commit_frames(
    const std::vector<std::string>& lines) {
    std::vector<CommitFrame> frames;
    std::vector<std::pair<char, int>> pending;
    int elapsed_ms = 0;
    for (const std::string& item : lines) {
        if (item.rfind("w ", 0) == 0) {
            elapsed_ms += std::stoi(item.substr(2));
        } else if (item == "c\n") {
            if (!pending.empty()) {
                frames.push_back(CommitFrame{elapsed_ms, pending});
                pending.clear();
            }
        } else if (item.size() >= 3 && item[1] == ' ' &&
                   (item[0] == 'd' || item[0] == 'm' || item[0] == 'u')) {
            pending.emplace_back(item[0], std::stoi(item.substr(2)));
        }
    }
    return frames;
}

bool frame_has(
    const CommitFrame& frame,
    char command,
    int contact) {
    for (const auto& operation : frame.operations) {
        if (operation.first == command && operation.second == contact) {
            return true;
        }
    }
    return false;
}

std::filesystem::path chart_48_expert_path() {
    std::filesystem::path source = std::filesystem::absolute(__FILE__);
    std::filesystem::path root = source.parent_path();
    for (int level = 0; level < 4; ++level) {
        root = root.parent_path();
    }
    const auto direct = root / "resource" / "charts" / "bestdori"
        / "48" / "expert.json";
    if (std::filesystem::exists(direct)) {
        return direct;
    }
    root = std::filesystem::current_path();
    for (int level = 0; level < 8; ++level) {
        const auto candidate = root / "resource" / "charts" / "bestdori"
            / "48" / "expert.json";
        if (std::filesystem::exists(candidate)) {
            return candidate;
        }
        root = root.parent_path();
    }
    throw std::runtime_error("test chart 48/expert.json was not found");
}

bool script_has_valid_contact_lifecycle(
    const std::vector<std::string>& lines) {
    bool active[kMaxContacts] = {};
    for (const std::string& item : lines) {
        if (item.size() < 3 || item[1] != ' ' ||
            (item[0] != 'd' && item[0] != 'm' && item[0] != 'u')) {
            continue;
        }
        const int contact = std::stoi(item.substr(2));
        if (contact < 0 || contact >= kMaxContacts) {
            return false;
        }
        if (item[0] == 'd') {
            if (active[contact]) {
                return false;
            }
            active[contact] = true;
        } else if (!active[contact]) {
            return false;
        } else if (item[0] == 'u') {
            active[contact] = false;
        }
    }
    for (bool contact_active : active) {
        if (contact_active) {
            return false;
        }
    }
    return true;
}

bool compile_throws(std::vector<ScheduledAction> actions) {
    TouchScriptCompiler compiler;
    try {
        static_cast<void>(compiler.compile(actions, EngineConfig{}, 0.0));
    } catch (const std::runtime_error&) {
        return true;
    }
    return false;
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

    // 长等待被切成 250ms 段：1000ms = 4×250，500ms = 2×250。
    CHECK(text.find("w 250\nd 2") != std::string::npos);
    CHECK(text.find("w 250\nd ") != std::string::npos);
    // TAP 按住 50ms；到 hold UP 还剩 450ms，分段为 250 + 200。
    CHECK(text.find("w 200\nu 2") != std::string::npos);
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
    auto script = compiler.compile(actions, config, 0.0, false);
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
    auto script = compiler.compile(actions, config, 0.0, false);
    const std::string text = join(script);
    // 每次 DOWN 的 5ms 欠账按每段 ±1ms 上限偿还：后两段 500ms
    // 都变为 499ms，切段后呈现为 250 + 249。
    CHECK(text.find("w 249\n") != std::string::npos);
    CHECK(sum_waits(script) == 1498);
}

void test_rounding_loss_is_compensated_and_bounded() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    std::vector<ScheduledAction> actions = {
        action(ActionKind::Down, 0, 0.0166, 1),
        action(ActionKind::Down, 1, 0.0332, 2),
        action(ActionKind::Down, 2, 0.0498, 3),
    };
    auto script = compiler.compile(actions, config, 0.0, false);
    const int total = sum_waits(script);
    // 精确总等待 49.8ms，补偿后四舍五入误差 ≤2ms。
    CHECK(total >= 48);
    CHECK(total <= 52);
    CHECK(std::abs(total - 50) <= 2);
}

void test_fractional_windows_do_not_accumulate_rounding_phase() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    double window_start_s = 50000.123456789;
    double device_cursor_s = window_start_s;
    double max_phase_error_s = 0.0;

    // 约 120 秒、400 个非整数毫秒窗口。真实滚动发布的窗口边界受主机
    // 单调时钟影响，不会恰好落在整数毫秒；设备只能执行整数 w，因此
    // 编译器必须跨块携带唯一的亚毫秒相位误差，不能让它随机游走。
    for (int index = 0; index < 400; ++index) {
        const double duration_s = 0.2997134
            + static_cast<double>(index % 7) * 0.0001371;
        const double window_end_s = window_start_s + duration_s;
        const auto script = compiler.compile(
            {}, config, window_start_s, false, window_end_s);
        device_cursor_s += static_cast<double>(sum_waits(script)) / 1000.0;
        max_phase_error_s = std::max(
            max_phase_error_s,
            std::abs(device_cursor_s - window_end_s));
        window_start_s = window_end_s;
    }

    CHECK(max_phase_error_s <= 0.000501);
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
    CHECK(text.find("w 150\nd 8 ") != std::string::npos);
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
    CHECK(text.find("w 246\n") != std::string::npos);
    // 1496ms 前导 + TAP 50ms 按压 = 1546ms。
    CHECK(sum_waits(script) == 1546);
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
    CHECK(sum_waits(script) == 1080);  // 前导 1000ms + FLICK 80ms。
    CHECK(script_has_valid_contact_lifecycle(script));
}

void test_hold_tail_flick_reuses_active_contact() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    ScheduledAction tail = action(ActionKind::Flick, 3, 1.0, 2);
    tail.flick_direction = 0;
    auto script = compiler.compile({
        action(ActionKind::Down, 1, 0.1, 2),
        action(ActionKind::Move, 3, 1.0, 2),
        tail,
    }, config, 0.0);
    CHECK_EQ(count_commands(script, 'd'), 1);
    CHECK(count_commands(script, 'm') >= 2);
    CHECK_EQ(count_commands(script, 'u'), 1);
    CHECK(script_has_valid_contact_lifecycle(script));
}

void test_same_time_chord_shares_one_commit() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    auto script = compiler.compile({
        action(ActionKind::Tap, 1, 1.0),
        action(ActionKind::Tap, 5, 1.0),
    }, config, 0.0);
    const std::string text = join(script);
    // 两个 DOWN 必须相邻，并由同一个 commit 同时送入内核。
    CHECK(text.find(
        "d 7 340 565 50\nd 8 940 565 50\nc\n") != std::string::npos);
    CHECK(script_has_valid_contact_lifecycle(script));
}

void test_invalid_contact_lifecycle_is_rejected() {
    CHECK(compile_throws({
        action(ActionKind::Down, 1, 0.1, 2),
        action(ActionKind::Down, 2, 0.2, 2),
        action(ActionKind::Up, 2, 0.3, 2),
    }));
    CHECK(compile_throws({action(ActionKind::Move, 1, 0.1, 2)}));
    CHECK(compile_throws({action(ActionKind::Up, 1, 0.1, 2)}));
    CHECK(compile_throws({action(ActionKind::Down, 1, 0.1, 2)}));
}

void test_contact_state_survives_streaming_chunks() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    auto first = compiler.compile({
        action(ActionKind::Down, 1, 0.1, 2),
    }, config, 0.0, false, 0.2);
    auto second = compiler.compile({
        action(ActionKind::Move, 3, 0.3, 2),
        action(ActionKind::Up, 3, 0.4, 2),
    }, config, 0.2, true, 0.5);
    first.insert(first.end(), second.begin(), second.end());
    CHECK(script_has_valid_contact_lifecycle(first));
    CHECK_EQ(sum_waits(first), 500);
}

void test_empty_window_advances_device_time() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    auto script = compiler.compile({}, config, 1.0, false, 1.5);
    CHECK_EQ(sum_waits(script), 500);
    CHECK(count_wait_lines(script) >= 2);  // 受 max_wait_ms=250 限制。
}

void test_queued_transient_contact_is_not_reused_early() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    auto first = compiler.compile({
        action(ActionKind::Tap, 1, 1.0),
    }, config, 0.9, false);
    auto second = compiler.compile({
        action(ActionKind::Tap, 2, 1.025),
    }, config, 1.0, false);
    CHECK(join(first).find("\nd 7 ") != std::string::npos);
    CHECK(join(second).find("\nd 8 ") != std::string::npos);
}

void test_failed_chunk_does_not_poison_contact_state() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    static_cast<void>(compiler.compile({
        action(ActionKind::Down, 1, 0.1, 2),
    }, config, 0.0, false));
    bool rejected = false;
    try {
        static_cast<void>(compiler.compile({
            action(ActionKind::Down, 2, 0.2, 2),
        }, config, 0.1, false));
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    CHECK(rejected);
    const auto recovery = compiler.compile({
        action(ActionKind::Move, 3, 0.3, 2),
        action(ActionKind::Up, 3, 0.4, 2),
    }, config, 0.1);
    CHECK(count_commands(recovery, 'm') == 1);
    CHECK(count_commands(recovery, 'u') == 1);
}

void test_transient_tail_is_deferred_across_window_boundary() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    auto first = compiler.compile({
        action(ActionKind::Tap, 1, 0.49),
    }, config, 0.0, false, 0.5);
    auto second = compiler.compile({
        action(ActionKind::Tap, 2, 0.51),
    }, config, 0.5, true, 0.6);
    first.insert(first.end(), second.begin(), second.end());
    const auto down_times = down_times_ms(first);
    CHECK_EQ(down_times.size(), static_cast<std::size_t>(2));
    CHECK_EQ(down_times[0], 490);
    CHECK_EQ(down_times[1], 510);
    CHECK_EQ(down_times[1] - down_times[0], 20);
    CHECK_EQ(sum_waits(first), 600);
    CHECK(script_has_valid_contact_lifecycle(first));
}

void test_flick_moves_are_deferred_across_window_boundary() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    ScheduledAction flick = action(ActionKind::Flick, 1, 0.49);
    auto first = compiler.compile({flick}, config, 0.0, false, 0.5);
    auto second = compiler.compile({
        action(ActionKind::Tap, 2, 0.51),
    }, config, 0.5, true, 0.6);
    first.insert(first.end(), second.begin(), second.end());
    const auto down_times = down_times_ms(first);
    CHECK_EQ(down_times.size(), static_cast<std::size_t>(2));
    CHECK_EQ(down_times[0], 490);
    CHECK_EQ(down_times[1], 510);
    CHECK_EQ(count_commands(first, 'm'), 8);
    CHECK_EQ(count_commands(first, 'u'), 2);
    CHECK_EQ(sum_waits(first), 600);
    CHECK(script_has_valid_contact_lifecycle(first));
}

void test_future_hold_down_reservation_avoids_cross_boundary_flick_contact() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    ScheduledAction flick = action(ActionKind::Flick, 3, 0.49);
    ScheduledAction future_down = action(ActionKind::Down, 5, 0.55, 7);
    const auto first = compiler.compile(
        {flick}, config, 0.0, false, 0.5, {future_down});
    CHECK_EQ(count_commands(first, 'd'), 1);
    CHECK(join(first).find("d 7 ") == std::string::npos);
    CHECK(join(first).find("d 8 ") != std::string::npos);
    const auto first_receipts = compiler.last_execution_receipts();
    CHECK_EQ(first_receipts.size(), static_cast<std::size_t>(1));
    CHECK_EQ(first_receipts[0].action_token, static_cast<uint64_t>(1));

    const auto second = compiler.compile({
        future_down,
        action(ActionKind::Up, 5, 0.75, 7),
    }, config, 0.5, true, 0.8);
    const auto second_receipts = compiler.last_execution_receipts();
    CHECK_EQ(second_receipts.size(), static_cast<std::size_t>(2));
    CHECK_EQ(second_receipts[0].action_token, static_cast<uint64_t>(2));
    CHECK_EQ(second_receipts[1].action_token, static_cast<uint64_t>(3));
    std::vector<std::string> combined = first;
    combined.insert(combined.end(), second.begin(), second.end());
    CHECK(script_has_valid_contact_lifecycle(combined));
}

void test_large_absolute_clock_canonicalizes_tap_up_and_hold_down() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    const double anchor = 50000.0;
    const double tap_due = anchor + 24.15;
    const auto first = compiler.compile({
        action(ActionKind::Tap, 3, tap_due),
    }, config, anchor + 24.0, false, tap_due);
    CHECK(join(first).find("d 7 ") != std::string::npos);

    const auto second = compiler.compile({
        action(ActionKind::Down, 5, anchor + 24.2, 7),
        action(ActionKind::Up, 5, anchor + 24.4, 7),
    }, config, tap_due, true, anchor + 24.4);
    std::vector<std::string> combined = first;
    combined.insert(combined.end(), second.begin(), second.end());
    CHECK(script_has_valid_contact_lifecycle(combined));
}

void test_tap_receipt_emits_on_down_before_deferred_up() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    const auto first = compiler.compile({
        action(ActionKind::Tap, 1, 0.49),
    }, config, 0.0, false, 0.5);
    CHECK_EQ(count_commands(first, 'd'), 1);
    const auto first_receipts = compiler.last_execution_receipts();
    CHECK_EQ(first_receipts.size(), static_cast<std::size_t>(1));
    CHECK_EQ(first_receipts[0].action_token, static_cast<uint64_t>(1));
    CHECK(first_receipts[0].command == TouchCommandKind::Down);
    CHECK(std::abs(first_receipts[0].planned_engine_s - 0.49) < 1e-9);
    CHECK(first_receipts[0].line_index < first.size());
    CHECK(first[first_receipts[0].line_index].rfind("d ", 0) == 0);

    const auto second = compiler.compile({}, config, 0.5, true, 0.6);
    const auto& receipts = compiler.last_execution_receipts();
    CHECK(receipts.empty());
    CHECK_EQ(count_commands(second, 'u'), 1);
}

void test_flick_receipt_emits_on_down_before_deferred_up() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    ScheduledAction flick = action(ActionKind::Flick, 1, 0.49);
    flick.flick_direction = 1;
    const auto first = compiler.compile({flick}, config, 0.0, false, 0.5);
    CHECK_EQ(count_commands(first, 'd'), 1);
    CHECK(count_commands(first, 'm') >= 1);
    const auto first_receipts = compiler.last_execution_receipts();
    CHECK_EQ(first_receipts.size(), static_cast<std::size_t>(1));
    CHECK_EQ(first_receipts[0].action_token, static_cast<uint64_t>(1));
    CHECK(first_receipts[0].command == TouchCommandKind::Down);
    CHECK(std::abs(first_receipts[0].planned_engine_s - 0.49) < 1e-9);
    CHECK(first_receipts[0].line_index < first.size());
    CHECK(first[first_receipts[0].line_index].rfind("d ", 0) == 0);

    const auto second = compiler.compile({}, config, 0.5, true, 0.6);
    const auto& receipts = compiler.last_execution_receipts();
    CHECK(receipts.empty());
    CHECK(count_commands(second, 'm') >= 1);
    CHECK_EQ(count_commands(second, 'u'), 1);
}

void test_explicit_actions_emit_exact_command_receipts() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    const auto script = compiler.compile({
        action(ActionKind::Down, 1, 0.1, 2),
        action(ActionKind::Move, 2, 0.2, 2),
        action(ActionKind::Up, 2, 0.3, 2),
    }, config, 0.0);
    const auto& receipts = compiler.last_execution_receipts();
    CHECK_EQ(receipts.size(), static_cast<std::size_t>(3));
    const TouchCommandKind commands[] = {
        TouchCommandKind::Down,
        TouchCommandKind::Move,
        TouchCommandKind::Up,
    };
    const char prefixes[] = {'d', 'm', 'u'};
    const double planned[] = {0.1, 0.2, 0.3};
    for (std::size_t index = 0; index < receipts.size(); ++index) {
        CHECK_EQ(receipts[index].action_token,
            static_cast<uint64_t>(index + 1));
        CHECK(receipts[index].command == commands[index]);
        CHECK(std::abs(receipts[index].planned_engine_s - planned[index])
            < 1e-9);
        CHECK(receipts[index].line_index < script.size());
        CHECK(script[receipts[index].line_index][0] == prefixes[index]);
    }
}

void test_tail_flick_receipt_uses_first_move_without_new_down() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    ScheduledAction tail = action(ActionKind::Flick, 3, 0.5, 2);
    tail.flick_direction = 0;
    const auto script = compiler.compile({
        action(ActionKind::Down, 1, 0.1, 2),
        tail,
    }, config, 0.0);
    const auto& receipts = compiler.last_execution_receipts();
    CHECK_EQ(receipts.size(), static_cast<std::size_t>(2));
    CHECK_EQ(receipts[1].action_token, static_cast<uint64_t>(2));
    CHECK(receipts[1].command == TouchCommandKind::Move);
    CHECK(std::abs(receipts[1].planned_engine_s - 0.51) < 1e-9);
    CHECK(receipts[1].line_index < script.size());
    CHECK(script[receipts[1].line_index].rfind("m ", 0) == 0);
    CHECK_EQ(count_commands(script, 'd'), 1);
    CHECK(script_has_valid_contact_lifecycle(script));
}

void test_reset_contacts_clears_receipts_and_token_sequence() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    static_cast<void>(compiler.compile({
        action(ActionKind::Tap, 1, 0.1),
    }, config, 0.0));
    CHECK_EQ(compiler.last_execution_receipts()[0].action_token,
        static_cast<uint64_t>(1));

    compiler.reset_contacts();
    CHECK(compiler.last_execution_receipts().empty());
    static_cast<void>(compiler.compile({
        action(ActionKind::Tap, 2, 0.2),
    }, config, 0.0));
    CHECK_EQ(compiler.last_execution_receipts()[0].action_token,
        static_cast<uint64_t>(1));
}

void test_mixed_hold_tail_and_tap_share_first_commit() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    const auto script = compiler.compile({
        action(ActionKind::Down, 1, 0.1, 2),
        action(ActionKind::Move, 3, 1.0, 2),
        action(ActionKind::Up, 3, 1.0, 2),
        action(ActionKind::Tap, 5, 1.0),
    }, config, 0.0);
    const auto frames = commit_frames(script);
    bool shared_first_phase = false;
    bool released_later = false;
    for (const CommitFrame& frame : frames) {
        if (frame.elapsed_ms == 1000 && frame_has(frame, 'm', 2)
            && frame_has(frame, 'd', 7)) {
            shared_first_phase = true;
        }
        if (frame.elapsed_ms == 1000 && frame_has(frame, 'u', 2)
            && !frame_has(frame, 'm', 2)) {
            released_later = true;
        }
    }
    CHECK(shared_first_phase);
    CHECK(released_later);
}

void test_mixed_hold_head_and_unrelated_move_share_commit() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    const auto script = compiler.compile({
        action(ActionKind::Down, 0, 0.1, 3),
        action(ActionKind::Move, 2, 1.0, 3),
        action(ActionKind::Down, 5, 1.0, 2),
        action(ActionKind::Up, 2, 1.5, 3),
        action(ActionKind::Up, 5, 1.5, 2),
    }, config, 0.0);
    const auto frames = commit_frames(script);
    bool shared = false;
    for (const CommitFrame& frame : frames) {
        if (frame.elapsed_ms == 1000 && frame_has(frame, 'm', 3)
            && frame_has(frame, 'd', 2)) {
            shared = true;
        }
    }
    CHECK(shared);
}

void test_duplicate_same_contact_operation_is_rejected() {
    CHECK(compile_throws({
        action(ActionKind::Down, 1, 0.1, 2),
        action(ActionKind::Move, 2, 1.0, 2),
        action(ActionKind::Move, 3, 1.0, 2),
        action(ActionKind::Up, 3, 1.5, 2),
    }));
}

void test_tail_flick_keeps_same_contact_move_and_up_in_separate_commits() {
    TouchScriptCompiler compiler;
    EngineConfig config;
    ScheduledAction tail = action(ActionKind::Flick, 3, 1.0, 2);
    const auto script = compiler.compile({
        action(ActionKind::Down, 1, 0.1, 2),
        action(ActionKind::Move, 3, 1.0, 2),
        tail,
    }, config, 0.0);
    const auto frames = commit_frames(script);
    int final_move_frames = 0;
    int final_up_frames = 0;
    for (const CommitFrame& frame : frames) {
        if (frame.elapsed_ms == 1080 && frame_has(frame, 'm', 2)) {
            ++final_move_frames;
            CHECK(!frame_has(frame, 'u', 2));
        }
        if (frame.elapsed_ms == 1080 && frame_has(frame, 'u', 2)) {
            ++final_up_frames;
            CHECK(!frame_has(frame, 'm', 2));
        }
    }
    CHECK_EQ(final_move_frames, 1);
    CHECK_EQ(final_up_frames, 1);
}

void test_chart_48_mixed_groups_share_unrelated_first_phase() {
    EngineConfig config;
    const ChartTimeline timeline =
        ChartTimeline::from_json_file(chart_48_expert_path().string());
    const auto actions = compile_pure_chart_actions(timeline, config);
    TouchScriptCompiler compiler;
    const auto frames = commit_frames(compiler.compile(actions, config, 0.0));

    int expected_groups = 0;
    int shared_groups = 0;
    for (std::size_t begin = 0; begin < actions.size();) {
        std::size_t end = begin + 1;
        while (end < actions.size()
            && actions[end].due_s == actions[begin].due_s) {
            ++end;
        }

        bool has_down_action = false;
        std::vector<std::pair<char, int>> lifecycle_first;
        for (std::size_t index = begin; index < end; ++index) {
            const ScheduledAction& item = actions[index];
            if ((item.kind == ActionKind::Tap
                    || item.kind == ActionKind::Flick)
                && item.contact < 0) {
                has_down_action = true;
            } else if (item.kind == ActionKind::Down) {
                has_down_action = true;
            } else if (item.kind == ActionKind::Move) {
                lifecycle_first.emplace_back('m', item.contact);
            } else if (item.kind == ActionKind::Up) {
                bool already_has_move = false;
                for (const auto& operation : lifecycle_first) {
                    already_has_move = already_has_move
                        || (operation.first == 'm'
                            && operation.second == item.contact);
                }
                if (!already_has_move) {
                    lifecycle_first.emplace_back('u', item.contact);
                }
            }
        }

        const int due_ms = static_cast<int>(
            std::lround(actions[begin].due_s * 1000.0));
        std::vector<int> actual_downs;
        if (has_down_action) {
            for (const CommitFrame& frame : frames) {
                if (std::abs(frame.elapsed_ms - due_ms) > 2) {
                    continue;
                }
                for (const auto& operation : frame.operations) {
                    if (operation.first == 'd') {
                        actual_downs.push_back(operation.second);
                    }
                }
            }
        }

        bool group_is_mixed = false;
        int expected_down = -1;
        std::pair<char, int> expected_lifecycle{'?', -1};
        for (const auto& lifecycle : lifecycle_first) {
            for (int down : actual_downs) {
                if (down != lifecycle.second) {
                    group_is_mixed = true;
                    expected_down = down;
                    expected_lifecycle = lifecycle;
                    break;
                }
            }
            if (group_is_mixed) {
                break;
            }
        }

        if (group_is_mixed) {
            ++expected_groups;
            bool shared = false;
            for (const CommitFrame& frame : frames) {
                if (std::abs(frame.elapsed_ms - due_ms) > 2
                    || !frame_has(frame, expected_lifecycle.first,
                        expected_lifecycle.second)) {
                    continue;
                }
                for (const auto& operation : frame.operations) {
                    if (operation.first == 'd'
                        && operation.second == expected_down) {
                        shared = true;
                        break;
                    }
                }
                if (shared) {
                    break;
                }
            }
            shared_groups += shared ? 1 : 0;
        }
        begin = end;
    }
    CHECK(expected_groups >= 40);
    CHECK_EQ(shared_groups, expected_groups);
}

}  // namespace

int run_touch_script_tests() {
    test_basic_hold_lifecycle_ordering();
    test_commit_precedes_every_wait();
    test_per_type_offset_shortens_waits_with_clamp();
    test_rounding_loss_is_compensated_and_bounded();
    test_fractional_windows_do_not_accumulate_rounding_phase();
    test_transient_contact_avoids_active_hold();
    test_song_offset_and_press_bias_map_to_engine_time();
    test_flick_emits_down_move_up_swipe();
    test_hold_tail_flick_reuses_active_contact();
    test_same_time_chord_shares_one_commit();
    test_invalid_contact_lifecycle_is_rejected();
    test_contact_state_survives_streaming_chunks();
    test_empty_window_advances_device_time();
    test_queued_transient_contact_is_not_reused_early();
    test_failed_chunk_does_not_poison_contact_state();
    test_transient_tail_is_deferred_across_window_boundary();
    test_flick_moves_are_deferred_across_window_boundary();
    test_future_hold_down_reservation_avoids_cross_boundary_flick_contact();
    test_large_absolute_clock_canonicalizes_tap_up_and_hold_down();
    test_tap_receipt_emits_on_down_before_deferred_up();
    test_flick_receipt_emits_on_down_before_deferred_up();
    test_explicit_actions_emit_exact_command_receipts();
    test_tail_flick_receipt_uses_first_move_without_new_down();
    test_reset_contacts_clears_receipts_and_token_sequence();
    test_mixed_hold_tail_and_tap_share_first_commit();
    test_mixed_hold_head_and_unrelated_move_share_commit();
    test_duplicate_same_contact_operation_is_rejected();
    test_tail_flick_keeps_same_contact_move_and_up_in_separate_commits();
    test_chart_48_mixed_groups_share_unrelated_first_phase();
    return 0;
}
