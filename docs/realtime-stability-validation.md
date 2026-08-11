# 实时演奏稳定性验收

本轮改动先建立可关联、可复现的观测能力，再用真机数据选择视觉设置和截图后端。除非截图基准证明当前后端无法满足门禁，否则不改变实时触控策略。

## 产物关联

每轮演奏生成唯一 `run_id`。结果 JSON、校准报告和 Debug Recorder `summary.json` 必须同时记录相同的 `run_id` 与 `song_id`；Debug 关闭时 `debug_recording_path` 为 `null`。正式结果还记录经游戏界面读回验证的实际流速、按键类型、点击特效和判定辅助状态。

`song_id` 使用难度选择成功时已有截图的 `song-phash-v1` 感知哈希，不增加截图。无法可靠识别时记录 `unknown`；校准不会把 `unknown` 当作有效歌曲。

所有原始截图、录像、trace、基准结果和实验结果保留在 Git 忽略目录，禁止提交设备序列号、日志或 Profile。

“一键实时演奏”保持纯监听、不导航的既有语义，因此不会自行打开设置页。它只接受最近 15 分钟内由视觉设置门禁产生的实际读回；缓存缺失或过期时会拒绝启动，绝不使用 MFA 目标配置冒充游戏实际状态。

## 开演前结构化终态

在已建立 `LiveRunContext` 的正常任务路径中，ProfileCheck、流速设置门禁或 ProfilePlay 的引擎前准备失败时，也必须写入 `screencap/realtime-result-*.json`。该结果使用 `valid:false`、`result_status:"preflight_error"`，并通过 `terminal_stage` 区分 `profile_check`、`performance_settings_gate` 与 `profile_play_preflight`；同时保留本轮 `run_id`、`song_id`、模式、难度、已经实际读回的视觉/流速设置及明确 `reason`。写入终态时会消费本轮一次性 Play token，后续直接调用不得复用旧歌曲身份。

开演前失败不得伪造 PERFECT/MISS 等结算字段，`processed_frames` 与 `dispatched_actions` 均为零，`debug_recording_path` 为 `null`，也不得生成 Recorder `summary.json`。视觉评估路径的此类结果继续设置 `eligible_for_profile_acceptance:false`。结构化产物写入采用尽力而为语义：产物写入失败不得掩盖最初的门禁失败原因。

## 视觉组合筛选

所有样本使用同一歌曲、难度、流速和高密度时间段，并通过 MFA 的 `视觉设置评估（实验）`运行。实验结果不得更新 accepted Profile。

Project Interface 的多个选项覆盖按任务选项顺序应用，同一节点字段采用后写替换，不会深度合并 `custom_action_param`。因此视觉评估模式不再向正式节点写入只有实验标记的局部参数：模式选项只做结构路由，依次进入专用的 `RealtimeLiveVisualEvaluationRequireProfile`、`RealtimeLiveVisualEvaluationSettingsGate`、`RealtimeLiveVisualEvaluationStart` 与 `RealtimeLiveVisualEvaluationPlay`；五档难度为实验检查、设置和 Play 节点提供完整参数。普通 Formal 仍走原有节点，保持严格环境匹配。

1. 固定点击特效 1、关闭判定辅助，依次录制 TYPE1–7 的相同短片。淘汰错误 HOLD、重复动作、粘连或严重遮挡的类型，并按检测完整性、轨迹连续性、置信度和动作时序抖动排序。前两名各做两次同曲完整实验。
2. 固定胜出的按键类型、关闭判定辅助，依次录制 TAP EFFECT 1–5 的相同短片。前两名各做两次同曲完整实验。
3. 对最终按键/特效组合分别录制判定辅助开启和关闭的标准短片。开启没有提高完整性或降低 miss 时默认关闭。
4. 使用最终组合重新正常校准，再做三次同曲正式演出。至少两次 FC，另一轮 miss 不超过 1；不得出现错误 HOLD、粘住触点、业务失败或无法解释的长帧。

完整演出的排序依次使用 miss 中位数、最差 miss、动作一致性。不要把当前偏移或推测值写死，最终偏移由新校准结果确定。

## Recorder 关闭门禁

Recorder 的实时入口只做非阻塞入队；队列饱和时丢弃诊断帧并增加丢帧计数，不允许磁盘写入或编码反压实时线程。关闭时使用有界等待：正常的小批量视频帧先按 FIFO 顺序编码，再由关闭哨兵结束线程，避免 `record()` 后立即 `close()` 把已经接受的视频帧误清空。若 record worker 或 encoder 超过关闭时限，先原子写入带 `record_worker_finalized:false` 或 `encoder_finalized:false` 及明确错误的 provisional summary；后台线程真正结束后再原子替换为 finalized summary 和最终计数。超出有界关闭窗口的积压仍可被丢弃并显式计数，关闭过程不得无限阻塞任务终态。

## 截图后端基准

第一阶段结果中的 `stage_timings_ms.capture` 和 `frame_interval_outliers[].dominant_stage` 用于确认截图卡顿。只测试设备支持的无损截图方式；专用 CLI 复用 `LatestFrameObserver`，为每个候选建立独立控制器并连续执行三轮、每轮五分钟。

使用固定 Conda Python 运行专用 CLI。命令默认只校验计划并输出 `mode:"dry-run"`，不会连接控制器；只有显式加入 `--execute` 才执行正式基准。正式执行前必须完全关闭 MFA 和 ALAS，避免多个控制器争用同一模拟器。CLI 仅允许 `EmulatorExtras`、`RawByNetcat`、`RawWithGzip`、`Encode` 与 `EncodeToFileAndPull`；有损的 `MinicapDirect`、`MinicapStream` 会被硬拒绝，不能作为候选或回退。

```powershell
$python = 'D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe'

# 默认 dry-run：3 轮 × 300 秒，只打印计划。
& $python scripts\benchmark_screencap_backends.py `
  --backend EmulatorExtras `
  --backend RawByNetcat `
  --backend RawWithGzip `
  --backend Encode `
  --backend EncodeToFileAndPull `
  --baseline-backend EmulatorExtras

# 关闭 MFA/ALAS 并复核计划后，才允许正式执行。
& $python scripts\benchmark_screencap_backends.py `
  --backend EmulatorExtras `
  --backend RawByNetcat `
  --backend RawWithGzip `
  --backend Encode `
  --backend EncodeToFileAndPull `
  --baseline-backend EmulatorExtras `
  --execute
```

正式执行为每个后端建立独立控制器，只调用截图，不发送点击或触控。每轮报告写入 Git 忽略的 `debug/screencap-benchmarks/screencap-benchmark-*.json`，汇总与排序写入同目录的 `screencap-benchmark-suite-*.json`；报告不得包含 ADB 路径或设备序列号。

候选必须同时满足：

- 15 分钟内没有大于 150 ms 的截图等待；
- capture P95 不比当前后端恶化超过 5 ms；
- 相同画面的检测结果和置信度无实质退化。

多个候选通过时，依次比较最大等待、P95、平均耗时。另对同曲 Debug 开/关各做三轮，有效 FPS 下降不得超过 3%，Recorder 队列阻塞不得传播到实时线程。

当前 CLI 的 `timing_qualified` 只有在请求且实际覆盖至少三轮、每轮 300 秒时才可能为真；更短的 smoke run 仍输出指标，但会明确标记证据不足。正式资格还会判断最大等待、相对基线的 P95 和无效帧计数；“有效帧”只表示返回了非空图像数组。CLI 不保存原始画面，也不验证相同画面上的音符检测结果或置信度等价。因此 `timing_qualified:true` 只是耗时门禁通过，最终候选仍必须另外使用相同画面样本完成 detector 输出与置信度等价验证，不能仅凭该字段切换正式后端。

若没有无损后端通过，才进入独立的第二阶段修复。必须先证明控制器允许截图和触控并行；严格串行时禁止伪异步触控，只使用最佳无损后端并进行安全重同步。

## 离线故障注入

`scripts/replay_realtime_trace.py` 支持 `--inject-gap-ms`、`--drop-frames` 和 `--fault-after-frame`。对 150/350/500/1000 ms 间隔注入以及真实 359 ms trace，验收条件是回放确定、无新增重复点击、无迟到 HOLD、无错误补点、结束后无活动触点。丢帧可以安全漏打，但不得猜测未知音符。

新 trace 会从相邻 `summary.json` 自动取得难度和初始偏移，并逐帧应用 trace 中记录的动态 timing feedback；使用 `--fixed-timing-offset` 才会禁用动态偏移。缺少 session 元数据的旧 trace 必须显式传入，避免把 Normal 录制静默按 Hard 的滑动 HOLD 语义回放。例如 `realtime-20260808-000656` 的正确参数是 `--difficulty Normal --timing-offset-ms -12`。

`tests/test_replay_realtime_trace.py` 在本机 Git 忽略的历史录制存在时，固定验证 `realtime-20260808-000656` 的 277 个动作逐字段完全一致、150/350/500/1000 ms 注入矩阵，以及 `realtime-20260807-235842` 的真实 359 ms 长帧。其他环境没有这些私有录制时会跳过对应本地门禁，不会把日志或设备数据提交到仓库。

## 2026-08-09 实机视觉组合采集结果

在真实雷电模拟器（1280x720 / Normal / 流速 3.5）上完成 TYPE1–7、TAP1–5、assist on/off 的
视觉门禁读回与演奏表现采集：

| 组合 | 门禁读回 | 演奏表现 |
| --- | --- | --- |
| TYPE1 / TAP1 / assist off | 1->1 | 完整演奏，fps 53.2 |
| TYPE2 / TAP1 / assist off | 1->2 | 生命耗尽（93 动作） |
| TYPE3 / TAP1 / assist off | 2->3 | 生命耗尽（1 动作） |
| TYPE4 / TAP1 / assist off | 3->4 | 生命耗尽（232 动作） |
| TYPE5 / TAP1 / assist off | 5->5 | 生命耗尽（14 动作） |
| TYPE6 / TAP1 / assist off | 5->6 | 生命耗尽（40 动作） |
| TYPE7 / TAP1 / assist off | 6->7 | 生命耗尽（17 动作） |
| TYPE1 / TAP2 | 1->2 | 完整演奏，304 动作，fps 53.3 |
| TYPE1 / TAP3 | 2->3 | 生命耗尽（103 动作） |
| TYPE1 / TAP4 | 3->4 | 完整演奏，259 动作，fps 59.9 |
| TYPE1 / TAP5 | 4->5 | 生命耗尽（183 动作） |
| TYPE1 / TAP4 / assist on | 0->on | 完整演奏，261 动作，fps 58.3 |
| TYPE1 / TAP4 / assist off | on->off | 完整演奏，259 动作，fps 59.9 |

胜出组合：**TYPE1 + TAP4 + assist off**。

修复内容：

- TYPE 读回改为整段 `TYPE1..TYPE7` 标签模板匹配（`resource/image/performance_settings/type_labels/`），
  不再依赖窄数字字形单独分类（TYPE5 曾被读成 3，TYPE1 会拾取衬线像素）。
- 生命保护触发后暂停覆盖层无法确认时不再升级为硬失败：触点已释放，按生命耗尽干净收尾，
  校准正式轮可以重试而不是整体失败。

胜出组合重新校准生成 `normal-20260809175514.json`（accepted=true，偏移 -6 ms）。
随后使用该 Profile 完成三次同曲正式演奏（3/3 完整结算，miss 7–9，无 FC）：

| 轮次 | perfect | great | good | bad | miss | fps |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 201 | 24 | 1 | 1 | 9 | 59.9 |
| 2 | 208 | 18 | 1 | 1 | 7 | 51.1 |
| 3 | 199 | 26 | 0 | 3 | 8 | 51.7 |

正式验收目标（至少两次 FC、另一轮 miss<=1）尚未达到；当前引擎在 Normal Lv.14 上仍会漏 7–9 个音符。
剩余计划：截图后端 3×300s 正式基准、Debug on/off 对照、同画面 detector 等价验证。

### 截图后端 3×300s 正式基准结果（2026-08-09）

| 后端 | 最大截图 | P95 | 均值 | >150ms 帧 | 结论 |
| --- | --- | --- | --- | --- | --- |
| EmulatorExtras（当前） | 16–31 ms | 16 ms | 3.8–4.5 ms | 0 | **timing_qualified=true** |
| RawByNetcat | — | — | — | — | 控制器/截图初始化失败（nc 端口不可达） |
| RawWithGzip | 312 ms | 266 ms | 229.8 ms | 3917 | 不合格 |
| Encode | 1141 ms | 516 ms | 410.1 ms | 2195 | 不合格 |
| EncodeToFileAndPull | 640 ms | 547 ms | 494.8 ms | 1821 | 不合格 |

结论：只有当前 EmulatorExtras 通过耗时门禁，没有可切换的无损后端，因此不进入第二阶段伪异步截图/触控修复；
detector 输出/置信度等价验证在无候选后端时可延后。RawByNetcat 的失败是 LDPlayer 上 nc 服务不可达，
不是代码问题（CLI 已把该后端标为 controller setup failed）。

### Debug on/off 对照（正式演奏 ×3，TYPE1/TAP4）

| 组 | 轮次 | perfect | great | good | bad | miss | fps |
| --- | --- | --- | --- | --- | --- | --- |
| Debug on | 1 | 201 | 24 | 1 | 1 | 9 | 59.9 |
| Debug on | 2 | 208 | 18 | 1 | 1 | 7 | 51.1 |
| Debug on | 3 | 199 | 26 | 0 | 3 | 8 | 51.7 |
| Debug off | 1 | 206 | 20 | 0 | 2 | 8 | 59.9 |
| Debug off | 2 | 210 | 18 | 0 | 2 | 6 | 59.9 |
| Debug off | 3 | 200 | 23 | 4 | 0 | 9 | 59.8 |

结论：Debug off 三轮有效 FPS 稳定在 59.8–59.9；Debug on 有两轮降到 51.1/51.7
（约 -14%），未满足“下降≤3%”的门禁，说明录制编码在部分轮次明显挤占截图/检测预算。
判定与动作数在两组之间没有系统性差异（miss 6–9）。若追求稳定帧率，正式演奏建议 Debug off。

### 视觉主观评估（vision 逐帧分析，2026-08-10）

从各轮 Debug 录制的 `playfield.avi` 抽取代表帧，用视觉模型对音符头清晰度/对比度、
flick 可辨识度、hold 可见度、判定线干扰四项打分（1–5，5 最好）：

| 按键类型 | 样式 | 平均分 | 备注 |
| --- | --- | --- | --- |
| TYPE1 | 半透明扁梭形+细亮边 | ~15 | 对比度一般，亮背景易融合 |
| TYPE2 | 圆形+亮白描边 | ~15 | 冷蓝与背景撞色，远端难辨 |
| TYPE3 | 无描边纯色圆片 | ~12 | 紫色与背景融合，hold 可见度差 |
| TYPE4 | 菱形+高亮描边 | ~18 | 轮廓锐利、hold 醒目、flick 箭头清晰 |
| TYPE5 | 高饱和胶囊+粗白描边 | ~16.5 | 色彩鲜艳、轮廓清晰 |
| TYPE6 | 扁平椭圆+亮边 | ~15.5 | 淡蓝与冷光背景接近 |
| TYPE7 | 圆角矩形+高饱和发光 | ~16 | 绿色音符醒目，hold 可见 |

打击特效（TYPE1 下 TAP1–5）：按 trace 中每次 TAP 动作时间点抽帧，并用 OCR 筛选
**同一判定等级（PERFECT，特效最大）** 后再对比，避免不同准确度特效混比：

| 特效 | PERFECT 同档平均分 | 说明 |
| --- | --- | --- |
| TAP1 | ~17 | 白圈+星芒，尺寸中等 |
| TAP2 | ~16.5 | 淡紫星形轮廓+细光点 |
| TAP3 | ~17 | 淡蓝双层扩散圈，消散快 |
| TAP4 | ~17 | 青蓝双层环+白核，遮挡/噪音稳定，实测 fps 最高（59.9） |
| TAP5 | ~16 | 暖黄光晕+五角星 |

PERFECT 同档下五种特效差异很小（16–17/20）；TAP4 在“不遮挡/低噪音”两项持续高分且
实测帧率最高，因此推荐仍为 **TYPE4 + TAP4 + assist off**。

**视觉结论：TYPE4 最适合识别，TAP4 干扰最少。推荐组合 TYPE4 + TAP4 + assist off。**
注意当前检测器阈值是按 TYPE1 风格调参的，切到 TYPE4 后需要按菱形实心音符重新标定颜色/轮廓阈值，
这正是下一阶段“针对默认打歌识别逐类优化”的起点；TYPE4 引擎实测已能部分识别（232 动作），调参空间较大。

### TYPE4 检测器调参（第一步，2026-08-10）

从 TYPE4 录制测量：普通音符头是 12×12~19×19 的纯色实心菱形（H≈104），远小于
TYPE1 的轮廓包裹梭形。原检测器两个几何条件把 TYPE4 头部全部拒绝：

- 宽高比要求 ≥1.15，而 TYPE4 头接近 1.0（改为 ≥0.85）；
- 最小宽度曲线 `12 + 45*progress`，而 TYPE4 头上部只有 8~14px（tap/skill 改为
  `6 + 18*progress`）。

实测效果（同帧采样）：TYPE4 每帧 tap 中位数 4→5、峰值 7→9；TYPE1 保持 3→4 且
flick 误报为 0。hold/skill 阈值未动。已加入方形小头回归测试，`verify.ps1` 506 项通过。

**未完成：TYPE4 的 flick 箭头是白色水平箭头（S≈30、V≈215），原品红 chevron 通道
检测不到（全程仅 1 次 flick 动作）。** 曾尝试在 FLICK 检测中增加白色通道，但白色
同时覆盖音符描边，误报过多；需要下一步用“箭头与音符头分离 + 水平列宽度差”做专门
的白色箭头检测，或先采集更多 TYPE4 flick 样本再标定。

## 2026-08-11 TYPE4 真机校准与 TYPE1/TAP1 正式对比

本轮先确认校准流程已修复：`CalibrationRunner` 不再要求三首不同歌曲，三次有效排练
（可重复同曲）后直接进入一次正式验证，正式轮存活且 hit_rate>=0.80 即写入
accepted Profile。TYPE4/TAP1/assist off 在 Normal/流速 3.5 上完整跑通
“3 排练 + 1 正式”，生成 `normal-20260811015814.json`（offset 11ms，accepted=true）。

### TYPE4 + TAP1（正式演奏）

| 批次 | 轮次 | perfect | great | good | bad | miss | fps |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 233 | 8 | 0 | 1 | 3 | 59.9 |
| 1 | 2 | 227 | 12 | 3 | 1 | 3 | 52.1 |
| 1 | 3 | 236 | 6 | 2 | 0 | 2 | 51.8 |
| 2 | 1 | 235 | 8 | 0 | 0 | 3 | 59.9 |
| 2 | 2 | 233 | 11 | 0 | 0 | 2 | 51.5 |
| 2 | 3 | 231 | 10 | 0 | 2 | 3 | 51.2 |
| 3（offset 17ms 实验） | 1 | 231 | 11 | 0 | 1 | 3 | 59.9 |
| 3（offset 17ms 实验） | 2 | 225 | 18 | 0 | 0 | 3 | 60.0 |
| 3（offset 17ms 实验） | 3 | 234 | 10 | 0 | 0 | 2 | 51.0 |

offset 11→17ms 只把 fast/slow 从“慢多快少”拉回平衡，miss 无变化（9 轮均值 2.67），
说明 TYPE4 的剩余 miss 是检测漏判而非时机偏差。

### TYPE1 + TAP1（同曲正式演奏，当前代码）

切换到 TYPE1/TAP1/assist off 后使用已验收的 `normal-20260809145800.json`
（offset -33ms）同曲正式演奏 6 轮：

| 批次 | 轮次 | perfect | great | good | bad | miss | fps |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 222 | 20 | 2 | 0 | 2 | 60.0 |
| 1 | 2 | 217 | 26 | 1 | 1 | 1 | 60.0 |
| 1 | 3 | 215 | 20 | 1 | 0 | 1 | 51.7 |
| 2 | 1 | 216 | 26 | 1 | 1 | 2 | 60.0 |
| 2 | 2 | 221 | 31 | 1 | 1 | 2 | 60.0 |
| 2 | 3 | 218 | 25 | 0 | 2 | 1 | 51.7 |

**正式对比结论（2026-08-11）：**

- 用户历史基线（TYPE1/TAP1，7 轮）：miss 3/3/1/5/2/1/1，均值 ≈2.3，最差 5。
- TYPE4/TAP1（9 轮）：miss 均值 2.67，全部 2–3，未超过基线。
- TYPE1/TAP1 当前代码（6 轮）：miss 2/1/1/2/2/1，均值 1.5，最差 2，六轮全部 ≤2，
  三轮 =1。已超过“先前机器人正式演奏结果”。

最终选择 **TYPE1 + TAP1 + assist off** 作为默认组合（`selection.json` 已回写）。
TYPE4 的视觉主观分更高，但真机 miss 不如 TYPE1；下一阶段若要继续降低 miss，
应从 TYPE1 的漏判（flick/hold/同轨密集）入手，而不是更换按键皮肤。

## 2026-08-11 Hard 难度推进（尚未达到 Normal 同级稳定性）

目标：Hard 难度稳定达到 Normal 同级准确率。当前 Normal 基线（TYPE1/TAP1）：
6 轮 miss 2/1/1/2/2/1，均值 1.5。Hard 尚未达到该水平，仍在推进。

### Hard 校准

Hard 三次排练可完整通过（miss 2/6/6，hit_rate 0.985–0.995），但正式验证轮在
约 55–70 秒生命耗尽导致整体失败。已修复校准流程：`calibration-formal` 生命耗尽
不再让整个校准失败，而是写入 `valid:false` 报告并允许重试
（`profile_play_action.py`，新增 `test_life_depleted_calibration_formal_round_can_retry`）。

### Hard 正式轮实验（TYPE1/TAP1，手动 hard Profile `hard-20260811090000.json`）

| 配置 | 轮次 miss | 结果 |
| --- | --- | --- |
| offset 0（初版） | 4, 6, 死亡@55s | 2/3 存活 |
| offset +12 | 7, 死亡@66s | 1/2 存活 |
| offset 0 + 遮挡修复 | 死亡@76s | 未存活 |
| offset -20 + 遮挡修复 | 1, 5, 死亡@70s | 2/3 存活，最佳轮 miss=1 |

### 已提交的 Hard 相关修复

- `fix: detect horizontal flick arrows...`：Hard 谱存在横向/非上向 flick 箭头的
  宽扁翼条，重新组装为 FLICK，避免被当普通音符点掉。
- `fix: suppress late flick ring residue rescues...`：flick 触发后同轨 0.45s 内
  的 first-visible TAP 残片不再补一次多余点击。
- `fix: fire trusted long-falling heads occluded just before the trigger target
  on slide charts`：滑条（Hard+）谱中，长距离可信下落音符在触发线前几像素被
  绿条遮挡丢失时，允许在目标线下方 6px 内提前触发。Normal（无滑条）行为不变，
  黄金重放测试保持精确一致。

### 根因与剩余差距

离线 trace 取证（死亡轮 `realtime-20260811-094641`）发现：密集滑条段中，音符头
在判定线附近被绿条遮挡，跟踪器发生碎片重分配（y 回跳、down_frames 清零），
导致长下落轨道在到达触发线前丢失；重放对比显示修复后该轮多打出 6 个此前丢失的
动作。各轮死亡时间随修复逐步延长（55→62→66→70→76→84s），但最难的 B/C 两首
在最后 5–10 秒仍会漏掉足够多的音符把生命打空。

尚未解决：密集滑条段的跟踪器重分配与“滑条相邻音符”过滤仍是漏判主要来源，
需要更深入的 tracker 改造（同物理音符的碎片合并、轨道防回跳）才能达到
Normal 同级稳定性。当前状态：Hard 最佳轮 miss=1，但最差轮仍可能死亡。

### 2026-08-11 跟踪器实验与回滚

继续尝试两个跟踪器改动（均通过 513 项测试）：

- `keep downward motion credit across small fragment jitter`：同轨碎片在 6px
  上跳容差内不再清零 down_frames。
- `anchor slide-chart tracks to the playable head`：滑条谱轨道遇到上跳碎片时
  保持头锚点。

真机对比显示这两个改动让 song B 从“miss 5 存活”退化为“生命耗尽”，且死亡轮
离线重放的重复判定从 11 增至 30，说明多打出的动作以重复误触为主。两个提交已
回滚（`c0f80bc`），代码回到遮挡修复版本（`9e7e427` 等价）。

回滚后确认批次（offset -20，连续三轮均为此前最易死亡的 song C）：

| 轮次 | perfect | great | good | bad | miss | fps |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 309 | 31 | 1 | 6 | 4 | 57.6 |
| 2 | 320 | 72 | 1 | 4 | 4 | 49.3 |
| 3 | 320 | 70 | 2 | 2 | 7 | 57.0 |

三轮全部存活并完成结算（此前 song C 多轮死亡）。但 miss 4/4/7（均值 5.0）
仍高于 Normal 的 1.5，Hard 目标尚未达成。

### 2026-08-11 后续实验与最终状态

继续尝试“滑条相邻音符按贴边判断”修复（`238d97c`）：只吞掉真正贴着绿条边缘的
相邻轨道 TAP，轨道中心的真实音符放行。512 项测试通过，但真机 song C 从
“3/3 存活”退回“67 秒死亡”，再次出现与信任放宽相同的回归：增加相邻轨道放行
会引入错误输入。已回滚（`859686f`）。

回滚后复验（9e7e427 等价代码 + offset -20）：song C 本轮在 74 秒死亡。
综合全部 11 轮 Hard 正式数据，song C 的死亡呈随机性（约一半轮次死亡，存活轮
miss 4–7），不存在某个简单阈值能稳定消除。Hard 与 Normal 的差距根源在密集滑条
段的跟踪器重分配，属于需要重构 tracker 的大改动，当前代码已固定在可验证的最优
状态：song A miss 1–2，song B/C miss 4–7 且约半数死亡。

### 2026-08-11 流速实验（3.0 / 4.0）与最终复验

尝试通过调整 Hard 流速减少遮挡漏判：

- 3.0（更慢，音符更挤）：流速门禁成功（3.50→3.00 读回确认），第 1 轮 60 秒
  死亡，无效。
- 4.0（更快，音符更分散）：流速门禁成功（3.00→4.00 读回确认），flick 动作从
  27 降到 6（快速箭头更难检测），第 1 轮 60 秒死亡，无效。

两个实验 Profile 已停用（`.bak`），`selection.json` 与游戏流速恢复 3.5。
最终复验（3.5 + 遮挡修复 + offset -20）：miss 7 存活 / 第 2 轮 94 秒死亡。
累计 14 轮 Hard 正式数据确认：song A 稳定 miss 1–2，song B/C miss 4–7 且约
半数轮次生命耗尽。Hard 目标仍未达成，剩余差距需要 tracker 级重构而非参数调整。

### 2026-08-11 遮挡预测头实验（tracker 重构第一版，已回滚）

实现“被绿条遮挡时按速度外推头位置”的 tracker 方案：轨道遇到上跳碎片时不再把
代表点上移，而是用最后真实头位置 + 速度预测当前位置，让触发线可达。514 项测试
通过，但离线重放三个完整 Hard 轮显示 transient 动作增加约 25%（476→584、
440→531、436→552），多出的动作主要是相邻轨道 80–120ms 内的二次触发（同一物理
音符被相邻轨道碎片重复判定），与之前所有“放宽”类修复相同的回归模式。

结论：预测头本身逻辑正确，但碎片在相邻轨道的分裂仍会产生重复触发；必须先解决
“同物理音符跨相邻轨道碎片合并”（在 `_assign` 层按 x 连续性合并），才能让预测
头方案有效。该实验已回滚，代码保持 511 项测试通过的已验证状态。

### 2026-08-11 跨轨碎片合并（已提交 `551cc3e`）

实现相邻轨道边界贴边碎片合并：同一物理音符被透视分割到相邻轨道时，两个碎片都
贴近共享边界（各自偏离轨道中心 >12% 轨距），合并为一个轨道，保留最低（头部）
碎片；真实和弦音符位于各自轨道中心，不受影响。仅滑条谱（Hard+）启用，Normal
黄金重放保持不变。

离线重放三个完整 Hard 轮：动作数与录制几乎完全一致（645→646、616→617、
626→626，无过度触发），同轨重复判定 2–7。真机批次：miss 7 存活 / 第 2 轮 77
秒死亡，与合并前持平，未改变死亡率。

累计 16 轮 Hard 正式数据：song A miss 1–2；song B/C miss 4–7、约半数轮次生命
耗尽。所有已提交修复均为安全改进（511–514 项测试通过），但 Hard 与 Normal 的
差距仍未消除，剩余问题集中在密集滑条段音符头在判定线前的丢失，尚未找到不引入
重复触发的可靠恢复手段。

### 2026-08-11 TYPE4/TAP4 与 TYPE4/TAP1 Hard 正式对照（用户要求复核）

用户指出视觉评估曾推荐 TYPE4+TAP4，要求复核该组合在 Hard 的表现。在检测器放宽、
遮挡触发放宽、跨轨合并、冻结轮内偏移全部生效后补测：

| 组合 | 结果 | 死亡轮动作构成 |
| --- | --- | --- |
| TYPE4 + TAP4 | 57 秒生命耗尽 | tap 173 / flick 3 / hold 14（242 动作） |
| TYPE4 + TAP1 | 75 秒生命耗尽 | tap 284 / flick 16 / hold 33（446 动作） |
| TYPE1 + TAP1（冻结偏移对照） | miss 7/6/5 三场存活；另批次 miss 7 存活/死亡 | tap 420–435 / flick 24–28 / hold 42–46 |

结论：TYPE4 在现有检测器下 Hard 不可行——小菱形头导致 hold 头确认（33 vs 43）
和 flick 识别（16 vs 25）明显偏低，不是特效干扰问题。TYPE1+TAP1 仍是实际最优，
已还原为默认组合；游戏内设置也已由门禁从 TYPE4/TAP1 改回 TYPE1/TAP1。

### 2026-08-11 TYPE1+TAP4 Hard 对照（用户建议）

用户建议保留 TYPE1（识别好）换 TAP4（视觉干扰少）。补测结果：

| 轮次 | miss | 结果 | 关键指标 |
| --- | --- | --- | --- |
| 1 | 10 | 完整结算 | hold 45 / flick 21 / rejected_holds 19 |
| 2 | — | 85 秒生命耗尽 | hold 46 / flick 27 / rejected_holds 13 / tap 390 |

TAP4 确实让 hold 候选极干净（rejected 13–19 vs TAP1 的 313–343），hold 数
（45–46）也不输 TAP1，但 tap 动作量（390 vs 430）和 miss（10 vs 2–7）明显更差，
第 2 轮仍死亡。TYPE1+TAP1 仍是 Hard 最优组合；默认已还原。

另修复一个真实竞态 Bug：绿条释放与同一帧 MOVE 竞争时
`cannot move inactive touch contact` 会让整首歌崩溃（`fcfef94`，已推送）。

### 2026-08-11 冻结偏移配置存活率汇总

冻结 Hard+ 轮内自适应偏移（`2de1b74`）后的 TYPE1/TAP1 正式批次（每批 3 轮）：

| 批次 | 轮次 miss / 结果 |
| --- | --- |
| 21:53 | 7 存活 / 6 存活 / 5 存活（3/3） |
| 22:13 | 7 存活 / 死亡 |
| 22:22 | 2 存活 / 死亡 |
| 22:47 | 7 存活 / 死亡 |

累计 6/10 存活（60%），与基线 50–60% 相比没有显著提升；冻结偏移保持（不引入
回归、Normal 不受影响），但不是 Hard 稳定性的解。Hard B/C 的存活与 miss 波动
仍是当前未解决的核心问题。

### 2026-08-11 TAP4 深挖结论（用户坚持 TAP4 路线）

在“同一张 Hard 谱（判定总音符 401）”上严格对比 TYPE1+TAP4 与 TYPE1+TAP1：

- TAP4：tap 400 / flick 21 / miss 10；TAP1：tap 421–442 / flick 27–29 /
  miss 4–7。
- 检测器输出率几乎相同（tap/flick 每帧检测数一致），损失发生在规划器转动作
  阶段，且分散在所有触发路径（crossing −2、tail-ring −2、dropout −2 等），
  没有单一可标定机制。
- 未动作事件的 min_y 分布两组合几乎一样（长下落未动作率均约 50%，多为 hold
  伪影），排除了“TAP4 使头更晚出现”的假设。
- 同一张谱 TAP1 自身也有 miss 4↔7 的轮间波动，TAP4 的实际差距约 3–6 miss。

尝试的两个 TAP4 专属机制（干净环境放行相邻轨 tap、干净环境放宽 dropout
救援）在重放中无恢复或给 TAP1 带来小风险，均回滚。结论：TAP4 的 hold 噪声低
是真实优势，但其 flick/tap 召回小幅变差没有可用的规划器修复手段；要发挥 TAP4
优势需要检测器层面的 TAP4 专属标定（flick 匹配阈值、头入轨时机），当前未实施。

### 2026-08-12 BestDori 谱面对齐工具链（谱面身份 / 漏判定位）

用户确认当前曲目固定为 SAVIOR OF SONG，并给出 BestDori 谱面页面
（`https://bestdori.com/tool/chart/306/hard/SAVIOR-OF-SONG`）。谱面 JSON
端点实际是 `/api/charts/{id}/{difficulty}.json`（如
`https://bestdori.com/api/charts/306/hard.json`），需要带浏览器
Referer/Origin 头才能绕过 403。

新增两个离线工具：

- `scripts/align_trace_to_chart.py`：把 trace 的动作时间轴对齐到 BestDori
  谱面判定时间轴（粗-细网格搜索引擎启动偏移），输出每轮的
  matched/missed/spurious、漏判清单和多余动作清单；支持
  `--json <file>` 落盘完整报告。
- `scripts/compare_chart_timelines.py`：两轮 trace 之间按 lane 对齐动作
  序列，判断是否同一张谱（解决 song-phash 碰撞无法区分谱面的问题）。

谱面身份结论：`song_identity.py` 的 ROI `(40,110,410,490)` 实际是选歌列表
区域，绝大部分像素是固定 UI 和选中态高亮，不同歌曲/难度的 phash 只差
2–3 bit，落在 `same_song` 的 8-bit 容差内——因此 phash 不能证明“同谱”。
要做同谱对比必须用动作时间线/谱面对齐，而不是 phash。

### 2026-08-12 TAP4 卡死 hold 根因与修复

用谱面对齐复盘 TYPE1+TAP4 完整结算轮 `realtime-20260811-224139`
（game miss=10）时发现一个确定性 Bug：

1. 歌曲 75.30s 处 lane 0 的 hold 正常释放，但释放瞬间其尾环（height 16、
   confidence 0）被检测为 lane 1 的新 hold 头；
2. 旧代码的 `_linked_hold_body` 把这个小头链接到**同一帧正在释放的
   lane 0 宽 body**（宽 316px，横向覆盖到 lane 1），使弱片段获得
   `body_confirmed=True` 并启动新 hold；
3. 该幽灵 hold 挂在 contact 1 上持续 **16.9 秒**（直到
   `hold_max_seconds` failsafe），期间 lane 0 被 `occupied_lanes` 占住，
   真实 hold（79.844s–80.156s）从未启动 → 2 个判定丢失；
4. 同配置 TAP1 轮次无此卡死（hold 时长 >5s 的轮数为 0），TAP4 专属。

修复（`agent/realtime/touch_planner/holds.py`）：`_linked_hold_body` 现在
排除 `_hold_released_at` 中最近 `hold_start_suppress_seconds`（0.35s）内
释放过的 lane 上的 body——刚释放的旧 body 不再为新头背书。只排除“刚释放”，
不排除“仍活跃”：lane 5 活跃 hold 的 body 仍可为 lane 6 的齐奏新头提供
链接（避免误伤和弦对）。

离线重放验证（同 trace、同 planner 配置）：

| 轮次 | 修复前（录制） | 修复后重放 |
| --- | --- | --- |
| TAP4 224139 | matched 396 / missed 2 / spurious 106 | matched 397 / missed 1 / spurious 105 |
| TAP1 225018 | matched 394 / missed 4 / spurious 122 | matched 394 / missed 4 / spurious 122（零回归） |

Normal 黄金重放（`realtime-20260808-000656`、`realtime-20260807-235842`）
保持精确一致；全量 pytest 518 通过（含 2 个新回归测试）。剩余 1 个
hold-tail 漏判（80.156s）是 TAP1/TAP4 共有的尾释放 grace 问题
（body 提前消失时 0.35s grace 使释放晚约 0.4s），本次未改动。

另确认：TAP4 总多余动作（106）低于同配置 TAP1 冻结轮（122–144），
但 lane 3 集中了 30 个幽灵 TAP（TAP1 为 11–18）；它们与真实音符
判定的“按错轨道”无关，游戏按空轨处理，不是 miss 主因。检测器级
“宽扁条过滤”会误杀 3/115 个贴线真实音符，已证伪，未采用。

### 2026-08-12 谱面时间轴预测 v1：chart-tail 尾释放救援

实现 `agent/realtime/chart_timeline.py`（BestDori 谱面解析、
`resource/charts/song-306-hard.json` 固定数据）与
`agent/realtime/chart_predictor.py`（`ChartPredictor`）：

- 用前 16 个可信动作粗-细网格校准引擎↔谱面偏移（0.02s 粗搜 + 0.005s
  细搜，同计数取时差总和最小）；校准失败则整轮关闭（防止换歌误判）。
- **chart-tail 救援**：hold 身体在尾判定前消失时（现有 0.35s grace
  会导致释放晚 ~0.4s 被判定 miss），按谱面尾时间释放；仅在身体不可见
  且手指当前 lane == 谱面尾 lane 时触发，避免提前释放滑条尾。
- 按压预测（`chart_predict_presses`）保留代码但默认关闭：离线重放显示
  86% 的预测按压会与正常 crossing 重复，密集谱会打到下一个音符，需
  真机调参后再启用。

离线重放（同一 trace、同一 planner 配置）：

| 轮次 | 无图表基线 | chart-tail v1 |
| --- | --- | --- |
| TAP4 224139 | 397/1/105（修复卡死 hold 后） | **398/0/104**，3 次按谱面时间释放，最长 hold 3.3s→1.8s |
| TAP1 225018 | 394/4/122 | 394/4/122（零回归，1 次消失身体救援） |

Normal 黄金重放不受影响（`chart_prediction` 默认关闭）。527 项 pytest
通过（含 chart_timeline / chart_predictor 7 项新测试）。

### 2026-08-12 真机批次：按压预测 A/B 与当前最优配置

部署 chart-tail + 卡死 hold 修复后，在模拟器上采集多批 Hard/TAP4 正式轮
（song=SAVIOR OF SONG，offset -20 冻结，`hard-20260811223000.json`）：

| 配置 | 轮次结果（game miss） | 说明 |
| --- | --- | --- |
| 按压关 + chart-tail（02:00 批） | 3 / 死亡@~76s | 完整结算轮 miss=3（TAP4 历史最佳），另一轮密集段死亡 |
| 按压开（+0ms，02:00 批） | 7 / 6 / 7，三轮全完成 | FAST 40–118，游戏建议后移 25–30ms |
| 按压开（+30ms，02:18 批） | 死亡@~76s | 死前谱面漏判仅 2 个，生命仍耗尽——死亡来自判定时机而非覆盖 |

结论：

- 谱面覆盖已不是瓶颈（死亡轮死前漏判 13→3，完成轮 398/398）。
- 游戏生命耗尽主要来自“覆盖到但判定时机差”的音符（BAD/MISS），
  而按压预测目前整体偏早（FAST 高），且按压偏置会连带影响 hold 尾。
- 当前生产配置为**按压预测关闭**（`chart_predict_presses=false`），
  chart-tail 准时释放保留；这是 miss=3 完成轮对应的配置。
- 下一步最有价值的实验：用引擎实时的 FAST/SLOW 反馈驱动按压偏置
  （替代固定 +30ms），把图表按压校准到游戏音频判定线；以及继续修
  tracker 的 hold 漂移（漂移 hold 占用车道会挡住后续真实 hold 头）。

### 2026-08-12 高 FPS 批次与偏移实验

模拟器恢复 58 FPS 后（此前 40–43 FPS），按压关闭配置连续三轮全部
完成：miss 7 / 5 / 6，但 result FAST 71–84、SLOW 0–3——整体偏早。
把冻结偏移从 -20 改为 -32 再测三轮：miss 7 / 5 / 4，FAST 仍 70–86，
游戏建议继续后移（每轮恰好 -12 步进）。结论：

- 高 FPS 下完成率大幅提升（58 FPS 三轮全完成），但判定时机偏早；
- tap 触发线被 `min(judgement_y - 3, calibrated, predicted)` 钳制在
  ~562px，负偏移对 tap 实际无效（hold 释放线仍受偏移影响）；
- 游戏建议的“后移 25–44ms”无法通过现有 offset 通道作用于 tap；
- 当前 profile 已恢复 -20（历史最优 miss=3 对应值）。

累计真机完成轮（TAP4 + chart-tail）：miss 3 / 4 / 4 / 5 / 5 / 6 / 6 /
7 / 7 / 7 / 7；低 FPS 时仍有约一半轮次在密集段生命耗尽。剩余瓶颈：
1) tap 触发时机偏早（钳制导致 offset 无效，需放宽触发线或改用
   FAST/SLOW 驱动的自适应）；2) 40 FPS 下密集段 tracker/漂移问题。

### 2026-08-12 触发钳制修复（关键突破）

`ordinary._ordinary_trigger_y` 原来用 `min(judgement_y-3, calibrated,
predicted)`，预测线（~557px）永远赢，导致 -20ms 偏移对 tap 无效、
所有高 FPS 轮 FAST 70–86。改为 Hard+ 用
`min(judgement_y+6, max(calibrated, predicted))`：

- 负偏移能把触发推迟到线上附近（~571px），预测线仍是正偏移/慢捕获的
  下限；
- 物理跨线兜底保留，并新增“当前 y 已到线即触发”（已 fired 轨道除外）；
- Normal 完全走旧逻辑，黄金重放精确不变（528 项 pytest 通过）。

真机复验（TAP1 与 TAP4 各一轮三连，55–60 FPS）：

| 配置 | 三轮 miss | result FAST/SLOW |
| --- | --- | --- |
| TAP1 + 新触发 | 6 / 4 / 3 | 19/11、11/18、18/24 |
| TAP4 + 新触发 | 4 / 4 / 3 | 11/29、11/30、10/32 |

FAST 从 70–86 降到 10–11，SLOW 略高（11–32），与历史 miss=3 轮的
“稍偏慢”特征一致；三轮全部完成。这是 TAP4 首次连续三轮 miss≤4 且
全部结算。剩余死亡轮（约 1/4）死前漏判仅 2–4 个，全部是 hold 头/尾
（tracker 漂移/被占用），tap 覆盖与时机已不再是瓶颈。

按压预测（chart_predict_presses）维持关闭：新触发下未再完成 A/B
（环境门禁/恢复反复 flake），待稳定环境补测；代码与 +30ms 偏置保留。
