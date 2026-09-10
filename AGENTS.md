# AI 上手指南

> 本文档为 AI 助手（Codex、Claude 等）提供项目上下文。人类读者请参阅 [README.md](README.md)。

## 项目身份

基于 MaaFramework 的 BanG Dream! 自动化项目。通过 MFAAvalonia GUI 加载 Python Agent，控制 Android 模拟器完成自动演出、实时触控演奏、校准和挑战演出。

- 仓库：`https://github.com/coatcn1/MaaBanGDream`
- 当前版本：`v1.3.1`
- 许可证：GPL-3.0-only

## MaaBanGDream 运行布局

本项目日常开发和运行使用下面两个目录，不可混用。定制 MFAAvalonia 的独立源码仓库仅在需要修改或重建 MFA Core 时使用，见后文“MFA 定制运行时保护”。

| 目录 | 用途 |
| --- | --- |
| `D:\Documents\workplace\MaaBanGDream` | Git 仓库，唯一源码目录 |
| `D:\Documents\workplace\.tools\MFAAvalonia-profile-v3` | MFA 运行目录（Git 忽略），含 `interface.json` 和 `resource/` 部署副本 |

MFA 不会直接读仓库资源。修改代码后必须通过 `scripts/launch-mfa.ps1` 同步部署并重启 MFA。

## 固定运行环境

所有路径和版本都是硬约束，不可随意更改：

```
Conda:    D:\Documents\workplace\.tools\Miniconda3
环境名:   maabangdream
Python:   D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe (3.12.13)
```

| 组件 | 固定版本 |
| --- | --- |
| Miniconda | 26.5.3-1 (SHA-256: `60ab6c...de57a`) |
| Python | 3.12 |
| MaaFw (PyPI) | 5.10.2 |
| MFAAvalonia | 2.12.0 |
| .NET Binding | 5.8.0 |
| .NET Runtime | 10 |
| Conda 源 | `conda-forge` only, `nodefaults` |

精确组合记录：[runtime-compatibility.json](runtime-compatibility.json)

## 当前分支状态

```
main                                      ← 发布主线
```

功能与修复一律从 `main` 最新提交拉取 `feature/*` / `fix/*` 分支，验收后合并回 `main` 并删除分支；不再维护跨版本累积的“统一开发分支”。

定制 MFAAvalonia 独立开发分支为 `feature/performance-visual-settings`；两个仓库必须分别提交和推送。

## 仓库结构

```
agent/                  # Python Agent（MFA 通过 server.py 调用）
  server.py             # Agent 入口
  realtime/             # 实时触控引擎核心
  profile_manager.py    # Profile 管理器（stdin JSON 接口）

resource/               # MaaFramework 任务资源
  pipeline/             # Pipeline JSON 定义
  image/                # 模板图片

scripts/                # 运维脚本
  setup.ps1             # 创建/修复 Conda 环境
  verify.ps1            # 运行 pytest + 运行时兼容检查
  launch-mfa.ps1        # 同步资源 → 部署 → 启动 MFA
  check_runtime.py      # 运行时兼容性检查
  replay_realtime_trace.py  # 离线重放 trace.jsonl

tests/                  # pytest 测试
  fixtures/             # 测试 fixture 数据

profiles/               # 实时演奏 Profile（Git 忽略）
docs/                   # 额外文档
```

## 关键约定

- **分支命名**：`feature/*` 做功能，`fix/*` 做修复；完成验收并合并回 `main` 后删除分支
- **合并方式**：Merge 到 `main`（`--no-ff`，创建合并提交，保留分支完整提交历史），不用 squash 压平历史
- **PR 流程**：Draft PR → 检查通过 → Ready → Merge（创建合并提交）
- **禁止提交**：ADB 路径、设备序列号、日志、截图、Profile、`.venv`、MFA 运行目录
- **发布门禁**：`verify.ps1` 全部通过 + 工作树干净 + 真机门禁满足
- **歌曲模式**：仅支持当前曲目和随机选曲，不支持按名称指定
- **不在迁移范围**：旧 BDAS 的 Electron、PyWebIO、自建调度器

## MaaBanGDream Subagent 路由

用户级自定义 Agent 只用于边界清晰的委派，主 Agent 始终负责需求、证据链、作用域和最终结论。

- `mbd_log_scanner`：扫描 MFA 日志、`summary.json` 和 `realtime-result-*.json`，只提取事实，不修改源码，不单独定根因。
- `mbd_trace_scanner`：扫描 `checkpoints.jsonl`、`lifecycle.jsonl`、`events.jsonl` 和 `trace.jsonl` 的有限窗口，只提取事件链，不修改源码。
- `mbd_replay_runner`：运行离线 replay、对照基线和候选行为；允许生成 `.local/` 下的临时产物，但不修改正式源码。
- `mbd_implementer`（Terra High）：在主 Agent 已给出有界证据、最小假设和验收标准后，承担 Python/C++、测试、诊断、Legacy、replay、导航及 realtime 修改；核心 timing 任务也统一由该角色实现，不再额外转交第二个写入 Agent。

日志与 trace 扫描可在输入互不依赖时并行；replay 与实现按证据依赖顺序串行。主 Agent 负责 timing、phase、drift、scheduler、minitouch、ChartPredictor 锁相、Profile timing 及 Native/Legacy 共用链路的根因审查、反例检查和修改后复核；存在两个及以上竞争根因时，必须先补足证据或最小可证伪实验，再交给 `mbd_implementer`。同一批文件只能由主 Agent 或 `mbd_implementer` 中的一方修改。

Realtime 修改的闭环固定为：证据提取 → 必要的独立审查 → 最小实现 → 定向测试 → 离线 replay → 必要的修改后复核 → 用户真机验收。离线结果不得替代真机验收，也不得据此宣称“已修复”。

## 测试与验证

```powershell
# 完整验证（pytest + 运行时兼容检查）
.\scripts\verify.ps1

# 仅运行时兼容检查
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe scripts\check_runtime.py --mfa-root D:\Documents\workplace\.tools\MFAAvalonia-profile-v3

# 仅 pytest
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe -m pytest tests/ -v
```

pytest 临时目录固定在 `.local/pytest-<进程号>`（Git 忽略），不使用系统 AppData。

## 演出类型流程

任务入口定义在 `interface.json` 的 `task`，每个入口对应 `resource/pipeline/*.json`。
所有演出任务都先经过进程互斥检查，再用 `CommonRecover` 恢复主页/登录；带次数的任务
在每局结束回主页并由 `TaskProgress`/`TaskOutcome` 报告。单局演奏的统一核心是
`RealtimeProfilePlay`（`agent/realtime/profile_play_action.py`）。

### 0. RealtimeProfilePlay：单局演奏通用内部流程

1. 解析运行时选项与 Profile，确认游戏前台，校验环境签名（分辨率/DPI/帧率/画质/流速）。
2. 从准备页身份解析本地谱面（`resolve_confirmed_chart`）；Native 可用时消费预武装后端
   （`consume_prearmed_backend`）。
3. 建立 `debug/recordings/<run_id>` 证据包并保存 preflight 截图。
4. 需要最终封面复核的模式执行 `wait_for_final_cover`：封面 pHash + 等级 + 标题；黑场转场
   期间密集采样；失败时保留准备页谱面或降级 Legacy。
5. Native 路径：`NativeStartPhotogate` 首音门控（生命条 + 六轨判定标记、稳定窗口、500ms
   宽限、协力“其他成员正在准备中”弹窗拦截）→ `NativeMinitouchBackend` 启动 →
   C++ PlaybackSession 滚动发布 → 约 5Hz 生命/终态监控。
   Legacy 路径：`NoteDetector` + `RealtimePlanner` + `ControllerTouchDispatcher` 60FPS
   视觉演奏，可选数值生命保护。
6. 终态判定（结算/生命失败/用户停止/超时）→ 释放全部触点 → Native 完整性门禁 →
   写 `screencap/realtime-result-*.json`。
7. 结果处理：单人/挑战解析判定并回写 FAST/SLOW timing offset，协力只推进结算；失败按
   `play_failure_retry_count` 重试。

### 1. 单人实时演奏（RealtimeLive，入口 RealtimeMultiLive）

1. 进程互斥 → `CommonRecover` 主页 → `RealtimeGameEffectSettingsGate` 演出特效设置
   （`game_effect_settings_enabled=false` 时跳过）。
2. 局循环：主页 → 演出选择（`LiveSelectFind`）→ 自由演出 → 歌曲选择标记 →
   `RealtimeDifficultySelect`（点击目标难度并读等级/标题/封面身份，确认本地谱面）。
3. 准备页按正式/排练分路：
   - 正式：切到正式标记，检查必须有可用 Profile，执行 `RealtimeFormalPreflight`
     （关闭自动演出、3D Cut-in、3D/MV 显示）和 `RealtimePerformanceSettingsGate`
     （流速；跳过时仍执行 Native 预武装），开始后进入 `RealtimeProfilePlay`。
   - 排练：关闭 Demo 演出显示 → 同上门禁 → 开始 → `RealtimeProfilePlay`（rehearsal 参数）。
4. 每局结束 `CommonRecover` 回主页 → `TaskProgress` 计数 → 循环或 `TaskOutcome`。

### 2. 协力演出（CooperativeLive）

1. `CooperativeLiveConfigure` 依次配置：入房方式/档位/房号/难度/次数/结算动作/成员退出
   策略/调试。
2. 主页 → 演出选择 → 协力入口；按配置进普通房、好友邀请或私房；`verify_room_entry`
   确认已离开房间选择页。
3. 房间准备：等准备页 → 点难度并复核 → 演出特效/流速门禁（跳过时仍处理预武装/推迟）→
   点“准备完毕”并确认按钮消失（最多 3 次）。
4. `RealtimeProfilePlay`（cooperative 参数）：最终封面必确认（黑场与 5 封面准备页处理）；
   Native deferred 预武装；首拍门控拦截“其他成员正在准备中”弹窗；
   `cooperative_jitter_enabled` 时末尾漏 1~2 个单点。
5. 结算：`cooperative_result=advanced`（右下角 + ESC 循环推进到 PGGBM 并返回）；之后
   `return_to_room_selection` 回房间选择，好友/私房走 `stay_in_room`；成员退出按策略
   确认结束或重连。
6. 结算后识别不到房间页：先继续推进剩余结算页，仍失败走 `CommonRecover`（允许重启游戏）
   恢复主页继续下一局，不终止任务；最后一局 stay 失败直接按完成返回。
7. 次数循环 → `CooperativeLiveFinalize` 回主页 → `TaskOutcome`。

### 3. 一键实时演奏（ContinuousRealtimeLive）

1. 进程互斥；要求最近 15 分钟内有演出视觉设置读回复核（`require_recent_visual_settings`）。
2. 被动监听：每 0.1s 截图检测生命条（数值 ≥20 连续 3 帧）；检测到歌曲开始就调用一次
   `RealtimeProfilePlay`（`ignore_note_speed`、无生命保护、`require_completion=false`），
   打完继续监听下一首，直到用户停止。
3. 停止/失败保存最后一帧诊断截图到 `debug/recordings/listener-*`。

### 4. 实时校准（RealtimeCalibration）

1. 进程互斥 → 主页 → 演出特效门禁 → 读难度/歌曲模式/续跑模式/调试选项。
2. `CalibrationSessionStore` 新建或续跑会话（`auto`/`restart`）；环境签名一致才复用；
   生成 `accepted=false` 的候选 Profile。
3. 阶段固定为 `rehearsal-1`（一首排练）→ `formal-validation`（一首正式验证）。
4. 每局用 `calibration_round_plan` 构造 override 跑 `RealtimeProfilePlay`；FAST/SLOW
   收敛结果回写 timing offset；排练失败/技术故障可暂停续跑，正式局生命归零直接拒绝。
5. 正式验证通过条件：结果有效、完成、存活且 miss<10；通过后候选 Profile 标记
   `accepted=true` 并写校准会话报告。

### 5. 挑战演出（ChallengeLive）

1. 进程互斥 → 主页 → 演出特效门禁 → 局数门控 → `RealtimeProfileCheck`。
2. 主页 → 演出选择 → 挑战入口 → 歌曲标记 → `RealtimeDifficultySelect` → 准备页。
3. 挑战点数选择/确认 → 乐队标记 → `RealtimeFormalPreflight`（关闭自动演出、3D Cut-in、
   3D/MV）→ `RealtimePerformanceSettingsGate` → 开始 → `RealtimeProfilePlay`。
4. 结束回主页 → 计数循环 → `TaskOutcome`。

### 6. 自动演出（AutoLive）

1. 进程互斥 → 主页 → 演出选择 → 自由演出 → 选难度 → 准备页。
2. 模板识别自动演出开关（开/关）与配额耗尽；点开始后被动等结算（`CommonRecover`），
   回主页循环计数。此任务不使用实时触控引擎。

### 非演出任务

- `DailyFreeGacha`（每日免费抽卡）和 `ManualFlowRecording`（手动流程录制）不属于演出流程，
  需要时再单独文档化。

## 需要警惕的点

1. **修改代码后必须重新部署**：MFA 不会自动读取仓库资源。改 `resource/` 或 `agent/` 后务必跑 `launch-mfa.ps1`。
2. **MFA 与 ALAS 不能同时运行**：前台输入保护会阻止向同一模拟器发送输入，但不会阻止两个工具同时运行导致的竞争。可以提示让用户手动关闭。
3. **Conda 环境是硬依赖**：不使用仓库 `.venv`。所有 Python 命令必须通过 Conda 环境的绝对路径执行。
4. **MaaFramework `max_hit` 陷阱**：MaaFramework 在同一个外层任务中保留节点命中计数，嵌套 `context.run_task()` 会复用计数器。校准等嵌套场景必须使用无 `max_hit` 的专用 Action。
5. **ctypes 回调异常**：MaaFw Python Binding 会吞掉回调中的 Python 异常。所有回调必须显式 try/except 并返回失败状态。
6. **Profile 环境签名**：分辨率、DPI、帧率、画质、音符流速五项中任一项变化都会使 Profile 失效。草稿 `accepted=false` 不能驱动正式演奏。
7. **离线重放优先**：触控逻辑修改先用 `trace.jsonl` 离线重放验证（`scripts/replay_realtime_trace.py`），再上真机。

## MFA 定制运行时保护

这台开发机使用的 MFAAvalonia **不是同版本的官方原版**。除 MaaBanGDream 仓库和 MFA 运行目录外，还有一个独立的定制 MFA 源码仓库：

| 目录 | 用途 |
| --- | --- |
| `D:\Documents\workplace\MFAAvalonia` | 定制 MFAAvalonia 源码，包含“演出设置”、Profile 管理和 Mirror 启动检查保护 |

- 定制分支：`feature/performance-visual-settings`
- 定制基线提交：`d7b381b2fa6a09e140d925fb1504bac19ca1f921`
- `MFAAvalonia.Core.dll` 即使显示相同的 `2.12.0` 版本，也不能视为内容相同。

### 禁止用官方 DLL 覆盖定制 DLL

绝对不要从官方 `v2.12.0` tag、临时 clone 或 NuGet 发布物重新编译 `MFAAvalonia.Core.dll` 后直接覆盖运行目录。这样会同时删除：

- 设置页中的“演出设置”入口；
- Profile 表格、生命保护和调试目录等本地功能；
- 启动时跳过“不支持 Mirror 更新源”检查的保护。

如果启动后“演出设置”消失，或右下角出现“该资源操作暂不支持 Mirror酱”，优先检查是否部署了错误的官方 Core DLL，不要先清空用户配置。

`scripts/patch-mfa-stop-status.ps1` 必须：

1. 默认使用 `D:\Documents\workplace\MFAAvalonia` 定制源码；
2. 检查源码包含 `PerformanceProfileSettingsUserControl`；
3. 检查源码包含 `SupportsSelectedResourceUpdateSource`；
4. 检查定制基线提交是当前源码的祖先；
5. 替换 DLL 前保存到运行目录的 `.maabangdream-backup/`；
6. 定制源码缺失时直接失败，禁止自动 clone 官方源码作为回退。

### 三类配置不可混淆

| 内容 | 文件/组件 | 部署时能否覆盖 |
| --- | --- | --- |
| 任务、任务选项、Pipeline override | 仓库 `interface.json` | 可以，由 `launch-mfa.ps1` 生成运行副本 |
| Pipeline 和模板 | 仓库 `resource/` | 可以，由 `launch-mfa.ps1` 同步 |
| 主题、背景、窗口布局、模拟器、任务选中值 | 运行目录 `config/` | 不可以，属于用户配置 |
| “演出设置”等定制页面和 Mirror 保护 | 定制 `MFAAvalonia.Core.dll` | 只能由定制 MFA 源码编译 |

不要用删除 `config/`、重建运行目录或恢复默认设置来解决 UI 入口缺失；先比较 DLL 哈希和定制源码。

### MFA 停止状态修复

人工停止时，Maa job 可能先抛出 `MaaJobStatusException`，随后 cancellation token 才被观察到。严格失败传播开启时，这会把用户停止误报为任务失败。

修复必须加在定制 MFA 源码的 `MFAAvalonia/Helper/ValueType/MFATask.cs`：

```csharp
catch (MaaJobStatusException) when (token.IsCancellationRequested)
{
    return MFATaskStatus.STOPPED;
}
```

不要为修这个问题换回官方 DLL。运行目录仍需设置 `ContinueRunningWhenError=false`，这样真实任务失败会保持失败，而用户停止会显示“已放弃本次任务”。

## 最近交互与任务生命周期陷阱

- **判断“演奏坏了”之前先确认 offset 路径**：单人排练（`require_profile=false`）从 timing offset 0 开始逐帧自校准，开局必然整段偏晚、几百个 SLOW/GREAT；正式与协力才从 Profile 的 timing offset 起步。排练的高 GREAT/SLOW 计数是固有标定行为，不能当作 Legacy 引擎回归的证据；同曲同 offset 起点的“排练对排练”才是引擎对照。

- **两个引擎同时“变差”先查共享的 Profile timing_offset_ms**：该字段被 Native 与 Legacy 正式局共同读取。它被错误写入（例如原 60 被写成 71）时，两个引擎的正式局会同时整体偏移，表现为“一次修改把两个引擎一起改坏”。排查此类问题优先对比 Profile 的历史值/备份，而不是先怀疑引擎或漂移补偿代码。2026-09-06 的误诊教训：这个 Profile 问题曾多轮没被查出，原因是（1）单人排练 `require_profile=false` 从 offset 0 开始自校准，开局必然整段偏晚、几百个 SLOW/GREAT，表象与引擎时序损坏一致；（2）正式局两个引擎同时变差，把排查引向共享的时钟/漂移补偿代码，而不是共享的 Profile 数据文件；（3）FAST/SLOW 混合和有符号漂移序列呈现“抖动”，进一步把方向带向时钟问题。因此“两个引擎一起坏”的第一动作是对比 `timing_offset_ms` 的历史值或备份，先排除数据被写坏再谈引擎。

- **MuMu Native 漂移是客户机时钟速率偏斜，且逐局可变**：2026-09-07 区分实验结论——同一份代码在雷电 Native `[FULL]FIRE BIRD` 2331 PERFECT/0 GREAT（漂移 p50 5.2ms），MuMu 同环境 Native 漂移按 20 秒分段斜率 1.09→1.47→4.89→5.64ms/s 加速增长、逐局 p50 在 146.8/108/54/88ms 间波动；MuMu 高性能模式（6核/12G）与关主机负载均无效。速率校正实验（`MAABANGDREAM_NATIVE_DRIFT_RATE_CORRECTION=1`）闭环在 MuMu 上会发散（估计值撞限幅、p50 回弹），保持默认关闭，不要把它当 MuMu 解法。真机分工：**雷电走 Native、MuMu 走 Legacy**（MuMu 新号 Legacy 实测 490P/17G/1M、hit 99.8%）。雷电开发实例与 MuMu 便携实例的 Profile 是两份独立副本，正式局会各自回写 offset，本就应按模拟器分开维护，不要互相拷贝。

- **MuMu 12 占用 127.0.0.1 的 5555/7555 会影子住雷电的 adb**：MuMuVMMHeadless 额外监听 5555/7555，雷电 Ld9BoxHeadless 的 7555 被影子后 `emulator-7554` 实际连到 MuMu（指纹/设备名都对，但物理上控制的是 MuMu）。开发 MFA 连雷电前必须关掉 MuMu（或给雷电/多开换端口）；MuMu 开着时勿再对 `emulator-7554` 做任何设备操作。MuMu 主 adb 仍是 `127.0.0.1:16384`。

- **便携包不能假设 ASCII 安装路径**：便携运行时自带的 cv2 对含中文等非 ASCII 字符的路径读写会失败（开演前证据截图报“无法保存实时演奏阶段证据截图”），Native `.pyd` 的窄字符 `std::ifstream` 也会把 UTF-8 谱面路径误解成 ANSI 乱码。图像读写必须走 `agent/realtime/vision_io.py` 的字节级 `imdecode`/`imencode`，新增谱面文件读取在 Windows 必须转 UTF-16 用 `_wfopen`；禁止在 Agent 里直接 `cv2.imread/imwrite`。

- **单人准备页身份复核**：FULL FIRE BIRD 与普通版封面相同但 Expert 为 28/27、本地 ID 为 243/187。单人及其校准在选择乐队页读取左下角标题、难度和等级，必须在 Native 预武装和点击开始前完成；选曲列表不再读标题。准备页等级与选曲页冲突仍硬拒绝，不能用谱面等级填充识别结果。最终封面只复核，首音只定时；不把单人 FULL 规则套到协力或挑战。
- **重试身份不能使用包装对象地址**：Maa Custom Action 回调会重建 Tasker 包装对象，使用底层句柄保持单局预算；耗尽不能清零，须由下一局入口显式 reset。
- **Custom Action 参数覆盖是整块替换**：在基础 Pipeline 新增参数时，必须同步所有 interface 难度选项和校准 override；只检查部署的基础 JSON 不足以证明实际回调参数。用独立进程中的真实 MaaFramework 应用覆盖后读取节点验证，避免 AgentServer 绑定无法创建 Resource。
- **Native 等待成本按实际命令计数**：亚毫秒理想等待可能舍入为零，长等待可能拆成多条 `w`；预估一条后必须按实际条数归还或补记成本，否则高密度谱面会累积提前。设备 jlog 必须与谱面逐段对齐，绝对漂移百分位不能代替有符号趋势。

- **开演顺序必须由本局转场证明**：准备页 → 全黑开演转场 → 歌曲封面 → 完整演奏场 → 等待其他成员弹窗消失（如有）→ 首音 → 演奏结束。准备页可能同时误命中生命条与六轨白色标记，黑场前不得据此启动；首音前的黑场/演奏场消失也不得算结算。候选使用 `launch-mfa.ps1 -OrderedStartupTrial` 显式启用，普通启动默认关闭；启动证据缺失必须记录具体阶段并有界失败。普通协力漏检短黑场时，只允许按第 27 条使用连续两帧、且经难度/等级/标题约束确认的本局最终封面替代黑场证据；严格 Ordered Startup 候选仍要求真实黑场。封面身份无法解析时仍按可信准备谱面/整局 Legacy 的既有降级规则处理，不得绕过首音门控。

1. **Pipeline override 坐标**：Custom Action 必须使用 MaaFramework 解析后的 `argv.box`。重新读取源 JSON 的 `target` 会丢弃用户选择的难度覆盖，例如 Expert 被点击成 Easy。
2. **状态节点不能滥用 `DirectHit`**：带模板、ROI 或阈值的状态判断必须使用实际识别算法。`DirectHit` 会无条件命中，例如把“还剩 10 次”误报成自动演出次数耗尽。
3. **主页模板阈值需要真图校验**：主页样本得分随活动轮播横幅和按钮角标变化，
   2026-09-06 实测约 `0.8177`，旧阈值 `0.82` 会漏识别并在主页反复 ESC→退出确认
   取消→ESC，最终重启游戏；非主页页面（准备页/选曲页/结算页/协力房间）最高仅
   `0.36`。所有 `home_marker` 节点当前统一为 `0.75`，调整时必须同时更新所有
   Pipeline 和契约测试。
4. **停止不是业务失败**：Custom Action 观察到 `context.tasker.stopping` 时应立即停止输入并返回中性成功；不要继续截图、点击、嵌套任务或记录业务失败原因。
5. **正式演奏时限**：旧的 300 秒上限会在长曲仍演奏时强制失败。正式演奏节点当前为 600 秒，并应在超时、生命保护、用户停止、结算识别等终态记录具体原因。
6. **禁止含糊日志**：不要写“详情见上一条日志”。终态日志必须包含当前阶段和可执行的具体原因；运行时原因通过 `TaskOutcome` 的 latest failure reason 传递。
7. **启动恢复必须有界**：未知界面最多按 ESC 恢复 60 秒；仍无法识别主页才重启游戏。登录画面应先识别“点击任意处/开始”，登录阶段不得过早发送 ESC。
8. **部署必须从包含所有已合并功能的分支进行**：`launch-mfa.ps1` 用当前工作树的 `interface.json` 和 Agent 覆盖运行目录。功能合并回 `main` 后一律从 `main` 部署；多个未合并 feature 分支并存时，从缺少某功能的旧分支部署，会把该功能从 MFA 里“部署丢”。合并并部署完成后删除 feature/fix 分支，避免残留分支误导后续工作。
9. **MFA 任务列表有“用户删除记忆”**：某次部署的 interface 缺少某个任务时，MFA 会把它记进 `config/instances/default.json` 的 `CurrentTasks`（`任务名<|||>Entry` 键）当作“用户已删除”，之后 interface 恢复该任务也不会加回。恢复方法：停止 MFA，从 `CurrentTasks` 删掉对应键再启动；不要在 MFA 运行时直接改该文件（内存会覆盖）。
10. **演出设置自动保存会覆盖用户设置**：MFA 演出设置页在读取 Profile 失败时会把界面默认值整体写回 `profiles/selection.json`，清空用户运行时选项（Native、演出特效、TAP EFFECT、判定辅助、重试次数、校准流速等）。MFA 侧已加“读取成功前禁止自动保存”的保护。新增运行时选项必须四处同步：`profile_store.py` 的 `DEFAULT_RUNTIME_OPTIONS` 与 `_validated_runtime_options`、MFA `PerformanceProfileSettingsUserControlModel.cs` 的属性/加载/Capture、AXAML 开关。
11. **登录下载确认框会被退出确认的取消模板误命中**：下载框与退出确认框都有灰色“取消”按钮，`quit_confirm_cancel.png` 在下载框上得分 0.952（阈值 0.9）。下载确认必须先于通用模态取消处理；下载进行中用进度标记被动等待，不发送 BACK/ESC。
12. **流速校准与演出特效设置同类**：`game_effect_settings_enabled=false` 时，开演前不打开齿轮读/改流速，直接信任声明值（`RealtimePerformanceSettingsGate` 已支持跳过）。不要把两者拆成两套开关语义。
13. **协力准备完毕点击必须确认送达**：点“准备完毕”后要确认按钮消失，最多重试 3 次，防止触控未送达导致倒计时结束后空演奏/跳车。协力结果与一次性弹窗用“点右下角确定 + ESC”交替循环推进（用户验证过可应付大多数页面）。
14. **抽卡任务的页面陷阱**：左侧卡池列表滑动找“每日3次免费演出招募”时，滑动起点避开列表底部的“生日纪念服装贩售”入口（否则被当成点击进商店）；9.4.3 免费单抽有“TOUCH TO CUT”剪票引导需要点一下；状态判断统一用“点免费按钮后是否出现确认弹窗”，不要用“剩余N回/尚未完成”状态模板（会互相误匹配）。协力漏键抖动只对 Native 路径生效，由 `cooperative_jitter_enabled` 开关控制。
15. **协力“其他成员正在准备中”弹窗会误触发首拍门控**：弹窗在演奏场建立后出现/消失（含缩放动画），或弹窗出现时背景变暗，会让判定带整行颜色大幅变化，被当成第一颗音符，把谱面时钟提前启动。Native 协力首拍门控必须：弹窗主体（中下部白色圆角矩形 + 左侧粉色图标）存在时不建立颜色基线；弹窗消失的那一帧只重置基线；冻结基线后若判定带大面积同向变化，视为弹窗/变暗转场而不是首音。首音只能是窄列局部变化；单人/校准/挑战不得引入该弹窗门控。
16. **协力漏键抖动的 jittered 副本会被误判为预武装谱面不一致**：开启 `cooperative_jitter_enabled` 后，Native 预武装解析会把同一首歌替换成 `debug/jittered-charts/<run_id>.json` 副本，而最终封面复核得到的仍是 canonical 路径；用路径判等会直接失败，导致本局零输入、生命归零。判等必须比较歌曲身份（`bestdori_song_id` + `difficulty` + `level`），身份一致时以预武装副本为准消费。
17. **Legacy 演奏的长条头不能既 DOWN 又 TAP**：视觉回退局里，hold 起手后其头部碎片会在后续帧被 first-visible rescue 成同轨道 TAP，一颗长条被按两次。抑制器必须记录各轨最近 hold 起手时刻，在 `hold_start_suppress_seconds` 窗口内拦截同轨道 TAP/FLICK；Native 路径不受影响。
18. **协力黑场转场不能被当成“没有封面”**：准备完成后游戏会先整屏黑一下，随后封面或演奏场淡入；final cover 等待在黑场时进入无 sleep 的密集采样窗口，并在该窗口结束前不因演奏场出现而放弃。标题 OCR 还会把省略号或右侧提示读成杂字（如“…”→“今の”），`title_similarity` 必须容忍首尾噪声，否则准备页谱面无法确认。
19. **跳过演出设置页不能跳过 Native 预武装**：`game_effect_settings_enabled=false` 时 `RealtimePerformanceSettingsGate` 直接返回，但单人非 deferred 流程的 Native 预武装就在这个门禁里；跳过时仍必须调用 `prepare_native_for_settings_gate`（或按 `defer_native_prearm` 推迟），否则开演前消费会报“预武装不存在或已被消费”，整局零输入。
20. **协力结算后识别不到房间页不能终止任务**：成员退出弹窗关闭后往往还在结算页，重连不能直接 `ensure_room_page`；应先继续推进结算回房间/主页，仍失败走 `CommonRecover` 重启游戏再进。非 stay 路径结算回不去时把本局计入完成并恢复主页继续下一局，最后一局 stay 失败直接按完成返回。演出结束后的结算导航不识别成员退出弹窗：`wait_for_post_score_destination` 必须传 `detect_member_exit=False`，成员退出检测只保留在房间/准备阶段。
21. **成员退出弹窗只在进入演奏前出现**：该弹窗只会在进入演奏前（整屏黑场转场之前）的房间/准备阶段出现，演奏过程中和结算画面绝对不会出现。检测只应保留在房间/准备阶段（当前 `wait_for_post_score_destination` 已传 `detect_member_exit=False`）。当前版本弹窗标题是“错误”（正文“由于XX退出房间。将返回房间选择界面。”，底部居中“确定”），`member_exit_title.png` 已替换为完整的“错误”标题（56×27，1280×720 实拍提取），锚点 `(399,158)`、确定按钮点击 `(638,525)`；因“错误”是通用标题，模板检测必须继续限制在房间/准备阶段。2026-09-07 用户实测补充：点“准备完毕”之后、黑场转场之前的窗口里成员退出弹窗仍会出现（此时已离开房间等待页，常规检测不覆盖），会挡住转场导致整局卡死；已加 `watch_member_exit_before_black()`——准备完毕后高频轮询到黑场出现，看到弹窗点“确定”并按成员退出策略处理，看到黑场立即退出窗口。
22. **协力生命归零的“断网跳车”流程（真实弹窗已提取，MuMu 断网机制受限）**：生命归零后按顺序执行：切断游戏网络 → 游戏退后台再切回 → 弹窗1“通信已中断。是否继续演出？※本次演出将变为单人演出※”点**左侧“中断”** `(508,447)` → 弹窗2“确认中断当前演出返回主页吗？※中断当前演出的话，将不会获得演出报酬。”点**右侧粉色“中断”** `(754,439)` → 恢复网络 → “连接失败。”弹窗有界点“重试”直到回主页。模板 `disconnect_continue_body.png`（锚点 488,313）与 `disconnect_confirm_body.png`（锚点 495,307）已从 2026-09-07 雷电录像提取。关键约束：**禁止用 `svc wifi` / 飞行模式开关网络**——实测（2026-09-06 与 2026-09-07 两次）`svc wifi disable` 和 `settings put global airplane_mode_on 1`+广播都会打断 MuMu 的 adb 通道（设备离线），因为 MuMu 客户机只有 `wlan0` 一张网卡，游戏流量和 adb 的 NAT 转发同路，任何真实断网都会连带杀掉引擎的截图/触控通道。**MuMu 按 UID 断网目前不可行**：Android 12 内核无 `xt_owner` 匹配模块、无 `nft`、无 `bpftool`，`cmd netpolicy` 也没有 `set uid-policy`。**iptables 门禁已在雷电实测可用**：`adb root` 后 shell uid 0，owner 模块存在，`GameNetworkGate` 对游戏 UID 的 REJECT/恢复端到端验证通过（2026-09-07）；MuMu 上会 fail-closed。MuMu root 已开（`root_permission=true`）。已实现：`cooperative_network.py` 按 UID 屏蔽/恢复、`connect_failed_body.png`、`dismiss_connect_failed`、`disconnect_jump_out()` 两段弹窗编排（全程 finally 恢复、模板缺失直接 fail-closed、带单元测试）、引擎生命归零钩子与 UI“断网跳车”选项。MuMu 上的可行路线待用户定：手动断网后自动化处理弹窗，或找 MuMu 主机侧网络开关；`mumu-cli control --vmindex 0 tool cmd -c "<guest cmd>"` 是独立于客户机网络的宿主机通道，adb 断掉时可用它执行 `settings put global airplane_mode_on 0` 恢复。
23. **MFA 双进程/配置切换闪退是上游 Avalonia 崩溃**：2026-09-07 用户实测同时开两个 MFA（本机+便携）或快速来回切换配置时，`MFAAvalonia.exe` 以 `0xc0000005` 崩溃在已卸载的 `external_renderer_ipc.dll`（Windows Application 事件日志 11:00:30、11:03:20）。这不是 Agent 代码问题，修复需要改定制 MFAAvalonia 源码/上游；暂按“单实例 + 少切换配置”规避。“配置2连雷电但输入派发到 MuMu”是既有 `emulator-7554` 端口影子问题（MuMu 运行时占用 127.0.0.1:7555），关 MuMu 后恢复正常，与本条目无关。
24. **协力最终封面能读到却不认识＝歌曲不在本地曲库**：trace 里若出现大量“final cover jacket does not match selected chart / song fingerprint is not confirmed”而 playfield 已可见，通常是游戏新增歌曲未同步进 `resource/charts` 目录（选曲页同样 song=unknown）。此时协力没有可信准备页谱面可回退，只能整局视觉演出；修复是重跑 `scripts/sync_bestdori_catalog.py` 同步曲库。注意区分第二种情形：日志先出现 `gate_mismatch ... selected_bestdori_id=<N>` 再出现 `resolve_failed` 的指纹变化，说明封面已按“14 bit + 等级”被仓库解析、却被 `FinalCoverGate` 的 8 bit 复核拒绝（实测 `Little Busters!` 稳定 10 bit）——封面门控必须与 `LocalChartRepository.resolve` 使用同一套“等级匹配才放宽到 14 bit”的阈值，不要改回 8 bit，否则 Native 拿不到谱面、整局降级视觉 Legacy。`CooperativePreparePopupDetector` 是像素启发式（白色圆角条+左侧粉色图标），不依赖模板，`live_prepare.png` 只用于单人/自动/挑战的演出准备节点，与协力等待弹窗无关。
25. **协力谱面校准窗必须容纳“其他成员准备中”的等待时长**：协力演奏场出现后歌曲可能再等十几秒才开始（实测 2026-09-08 `FIRE BIRD` 等待约 16.4 秒），引擎锚点与歌曲开始的真实相位可超过单人局默认的 12 秒候选窗；窗口过窄会排除真实相位，让周期性段落里的假相位接管谱面时钟（实测锁到 -4522ms、比真实 -13530ms 早约 8.9 秒，开局几个视觉按键后整段盲压到生命归零）。`ChartPredictor` 的 `calibration_early_window_s` 在协力模式放宽到 60 秒，其余模式保持 12 秒；不要再缩回固定小窗口。此外锁定后相位校验窗口只有 ±350ms，整段相位锁错时既匹配不到同 lane 判定也不会产生可计入残差，旧逻辑完全看不见——现在按“滑动窗口 24 个可信投影中 ≥65% 在同 lane ±350ms 内找不到任何谱面判定”fail-closed，放弃 chart 输入回退纯视觉。改动谱面校准时先用 `scripts/replay_realtime_trace.py --chart-prelude-window` 对真实 trace 离线重放对比锁定相位。
26. **Legacy 的 HOLD/Slide 开局锁相只能消费离散拓扑事件**：连续绿色像素和逐帧 HOLD 是同一条绿条的重复观测，绝不能当多个校准样本。只使用视觉管线已经确认并派发的 HOLD/Slide 头 DOWN（50ms 内聚合同一和弦）与同一 contact 的离散 lane transition；匹配已确认本地谱面的 hold path 后，至少 4 个节点、2 组事件、2 条路径共同确认，残差 MAD 与 confidence 达标且不存在等强相位候选才允许 `chart_calibrated`。单个绿条、技能绿色特效、重复 lane pattern 或错误候选必须继续纯视觉，既有 TAP/FLICK/SKILL 投影校准与 fail-closed 回退不得删除。HOLD 事件时间要先去掉 Profile press bias，避免锁相后谱面调度再次应用 timing offset；不得借此修改 Profile 或扩大 FAST/SLOW ±35ms feedback correction。
27. **协力准备后短黑场不能作为唯一退出证据**：2026-09-09 `coop-20260909-114620-401553` 中，准备完毕后先固定睡眠 2 秒、黑场又被后续 100ms 轮询漏掉，旧 `watch_member_exit_before_black()` 只认黑场/成员退出而静默等满 12 秒；随后 preflight 已是中段演奏，Native 约 18 秒晚入场。入房后先独立等待“不指定歌曲”或准备页 180 秒，超时必须退到桌面再切回游戏并结束任务；点击“不指定歌曲”后重新开始独立 60 秒准备页窗口，不能继续共用前一阶段剩余时间。点击准备完毕后必须高频观察按钮送达、成员退出和黑场，不能有固定盲等。普通协力开演可消费本轮真实黑场，或连续两帧稳定且由本局难度、等级、标题共同确认的最终封面；后者必须把封面帧与解析结果一次性交给 `RealtimeProfilePlay`，不得重新等待黑场。任意高纹理加载画面不能作为封面放行。开演观察窗统一使用 `MEMBER_DOWNLOAD_TIMEOUT_SECONDS=60`，覆盖烧条倒计时和成员准备较慢的场景。漏黑场 fallback 的完整演奏场 + 连续局部音符运动只可证明“歌曲已开始”，必须记录后 fail-closed，绝不能在中段启动 Native/Legacy；此后仅运行独立数值生命监控，确认 alive 后连续 3 帧 `<20` 或监控超时时退到桌面再切回游戏并停止任务。大面积转场不得作为证据；准备页可能同时误中生命条和六轨白色标记，静态元素绝不放行。60 秒内仍无黑场、匹配封面或可靠动态证据时同样安全跳车并停止。数值生命监控未触发跳车时，不得直接改阈值，应在调试证据中先检查可见/不可见样本、最低值、`<20` 连续帧、alive/dead 确认和首个候选截图。
28. **Native 触点释放必须由本轮设备 `r` 回执证明**：EvATive7 minitouch 的 `w` 会在设备 reader 内阻塞，reset 写入 socket、关闭连接或 kill 进程都不能单独证明排队的 `r` 已执行。只在本轮 reset 请求前记录的 jlog 游标之后，精确解析到 `command == "r"`，再完成本地句柄、设备进程和启动提交清理，才允许 `release_confirmed=true`；旧 `r` 不得复用。最大队列 750ms 时统一使用 1 秒停止预算（750ms 等执行 + 250ms 清理），超时仍强制关闭但必须 fail-closed。协力空血取消只有同时满足 jump_requested、life_depleted、双 cancelled、reset 执行确认及其余传输门禁时，才能进入断网跳车流程。

## 后续开发方向（已记录，暂缓或未开始）

- **MuMu Native 漂移**：定性为客户机时钟速率偏斜且逐局可变，速率校正实验闭环发散；暂不解决，MuMu 用 Legacy、雷电用 Native。见“最近交互”的 MuMu 时钟偏斜条目。
- **调试文件定时清理**：暂不做应用内自动清理；已有 `.local/clean-recordings.ps1`（保留最新 5 个）。若做，建议按保留天数在任务启动时修剪 `debug/recordings/*`，并留足证据窗口。
- **双 MFA 进程/配置切换闪退**：上游 Avalonia `external_renderer_ipc.dll` 0xc0000005；暂缓，规避方式为单实例、少切换配置。见第 23 条。
- **纯 GitHub 整包更新（v1.3.3 起，已实现双端）**：早期“逐文件 Range 增量”
  方案已废弃（354MB Python 运行库归档每次都进 diff、国内网络卡在 7%、中断后
  无法续传）。现行为：整包下载到 `.part`（HTTP Range 断点续传）→ 与发布附带
  `.zip.sha256` 校验 → 重启辅助脚本在进程退出后覆盖解压（保留
  config/profiles/logs/debug/screencap）→ 直接 ShellExecute 启动器重启；
  版本依据为 `update-manifest.json`（只在完整应用成功后写入）。更新包
  `MaaBanGDream-vX-win-x64-update.zip` 不含 Python 运行库归档与
  `resource/charts`（谱面走“演出设置 → 谱面辅助 → 同步”独立通道），约
  143MB；本机 `runtime/python/python.exe` 缺失才回退完整包。首启解压后删除
  `runtime/maabangdream-python.zip`。升级后安装目录名自动跟随版本（
  `MaaBanGDream-v1.3.3-win-x64` → `MaaBanGDream-v1.3.4-win-x64`），由
  update.ps1 的脱离启动器辅助进程改名（启动器退出码 2 协议），自定义目录名
  不改。关键坑：中文路径别经 `cmd /c` 转 ANSI 代码页（用 ShellExecute 直启
  `.cmd`）；生成的 `.ps1` 必须写 UTF-8 BOM；`SHA256.HashData(Stream)` 不关闭
  流，必须 `using`。
- **复用 MFA 原生 GitHub 更新界面（计划，暂缓）**：现自绘“MaaBanGDream 版本
  更新”卡片与状态文本较简陋。MFA 上游自带 GitHub 下载源
  （`VersionChecker.GetLatestVersionAndDownloadUrlFromGithubAsync` + 下载源
  下拉框 + 任务队列下载进度条 + Toast，不依赖 Mirror酱）。计划：`interface.json`
  补 `controller.github` 指向本仓库 → 复用上游“检查/下载/进度/Toast”整套 UI，
  只把“解压进 resource/”替换成“整包覆盖 + 重启脚本”；保留现有断点续传、sha256
  校验与目录改名逻辑。动定制 MFAAvalonia 的更新设置页，需要单独验收。
- **Special 谱面支持**、**更多演出类型**：未开始。

## 修改后的最低验收

除 `scripts/verify.ps1` 和运行时兼容检查外，涉及任务生命周期或 MFA 部署时至少完成：

1. 通过 `scripts/launch-mfa.ps1` 部署并启动；
2. 打开 MFA 设置页，确认“演出设置”存在且 Profile/参数能加载；
3. 确认最新启动日志包含“跳过启动资源版本检查”，且没有新的 Mirror 酱错误；
4. 从主页实际启动一次任务，确认能进入相应演出流程；
5. 运行中手动停止，确认显示“已放弃本次任务”，不是“任务运行失败”；
6. 对实时演奏改动先重放已有 `trace.jsonl`，再决定是否需要完整真机长曲验收。

MaaBanGDream 和定制 MFAAvalonia 是两个独立 Git 仓库。若一次修复同时修改两边，必须分别检查工作树、分别提交和推送，不能把一个仓库的源码复制进另一个仓库。

## 高密度谱面与流速闭环约束

1. **紫色外圈不是 FLICK**：普通音符的紫色外圈只能作为普通 TAP 的补充可见区域；只有检测到成组、同向的粉色箭头/折线后才能升级为 FLICK。修改颜色阈值时必须同时回归普通紫色音符与真实粉色箭头。
2. **实时热路径禁止阻塞**：截图、检测、跟踪和触控派发路径不得使用 `sleep`、ADB 前台查询或同步等待手势完成。FLICK 必须按帧推进 DOWN/MOVE/UP；停止、异常和歌曲终态必须立即释放全部触点。
3. **HOLD 必须有轨迹证据**：绿色技能特效、短圆环和判定线残影不能单独启动长按。HOLD 需要连续绿色轨迹、可信形状或跨帧一致运动；无绿条歌曲的离线重放必须保持 `hold_start=0`。
4. **游戏流速必须读取后修正**：`interface.json` 的 `note_speed` 只是目标声明，不能作为游戏已经采用该值的证据。准备界面齿轮会记住上次使用的标签页，每次调整流速前必须先点击第一个“演出设定”标签 `(297,155)`；不要再点击 `(430,155)`（该坐标属于“演出效果·音量设定”）。流速范围为 `1.00–12.00` 且首尾循环，按钮从左到右为 `-0.50/-0.10/-0.01/+0.01/+0.10/+0.50`；连续减法不能归一到最小值。每首歌必须用固定数字模板读取当前值、按差值修正并再次读取复核后才能开演。
5. **一个 Profile 固定一种流速**：Profile 环境签名继续精确记录流速。不同难度或同一难度可有不同流速 Profile，但一次四首歌校准过程中不得逐曲自适应修改流速；需要试验新流速时生成新的 Profile。
6. **密集同轨音符不得固定宽合并**：跟踪与轮廓拆分阈值必须随透视和音符头尺寸缩放。修改后至少覆盖同轨间距 8–20 px 的回归用例。
7. **日志必须可直接验收**：实时终态至少输出实际/期望流速、是否修正、TAP/FLICK/HOLD 动作数、帧间隔 P50/P95/最大值、有效 FPS 和明确终止原因。
8. **定制 MFA 脏工作树也必须重新部署**：部署标记不能只比较 MFA 的 Git `HEAD` 和 `MFATask.cs`。设置页/ViewModel 等未提交源码变化也要计入指纹，否则 `launch-mfa.ps1` 会误判“已部署”并继续运行旧 DLL。
9. **不要假定 Maa OCR 可用**：项目资源包当前不含 MaaFramework OCR 检测/识别模型；未显式部署并验证模型时，JOCR 会返回空结果且日志出现 `recer_ is null`。流速门禁使用仓库内纯黑白数字模板，不是 JOCR；模板读取失败或复核不一致时必须阻止开演，不能退回盲点按钮。
10. **共享封面不能单独确认歌曲**：早期歌曲和 `[FULL]` 版本可能使用同一封面。封面 pHash 只做候选收窄，必须结合难度等级与本地标题 OCR；等级冲突是硬拒绝条件。`[FULL]` 仅作为本地标题别名参与匹配，不能吞掉等级约束。
11. **难度数字模板必须覆盖 6/8**：难度等级读取曾因过严相似度门槛稳定拒绝含 6 或 8 的等级。改分类阈值后需用 5–40 的合成数字全集回归；共享封面仍歧义时可跨帧重试，但不得重复点击难度。
12. **结算导航只使用 Android BACK**：奖励、排名、活动和达成报酬页面禁止坐标盲点；判定详情页身份优先于相似的排名模板。每日首局奖励与七日奖励可能连续出现两个弹窗，应逐帧识别并有界发送 BACK。
13. **临轨绿条只在严格证据下取中点**：只有至少三次相邻轨来回切换、轨道跨度恰为 1 的锯齿 Slide，才把触点锚在两轨中点；普通滑条仍跟随谱面连接点，不能泛化成宽判定。
14. **谱面同步是显式维护操作**：MFA 的手动同步入口复用 `scripts/sync_bestdori_catalog.py`，只保存 Hard/Expert/Special，封面按 CN→JP→EN 回退。演奏热路径禁止联网；同步前应停止 Maa 任务，生成清单必须原子替换。
15. **首音门控必须先证明演奏场成立**：Native 的 60 FPS 截图循环不得使用固定 200 帧冻结期，也不得用加载页、歌曲信息页、全黑转场或演奏场淡入建立颜色基线。单人、校准、挑战和协力都必须先同时确认生命条与至少六轨白色判定标记；随后才按各自进入阶段使用连续稳定窗口和 500 ms 前奏残留宽限。两类模式不得引入不同的歌曲时间偏移，演奏场证据丢失必须重置基线。检测带到判定时刻的补偿必须由当前流速的真机录像对齐，不能直接照搬上游 30 ms；当前 Expert 速度 5.0 基线为 190 ms。结果报告必须保存演奏场等待、补偿、稳定、忽略和触发证据。
16. **最终封面复核谱面，首音只负责定时**：单人、校准、挑战和协力在完整演奏场前都必须观察最终歌曲信息页，使用居中封面复核本轮已解析的本地谱面；封面不得作为歌曲时钟起点。准备页难度、等级或共享封面标题发生真实冲突时仍硬拒绝；仅最终封面未识别时不得直接结束：已有可信准备页谱面则记录降级并继续原谱面，没有可信谱面则必须在发送任何触控前整局回退 Legacy 视觉演奏，禁止 Native/Legacy 中途混合。生命保护关闭时禁止继续构造数值 `LifeDetector`，但必须保留演奏场启动门控和约 5 Hz 的终态监控；协力不得先等到演奏场出现再启动会话。
17. **调试证据必须覆盖完整演奏生命周期**：实时调试记录不能只从音符热路径开始；同一 run ID 的证据包至少要关联准备页身份、开演前检查、最终封面、演奏场门控、输入引擎、结算、清理，以及所有降级和重试决定。高频阶段继续使用非阻塞 Trace/录像，低频阶段保存有界关键截图和结构化原因；任何门控失败、超时、异常、用户停止和重试前都必须留下终态现场。技术失败重试必须有可配置上限，每次重试先释放全部触点和 Native 会话并恢复到已识别页面；用户停止、配置冲突和身份硬冲突不得盲目重试。
