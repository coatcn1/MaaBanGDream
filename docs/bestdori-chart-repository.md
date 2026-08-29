# Bestdori 本地谱面仓库

本仓库把 Bestdori 当作开发/部署阶段的数据源，不把它当作实时演奏依赖。
运行时只读取已经校验的本地 JSON；断网、Bestdori 暂时不可用或接口变化都
不会让正在进行的演出发起网络请求。

## 当前覆盖

收藏 3 共 12 首代表歌曲：6 首原创、6 首翻唱。每首都有
Easy/Normal/Hard/Expert；当前存在 Special 的 5 首也一并收录，因此合计
53 张谱面。

| Bestdori ID | 歌曲 | 难度数 |
| ---: | --- | ---: |
| 170 | Jumpin' | 4 |
| 125 | 天下トーイツ A to Z☆ | 5 |
| 325 | EXIST | 4 |
| 489 | 迷星叫 | 5 |
| 522 | 詩超絆 | 5 |
| 540 | 影色舞 | 4 |
| 85 | ハッピーシンセサイザ | 5 |
| 499 | アイウエ | 4 |
| 697 | 最高到達点 | 4 |
| 532 | 祝福 | 4 |
| 306 | SAVIOR OF SONG | 5 |
| 595 | 「僕は…」 | 4 |

## 数据布局

```text
resource/charts/
  representative-songs.json      # 明确允许同步的歌曲集合
  manifest.json                   # 本地运行时唯一索引
  bestdori/<song-id>/<difficulty>.json
```

每张谱面使用 `schema_version: 1` 包装原始 Bestdori note list，记录歌曲 ID、
难度、等级、预计音符数、来源 URL、原始响应 SHA-256 和规范化谱面
SHA-256。运行时加载时会再次校验路径、歌曲、难度和内容哈希。

`manifest.json` 同时保存 Bestdori 封面资源的 SHA-256 和
`song-jacket-phash-v2`。封面只在同步时下载并计算指纹，不把 PNG 作为运行
资产提交；当前 UI 封面 ROI 为 1280×720 坐标 `(684,120,320,320)`。

## 更新命令

必须使用项目固定 Conda 环境：

```powershell
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe `
  scripts\sync_bestdori_charts.py `
  --song-list resource\charts\representative-songs.json `
  --output-root resource\charts
```

同步器只处理 `representative-songs.json` 明确列出的 ID，带超时、重试、
内容类型和谱面类型检查，并以临时文件 + 原子替换写入。接口新增未知音符
类型时同步直接失败，避免静默生成不完整仓库。

## 运行时选择与回退

```text
选歌/难度确认截图
        ↓
封面 pHash + 精确难度
        ↓
manifest 唯一命中 ──否──→ 整局纯视觉
        ↓是
本地哈希复核并加载谱面
        ↓
视觉动作校准时间相位
        ↓
谱面时序辅助 + 视觉闭环
        ↓ 连续 8 次可信失配
释放谱面独占触点并整局回退纯视觉
```

谱面解析支持分段 BPM、Long/Slide 完整 connection、显式 flick 和
`Directional` 的 Left/Right 方向。紫色普通音符或 Slide 类型本身都不会被
自动升级成 flick。

## 验证

```powershell
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe `
  -m pytest tests\test_sync_bestdori_charts.py `
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
