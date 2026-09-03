#pragma once

#include <stdexcept>
#include <string>

#include "maabangdream/types.hpp"

namespace mbdr {

// ChartTimeline 的编译细节（JSON 解析 + BPM 映射）在 src/chart_timeline.cpp。

// 解析失败的异常类型（统一由 pybind 转成 Python ValueError）。
class ChartParseError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

}  // namespace mbdr
