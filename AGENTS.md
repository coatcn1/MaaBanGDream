# MaaBanGDream规范

全文≤10000字符（含空白换行），仅长期规范，新增先去重。

## 文档边界

- README仅放简洁功能概览与使用入口；禁止开发/调查过程、候选迭代、测试/验收记录及数字。
- 详细说明写`docs/`，开发命令写`CONTRIBUTING.md`，变更写`CHANGELOG.md`，证据写交接/调试记录。
- 保留Logo/徽章/emoji；正文统一Markdown标题、短列表和编号步骤，表格/提示块按需，HTML限页首/页尾。

## 项目与目录边界

|路径|用途与写入边界|
|---|---|
|`D:\Documents\workplace\MaaBanGDream`|唯一源码仓库，项目修改均在此|
|`D:\Documents\workplace\.tools\MFAAvalonia-profile-v3`|开发运行目录，仅脚本部署，不提交|
|`D:\Documents\workplace\MFAAvalonia`|定制MFA源码，独立分支、提交和验证|
|用户正式安装目录|默认只读；无明确授权不得部署候选或改配置|

MFA不读源码目录；改`agent/`、`resource/`或`interface.json`后须经`scripts\launch-mfa.ps1`同步并重启开发MFA。

## 固定运行环境
- Python：`D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe`。
- Python 3.12；MaaFw 5.10.2；MFAAvalonia 2.12.0；.NET Runtime 10。
- Conda仅`conda-forge`，以`runtime-compatibility.json`为准；不用`.venv`，不临时换Python/MaaFw/MFA版本规避问题。
- pytest临时目录用Git忽略的`.local/pytest-<进程号>`，不写系统AppData。

## Git/修改

1. 改前核对`git status --short --branch`、远端及差异；保留用户改动，不混无关重构。
2. 从`main`建`feature/*`或`fix/*`，不直接在`main`开发；提交单目的，沿用格式。
3. PR以squash合入`main`，保留线性历史；不建merge commit，不强推或改写共享历史。
4. 人工注释用简体中文，解释原因/边界/失败策略，不逐行翻译。
5. 禁止提交日志、截图、Profile、设备序列号、凭据、本机绝对路径、缓存、运行目录及构建产物。
6. 行为或用户可见修改同步`CHANGELOG.md`。
7. 提交/推送/PR/合并/Release独立；无用户明确要求，不推送、合并或发布。

## 图像与证据工具
- 能识图即直接分析；简单OCR/文字提取/单模板确认/批量初筛可用`python C:\Users\Lenovo\.codex\skills\vision\vision.py <图片路径> "<问题>"`。
- 复杂页面、空间关系、坐标、Computer Use或外部vision矛盾时，用`view_image`或Computer Use屏幕上下文。
- 多图/视频先裁剪、缩放并限制必要帧；禁止手工读图片字节或把base64塞入对话。
- 页面故障合核截图/模板得分/坐标/日志/实际行为，不凭单模板。

## Subagent路由

明确职责，同批文件仅一人写。仅用`gpt-6.1-terra`/`gpt-6.1-sol`，禁GPT-5.6；通用角色显式指定模型，自定义角色核绑定，不符则换合规通用角色，职责/约束保持。
- `gpt-6.1-terra`：日志/结果只读扫描交`mbd_log_scanner`，trace/checkpoints/events/lifecycle有限窗口交`mbd_trace_scanner`。
- `gpt-6.1-sol`：`mbd_replay_runner`离线replay/基线比较，只写`.local/`；`mbd_implementer`依有界证据、最小假设及验收标准实现/测试。
- 电脑/浏览器/模拟器/真机操作（含CUA、ADB、MaaFramework、模拟器API及脚本截图/输入/导航/验收）仅委派显式`gpt-6.1-terra`的Subagent。主Agent负责目标、拆分与证据复核；同设备唯一操作Agent，保留身份、租约及触点释放门禁。

Realtime：取证→必要独立审查→最小实现→定向测试→离线replay→修改后复核→用户真机验收；离线不代替真机。

## 任务架构

演出入口先做进程互斥。普通任务由`CommonRecover`恢复主页；组曲先查可信断点，不能先回主页破坏现场。实时单局统一进入`agent/realtime/profile_play_action.py`的`RealtimeProfilePlay`。

单人主页查流速，选曲/准备页确认身份，正式/排练分路；挑战选点数后复用正式门禁。协力入房/准备送达/成员退出/最终封面/结算各有门禁；竞演沿普通协力选择/配置/启动，不加验收/实验开关，保留房型卡/满员开演/结算差异。校准固定排练一首＋正式验证一首。

`DailyFreeGacha`、`ManualFlowRecording`非演出流程，各按自身Pipeline。

## 生命周期

### 停止、失败与重试
- 任何任务不因非必要故障终止。演出体力读取/补充、成绩、调试截图/录像、日志、报告为附加功能：失败时有界重试、保存可用证据、明确警告，安全恢复后继续原任务，不转演出失败。仅人工停止、真实演出失败、无法安全继续的导航及身份/Profile/输入引擎等必要完整性失败可结束任务。
- 协力/竞演开演前成员退出、匹配取消、房间失效须确认弹窗，清本局预武装/旧凭据，确认安全页后重入；保留完成数，不耗真实失败预算。旧“退出即结束”配置无效；单次导航有界，累计退出/匹配次数不致终止，无法恢复安全页才算必要导航失败。曲中/结算不识别通用成员退出标题，不为重匹配退出演奏现场。
- 演奏采集、触控、生命和输入完整性优先。Trace、缓存、诊断回调、进度、日志异常仅关闭/跳过对应附加功能并有界警告，不打断输入；人工停止立即优先，不为诊断继续截图/操作。通过必要输入完整性门禁才确认完成。
- 校准/组曲附加报告或会话写盘失败不得丢本任务内存进度；跨任务/进程只信可信落盘凭据。缺必要成绩不接受校准Profile；抽卡安全确认、手动录制核心录像属任务目的，失败不伪报成功。
- 体力未知/库存不足只跳过本次补充，不停演出或反复退房。消费未明仅封锁后续自动消费、留未决记录；不得猜成功、重复确认或清保护。补充窗只按确认页面取消/关闭，必要时有界回主页。
- `context.tasker.stopping`为用户停止：立即停输入、释放触点、中性返回；不得再截图、点击、嵌套任务或记业务失败。
- 真实归零、身份冲突、Profile/流速不符、超时、引擎完整性失败保留明确原因；回主页不等于成功。
- MaaFramework可能吞回调异常；所有回调显式`try/except`并返回失败状态。
- 跨回调/进程预算只用`argv.task_detail.task_id`；RemoteTasker包装和`_handle`非稳定身份，缺有效task ID则fail-closed。
- `play_failure_retry_count=1`为首次后最多重试1次；新局入口显式重置预算。
- 嵌套`context.run_task()`复用`max_hit`计数，须用无`max_hit`的专用Action。

### 主页流速与Profile
- 仅主页自动补体力，够下一局不补；完整结算后预计不足才回主页。结合剩余次数/库存/允许类型，补后计划≤24；尾段总需能覆盖则优先补够，长任务减少返主页且避免可规避的一两局尾段。相近批次优先18–24、较少道具，再靠近20；剩少量不得机械补20。组曲最小完整三首、组内不补；未决消费与附加故障继续规则保持。
- `live_boost_target`默认3，仅0–3整数，测试0。自动导航在公共演出选择页确认/修正LIVE BOOST，关闭后重开读回；失败不得入房/选曲。挑战点数独立；被动一键/组曲中途续跑不离现场设置，双设备仅改隔离副本。
- `note_speed_settings_enabled=false`时任何任务不打开设置页，信任声明流速；开启时仅离主页前由`RealtimeGameSpeedSettingsGate`读取/修正/复核。准备页`RealtimePerformanceSettingsGate`只消费本任务结果，不开设置或复用跨任务凭据。
- Profile精确绑定分辨率/DPI/帧率/画质/流速，一份只对应一种流速；`accepted=false`草稿不驱动正式演奏。
- 当前Profile属界面正在配置的任务难度槽；Expert/Special同兼容等级，但请求/实际难度分记。
- Native/Legacy正式局共用`timing_offset_ms`；两者同时整体变差先核历史值/备份，再查时钟/调度器。

### 歌曲身份与难度
- 最终封面在完整曲库严格匹配、实际候选唯一且难度/等级等通过时直接采用，跳过标题OCR，标题误读不得否决。任意top1、宽容差或先按标题/等级收窄的唯一不算。共享/不确定封面仍用实读标题消歧；区服等级容忍保留实读标题及歌曲/难度/谱面SHA约束。准备页身份缺失/冲突时隔离旧谱面、延迟预武装，本局最终封面独立兜底；真实冲突不使用旧谱面输入。
- 只负责选曲的页面不得用曲库标准标题冒充实拍；身份由准备页/最终封面门禁确认。
- 标准难度颜色检测先确认按钮布局；组曲每曲选曲页须稳定送达，未确认仅有界重试进入，不发随机/难度输入。点击回执不代替送达。
- 单人/挑战/协力/自动请求Special，仅确认不可选才显式回退Expert；校准必须精确Special。
- `requested_difficulty`、`effective_difficulty`贯穿Profile/流速/谱面/预武装/播放/结果；实际Special无可信本地谱面或方向语义则触控前失败。
- Custom Action参数整块替换；新增须同步基础Pipeline、全部interface选项和校准override，用真实MaaFramework覆盖结果验证。

## Realtime引擎约束

### 开演与时间轴
- 须本局证据证明：准备页→黑场→最终封面→完整演奏场→可选协力等待弹窗消失→首音；准备页静态生命条/判定线不触发首拍。
- 有异步序号/复用标志时Native首音只用新截图；旧帧不按新轮询时间推进稳定窗口/grace或改插值参考。无元数据的直接调用保留兼容路径。
- Native启动前冻结首音锚点/Profile timing offset；本局FAST/SLOW不重写偏移。设备jlog漂移不等于游戏判定相位。
- Native逐块延迟校准仅改未编译未来切片；改残差/等待成本/设备时钟校正前，用真实jlog/trace有符号分段对齐，不只看绝对百分位。
- 游戏相位恢复须本局可信画面；自动匹配另须开场基准/可辨谱面结构/多新帧一致；执行漂移归零不代替游戏对齐。视觉/判定反馈共用仲裁，不重复叠加或撤销别来源请求，保留执行回执/触点/未来切片门禁。分析复用截图及有界后台队列；证据失效/观察器故障只降级，不停演奏。
- MuMu Native曾有逐局客户机时钟速率偏斜，须当前环境复核；历史Legacy分工不硬禁Native。用户选Native时两端按引擎/匹配Profile验证，时钟/首音证据分设备记录。

### 输入与触点
- 截图/检测/跟踪/派发热路径不阻塞等待或同步ADB前台查询。
- Native停止须本轮generation全部合法触点`UP → commit → reset → commit`完整发送，并有本轮请求游标后的连续jlog执行回执及本地/设备清理证据才置`release_confirmed=true`；单个/历史`r`、断socket、kill不代替序列。证据只证明minitouch协议/清理，不是内核evdev触点实读。
- Legacy HOLD/Slide仅由连续轨迹和离散拓扑事件建立；长条头DOWN后抑制同轨重复TAP。
- 普通紫色外圈非FLICK；仅成组同向粉色箭头/折线才升级FLICK。
- 密集同轨不按固定宽度合并，阈值随透视/音符头尺寸缩放。
- Directional `width=1..7`为跨轨数/判定尺寸；无谱面方向证据，纯视觉FLICK不冒充Left/Right。

### 文件与运行环境
- 中文路径图像读写用`agent/realtime/vision_io.py`的字节级`imdecode`/`imencode`，Agent禁直接`cv2.imread/imwrite`。
- Windows Native谱面用UTF-16路径及`_wfopen`，不依赖窄路径。
- 证据写`debug/recordings/<run_id>`、`screencap/`，不提交。
- 轻量Trace/关键截图不代替连续开场画面；分析黑场/封面/首音边界保留同局录像及帧时间映射，缺画面不把推测作定论。录像复用截图、有界后台编码，实时路径不增同步ADB截图/编码/写盘；失败只警告。

## 模式规则

### 单人、挑战与校准
- 单人/挑战准备页核标题/等级/难度/Profile/正式选项/Native预武装；正式准备页先`3D演出/动画MV → OFF`，确认后再关3D Cut-in，不颠倒。
- 关闭流速设置仅跳过设置页，不跳Native预武装。
- 单人排练从timing offset 0逐帧自校准，开局大量SLOW/GREAT不与正式直接比较。
- 校准签名一致才续跑；正式验证归零直接拒绝，不产已接受Profile。

### 协力与竞演
- “准备完毕”点击后确认按钮消失，最多重试三次；随后高频看成员退出/黑场/本局封面，不固定盲等。
- “其他成员正在准备中”仅开演前处理；存在时不建首音颜色基线，消失帧重置。首音仅窄列局变触发，判定线双FLICK不作弹窗。
- 成员退出仅房间/准备/黑场前窗口检测；曲中/结算禁通用“错误”标题。
- 协力jitter预武装副本与canonical谱面比歌曲身份，不比路径。
- 归零选项/处理两模式一致，不增竞演专属接管开关。统一名“跳车”，触发提示“请手动跳车”；开启则停输入、完整释放、HOME→切回游戏后结束整任务；关闭则继续演奏/结算。每次系统输入后先查人工停止。当前不隔离网络；若恢复自动隔离仅按游戏UID，禁`svc wifi`/飞行模式等切断ADB的方式。
- 最终封面硬身份冲突失败且不重试，仍走启动失败手动跳车，不能提前返回漏切换/提示；切换失败保留首因，切换中人工停止优先、中性返回。
- 竞演沿用协力结算/短时稳定读取/统计失败继续，但PGGBM用单人固定坐标，禁协力偏移。数字有效性、完成凭据、Profile接受资格分记，统计故障不否定完成。
- 结算找不到房间先继续安全节拍，再`CommonRecover`回主页下一局；末局stay失败可按已完成处理。
- 协力Special→Expert回退及部分首音须真实房间验收，不由单人外推。

### 组曲
- 次数按歌：有限3–999且为3倍数，0无限完整三首；跨组/失败重开迭代，不累积栈。完整三首结算才加3，不报任务前未要求的累计量。
- 会话阶段/歌曲身份/自由巡演选项/当前`task_detail.task_id`全一致才续第2/3曲。新任务/旧schema/缺失或不符ID将旧会话置`superseded`，从第1曲计数；新任务已在旧准备页则fail-closed。
- 自由巡演第四张选曲图仅选难度，不读身份或拒连续同歌；各曲准备页读封面/等级/实际难度/实拍标题，最终封面兜底。
- 三首Profile流速一致；课题巡演启用流速检查时回主页查后重入、确认阵容未变；组内禁设置页。
- 已启动曲归零/可分类瞬时引擎失败先回滚至本组前，按`play_failure_retry_count`有界重试：丢预武装、退出失败演出、会话置`superseded`，重演完整三首，同组均不计数。
- 非结算失败退出走`MedleyLiveFailedContinue → MedleyLiveFailedExit → MedleyQuitConfirmExit`：第一层左灰“退出”，第二层右粉“退出”；从第二层开始先安全退第一层再依序处理，绝不点星石“继续”。Profile/身份/谱面/流速硬冲突及人工停止不重试。
- 完成凭据与成绩采集分存，可信会话证明三首演完才加3。缺PGGBM/数字不稳/报告失败不撤完成、不伪造成绩；第三曲后依序尽力读三张，开“不检查结果”只跳数字并安全导航。
- 随机/课题巡演/跨组计数/中断续跑分别留真机验收记录。

### 一键实时与自动演出
- 一键须连续稳定开场封面及实际标题；身份不全继续等，不从中途启动。有本地谱面才Native，否则保留身份、整局Legacy；首曲后读PGGBM、回主页、结束。
- 自动只配置并等结算，不调Realtime；难度点击复核，自动开关/配额耗尽各有明确状态。免费抽卡次数以点免费后是否出确认窗为准，不用易互相误匹配的“剩余N次”模板。

## 统一结算与恢复
- 身份仅开演前/最终封面确认，结束后不重OCR标题/难度/等级否决身份；只读PGGBM/稳定数字/必要终点，组曲按会话曲序。
- 确认结束且触点清理后，结算识别/截图/保存/恢复故障仅警告继续，不重演或终止任务；按局隔离凭据，不掩真实死亡/未完成/停止。“不检查结果”只跳数字、仍认终点；校准须完整成绩才接受；已完成局结算技术故障按既有预算重试，耗尽中性保留续跑阶段，不报失败/无界重演。
- 结算只循环安全像素`(1279,719)` → Android BACK → 同像素；像素仅加速动画，BACK唯一推进。
- 禁点奖励/排名/总分/确定/下一步等可见按钮或历史坐标；每次输入后截图查终点，组曲还查下一PGGBM，不整轮连发后才识别。
- Controller代理不跨前台保护/嵌套任务长期保存，每次输入前重取。
- 有界耗尽保存现场并恢复，必要时最多重启游戏一次；真实失败仍失败，已完成后技术故障仍警告，不倒改完成。
- 未知页恢复≤60秒；登录下载确认先于通用退出弹窗，下载中被动等待，不发BACK/ESC。

## 定制MFAAvalonia保护

开发`MFAAvalonia.Core.dll`来自上表定制源码的`fix/speed-only-settings`；禁同版官方DLL覆盖，以免丢演出设置/Profile管理/Mirror源保护。
- `scripts/patch-mfa-stop-status.ps1`验定制源码特征/基线祖先，替换前备份；源码缺失直接失败，不clone官方回退。
- 部署指纹覆盖定制脏树，不只比HEAD/单文件。
- interface/Pipeline/模板可覆盖；`config/`、Profile、主题、窗口布局、模拟器配置是用户数据，不删/重建修UI。
- 保留`ContinueRunningWhenError=false`：真实失败仍失败，人工停止显示“已放弃本次任务”。
- Profile读取失败禁自动保存默认值；新增运行选项同步Python默认/校验、ViewModel属性/加载/Capture和AXAML。
- MFA/Python JSON管道显式无BOM UTF-8、`PYTHONIOENCODING=utf-8`，不依赖控制台/继承环境。严格解码失败及时清子进程保首因，不只等退出致堵流；进度日志容错。回归覆盖父CP936/UTF-8、Python继承编码、中文/特殊字符双向、旧Profile加载/保存重开，不只强制UTF-8宿主。加载/保存异常记日志，不输出完整Profile。
- 双MFA/频切配置的上游Avalonia崩溃尚未项目侧修复；当前单实例、少切配置规避。

## 模拟器边界
- MFA/MBD与ALAS等不得同时控制同设备。
- MuMu可监听`127.0.0.1:5555/7555`并影子雷电ADB；冲突仅用端口归属已核验独立地址。空闲时重建主连接/截图上下文，不改保存序列号，不重启共享ADB；MuMu主ADB `127.0.0.1:16384`。
- 同时核前台应用/设备指纹/物理模拟器，不只信序列号。
- 双设备独立Agent/控制器/资源/Profile/日志根，共享租约防同设备别名并发；本次触点释放凭据到手才释租。无头前关确认空闲GUI，不删互斥；脚本起房前核双方消耗0及演奏器就绪。

## 更新/打包/许可
- Release仅用户明确要求、`main`干净、验证及所需真机验收完成才做；见`docs/release-package.md`、`docs/release-notes-template.md`、技能release runbook。
- 完整包保留版本目录外壳；runtime-free ZIP的`interface.json`在根，不含Python运行库归档/`resource/charts`；谱面走“演出设置 → 谱面辅助 → 同步”。
- `.part`续传及SHA-256保留；完整应用成功才写`update-manifest.json`，保留`config/profiles/logs/debug/screencap`。
- 中文路径启动/改名用PowerShell/ShellExecute，不经`cmd /c`；生成PowerShell脚本UTF-8 BOM。
- v1.4.0起自有代码PolyForm Noncommercial 1.0.0；第三方/品牌依`LICENSING-MaaBanGDream.md`、`THIRD-PARTY-NOTICES.md`、`TRADEMARKS-MaaBanGDream.md`，包带正文，不用根许可证覆盖第三方。

## 最低验证与交付

```powershell
.\scripts\verify.ps1
$projectPython='D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe'
& $projectPython scripts\check_runtime.py --mfa-root D:\Documents\workplace\.tools\MFAAvalonia-profile-v3
& $projectPython -m pytest tests/ -v
```

提交前`git diff --check`、检查未跟踪文件。涉及生命周期/部署/实时输入，分报测试/开发MFA部署/真机验收/未验模式/风险/回退。“已构建”“测试通过”“已部署”“真机验收通过”不互相替代。
