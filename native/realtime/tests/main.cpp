// 轻量 C++ 测试运行器：便于在无 MSVC 的便携工具链下直接编译运行，
// 也通过 CTest 调用。

#include <cstdio>
#include <exception>
#include <string>

#include "test_macros.hpp"

int run_chart_timeline_tests();
int run_pure_chart_tests();
int run_scheduler_tests();
int run_song_clock_tests();

namespace {

template <typename Function>
void run_group(const char* name, Function&& function) {
    try {
        function();
    } catch (const std::exception& exc) {
        std::fprintf(stderr, "CRASH %s: %s\n", name, exc.what());
        ++mbdr_test::g_failures;
    }
}

}  // namespace

int main() {
    run_group("chart_timeline", run_chart_timeline_tests);
    run_group("pure_chart", run_pure_chart_tests);
    run_group("scheduler", run_scheduler_tests);
    run_group("song_clock", run_song_clock_tests);
    if (mbdr_test::g_failures == 0) {
        std::printf("ok: %d checks passed\n", mbdr_test::g_checks);
        return 0;
    }
    std::printf("failed: %d/%d checks\n",
        mbdr_test::g_failures, mbdr_test::g_checks);
    return 1;
}
