cmake_minimum_required(VERSION 3.20)
project(maabangdream_native_realtime VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# pybind11：优先使用已安装的 CMake 包（pip 安装后可用 pybind11_DIR 指向），
# 没有时回退 FetchContent。
find_package(pybind11 CONFIG QUIET)
if(NOT pybind11_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        pybind11
        GIT_REPOSITORY https://github.com/pybind/pybind11.git
        GIT_TAG v2.13.6
    )
    FetchContent_MakeAvailable(pybind11)
endif()

set(REALTIME_SOURCES
    src/bindings.cpp
    src/chart_timeline.cpp
    src/pure_chart.cpp
    src/scheduler.cpp
    src/song_clock.cpp
    src/touch_script.cpp
)

pybind11_add_module(maabangdream_realtime ${REALTIME_SOURCES})
target_include_directories(maabangdream_realtime PRIVATE
    include
    third_party
)

if(MSVC)
    target_compile_options(maabangdream_realtime PRIVATE /W4 /permissive-)
else()
    target_compile_options(maabangdream_realtime PRIVATE
        -Wall -Wextra -Wno-nullability-completeness)
endif()

set_target_properties(maabangdream_realtime PROPERTIES
    OUTPUT_NAME "maabangdream_realtime"
    PREFIX ""
    SUFFIX ".pyd"
)

# C++ 单元测试（轻量断言运行器，无第三方依赖）。
add_executable(mbdr_tests
    tests/main.cpp
    tests/test_chart_timeline.cpp
    tests/test_pure_chart.cpp
    tests/test_scheduler.cpp
    tests/test_song_clock.cpp
    tests/test_touch_script.cpp
    src/chart_timeline.cpp
    src/pure_chart.cpp
    src/scheduler.cpp
    src/song_clock.cpp
    src/touch_script.cpp
)
target_include_directories(mbdr_tests PRIVATE
    include
    third_party
    tests
)
if(MSVC)
    target_compile_options(mbdr_tests PRIVATE /W4 /permissive-)
else()
    target_compile_options(mbdr_tests PRIVATE
        -Wall -Wextra -Wno-nullability-completeness)
endif()

enable_testing()
add_test(NAME mbdr_tests COMMAND mbdr_tests)
