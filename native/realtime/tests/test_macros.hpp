#pragma once

#include <cstdio>
#include <string>

// 轻量断言宏：供所有测试翻译单元共享。

namespace mbdr_test {

inline int g_checks = 0;
inline int g_failures = 0;

inline void report_failure(const char* file, int line, const std::string& message) {
    std::fprintf(stderr, "FAIL %s:%d: %s\n", file, line, message.c_str());
    ++g_failures;
}

}  // namespace mbdr_test

#define CHECK(condition)                                                       \
    do {                                                                       \
        ++mbdr_test::g_checks;                                                 \
        if (!(condition)) {                                                    \
            mbdr_test::report_failure(__FILE__, __LINE__,                      \
                "CHECK failed: " #condition);                                  \
        }                                                                      \
    } while (false)

#define CHECK_EQ(lhs, rhs)                                                     \
    do {                                                                       \
        ++mbdr_test::g_checks;                                                 \
        const auto left_value = (lhs);                                         \
        const auto right_value = (rhs);                                        \
        if (!(left_value == right_value)) {                                    \
            std::string message = "CHECK_EQ failed: " #lhs " == " #rhs;        \
            mbdr_test::report_failure(__FILE__, __LINE__, message);            \
        }                                                                      \
    } while (false)
