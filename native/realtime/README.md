# Native Realtime Engine V2

MaaBanGDream 的实验性 C++ 演奏核心。它在音乐开始前完成谱面编译和武装，
从第一个音符起独占 minitouch 输入，并以绝对谱面时间滚动发布短块。Native
通过全部离线门禁和用户真机验收前保持默认关闭；Python Legacy 继续作为独立
回退方案，但一次演奏中禁止两套引擎混合输入或在故障后中途切换。

## 会话边界

正式生命周期固定为：

```text
prearm -> 光闸启动 -> 滚动 publish -> drain/finish 或 cancel -> report
```

- `prearm` 在点击开始前解析谱面、生成动作并建立 Native 会话；播放节点只消费
  匹配 `run_id + chart_path` 且仍在有效期内的预武装会话，不允许开演后临时构建；
- 光闸确认音乐起点后，由 C++ `PlaybackSession` 锁定首拍绝对时刻并负责
  截止时间分窗；旧的逐 tick `ActionScheduler` 与视觉观测型
  `SongClockSynchronizer` 继续作为离线诊断组件，不进入纯谱面正式热路径；
- 发布器默认维持约 500 ms 前瞻、200 ms 低水位，设备侧待执行队列硬上限为
  750 ms；每块按绝对截止时间重新计算 minitouch 相对等待，避免逐条等待误差累积；
- 已执行块的 jlog 反馈只校正尚未发送的未来块，不重写已经进入设备队列的动作；
- 正常结束必须先排空最后一块并确认执行证据完整；取消则停止生产、请求 reset，
  必要时终止 minitouch 并在下次任务前重建连接；
- 任何 Native 故障都安全释放触点并结束本局，不在演奏中切回 Legacy。

Python 只负责 MFA 编排、页面导航、演出前门禁、歌曲与谱面确认、开演锚点、约
5 Hz 的生命/终态监控以及结果解析。开演后不再运行 60 FPS 的 Python
detector/planner/timing-feedback 循环，也不接收或调度单个音符。

## 动作语义

- TAP 基线保持 50 ms，FLICK 基线保持 80 ms，Slide 每 10 ms 插值一次；这些
  参数可配置，但验收期间必须锁定；
- HOLD 从起点持续移动到谱面最终尾点后才释放；尾部 FLICK 复用同一活跃触点执行
  `MOVE -> UP`，禁止再次 `DOWN`；
- 同时音符按同一帧提交：同一和弦的 DOWN/MOVE 先写入，再由同一个 `c` 统一提交，
  不因单个 TAP 的保持时间而串行错开；
- 触点状态机拒绝重复 DOWN、未激活 MOVE/UP 和触点泄漏；正常结束、取消和异常
  终态都必须释放全部触点。

## 执行证据与时钟

Native 异步读取 minitouch jlog，并按完整低层命令 FIFO 校验计划与实际执行顺序。
编译器为动作生成执行回执：普通 TAP/FLICK 以首次 DOWN、尾部 FLICK 以首次 MOVE、
HOLD 以各自 DOWN/MOVE/UP 作为来源命令；来源命令向后关联到所属提交 `c`，以该次
commit 的 `end_ms` 作为系统可见执行时刻，因此同一和弦共享完全相同的实际时间戳。
缺失、额外或乱序命令一律 fail-closed。

启动探测日志与正式播放日志隔离。主机与设备单调时钟通过探测往返中点建立映射，
报告同时记录时钟偏移、映射依据和不确定度；不确定度超过 1 ms 时，不把绝对漂移
指标视为有效。播放会话只有在 `planned == sent == executed`、jlog 证据完整且没有
分块下溢、后端与 C++ 会话都处于 `finished`、并在 500 ms 内确认设备端
minitouch PID 已退出时，才允许报告正常完成。准备超时会封闭启动代次；迟到线程
只能自清理，禁止在 stop 后重新连接或发布探测命令。

每次运行生成唯一 run ID，并把游戏判定与 Native 执行统计关联到同一报告。报告
至少包含计划/发送/执行动作数、各手势数量、和弦组数、分块数、队列深度与下溢、
时钟偏移与不确定度、漂移 P50/P95/最大值、停止释放延迟、jlog 路径和明确终止原因。
释放报告同时包含 `release_confirmed` 与独立失败原因；reset 没有协议 ACK，单纯成功
写入 `r` 不作为释放完成证据。reset 发送与本地句柄清理均为有界异步路径，TCP
发送设有 100 ms 上限；最终正向证据仍来自设备端 PID/唯一 socket 进程退出。

## 目录

```text
include/maabangdream/   公共头文件（数据模型、时间轴、调度、同步、播放会话）
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
- 没有 MSVC 时回退到 conda 环境内的 zig 便携工具链；
- 产物输出到 `agent/realtime/native/maabangdream_realtime.pyd`（Git 忽略），
  构建脚本最后会在固定 CPython 3.12 x64 环境内做导入自检并运行 C++ 测试。

## 验证边界

虚拟压力测试用于验证调度、队列、协议和取消路径，不等同于 Android/minitouch
真机验收：

```powershell
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe `
  scripts\stress_native_realtime.py --duration 120 --mode both --pretty
```

空闲和 CPU 压力两种模式都必须满足：零分块下溢、绝对时延 P95 不超过 3 ms、
最大值不超过 8 ms、取消释放不超过 500 ms。随后仍须离线 trace 重放、完整
`scripts/verify.ps1`，以及冻结配置后的用户真机连续局验收。详细门槛见
[`docs/realtime-engine-v2-goals.md`](../../docs/realtime-engine-v2-goals.md)。

## 关键约定

- 时间关系为 `chart_time = engine_time + song_offset_s`；
- Native 使用经过验收并冻结的 Profile 偏移，正式验收期间不做自动时延学习；
- Profile 环境签名继续严格包含分辨率、DPI、帧率、画质和流速；
- Native 默认关闭；未显式选择且未完成预武装时，Legacy 行为保持不变。
