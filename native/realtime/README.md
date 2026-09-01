# Native Realtime Engine V2

MaaBanGDream 实时演奏引擎的 C++ 部分（第一阶段：Pure Chart + Scheduler +
SongClockSynchronizer）。Python 侧编排、导航、结算与触控派发仍由
`agent/realtime` 负责；本目录只承担离线可验证的核心计算。

## 目录

```text
include/maabangdream/   公共头文件（数据模型、时间轴、调度、同步）
src/                    实现与 pybind11 binding
tests/                  轻量 C++ 断言测试（无第三方测试框架）
third_party/            nlohmann/json（MIT，单头文件）
CMakeLists.txt          标准 MSVC x64 + pybind11 构建
```

## 构建

```powershell
.\scripts\build_native_realtime.ps1
```

- 有 MSVC（vswhere 可发现）时走 CMake + MSVC x64；
- 没有 MSVC 时回退到 conda 环境内的 zig 便携工具链（已验证产出 `.pyd`
  可被固定 CPython 3.12 x64 环境导入）；
- 产物输出到 `agent/realtime/native/maabangdream_realtime.pyd`（Git 忽略），
  构建脚本最后会在目标 Python 环境内做导入自检并运行 C++ 单元测试。

## 关键约定

- 时间约定：`chart_time = engine_time + song_offset_s`；
- `contact=-1` 表示瞬态动作，由 Python `ControllerTouchDispatcher` 派发时
  分配；hold 生命周期（DOWN/MOVE/UP/尾部 FLICK）携带确定性 contact；
- 相位同步只有唯一解满足样本数、lane 数、MAD、前置保护与锚点约束时才锁定，
  否则 fail-closed，绝不盲打；
- Native 默认关闭，Legacy Python 引擎保持不变。
