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
