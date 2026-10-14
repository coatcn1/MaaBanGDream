# 实时演奏稳定性验收

本页记录持续适用的证据与验收方法。历史实验见 [2026 年 8 月记录](history/realtime-stability-experiments-2026-08.md)，Native 设计目标见 [V2 验收基线](realtime-engine-v2-goals.md)。

## 产物关联

每轮演奏生成唯一 `run_id`。结果 JSON、校准报告和 Debug Recorder `summary.json` 必须同时记录相同的 `run_id` 与 `song_id`；Debug 关闭时 `debug_recording_path` 为 `null`。正式结果记录目标流速和经游戏界面读回验证的实际流速。

歌曲身份由准备页或本局最终封面确认，封面指纹使用 `song-jacket-phash-v2`，并结合实读标题与实际难度；不能用曲库标准标题冒充截图证据。缺失时记录 `unknown` 并等待门控补全，校准不会把它当作有效歌曲。

所有原始截图、录像、trace、基准结果和实验结果保留在 Git 忽略目录，禁止提交设备序列号、日志或 Profile。

“开演前自动设置并验证流速”关闭时不读取、修改或验证设置页；开启时，普通演出任务每次都在离开主页前真实进入设置弹窗，只读取、按需修正并复核流速，准备页禁止再次打开设置，也不保存跨任务跳过凭据。镜像、判定辅助、连击显示、FAST/SLOW、NOTE TYPE 与 TAP EFFECT 由用户按[使用指南](usage.md#游戏与引擎设置)手动配置，自动流程不再检查或修改。

“一键实时演奏”在监听和开演前不主动导航，因此不会自行打开设置页。流速开关开启时，它只接受最近 15 分钟内由流速门禁产生的实际读回；缓存缺失或过期时会拒绝启动。流速开关关闭时完全跳过这项检查并信任用户声明值。

## 开演前结构化终态

在已建立 `LiveRunContext` 的正常任务路径中，ProfileCheck、流速设置门禁或 ProfilePlay 的引擎前准备失败时，也必须写入 `screencap/realtime-result-*.json`。该结果使用 `valid:false`、`result_status:"preflight_error"`，并通过 `terminal_stage` 区分 `profile_check`、`performance_settings_gate` 与 `profile_play_preflight`；同时保留本轮 `run_id`、`song_id`、模式、难度、已经实际读回的流速及明确 `reason`。写入终态时会消费本轮一次性 Play token，后续直接调用不得复用旧歌曲身份。

开演前失败不得伪造 PERFECT/MISS 等结算字段，`processed_frames` 与 `dispatched_actions` 均为零，`debug_recording_path` 为 `null`，也不得生成 Recorder `summary.json`。结构化产物写入采用尽力而为语义：产物写入失败不得掩盖最初的门禁失败原因。

## Recorder 关闭门禁

Recorder 的实时入口只做非阻塞入队；队列饱和时丢弃诊断帧并增加丢帧计数，不允许磁盘写入或编码反压实时线程。关闭时使用有界等待：正常的小批量视频帧先按 FIFO 顺序编码，再由关闭哨兵结束线程，避免 `record()` 后立即 `close()` 把已经接受的视频帧误清空。若 record worker 或 encoder 超过关闭时限，先原子写入带 `record_worker_finalized:false` 或 `encoder_finalized:false` 及明确错误的 provisional summary；后台线程真正结束后再原子替换为 finalized summary 和最终计数。超出有界关闭窗口的积压仍可被丢弃并显式计数，关闭过程不得无限阻塞任务终态。

## 截图后端基准

第一阶段结果中的 `stage_timings_ms.capture` 和 `frame_interval_outliers[].dominant_stage` 用于确认截图卡顿。只测试设备支持的无损截图方式；专用 CLI 复用 `LatestFrameObserver`，为每个候选建立独立控制器并连续执行三轮、每轮五分钟。

使用固定 Conda Python 运行专用 CLI。命令默认只校验计划并输出 `mode:"dry-run"`，不会连接控制器；只有显式加入 `--execute` 才执行正式基准。正式执行前必须完全关闭 MFA 和 ALAS，避免多个控制器争用同一模拟器。CLI 仅允许 `EmulatorExtras`、`RawByNetcat`、`RawWithGzip`、`Encode` 与 `EncodeToFileAndPull`；有损的 `MinicapDirect`、`MinicapStream` 会被硬拒绝，不能作为候选或回退。

```powershell
$python = '..\.tools\Miniconda3\envs\maabangdream\python.exe'

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


## 谱面对齐诊断

- `scripts/align_trace_to_chart.py`：把本地 Trace 动作与谱面时间轴对齐，输出 matched / missed / spurious，支持 `--json` 保存报告。
- `scripts/compare_chart_timelines.py`：按轨道比较两轮动作序列，辅助核对是否为同一谱面。

两者属于离线诊断；结果不能替代最终封面身份确认或真机验收。报告保存在 Git 忽略目录。

## 开发验证开关

以下开关用于已授权的开发验证，修改后重启开发 MFA；普通用户无需配置。

| 入口 | 默认与作用 |
| --- | --- |
| `-OrderedStartupTrial` | 默认关闭，验证按黑场、封面、演奏场顺序建立开演证据 |
| `-DisableNativeTimingCompensation` | 暂时关闭默认启用的 Native 等待成本与启动补偿 |
| `MAABANGDREAM_NATIVE_WAIT_JITTER_TRIAL=1` | 默认关闭，只过滤未来等待成本预测；不改已发生卡顿、首音锚点或 Profile 偏移 |

等待成本异常值候选的实际状态见 Agent 启动日志及 Native 结果 `timing_trial.wait_jitter_guard.enabled`；删除变量并重启可恢复既有补偿。等级差异规则的关闭入口见[曲库说明](bestdori-chart-repository.md)。
