# Bestdori 本地谱面仓库

Bestdori 提供可同步到本机的谱面和封面。实时演奏只读取已经校验的本地 JSON，
不会在演奏过程中联网请求谱面。

## 当前覆盖

同步范围为 Bestdori 完整歌曲目录，每首保存 Hard、Expert 和确实存在的 Special。
当前歌曲、谱面、封面与错误数量以本机 `manifest.json` 和“设置 → 谱面更新”
显示为准。封面优先使用 CN 资源，缺失时依次回退 JP、EN。

## 数据布局

```text
resource/charts/
  representative-songs.json      # 代表曲目同步与验证清单
  manifest.json                   # 本地运行时唯一索引
  bestdori/<song-id>/<difficulty>.json
  bestdori/<song-id>/jacket-<server>-<n>.png
```

每张谱面使用 `schema_version: 1` 包装原始 Bestdori note list，记录歌曲 ID、
难度、等级、预计音符数、来源 URL、原始响应 SHA-256 和规范化谱面
SHA-256。运行时加载时会再次校验路径、歌曲、难度和内容哈希。

`manifest.json` 同时保存 Bestdori 封面资源的本地路径、来源服务器、
SHA-256 和 `song-jacket-phash-v2`。当前 UI 封面 ROI 为 1280×720 坐标
`(684,120,320,320)`。

## 更新命令

在 MFA 独立的“设置 → 谱面更新”分类可查看数量、同步进度并手动同步。
自动更新默认开启，间隔为 24 小时，可关闭或设置为 1–720 小时；按上次成功检查
时间判断是否到期，只在所有实例没有执行或排队任务时联网。失败至少退避 15 分钟，
界面显示上次成功、下次检查和最近错误。手动维护前也应停止正在运行的 Maa 任务。

同步校验并复用有效资源，按需下载新增、缺失、损坏或索引音符数变化的资源；失败或
中断保留旧清单引用的可用资源。同音符数的远端内容变更无法仅靠索引增量检查发现。

在仓库根目录执行以下命令，使用项目固定 Conda 环境：

```powershell
& ..\.tools\Miniconda3\envs\maabangdream\python.exe `
  scripts\sync_bestdori_catalog.py `
  --output-root resource\charts `
  --jacket-server cn `
  --jacket-fallback-server jp,en `
  --prune-other-difficulties
```

同步器处理 Bestdori 当前完整歌曲索引，带并发上限、超时、重试、内容类型
和谱面类型检查，以临时文件 + 原子替换写入，支持中断后续跑。单曲缺失或
未知音符类型会写入 manifest 错误清单，不阻断其余歌曲。

## 运行时选择与回退

```text
准备页证据（封面 / 标题 / 实际难度与等级）
        ↓ 身份缺失或冲突时隔离旧谱面，延迟预武装
本局最终封面 + 实读标题（等级辅助）
        ↓ 身份缺失则继续等待，冲突或超时则触控前停止
确认本局歌曲与实际难度
        ├─ Easy/Normal 无本地谱面 → 整局 Legacy
        ↓ 其他需本地谱面的情况
可信本地谱面校验（路径 / ID / 难度 / SHA-256）
        ↓ 校验失败则触控前停止
所选引擎演奏（Native 中途不切换 Legacy）
```

封面 pHash 只收窄候选，歌曲身份还需结合难度等级和实读标题；OCR 与谱面解析
均离线运行。准备页身份缺失或冲突时，隔离旧谱面、延迟预武装，由本局开场的
连续稳定最终封面和独立实读标题补全。身份仍不明确时不得启动纯视觉盲打。

完整歌曲目录中封面唯一、最终实读标题一致时，默认允许观测等级与本地元数据
相差 1 级。共享封面、无可信最终标题或相差超过 1 级时，不能使用此容差；共享
封面仍需按标题与等级消歧。可用 Agent 参数 `--disable-regional-level-drift`，或
环境变量 `MAABANGDREAM_REGIONAL_LEVEL_DRIFT_TRIAL=0` 关闭该规则；开发启动脚本
也支持 `-DisableRegionalLevelDrift`。旧 `--regional-level-drift-trial` 启用参数保留兼容，
显式关闭参数优先于旧启用参数。

确认身份后，运行时复核本地谱面的路径、歌曲、难度和 SHA-256，再交给所选引擎。
Native 必须有可信本地谱面，启动后禁止中途切换 Legacy。Easy/Normal 未保存在
本地谱面库，可在身份确认后整局使用 Legacy；不能借此绕过身份门禁或降低难度。

谱面解析支持分段 BPM、Long/Slide 完整 connection、显式 flick 和
`Directional` 的 Left/Right 方向。紫色普通音符或 Slide 类型本身都不会被
自动升级成 flick。

## 验证

```powershell
& ..\.tools\Miniconda3\envs\maabangdream\python.exe `
  -m pytest tests\test_sync_bestdori_charts.py `
  tests\test_sync_bestdori_catalog.py `
  tests\test_representative_chart_repository.py `
  tests\test_chart_repository.py tests\test_final_cover_gate.py `
  tests\test_chart_timeline.py tests\test_chart_predictor.py -v

.\scripts\verify.ps1
```

实时触控改动还必须先使用 `scripts/replay_realtime_trace.py` 重放现有 trace，
再进行 MFA 部署和真机演出验收。

## 数据来源说明

生成文件保留 Bestdori 来源 URL 和内容哈希，便于复核与更新。MaaBanGDream 的
项目许可证不自动改变游戏谱面数据及封面资源本身的权利状态；公开发布数据快照前
仍需由维护者确认相关站点与游戏内容的分发要求。
