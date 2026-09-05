# AI 上手指南

> 本文档为 AI 助手（Codex、Claude 等）提供项目上下文。人类读者请参阅 [README.md](README.md)。

## 项目身份

基于 MaaFramework 的 BanG Dream! 自动化项目。通过 MFAAvalonia GUI 加载 Python Agent，控制 Android 模拟器完成自动演出、实时触控演奏、校准和挑战演出。

- 仓库：`https://github.com/coatcn1/MaaBanGDream`
- 当前版本：`v1.2.3`
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
feature/cooperative-safety                ← 当前统一开发分支（已合并抽卡、登录下载与协力改动）
```

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

- **分支命名**：`feature/*` 做功能，`fix/*` 做修复
- **合并方式**：Squash Merge 到 `main`
- **PR 流程**：Draft PR → 检查通过 → Ready → Squash Merge
- **禁止提交**：ADB 路径、设备序列号、日志、截图、Profile、`.venv`、MFA 运行目录
- **发布门禁**：`verify.ps1` 全部通过 + 工作树干净 + 真机门禁满足
- **歌曲模式**：仅支持当前曲目和随机选曲，不支持按名称指定
- **不在迁移范围**：旧 BDAS 的 Electron、PyWebIO、自建调度器

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

1. **Pipeline override 坐标**：Custom Action 必须使用 MaaFramework 解析后的 `argv.box`。重新读取源 JSON 的 `target` 会丢弃用户选择的难度覆盖，例如 Expert 被点击成 Easy。
2. **状态节点不能滥用 `DirectHit`**：带模板、ROI 或阈值的状态判断必须使用实际识别算法。`DirectHit` 会无条件命中，例如把“还剩 10 次”误报成自动演出次数耗尽。
3. **主页模板阈值需要真图校验**：当前主页样本得分约 `0.837`，阈值 `0.88` 会漏识别并继续按 ESC，最终弹出关闭游戏确认。所有主页 marker 当前统一为 `0.82`，调整时必须同时更新所有 Pipeline 和契约测试。
4. **停止不是业务失败**：Custom Action 观察到 `context.tasker.stopping` 时应立即停止输入并返回中性成功；不要继续截图、点击、嵌套任务或记录业务失败原因。
5. **正式演奏时限**：旧的 300 秒上限会在长曲仍演奏时强制失败。正式演奏节点当前为 600 秒，并应在超时、生命保护、用户停止、结算识别等终态记录具体原因。
6. **禁止含糊日志**：不要写“详情见上一条日志”。终态日志必须包含当前阶段和可执行的具体原因；运行时原因通过 `TaskOutcome` 的 latest failure reason 传递。
7. **启动恢复必须有界**：未知界面最多按 ESC 恢复 60 秒；仍无法识别主页才重启游戏。登录画面应先识别“点击任意处/开始”，登录阶段不得过早发送 ESC。
8. **部署必须从包含所有未合并功能的分支进行**：`launch-mfa.ps1` 用当前工作树的 `interface.json` 和 Agent 覆盖运行目录。多个未合并 feature 分支并存时，从缺少某功能的旧分支部署，会把该功能从 MFA 里“部署丢”。当前统一工作分支是 `feature/cooperative-safety`。
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
