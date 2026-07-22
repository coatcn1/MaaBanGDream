# MaaBanGDream

基于 MaaFramework 的《BanG Dream! 少女乐团派对！》自动化项目。

- 当前开发版本：`0.4.0`
- 最新已发布版本：`v0.3.0` 开发预览
- 项目仓库：https://github.com/coatcn1/MaaBanGDream

## 当前能力

- Project Interface V2，可由 MFAAvalonia 加载。
- MaaFramework Core 与 Python Agent 固定为 5.10.2。
- 页面连通性测试：`主页 → 演出 → 自由演出 → 选曲页 → 主页`。
- 单轮自动演出：使用游戏当前选中的歌曲，确认自动演出已开启后开始，完成结算并返回主页。
- 开发中的多轮自动演出支持当前曲目或每轮随机选曲，并可选择 Easy、Normal、Hard、Expert、Special 难度。
- 自动演出次数耗尽时停止任务，不点击开关或开始按钮。
- `CommonRecover` 可优先点击安全节点，随后按 1.5 秒间隔发送 BACK；超时后最多重启游戏两次。
- 登录提示、通用关闭按钮、结算、奖励和剧情跳过处理。
- Pipeline 契约、PNG 完整性与来源哈希、恢复边界和运行时版本组合检查。
- 实时演奏 Profile v1 基础：严格绑定分辨率、DPI、游戏帧率、演出画质和音符流速；任何字段变化都会使 Profile 失效。
- Profile 默认保存在已忽略的 `profiles/`，只接受目录内相对 JSON 文件；未经用户真机验收的 Profile 不允许驱动实时演奏。
- MFAAvalonia 已提供“实时取帧检查（零触控）”：连续截图 5 秒，输出有效帧数、实际帧率、最大取帧耗时、超时帧和无效帧，不执行任何输入操作。
- “实时音符观察（零触控）”可在用户手动进入排练后观察 10 秒，以最多 60 FPS 统计 Tap、Skill、Hold、Flick 和七轨分布；仍不执行触控。

不计划提供按名称指定歌曲；歌曲模式固定为当前曲目和随机选曲。当前不支持 Profile、挑战演出、每日调度或实时演奏。旧项目的 Electron、PyWebIO 和自建调度器不在迁移范围内。

## 环境要求

- Windows 10/11
- Python 3.12
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

完整运行时检查需要提供本机 MFAAvalonia 目录：

```powershell
.\.venv\Scripts\python.exe scripts\check_runtime.py --mfa-root <MFAAvalonia目录>
```

把 `interface.json` 和 `resource` 部署到 MFAAvalonia 项目目录后即可启动。发布前还必须完成连接、截图、点击、BACK、应用启停、相关页面闭环及停止任务安全性真机验收。本机 ADB 路径、设备序列号、日志、截图、Profile、虚拟环境和 MFAAvalonia 运行目录均不得提交。

## 项目进度

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| 独立 MaaFramework 项目 | 已完成 | 与旧 BDAS 工作树分离，不带入旧仓库脏改动 |
| 基础运行环境 | 已完成 | Python 3.12、MaaFw 5.10.2、MFAAvalonia 2.12.0、.NET 10 |
| 运行时兼容门禁 | 已完成 | 锁定 Python、Core、MFA、.NET Binding 与 PI 组合 |
| 最小页面闭环 | 已完成 | 已在真实雷电模拟器验收 |
| 故障恢复 | 已完成 | 已覆盖安全节点、BACK、停止检测及重启上限 |
| GitHub 首次预发布 | 已完成 | 公开发布 `v0.1.0` prerelease |
| 单轮自动演出 | 已完成 | 当前歌曲、单轮、结算后返回主页；已通过关闭与开启状态真机验收 |
| 多轮、随机曲目与难度 | 已完成 | 1–99 轮，当前曲目/随机选曲，五档难度；已由用户真机验收 |
| 实时演奏 | 进行中 | 已开始独立 Python Agent 基础；当前不发送触控 |
| Profile 系统 | 进行中 | Profile v1 草稿、原子验收、环境签名和最新已验收 Profile 解析已接入；Easy Profile 已由用户真机验收并在本机激活 |
| 挑战演出 | 未开始 | 在自动演出基础上实现 |
| 每日调度 | 未开始 | 最后接入，不迁移旧调度器 |

## 进度与变更记录

### 2026-07-22

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

1. 实时演奏 Agent：只观察模式
   - 接入 MaaController 截图形成最新帧缓冲，记录帧率、延迟和丢帧；不发送任何触控。
   - 停止任务或 Agent 断开后必须立即退出，并验证没有残留进程。
2. Profile 校准接入
   - 已完成 Easy 草稿生成、用户验收激活和 Profile 驱动排练入口；下一步用完整歌曲结算的 FAST/SLOW 反馈微调时序偏移。
3. 挑战演出
   - 复用自动演出能力，补充挑战专属页面和恢复流程。
4. 每日调度
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
