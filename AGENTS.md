# MaaBanGDream Agent 指南

本文件只记录会长期影响开发、诊断、部署和验收的规则。历史故障经过、已经完成的修改、
一次性测试数字、提交号和发布结果写入 `CHANGELOG.md`、交接文档或调试证据，不再追加到
本文件。实现细节以源码和测试为准；用户说明以 `README.md` 和 `docs/` 为准。

## 项目与目录边界

MaaBanGDream 基于 MaaFramework，通过定制 MFAAvalonia 加载 Python Agent，控制 Android
设备完成自动演出、实时触控、校准、协力、挑战和组曲任务。

| 路径 | 用途 | 写入规则 |
| --- | --- | --- |
| `D:\Documents\workplace\MaaBanGDream` | 唯一源码仓库 | 所有项目修改在这里完成 |
| `D:\Documents\workplace\.tools\MFAAvalonia-profile-v3` | 开发 MFA 运行目录 | 只由部署脚本同步，不提交 |
| `D:\Documents\workplace\MFAAvalonia` | 定制 MFAAvalonia 源码仓库 | 独立分支、提交和验证 |
| 用户正式安装目录 | 已发布客户端 | 默认只读；未经明确授权不得部署候选或修改配置 |

MFA 不直接读取源码目录。修改 `agent/`、`resource/` 或 `interface.json` 后，必须通过
`scripts\launch-mfa.ps1` 同步并重启开发 MFA。

## 固定运行环境

- Python：`D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe`
- Python 版本：3.12；MaaFw：5.10.2；MFAAvalonia：2.12.0；.NET Runtime：10
- Conda 仅使用 `conda-forge`，配置以 `runtime-compatibility.json` 为准。
- 不使用仓库 `.venv`，不临时更换 Python、MaaFw 或 MFA 版本规避问题。
- pytest 临时目录使用 Git 忽略的 `.local/pytest-<进程号>`，不写系统 AppData。

## 修改与 Git 规则

1. 修改前检查 `git status --short --branch`、当前分支、远端和现有差异；用户已有修改不得
   覆盖、回退或混入无关重构。
2. 功能和修复从 `main` 建立 `feature/*` 或 `fix/*` 分支，不直接在 `main` 开发。
3. 提交保持单一目的，使用仓库既有提交格式；通过 PR 以 squash 合并到 `main`，遵守
   GitHub 的线性历史规则，不创建 merge commit，不强制推送或改写共享历史。
4. 代码注释使用简体中文，重点说明设计原因、边界和失败策略，不逐行翻译代码。
5. 禁止提交日志、截图、Profile、设备序列号、账号凭据、本机绝对路径、缓存、运行目录和
   构建产物。
6. 行为或用户可见修改同步 `CHANGELOG.md`；只有长期规则变化才修改本文件。不要把调查日志
   或已完成验收继续堆入 `AGENTS.md`。
7. 提交、推送、PR、合并和 Release 是独立动作。未经用户明确要求，不推送、不合并、不发布。

## 图像与证据工具

- 当前模型能直接理解用户附图时直接分析。
- 简单 OCR、文字提取、单模板确认或批量帧初筛可使用：
  `python C:\Users\Lenovo\.codex\skills\vision\vision.py <图片路径> "<问题>"`。
- 复杂页面结构、空间关系、点击坐标、Computer Use 操作，或外部 vision 结果矛盾时，使用
  `view_image` 或 Computer Use 屏幕上下文。
- 多图和视频先裁剪、缩放并限制必要帧；禁止手工读取图片字节或把 base64 塞入对话。
- 页面故障必须把截图、模板得分、坐标、日志和实际行为一起核对，不能只凭单一模板推断。

## Subagent 路由

仅在任务边界清楚时委派；主 Agent 负责需求、证据链、作用域和最终结论。同一批文件只能由
一个写入 Agent 修改。

- `mbd_log_scanner`：只读扫描 MFA 日志、`summary.json`、结果 JSON。
- `mbd_trace_scanner`：只读扫描 checkpoints、events、lifecycle、trace 的有限窗口。
- `mbd_replay_runner`：离线 replay 和基线对比，只能在 `.local/` 生成临时产物。
- `mbd_implementer`：在已有有界证据、最小假设和验收标准后实施代码与测试修改。

Realtime 闭环固定为：证据提取 → 必要的独立审查 → 最小实现 → 定向测试 → 离线 replay →
修改后复核 → 用户真机验收。离线结果不能替代真机验收。

## 任务架构

所有演出入口都先做进程互斥。普通任务由 `CommonRecover` 恢复主页；组曲必须先检查是否存在
可信断点，不能先恢复主页破坏现场。单局实时演奏统一进入
`agent/realtime/profile_play_action.py` 的 `RealtimeProfilePlay`。

| 任务 | 核心边界 |
| --- | --- |
| 单人实时 | 主页检查流速，选曲与准备页确认身份，正式/排练分路 |
| 协力 | 入房、准备送达、成员退出、最终封面和协力结算均有独立门禁 |
| 一键实时 | 被动等待开场身份；只演奏一首，完成后读取结果并恢复主页 |
| 实时校准 | 固定为排练一首 + 正式验证一首；环境签名一致才允许续跑 |
| 挑战 | 选择点数后复用正式演奏门禁 |
| 自动演出 | 不使用实时触控引擎，只配置自动演出并等待结算 |
| 组曲 | 每组三首、按歌曲计数；准备页补全身份，第三首后读取三张 PGGBM |
| 团队演出 Fes | 联机房间制活动，v1 只支持自动匹配（不创建/加入私人房间）；四档难度无 Special；入口/匹配/准备完按 OCR 文本识别，回主页后由 `FesLiveFlow` 重导航；未经真机验收不得发布 |

`DailyFreeGacha` 和 `ManualFlowRecording` 不属于演出流程，按各自 Pipeline 处理。

## 通用生命周期约束

### 停止、失败与重试

- `context.tasker.stopping` 表示用户停止：立即停止输入、释放触点并中性返回；不得继续截图、
  点击、嵌套任务或记录业务失败。
- 真实生命归零、身份冲突、Profile/流速不一致、超时和引擎完整性失败必须保留明确原因；
  恢复到主页不等于任务成功。
- MaaFramework 回调异常可能被绑定吞掉，所有回调必须显式 `try/except` 并返回失败状态。
- 跨回调或跨进程预算只使用 `argv.task_detail.task_id`；RemoteTasker 包装对象和 `_handle`
  都不是稳定任务身份。缺少有效 task ID 时 fail-closed。
- `play_failure_retry_count=1` 表示首次尝试后最多重试一次。新一局入口负责显式重置预算。
- 嵌套 `context.run_task()` 会复用 MaaFramework 的 `max_hit` 计数；嵌套流程使用无
  `max_hit` 的专用 Action。

### 主页流速与 Profile

- `note_speed_settings_enabled=false` 时任何任务都不得打开设置页，直接信任声明流速。
- 开启时只在离开主页前由 `RealtimeGameSpeedSettingsGate` 读取、按需修正并复核；准备页的
  `RealtimePerformanceSettingsGate` 只消费本任务结果，禁止打开设置页或复用跨任务凭据。
- Profile 与分辨率、DPI、帧率、画质和流速精确绑定；`accepted=false` 的草稿不能驱动
  正式演奏。同一 Profile 只对应一种流速。
- Profile 当前选择属于界面正在配置的任务难度槽位；Expert 与 Special 属同一兼容等级，
  但请求难度和本局实际难度必须分别记录。
- Native 和 Legacy 正式局共享 `timing_offset_ms`。两种引擎同时整体变差时，先核对 Profile
  历史值和备份，再调查时钟或调度器。

### 歌曲身份与难度

- 封面 pHash 只能收窄候选，必须结合实际难度等级和实读标题。准备页身份缺失或冲突不提前
  终止任务，应隔离旧谱面并延迟预武装，由本局开场最终封面和实读标题独立兜底；最终仍有
  冲突时不得用旧谱面输入。区服等级例外必须绑定已验证歌曲、难度与谱面 SHA，不能宽泛容错。
- 选曲页只负责选择时，不得用曲库标准标题冒充截图实读证据；身份在相应准备页或最终封面
  门禁中确认。
- 标准难度颜色检测必须先确认选曲页按钮布局；组曲进入每曲选曲页需有稳定页面送达证据，
  未确认时仅有界重试进入，不得发送随机或难度输入。点击回执不能代替页面送达。
- 请求 Special 时，单人、挑战、协力和自动演出只有在 Special 确认不可选时才允许显式
  回退 Expert；实时校准必须精确选择 Special。
- `requested_difficulty` 与 `effective_difficulty` 必须贯穿 Profile、流速、谱面、预武装、
  播放和结果。实际 Special 缺少可信本地谱面或方向语义时，触控前失败。
- Custom Action 参数覆盖是整块替换；新增参数必须同步基础 Pipeline、所有 interface 选项
  和校准 override，并用真实 MaaFramework 覆盖结果验证。

## Realtime 引擎约束

### 开演与时间轴

- 开演顺序必须由本局证据证明：准备页 → 黑场 → 最终封面 → 完整演奏场 → 可选协力等待
  弹窗消失 → 首音。准备页静态生命条或判定线不得触发首拍。
- 首音锚点和 Profile timing offset 在 Native 启动前冻结；本局内不得用 FAST/SLOW 重写
  Native Profile 偏移。设备 jlog 漂移不是游戏 FAST/SLOW 判定相位。
- Native 逐块命令延迟校准只允许修正尚未编译的未来切片。修改残差、等待成本或设备时钟
  校正前，必须对真实 jlog 和 trace 做有符号分段对齐，不能只看绝对百分位。
- MuMu Native 存在逐局变化的客户机时钟速率偏斜，速率校正实验不能稳定闭环；当前分工是
  雷电使用 Native、MuMu 使用 Legacy。

### 输入与触点

- 实时截图、检测、跟踪和派发热路径禁止阻塞等待或同步 ADB 前台查询。
- Native 停止只有在本轮 reset 请求之后的 jlog 明确执行 `r`，并完成本地与设备清理时，
  才能标记 `release_confirmed=true`；历史 `r`、断开 socket 或 kill 进程不能替代回执。
- Legacy HOLD/Slide 只能由连续轨迹和离散拓扑事件建立；长条头 DOWN 后要抑制同轨重复 TAP。
- 普通音符紫色外圈不是 FLICK；只有成组、同向的粉色箭头/折线证据才能升级为 FLICK。
- 密集同轨音符不能用固定宽度合并；阈值要随透视和音符头尺寸缩放。
- Directional `width=1..7` 表示横跨轨道数和判定尺寸；没有谱面方向证据时不得由纯视觉
  FLICK 冒充 Left/Right。

### 文件与运行环境

- 便携包可能位于中文路径。图像读写使用 `agent/realtime/vision_io.py` 的字节级
  `imdecode`/`imencode`；不要在 Agent 中直接使用 `cv2.imread/imwrite`。
- Windows Native 谱面文件读取使用 UTF-16 路径和 `_wfopen`，不得依赖窄字符路径。
- 调试证据写入 `debug/recordings/<run_id>` 和 `screencap/`，不得提交 Git。

## 各模式的关键规则

### 单人、挑战与校准

- 单人和挑战在准备页完成标题、等级、难度、Profile、正式演出选项和 Native 预武装检查。
- 正式准备页先切换 `3D演出/动画MV → OFF`，确认 OFF 后再检测并关闭 3D Cut-in；顺序不能
  颠倒。
- `note_speed_settings_enabled=false` 只跳过设置页，不能跳过 Native 预武装。
- 单人排练从 timing offset 0 开始逐帧自校准，不能拿排练开局的大量 SLOW/GREAT 与正式局
  直接比较。
- 校准会话仅在环境签名一致时续跑；正式验证生命归零直接拒绝，不能生成已接受 Profile。

### 协力

- 点击“准备完毕”后必须确认按钮消失，最多重试三次；随后高频观察成员退出、黑场和本局
  最终封面，不能固定盲等。
- “其他成员正在准备中”弹窗只在开演前处理。弹窗存在时不建立首音颜色基线，消失帧重置
  基线；首音只能由窄列局部变化触发，判定线附近的双 FLICK 不能当弹窗。
- 成员退出弹窗只在房间/准备/黑场前窗口检测；演奏中和结算阶段禁止检测通用“错误”标题。
- 开启协力 jitter 后，预武装副本与 canonical 谱面按歌曲身份比较，不能按路径判不一致。
- 生命归零后的断网跳车只能在支持按游戏 UID 隔离网络的设备上执行；禁止使用会同时切断
  ADB 的 `svc wifi` 或飞行模式。能力缺失时 fail-closed。
- 结算后找不到房间页时先继续安全结算节拍，再 `CommonRecover` 回主页继续下一局；最后一局
  stay 失败可按该局已完成处理。

### 组曲

- 次数按歌曲计数，有限输入必须为 3–999 且为 3 的倍数，0 表示无限循环完整三首。
  跨组与失败重开使用迭代，不累积调用栈。只有完整三首结算后才增加三个完成数；
  不报告任务开始前未要求演奏的累计量。
- 只有会话阶段、歌曲身份、自由巡演选项和当前 `task_detail.task_id` 全部一致时，才允许从
  第 2/3 曲准备页续跑。新任务、旧 schema、缺失或不一致 task ID 都将旧会话标记为
  `superseded` 并从第 1 曲重新计数；若新任务已落在旧准备页则 fail-closed。
- 自由巡演的第四张选曲图只选择难度，不读取身份，也不拒绝连续相同歌曲。每首到自己的
  准备页再读取封面、等级、实际难度和实拍标题；必要时最终封面补全。
- 三首 Profile 流速必须一致。课题巡演若启用流速检查，回主页检查后必须重新进入并确认
  阵容没有变化；组曲中途禁止打开设置页。
- 已启动单曲发生生命归零或可分类瞬时引擎失败时，先把进度恢复到本组开始前，再按
  `play_failure_retry_count` 有界重试。重试必须丢弃预武装、退出失败演出、将当前组会话标记
  为 `superseded`，并重演完整三首；失败曲和同组其余曲均不计数。
- 非结算的失败退出必须使用 `MedleyLiveFailedContinue → MedleyLiveFailedExit →
  MedleyQuitConfirmExit` 专用状态机：第一层点左侧灰色“退出”，第二层点右侧粉色“退出”；
  若从第二层确认页开始，先安全退回第一层再按顺序处理；绝不点击星石“继续”。Profile、
  身份、谱面、流速硬冲突及用户停止不重试。
- 三首的演出完成凭据与成绩采集状态独立保存。只有可信会话证明三首均已演完才增加三个
  完成数；PGGBM 缺失、数字不稳或报告保存失败不能撤销完成凭据，也不能伪造缺失成绩。
  第三曲后按曲序尽力读取三张 PGGBM；开启“不检查结果”时跳过数字读取并继续安全导航。

### 一键实时与自动演出

- 一键实时只接受连续稳定的开场封面和实际标题；无完整身份时继续等待，不从歌曲中途启动。
- 有本地谱面才允许 Native；无谱面难度保留已确认身份并整局使用 Legacy。第一首完成、读取
  PGGBM 并恢复主页后任务结束。
- 自动演出不调用 Realtime 引擎；难度点击后必须复核，自动演出开关和配额耗尽使用各自
  明确状态。每日免费抽卡是否还有次数，则以点击免费按钮后是否出现确认弹窗为准，不使用
  容易互相误匹配的“剩余 N 次”模板。

## 统一结算与恢复

- 歌曲身份只在开演前和本局最终封面确认；演奏完成后不得重新 OCR 标题、难度或等级来
  否决本局身份。结算只识别 PGGBM 页面、稳定判定数字和必要终点；组曲按会话曲序保存。
- 演出已确认结束且触点清理完成后，结算识别、截图、保存和恢复错误只记录警告，继续后续
  步骤；不能因此重演已完成的演出或终止整项任务。完成凭据必须按局隔离，不能掩盖真实
  死亡、未完成或用户停止。“不检查结果”只跳过成绩数字检查，仍识别必要终点；实时校准
  必须取得完整成绩才能接受 Profile，已完成演出的结算技术故障按既有预算重试，耗尽后
  中性保留可续跑阶段，不报告演出失败或无界重演。
- 所有演出结算只循环：最右下角安全像素 `(1279,719)` → Android BACK → 同一安全像素。
  像素点击只加速动画，BACK 是唯一页面推进输入。
- 禁止点击奖励、排名、总分、确定、下一步等可见按钮或历史按钮坐标。每次输入后重新截图
  检查必要终点；组曲还要检查下一张 PGGBM，不能整轮连发后才识别。
- Controller 代理不能跨前台保护或嵌套任务长期保存；每次输入前重新取得当前代理。
- 有界次数耗尽后保存现场并执行恢复，必要时只重启游戏一次。真实演出失败保持失败；已确认
  演出完成后的技术故障保持警告，不反向修改完成状态。
- 未知界面恢复最多 60 秒；登录下载确认先于通用退出弹窗处理，下载中被动等待，不发送
  BACK/ESC。

## 定制 MFAAvalonia 保护

开发运行目录使用的 `MFAAvalonia.Core.dll` 来自
`D:\Documents\workplace\MFAAvalonia` 的定制分支 `fix/speed-only-settings`，不能用同版本官方
DLL 覆盖。官方 DLL 会丢失“演出设置”、Profile 管理和 Mirror 更新源保护。

- `scripts/patch-mfa-stop-status.ps1` 必须验证定制源码特征和基线祖先，替换前备份 DLL；定制
  源码缺失时直接失败，不 clone 官方源码回退。
- 部署指纹必须覆盖定制 MFA 的脏工作树，不能只比较 Git HEAD 或单个源码文件。
- `interface.json`、Pipeline 和模板可以部署覆盖；`config/`、Profile、主题、窗口布局和模拟器
  配置属于用户数据，禁止删除或重建来修 UI。
- `ContinueRunningWhenError=false` 必须保留：真实失败保持失败，人工停止显示“已放弃本次任务”。
- “演出设置”读取 Profile 失败时不得自动保存界面默认值；新增运行时选项必须同步 Python
  默认值/校验、MFA ViewModel 属性/加载/Capture 和 AXAML 控件。
- 同时运行两个 MFA 或频繁切换配置可能触发上游 Avalonia 渲染器崩溃；当前规避是单实例、
  少切换配置。

## 模拟器边界

- MFA/MaaBanGDream 与 ALAS 等自动化工具不能同时控制同一设备。
- MuMu 可能监听 `127.0.0.1:5555/7555` 并影子雷电 ADB；连接雷电前关闭 MuMu 或使用不冲突
  端口。MuMu 主 ADB 为 `127.0.0.1:16384`。
- 前台应用、设备指纹和物理模拟器必须同时核对，不能只相信 ADB 序列号。

## 更新、打包与许可证

- Release 仅在用户明确要求、`main` 干净、验证和所需真机验收完成后执行。流程见
  `docs/release-package.md`、`docs/release-notes-template.md` 和技能中的 release runbook。
- 完整包保留版本目录外壳；runtime-free 更新 ZIP 的 `interface.json` 必须位于归档根目录，
  不包含 Python 运行库归档和 `resource/charts`。谱面通过“演出设置 → 谱面辅助 → 同步”。
- 更新继续使用 `.part` 断点续传和 SHA-256；只有包完整应用成功后才写
  `update-manifest.json`。保留 `config/profiles/logs/debug/screencap`。
- 中文路径启动和目录改名使用 PowerShell/ShellExecute，不经 `cmd /c` 转换路径；生成的
  PowerShell 脚本使用 UTF-8 BOM。
- v1.4.0 起项目自有代码使用 PolyForm Noncommercial 1.0.0；第三方许可证和品牌规则分别以
  `LICENSING-MaaBanGDream.md`、`THIRD-PARTY-NOTICES.md`、`TRADEMARKS-MaaBanGDream.md`
  为准。发布包必须携带相应正文，不能用根许可证覆盖第三方组件。

## 当前仍需真机覆盖的范围

- MuMu Native 时钟偏斜没有稳定解法，继续使用 Legacy。
- 双 MFA/快速切换配置的 Avalonia 崩溃尚未在本项目侧修复。
- 组曲随机歌曲、课题巡演、跨组计数和中断续跑需要分别保留真机验收记录。
- 协力 Special→Expert 回退和部分协力首音边界仍需真实房间证据，不能由单人结果外推。

## 最低验证与交付

```powershell
# 完整验证
.\scripts\verify.ps1

# 固定运行时检查
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe scripts\check_runtime.py `
  --mfa-root D:\Documents\workplace\.tools\MFAAvalonia-profile-v3

# pytest
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe -m pytest tests/ -v
```

提交前还要运行 `git diff --check` 并检查未跟踪文件。涉及任务生命周期、部署或实时输入时，
交付状态必须分别写清：

1. 自动化测试是否通过；
2. 是否已部署到开发 MFA；
3. 是否完成真实设备/游戏验收；
4. 仍未验证的模式、风险和回退方式。

“已构建”“测试通过”“已部署”和“真机验收通过”是四个不同状态，不能互相替代。
