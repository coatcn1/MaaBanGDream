# Bestdori 本地谱面仓库

本仓库把 Bestdori 当作开发/部署阶段的数据源，不把它当作实时演奏依赖。
运行时只读取已经校验的本地 JSON；断网、Bestdori 暂时不可用或接口变化都
不会让正在进行的演出发起网络请求。

## 当前覆盖

2026-08-30 全量快照包含 Bestdori 索引中的 809 首歌曲、1777 张谱面和
867 个封面文件。每首只保存 Hard、Expert，以及确实存在的 Special；不再
保存 Easy/Normal。封面优先使用 CN 资源，缺失时回退 JP，只有 5 首英语服
独占歌曲最终使用 EN，因此所有 809 首均至少有一个本地封面。

## 数据布局

```text
resource/charts/
  representative-songs.json      # 明确允许同步的歌曲集合
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

日常使用可在 MFA 的“设置 → 演出设置 → 谱面辅助”点击“同步/更新全部谱面”。
该入口显示本地歌曲、谱面、封面和错误数量，并逐行显示同步进度。同步属于显式
维护操作，应先停止正在运行的 Maa 任务；实时演奏本身不会触发同步。

命令行维护方式如下，必须使用项目固定 Conda 环境：

```powershell
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe `
  scripts\sync_bestdori_catalog.py `
  --output-root resource\charts `
  --jacket-server cn `
  --jacket-fallback-server jp,en `
  --prune-other-difficulties
```

同步器处理 Bestdori 当前完整歌曲索引，带并发上限、超时、重试、内容类型
和谱面类型检查，并以临时文件 + 原子替换写入。已存在且通过哈希验证的文件
会被复用，因此可在中断后续跑；单曲缺失会写入 manifest 错误清单而不阻断
其余歌曲。接口新增未知音符类型时该谱面会被明确记录为失败。

## 运行时选择与回退

```text
选歌/难度确认截图
        ↓
封面 pHash + 标题文字 + 难度等级
        ↓
manifest 唯一命中 ──否──→ 整局纯视觉（不降低难度）
        ↓是
本地哈希复核并加载谱面
        ↓
视觉动作校准时间相位
        ↓
谱面时序辅助 + 视觉闭环
        ↓ 连续 8 次可信失配
释放谱面独占触点并整局回退纯视觉
```

封面仍是低成本的第一身份信号，但不再假定每首歌都有唯一封面。共享封面时，
程序会在开演前用本地 PP-OCRv5 模型读取歌曲标题，并结合当前谱面等级消歧。
标题识别器接受调用方提供的任意 ROI；后续多人演出可从其歌曲通知或加载界面
提供标题区域，即使没有单人选歌页的封面位置，也能直接按标题匹配。所有 OCR
与谱面解析均离线运行；标题置信度不足或仍有多个候选时保持纯视觉，不会回退
到 Normal，也不会冒险加载不确定谱面。

谱面解析支持分段 BPM、Long/Slide 完整 connection、显式 flick 和
`Directional` 的 Left/Right 方向。紫色普通音符或 Slide 类型本身都不会被
自动升级成 flick。

## 验证

```powershell
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe `
  -m pytest tests\test_sync_bestdori_charts.py `
  tests\test_sync_bestdori_catalog.py `
  tests\test_representative_chart_repository.py `
  tests\test_chart_timeline.py tests\test_chart_predictor.py -v

.\scripts\verify.ps1
```

实时触控改动还必须先使用 `scripts/replay_realtime_trace.py` 重放现有 trace，
再进行 MFA 部署和真机演出验收。

## 数据来源说明

生成文件保留 Bestdori 来源 URL 和内容哈希，便于复核与更新。项目代码的
GPL-3.0-only 许可证不自动改变游戏谱面数据及封面资源本身的权利状态；公开
发布数据快照前仍需由维护者确认相关站点与游戏内容的分发要求。
