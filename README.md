# MaaBanGDream

基于 MaaFramework 的《BanG Dream! 少女乐团派对！》自动化项目。

- 当前开发版本：`0.5.0`
- 最新已发布版本：`v0.3.0` 开发预览
- 项目仓库：https://github.com/coatcn1/MaaBanGDream

## 当前能力

- Project Interface V2，可由 MFAAvalonia 加载。
- MaaFramework Core 与 Python Agent 固定为 5.10.2。
- MFA 首页只提供四个用户任务：自动演出、实时演奏、实时演奏校准、挑战演出；观察和诊断入口只保留在开发 Pipeline 中。
- 自动演出支持当前曲目/每轮随机选曲、五档难度和 1–99 轮。
- 实时演奏支持机器人排练/正式演奏、当前曲目/每轮随机选曲、五档难度、1–99 轮及可选整曲调试记录。
- 正式演奏和挑战演出必须加载与当前环境和难度完全匹配的已验收 Profile；缺失时在触控前停止。
- 实时演奏校准自动完成三首不同随机曲排练，按每轮 FAST/SLOW 调整延迟，再用第四首不同歌曲正式验证；通过后自动生成并启用 Profile。
- 挑战演出支持五档难度、200/400/800/1600 点和 1–99 轮，使用机器人正式演奏引擎。
- 自动演出次数耗尽时停止任务，不点击开关或开始按钮。
- `CommonRecover` 可优先点击安全节点，随后按 1.5 秒间隔发送 BACK；超时后最多重启游戏两次。
- 登录提示、通用关闭按钮、结算、奖励和剧情跳过处理。
- Pipeline 契约、PNG 完整性与来源哈希、恢复边界和运行时版本组合检查。
- 实时演奏 Profile v1 基础：严格绑定分辨率、DPI、游戏帧率、演出画质和音符流速；任何字段变化都会使 Profile 失效。
- Profile 默认保存在已忽略的 `profiles/`，只接受目录内相对 JSON 文件；未经用户真机验收的 Profile 不允许驱动实时演奏。
- MFAAvalonia 已提供“实时取帧检查（零触控）”：连续截图 5 秒，输出有效帧数、实际帧率、最大取帧耗时、超时帧和无效帧，不执行任何输入操作。
- “实时音符观察（零触控）”可在用户手动进入排练后观察 10 秒，以最多 60 FPS 统计 Tap、Skill、Hold、Flick 和七轨分布；仍不执行触控。

开启实时演奏的“调试记录”后，每轮会在已忽略的 `debug/recordings/realtime-<时间>/` 保存：

- `trace.jsonl`：每个分析帧的音符、Track ID、轨道、几何、生命状态和实际触控动作/原因；这是排查漏键、重复点击和错误滑动的主要证据。
- `playfield.avi`：完整判定区域回放；即使视频编码跟不上，JSONL 动作时间线也不会丢失。
- `events.jsonl` 与 `events/*.png`：自动保存长条保险松手、松手后再次补救触控等异常事件的对应画面。
- `summary.json`：记录帧数、视频丢帧数和异常截图数。结算识别图及数字 JSON 另存于已忽略的 `screencap/`。

不计划提供按名称指定歌曲；歌曲模式固定为当前曲目和随机选曲。当前仍不支持每日调度。旧项目的 Electron、PyWebIO 和自建调度器不在迁移范围内。

## 环境要求

- Windows 10/11
- Miniconda 26.5.3-1，独立环境 Python 3.12
- MaaFramework Native Core / Python binding 5.10.2（PyPI 包名 `MaaFw`）
- MFAAvalonia 2.12.0、.NET Binding 5.8.0
- .NET Desktop Runtime 10
- Android 设备分辨率 `1280×720`、DPI `240`

已验证的精确组合记录在 `runtime-compatibility.json`。任一组件版本变化都必须重新执行自动检查和真机验收，不能只依据主版本相同推断兼容。

## 安装与验证

```powershell
.\scripts\setup.ps1
.\scripts\verify.ps1
```

### Conda 环境固定配置

本项目不再使用仓库内 `.venv`。Windows venv 的启动器仍依赖创建它的用户级 Python；该 Python 被系统修复、卸载或因运行账户不同而不可访问时，`.venv` 目录虽然存在，实际仍无法启动。Conda 环境包含独立解释器，因此 MFA 不再依赖 `C:\Users\<用户名>\AppData` 下的 Python。

| 配置 | 固定值 |
| --- | --- |
| 发行版 | Miniconda `26.5.3-1`（Python 3.12 Windows x64 安装包） |
| 安装目录 | `D:\Documents\workplace\.tools\Miniconda3` |
| 环境名 | `maabangdream` |
| 环境目录 | `D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream` |
| Python 约束 | `3.12`（当前解析为 `3.12.13`） |
| Conda 软件源 | `conda-forge`，`nodefaults` 禁用默认源 |
| Python 依赖 | `requirements.txt`，其中 MaaFw 固定为 `5.10.2` |
| 环境声明 | `environment.yml` |

Miniconda 安装包固定为 `Miniconda3-py312_26.5.3-1-Windows-x86_64.exe`，官方 SHA-256：

```text
60ab6c430d19ca822841ecfc101d465f3c826ee2d2a3d6c028ffab0f3bcde57a
```

环境不存在或需要修复时，只运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

该脚本会按 `environment.yml` 中相同的约束创建或更新 `maabangdream`，并显式使用 `--override-channels --channel conda-forge`，避免触发 Anaconda 默认源的额外条款确认；随后从 `requirements.txt` 安装 Python 包。无需执行 `conda init`，也不需要把 Conda 加入系统 PATH。手工检查可使用绝对路径：

```powershell
D:\Documents\workplace\.tools\Miniconda3\Scripts\conda.exe env list
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe --version
```

完整运行时检查需要提供本机 MFAAvalonia 目录：

```powershell
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe scripts\check_runtime.py --mfa-root <MFAAvalonia目录>
```

把 `interface.json` 和 `resource` 部署到 MFAAvalonia 项目目录后即可启动。发布前还必须完成连接、截图、点击、BACK、应用启停、相关页面闭环及停止任务安全性真机验收。本机 ADB 路径、设备序列号、日志、截图、Profile、虚拟环境和 MFAAvalonia 运行目录均不得提交。

## 正确启动 MFAAvalonia

本机存在两个不同目录，不能混用：

- `D:\Documents\workplace\MaaBanGDream` 是 Git 仓库和唯一源码目录。
- `D:\Documents\workplace\.tools\MFAAvalonia` 是被 Git 忽略的 MFAAvalonia 运行目录，其中的 `interface.json` 和 `resource/resource` 只是部署副本。

MFAAvalonia 不会直接读取仓库中的新版资源。修改代码后如果只重启 EXE，可能继续显示旧任务，或进入资源下载引导页。标准启动方式是从仓库执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch-mfa.ps1
```

脚本会一次完成以下操作：

1. 检查 MFAAvalonia、`maabangdream` Conda 环境、Agent 和源码资源是否存在。
   同时检查 `Microsoft.NETCore.App 10.x`；缺失时会停止并给出安装命令，不再打开误导性的下载页。
2. 将仓库 `resource` 同步到 MFA 的 `resource/resource` 部署目录。
3. 从仓库生成部署用 `interface.json`，将资源路径改为 `./resource/resource`，并将 Agent 解释器指向 `maabangdream` Conda 环境、脚本指向仓库中的 `agent/server.py`。
4. 关闭已有 MFAAvalonia 进程，以 MFA 安装目录作为工作目录重新启动。

如果 MFA 显示“下载资源”而不是 MaaBanGDream 任务列表，不要点击下载；这表示启动了未同步的部署副本。关闭该页面并重新运行上述脚本。正常界面必须只显示：自动演出、实时演奏、实时演奏校准、挑战演出。

如果启动后打开的是微软 `.NET` 下载网页并且 MFA 进程立即退出，则是缺少 MFAAvalonia 2.12.0 要求的 .NET 10 Runtime。执行：

```powershell
winget install --id Microsoft.DotNet.Runtime.10 --exact --accept-package-agreements --accept-source-agreements
```

安装完成后重新运行 `launch-mfa.ps1`。可用 `dotnet --list-runtimes` 确认存在 `Microsoft.NETCore.App 10.x`。

如 MFAAvalonia 安装在其他位置，可显式传入：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch-mfa.ps1 -MfaRoot <MFAAvalonia目录>
```

## 项目进度

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| 独立 MaaFramework 项目 | 已完成 | 与旧 BDAS 工作树分离，不带入旧仓库脏改动 |
| 基础运行环境 | 已完成 | Miniconda 26.5.3-1 / `maabangdream` / Python 3.12、MaaFw 5.10.2、MFAAvalonia 2.12.0、.NET 10 |
| 运行时兼容门禁 | 已完成 | 锁定 Python、Core、MFA、.NET Binding 与 PI 组合 |
| 最小页面闭环 | 已完成 | 已在真实雷电模拟器验收 |
| 故障恢复 | 已完成 | 已覆盖安全节点、BACK、停止检测及重启上限 |
| GitHub 首次预发布 | 已完成 | 公开发布 `v0.1.0` prerelease |
| 单轮自动演出 | 已完成 | 当前歌曲、单轮、结算后返回主页；已通过关闭与开启状态真机验收 |
| 多轮、随机曲目与难度 | 已完成 | 1–99 轮，当前曲目/随机选曲，五档难度；已由用户真机验收 |
| 实时演奏 | 部分验收 | 正式连续演奏与结算恢复已通过；松手后原位补点修复待复验 |
| 智能校准与 Profile | 修复待验收 | 首轮报告筛选和单轮返回已修复；待确认三排练一正式完整闭环 |
| 完整歌曲与结算反馈 | 已完成 | 已实现最长 300 秒门禁、触点清理、逐次 ESC 结算识别及 FAST/SLOW 解析 |
| 挑战演出 | 已完成 | 当前活动曲、点数、连续轮次、Profile 与结算恢复已由用户真机验收 |
| 每日调度 | 未开始 | 最后接入，不迁移旧调度器 |

## 进度与变更记录

### 2026-07-22

- 将运行环境从依赖用户级 Python 的仓库 `.venv` 迁移到工作区独立 Miniconda：固定 Miniconda 26.5.3-1、环境名 `maabangdream`、Python 3.12 和 `conda-forge/nodefaults`。`setup.ps1`、`verify.ps1`、`launch-mfa.ps1` 与 MFA 部署配置全部改用该环境；README 记录完整路径、版本、哈希、重建和检查命令。
- 验证脚本将 pytest 临时目录固定到仓库已忽略的 `.local/pytest-<进程号>` 并关闭跨账户缓存，避免 MFA、用户终端和 Codex 使用不同 Windows 账户时再次访问 AppData 或旧 `.pytest_cache` 失败。
- 根据 19:47 校准日志再次修复“首轮结束停在主页”：结算已于 19:49:44 保存，旧逻辑却因文件时间筛选将其排除并在 19:50:57 异常退出。现改为“单轮运行前报告目录快照 → 运行后新增/变化报告”，不再依赖进程时间与文件系统时间一致；校准专用覆盖还会在识别主页后直接结束嵌套单轮，不再回到通用轮次门控等待。
- 使用用户开启调试记录后生成的三份整曲 `trace.jsonl` 定位正式演奏原位补点：长条 `UP` 后约 0.28–0.31 秒，尾环残影被新建为普通音符并触发同轨 `rescue`。新增松手后 0.4 秒残影抑制，但保留具有完整下落轨迹的真实 `crossing`；三份记录离线重放分别由 9/6/9 次降至 0，邻轨重复和长条尾补点继续保持 0。
- 调试记录新增 `events.jsonl` 和异常 PNG：自动保存长条保险松手及松手后再次补救触控的帧；离线重放报告新增 `recorded/replayed_post_release_rescues`，以后可以直接量化同类回归。
- 用户已确认正式连续演奏能够正确进入下一轮并结束，挑战演出也已正常完成；这两条流程的主页恢复问题关闭。当前只剩智能校准完整四轮和原位补点修复需要真机复验。
- 根据首次 v0.5.0 集中验收修复三项回归：长绿条不再被固定 6 秒保险提前释放（上限改为 20 秒），普通绿条的预测释放线由 y=570 前移到 y=555；正式/排练结算改为最多 30 次、每 1.5 秒一次 ESC，并在每次输入前重新解析画面。
- 校准首轮结束停在主页的首次根因不是导航，而是嵌套 Maa Pipeline 覆盖没有把临时 `calibration_report` 参数传给播放节点；校准改为直接读取播放 Action 已原子保存的本轮结算 JSON。随后发现文件时间筛选仍会误排除有效报告，已由上面的目录快照方案替换。
- 正式演奏次数为 2 却首轮结束的根因是原实现仅给结算解析 12 秒，正式演出动画尚未出现数字面板便失败；现恢复旧 BDAS 的逐次 ESC 结算策略和 45 秒窗口，成功解析后再交给主页恢复并进入下一轮。
- 挑战演出已完成真机验收，确认共用的正式实时引擎、结算恢复和连续轮次流程正常。
- 修复 MFA 启动流程：明确区分 Git 源码与 MFA 部署副本，新增 `scripts/launch-mfa.ps1` 自动同步 Interface/资源、写入部署路径并重启 MFA；README 增加下载引导页故障处理，禁止再手工启动未同步副本。
- 建立本地 `feature/formal-calibration-challenge`，将已通过验收的连续机器人排练快进合入本地 `main`；未推送 GitHub。
- MFA 首页收敛为四项任务，隐藏开发/观察入口但保留底层 Pipeline 和测试。
- 实时演奏增加正式模式：开始前严格匹配 Profile，自动切换正式并关闭自动演出、3D Cut-in 和 3D/MV 显示。
- 新增“三排练一正式”智能校准：每轮读取 FAST/SLOW，单轮最多调整 ±5 ms、总范围 ±250 ms；通过第四首不同歌曲正式验证后原子写入并启用 Profile。
- 新增挑战演出：支持四档点数、五档难度、连续次数，并复用正式实时引擎、Profile 门禁和结算恢复。
- 正式演奏与挑战演出只读取 Profile，不会在演奏中或结算后静默修改延迟。

- 开始本地分支 `feature/realtime-multi-live`：新增“连续机器人排练”，复用稳定的主页恢复、自由演出导航、当前/随机曲目、五档难度和 1–99 次轮次门控。
- 根据用户纠正，连续任务只进入游戏排练模式、强制关闭游戏演示，不再执行 Profile 拒绝门禁；正式演奏暂不暴露给用户。
- 修复黄键作为绿色长条起始头时被拆成 `SKILL TAP + HOLD DOWN` 的错误：检测器现将同轨、同一长条下端的黄头合并为一个长按，同时保留独立黄色短键。
- 新增可选整曲调试录制：每个处理帧写入音符、轨道、几何、生命状态和触控动作 JSONL，并异步保存回放 AVI 与编码丢帧统计；输出只保存在已忽略的 `debug/recordings/`，用于后续训练和离线回归。
- 修复排练点击开始后立即结束：Maa Agent 跨进程 Controller 代理被重复获取，Profile 解析使用第二个代理后引擎仍持有已失效的第一个代理；现整个 Action 只获取并复用一次 Controller。调试开关也改为覆盖完整参数，避免 MFA 的替换式嵌套覆盖意外恢复 Profile 门禁。
- 从 MFA 任务列表隐藏已被连续排练取代的“完整歌曲排练与结算采集”，防止旧勾选状态使两个实时任务排队运行；开发 Pipeline 和契约仍保留。
- 根据连续排练的整曲调试轨迹修复重复触控：黄键多点并非缺少数据，而是短暂丢失后新 Track 再次触发补救；现对同一处理帧启用同轨/邻轨共享判定，跨帧 120ms 仅抑制追踪重建产生的 `rescue`，不会吞掉具有正常运动过线轨迹的密集音符；Flick 优先覆盖相邻 Tap，并接通此前未使用的 `_last_trigger` 去重状态。
- 删除长条 `UP` 时主动补点附近音符的 `linked-tail` 行为；长条尾部现在只松开，不再次点击，并在松开后设置 250ms 同轨重启冷却，避免残留绿条产生 `DOWN → UP` 抖动。对用户验收录制离线回放后，总动作由 411 降至 312，短窗同/邻轨重复判定由 45 降至 0，长条尾补点由 33 降至 0。
- 新增 `scripts/replay_realtime_trace.py`，可直接用调试模式生成的 `trace.jsonl` 离线重放规划器，后续识别训练和触控回归无需反复消耗真机演出。
- 实时 Agent 每轮完成演奏、逐次 ESC 识别结算并记录 FAST/SLOW，Pipeline 再安全返回主页进入下一轮；用户停止、生命归零、解析失败或环境漂移均不继续下一轮。
- 根据用户已明确通过的 Hard 粉色 Flick 真机验收，生成本机 Hard Profile；这些 Profile 仍保留给独立校准/正式模式研究，不再阻塞机器人排练。
- 开始本地分支 `feature/realtime-result-calibration`：新增完整歌曲排练入口，使用已验收 Easy Profile 最长运行 300 秒。
- 新增演出结束状态机：必须先确认有效生命条存活，再连续 120 帧（约 2 秒）不可见才判定离开演出；开场、普通页面和短暂遮挡不会误结束。
- 完整歌曲结束、停止、生命归零或异常时仍统一释放触点；正常结束后将结算画面保存到已忽略的 `screencap/`，供后续 FAST/SLOW 解析建立真实回放测试。
- 用户完成首次完整 Easy 歌曲：7561 帧、389 次动作、正常识别演出结束。根据实际页面流程，结束后改为逐次发送 ESC、每次重新截图解析，识别到结算即停止继续返回。
- 已迁移固定 1280×720 结算数字解析器并用模拟器当前画面验证：PERFECT 170、GREAT 42、GOOD 0、BAD 0、MISS 3、FAST 33、SLOW 9，置信度 0.80；当前只建议将时序偏移从 0 调至 -5 ms，不自动修改已验收 Profile。
- 开始本地分支 `feature/realtime-profile-calibration`：将已通过的 Easy 机器人排练转化为本机 Profile 授权，不推送 Profile 文件。
- 修复 Profile 草稿仍调用 MaaFw 5.10.2 Shell/Controller 属性的问题；现与排练一致，从实际截图读取分辨率，DPI 使用锁定的本机参数 240。
- 新增 Profile 原子验收、验收时间审计、难度/环境二次核对和最新已验收 Profile 解析；未验收、难度错误或任一环境字段漂移均拒绝运行。
- 新增“Profile 驱动排练（Easy，30 秒）”入口，触控参数只从最新匹配的已验收 Profile 读取；当前等待一次真机门禁后再进入完整歌曲与结算反馈。
- 修复 Profile 驱动排练约一秒退出：生命保护此前可在尚未确认见过存活生命条时，因普通页面连续三帧零读数而误报死亡；现必须先连续确认存活，且确认前只截图、不发送任何触控。
- Easy Profile 驱动排练已通过用户真机验收；Hard 试跑发现粉色 Flick 被游戏判成普通点击，根因是原生 Down/Move/Up 在同一输入采样窗口内瞬时完成。Flick 现改为 36 ms 三段同步上滑，双 Flick 仍保持同相位，且不释放并行长按触点；修复后已通过用户 Hard 真机验收。
- 开始本地分支 `feature/realtime-note-tracking`：迁移同轨多音符 Track ID、唯一帧速度回归和短时轨迹记忆；观察日志增加 Tap/Skill/Flick 唯一轨迹计数，仍不触控。
- 连续迁移触控规划与 MaaTouch 协议核心：覆盖和弦去重、Flick、双长条、尾端释放、预测释放、六秒强制释放、连接音符同批提交以及停止时全部触点复位；当前仍未连接真实 MaaTouch 服务。
- MaaFramework 5.10.2 已原生提供多点 `TouchDown/TouchMove/TouchUp`，新增 Controller 调度适配层，停止、异常和关闭时都会尝试释放所有活动触点；优先采用框架原生输入，不依赖本机 ADB 路径。
- 新增尚未暴露到 MFA 任务列表的实时引擎核心，串联取帧、检测、规划和原生触控；正常结束、用户停止和内部异常均通过 `finally` 执行规划器复位与触点关闭。
- 新增 Profile 草稿任务：通过 MaaController 自动读取真实分辨率和 Android DPI，结合游戏帧率、画质、音符速度与难度生成本机 JSON；草稿强制为 `accepted=false`，不能直接授权正式演奏。
- 迁移生命条保护为同步 BGR 检测与三帧确认状态机：生命条不可见只记为未知，连续三帧接近零才判定死亡，避免加载页或结算页误判。
- 新增首个单一验收入口“机器人排练（Easy，30 秒）”：严格限制 1280×720、DPI 240、60 FPS、standard 画质和音符速度 2.0，环境漂移直接拒绝；正常、停止、生命归零或异常退出均释放全部触点。
- 修复机器人排练一秒结束：MaaFw Python Binding 5.10.2 漏声明 `MaaControllerPostShell` 的 ctypes 参数，64 位 Controller 句柄被截断；项目兼容层现显式绑定完整句柄、命令和超时类型。
- 自定义排练/Profile 回调现捕获并记录异常后返回失败，避免 ctypes 吞掉 Python 异常并让 MFA 将失败任务误报为成功。
- 排练路径不再调用 Agent 转发 Controller 的 Shell API；DPI 使用已锁定的 MFA 本机配置参数 240，分辨率由首张实际截图确认，避免 Shell/Controller 属性兼容缺陷阻塞实时引擎。
- 首次 30 秒机器人排练完成 1801 帧和 49 次动作；针对一枚短暂可见的黄色 Skill 音符漏击，启用“已识别音符首次近线补救”，仍保留颜色、几何、轨道校验和追踪去重；修复后已通过用户真机验收。
- 旧项目没有持续使用 Git；后续将其整体作为参考源码审查，不再以旧工作树的 tracked/untracked 状态判断可迁移性，但继续排除日志、截图、本机配置和缓存。
- 开始本地分支 `feature/realtime-note-observe`：只迁移音符视觉检测，不迁移未跟踪的旧多音符跟踪器，也不发送触控。
- 从旧仓库干净 `HEAD` 导入七轨 Tap、Skill、Hold、Flick 检测与离线几何测试，并记录 GPL-3.0-only 来源映射；运行依赖使用无 GUI 的 OpenCV。
- 新增 MaaController 实时音符观察入口，按 60 FPS 上限处理 BGR 截图并输出类型/轨道统计；任务不调用任何控制器输入 API。
- 用户分别在 Expert、Hard、Easy 完成三次观察，类型数量随难度递减且七轨总体均有覆盖；首次实测处理率约 32 FPS，定位并修复节流时间轴累计截图耗时后，复验达到 601 帧/10 秒（约 60.1 FPS），无停止或任务异常。
- 开始 `feature/realtime-profile-foundation`：重新实现实时演奏 Profile v1，不复制旧仓库中存在未提交修改的 Profile 代码与测试。
- 新增强类型环境签名，固定检查分辨率、DPI、游戏帧率、演出画质与音符流速；五项中任意一项变化都会拒绝使用旧 Profile。
- 新增 Profile 路径隔离、原子写入、版本检查、参数边界及“必须经用户真机验收”门禁；提供不可直接用于正式演奏的示例文件。
- 修复本机缺失的 Python 3.12.10 安装并重建项目 `.venv`；排查到一次停止任务后的 Agent 残留，后续实时任务必须增加进程与触点清理回归。
- 新增 `RealtimeObserve` Custom Action 与 MFAAvalonia 任务入口，使用 MaaController 做 5 秒只观察取帧并统计延迟/超时；单元与 Pipeline 契约保证该任务没有点击、滑动或应用控制节点。
- 用户完成两次零触控取帧验收：分别为 158.80 FPS / 156.80 FPS，最大取帧耗时均为 16 ms，超时帧和无效帧均为 0。
- 清理 MaaFramework 5.10.2 中已弃用的 `Toolkit.init_option` 调用，改用 `Tasker.set_log_dir`；MFA 打开期间 Agent 保持活动连接，关闭 MFA 后才应退出。
- 按用户决策取消指定歌曲功能，仅保留当前曲目和每轮随机选曲两种模式。
- 开始 `feature/multi-live-difficulty`：新增 1–99 轮次数输入、五档难度选择和原生 `max_hit` 轮次门控。
- 多轮结算后先安全回主页再进入下一轮；达到目标次数后正常完成，自动演出次数耗尽仍立即停止。
- 修复多轮第二次开始可能随机点击到按钮外的问题；开始节点改为只点击已识别按钮框内部。
- 新增启动回主页门禁：任务可从大多数普通页面启动，未识别主页时每 1.5 秒发送 ESC，并在登录、关闭或剧情安全节点出现时优先点击；超时后最多重启两次。
- 用户已完成 v0.3.0 真机验收，确认非主页启动恢复、Expert 难度和连续两轮流程通过。
- 在 `feature/auto-live-foundation` 分支新增当前歌曲单轮自动演出 Pipeline。
- 从旧仓库 `HEAD` 导入准备、自动演出状态、开始、结算、奖励和剧情模板，并记录 SHA-256 来源清单；未复制旧仓库工作区修改。
- 开始按钮只能从“自动演出已开启”节点到达；耗尽状态优先并直接停止；关闭状态最多尝试三次。
- 演出结果等待上限设为 300 秒，正常演出等待不会触发 60 秒未知页恢复。
- 扩展 `CommonRecover`，支持安全节点点击、逐次新截图和每项控制操作前的停止检测。
- 新增自动演出 Pipeline 契约、模板来源哈希和停止安全性测试；当前共 15 项自动测试。
- 修正选曲确认点击区域，并在真实雷电模拟器完成两轮验收：自动演出关闭时先开启再开始，已开启时直接开始；两轮均识别结算并返回主页。
- 验收日志确认演出等待期间没有 BACK，BACK 仅在结算恢复阶段发出；停止失败验收后没有继续控制游戏。
- 次数耗尽状态未在真机构造，本版本仅通过来源模板回放约束和 Pipeline 契约验证，发布说明保留此限制。
- 新增 v0.2.0 发布说明。
- 新增精确运行时组合检查并接入 `scripts/verify.ps1`。

### 2026-07-21

- 从旧项目迁出独立 MaaFramework 工程并建立 `main` 分支。
- 安装并验证 Python 3.12、MaaFw 5.10.2、MFAAvalonia 2.12.0 和 .NET 10。
- 完成 Project Interface V2、Python Agent、Pipeline、最小页面闭环和基础 `CommonRecover`。
- 创建公开 GitHub 仓库并发布 `v0.1.0` 开发预览。
- 项目由 `BDAS-Maa` 统一更名为 `MaaBanGDream`，通过 PR #1 合并。
- 将 README 确立为项目进度、修改和后续计划的统一记录入口。

## 后续计划

1. 最后两项真机复验
   - 运行一次智能校准，确认首轮回主页后立即进入第二、第三轮，并完成第四首正式验证与 Profile 写入。
   - 开启调试记录运行一轮实时演奏，确认长条松手后不再在原位置补点；助手直接读取 trace、异常截图和结算报告。
   - 正式连续演奏和挑战演出已通过，不重复消耗资源；中途停止安全检查仍保留为最终发布门禁。
2. v0.5.0 发布准备
   - 真机通过后复核本地提交、敏感文件和运行时包，再决定推送 PR 与 prerelease。
3. 每日调度
   - 使用 MaaFramework 任务组合重新实现，不迁移旧 Electron/PyWebIO 调度器。

以上各项分别使用独立 `feature/*` 分支和 Pull Request，不直接堆入 `main`。

## README 更新规则

以后每次代码修改都必须同步维护本文件：

1. 开始新里程碑时更新“项目进度”和“后续计划”。
2. 功能、修复、依赖或运行方式变化时更新对应章节。
3. 提交前在“进度与变更记录”顶部追加实际完成内容。
4. 只把已完成且验证过的结果标记为完成，未完成内容保留在计划中。
5. README 未同步、测试未通过或真机门禁未满足时，不提交、不合并、不发布。

## Git 工作流

- 功能使用 `feature/*` 分支，修复使用 `fix/*` 分支。
- 分支推送后先创建 Draft PR；检查通过后转为 Ready，再 Squash Merge 到 `main`。
- 提交前执行：

```powershell
git status --short
git diff --check
.\scripts\verify.ps1
```

- 发布前核对 `git diff main...HEAD`、提交历史、远端文件清单和敏感信息扫描。
- 只有验证通过且工作树干净时，才允许合并到 `main`、创建 annotated tag 和 prerelease。

详见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [发布检查清单](docs/release-checklist.md)。

## 许可证

本项目采用 GPL-3.0-only。
