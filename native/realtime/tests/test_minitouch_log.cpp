// minitouch jlog 解析与分类型延迟校准的单元测试：
// 归因模型对齐 autodori 的 mnt_callback（c 分摊给未提交的 d/m/u）。

#include <cmath>
#include <string>
#include <utility>

#include "maabangdream/minitouch_log.hpp"
#include "test_macros.hpp"

namespace {

using namespace mbdr;

MinitouchLogEvent event(double start, double end, double cost,
                        std::string command) {
    MinitouchLogEvent result;
    result.start_ms = start;
    result.end_ms = end;
    result.cost_ms = cost;
    result.command = std::move(command);
    return result;
}

void test_parse_rejects_handshake_lines() {
    MinitouchLogEvent out;
    CHECK(!parse_minitouch_log("v 1", &out));
    CHECK(!parse_minitouch_log("^ 10 1280 720 255", &out));
    CHECK(!parse_minitouch_log("$ 1234", &out));
}

void test_parse_extracts_fields() {
    MinitouchLogEvent out;
    CHECK(parse_minitouch_log(
        "jlog {\"st\": 12.3, \"et\": 12.5, \"c\": 0.2, \"cmd\": \"d 0 10 20 50\"}",
        &out));
    CHECK(std::abs(out.start_ms - 12.3) < 1e-9);
    CHECK(std::abs(out.end_ms - 12.5) < 1e-9);
    CHECK(std::abs(out.cost_ms - 0.2) < 1e-9);
    CHECK(out.command == "d 0 10 20 50");
}

void test_calibrator_attributes_commit_to_uncommitted_actions() {
    LatencyCalibrator calibrator;
    calibrator.observe(event(100.0, 100.3, 0.3, "d 0 10 20 50"));
    calibrator.observe(event(100.5, 101.4, 0.9, "c"));
    calibrator.observe(event(102.0, 122.7, 20.7, "w 20"));
    calibrator.observe(event(122.8, 123.0, 0.2, "u 0"));

    const TouchLatencyOffsets offsets = calibrator.offsets();
    // d 实际 0.3 + c 分摊 0.9 = 1.2ms。
    CHECK(std::abs(offsets.down_ms - 1.2) < 1e-9);
    // w 超出 0.7ms。
    CHECK(std::abs(offsets.wait_ms - 0.7) < 1e-9);
    // u 尚未被 commit 分摊，只有自身 0.2ms。
    CHECK(std::abs(offsets.up_ms - 0.2) < 1e-9);
    // interval 为 3 段：0.2 / 0.6 / 0.1 -> 平均 0.3ms。
    CHECK(std::abs(offsets.interval_ms - 0.3) < 1e-9);
    CHECK(calibrator.event_count() == 4);
}

void test_correction_uses_old_average_delta() {
    LatencyCalibrator calibrator;
    calibrator.observe(event(0.0, 0.5, 0.5, "d 0 10 20 50"));
    calibrator.observe(event(1.0, 1.4, 0.4, "d 0 10 20 50"));
    const TouchLatencyOffsets previous;
    // down 总成本 0.9 + interval 0.5 - 旧均值 0 × 各自计数 = 1.4ms。
    CHECK(std::abs(calibrator.correction_ms(previous) - 1.4) < 1e-9);
}

}  // namespace

int run_minitouch_log_tests() {
    test_parse_rejects_handshake_lines();
    test_parse_extracts_fields();
    test_calibrator_attributes_commit_to_uncommitted_actions();
    test_correction_uses_old_average_delta();
    return 0;
}
