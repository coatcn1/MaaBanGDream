<p align="center">
  <img src="docs/assets/maabangdream-logo.png" alt="MaaBanGDream Logo" width="260">
</p>

<h1 align="center">MaaBanGDream</h1>

<p align="center">
  <strong>BanG Dream! 自动化 · 实时演奏 · Bestdori 本地谱面辅助</strong>
</p>

<p align="center">
  基于 <a href="https://github.com/MaaXYZ/MaaFramework">MaaFramework</a> 的《BanG Dream! 少女乐团派对！》自动化项目
</p>

<p align="center">
  <a href="https://github.com/coatcn1/MaaBanGDream/releases"><img src="https://img.shields.io/badge/Version-v1.3.7-ff6f9f" alt="Version"></a>
  <img src="https://img.shields.io/badge/MaaFramework-5.10.2-4c8bf5" alt="MaaFramework">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4?logo=windows11&logoColor=white" alt="Windows">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0--only-3da639" alt="License"></a>
</p>

---

## ✨ 功能

- [x] 🎮 **自动演出** — 当前曲目 / 随机选曲，五档难度，支持 1–99 轮连续执行
- [x] 🎹 **单人实时演奏** — 支持 TAP、FLICK、HOLD、双押、长条配对、判定线补救与残影抑制
- [x] 👥 **协力演出** — 普通四档房、好友邀请或六位私人房间号入房，支持同房续演
- [x] 🎼 **Bestdori 本地谱面辅助** — 809 首歌曲的 Hard / Expert / Special 谱面，谱面主导时序并由视觉持续校准
- [x] 🔄 **MFA 谱面同步** — 在“演出设置 → 谱面辅助”中手动增量同步 Bestdori，CN 封面缺失时依次回退 JP / EN
- [x] 🎯 **实时演奏校准** — 一次排练收敛时序、一次正式验证，生成 Profile 后启用
- [x] 🏆 **挑战演出** — 四档点数、五档难度、连续轮次
- [x] 🧪 **调试记录** — 支持轻度 Trace 或完整记录；证据从最终封面门控前开始，并关联门控、引擎、结算、清理与重试关键截图
- [x] 📹 **手动流程录像** — 可在 MFA 中录制任意活动或界面操作，并生成可逐帧定位的 MKV 与首末帧

## 🚀 快速开始

### 普通用户

1. 前往 [Releases](https://github.com/coatcn1/MaaBanGDream/releases) 下载最新的 `MaaBanGDream-v*-win-x64.zip`
2. **完整解压**压缩包
3. 双击 `启动 MaaBanGDream.cmd`
4. 在 MFA 中选择需要执行的任务

> [!IMPORTANT]
> Windows 便携包已经包含定制 MFAAvalonia、MaaFramework、本地谱面、Agent、便携 Python 环境与 .NET 运行时。  
> 普通用户不需要额外安装 Python、Miniconda、.NET 或开发工具。

> [!NOTE]
> 首次启动会在解压目录内展开固定版本的便携 Python 环境，因此第一次启动可能比之后稍慢。

从 v1.3.6 起，启动时的版本检查和手动下载统一使用 MFA 原生 GitHub 更新功能。
开发候选在 GitHub API 明确限流时，会通过 GitHub 发布网页查询稳定版，仍校验下载包的 SHA-256。
“关于我们”采用居中项目卡片，展示专用 v1 Logo、介绍与项目链接；Logo、联系方式和许可证使用 MFA 原生玻璃卡片并随主题透明度变化，软件标志保留默认 Logo，相关素材随开发部署和发布包一起提供。“显示公告”读取 GitHub 最新稳定 Release 的简介，复用 MFA 发布说明窗口；下载继续使用原生进度提示，成功更新后展示对应版本的更新日志。
已安装运行库时下载约 148 MiB、可由 MFA 直接应用的 runtime-free 更新包；配置、Profile、日志、调试记录
与谱面库不会被常规更新覆盖，谱面仍由“演出设置 → 谱面辅助 → 同步”单独维护。

## 🖥️ 环境要求

| 组件 | 要求 |
| --- | --- |
| Windows | 10 / 11 x64 |
| Android 画面 | 1280 × 720 |
| DPI | 240 |
| Python（源码开发） | 3.12 |
| Miniconda 环境 | `maabangdream` |
| MaaFramework | 5.10.2 |
| MFAAvalonia | 2.12.0 |
| .NET Desktop Runtime | 10 |

精确版本组合记录在 [runtime-compatibility.json](runtime-compatibility.json)。

## 🎼 演奏与谱面辅助

MaaBanGDream 的实时演奏并不是简单的固定坐标点击，而是由视觉识别、触控规划与本地谱面共同完成。

| 能力 | 作用 |
| --- | --- |
| 实时视觉识别 | 检测 TAP / FLICK / HOLD 等音符及演奏状态 |
| Bestdori 本地谱面 | 提供歌曲结构、时间轴和长条 / 滑条信息 |
| 谱面辅助 | 由谱面提供时序先验；Legacy 可用 TAP / FLICK / SKILL 投影及多节点 HOLD / Slide 头与换轨拓扑完成锁相 |
| Profile 校准 | 针对当前模拟器与游戏设置生成匹配参数 |
| FAST / SLOW 反馈 | 用于实时 Timing 调整与结果分析 |
| 调试 Trace / 录像 | 同一 run ID 关联准备证据、最终封面、演奏场门控、触控引擎、结算、清理、降级与重试决定；数值生命监控提供有界汇总和首个低血候选截图，协力准备后最多观察 60 秒并记录黑场或“已错过转场”的动态演奏场证据 |
| 最终封面门控 | 单人、校准、挑战和协力均观察最终歌曲信息页；协力漏检短黑场时，连续两帧且与本局身份一致的封面可直接证明正常转场。未识别但准备页谱面可信时继续原谱面，无可信谱面时在触控前整局回退 Legacy |
| 有界失败重试 | “演出设置 → 任务安全”可设置 0–3 次；普通单人、校准与协力每次重试前都会释放会话并恢复到已识别页面；挑战演出不自动重试 |
| 生命终态监控 | 数值生命只用于确认演奏场、识别生命归零和触发协力跳车，不再提供低血量提前暂停 |
| 协力跳车 | 协力生命归零时停止演奏、确认 Native 触点已释放并切回游戏；若开演黑场漏检，则不启动引擎，只监控生命并在归零或超时后退到桌面再切回游戏。随后结束任务并提示用户手动断网跳车；Maa 不自动修改模拟器网络 |
| 协力准备恢复 | 入房后独立等待“不指定歌曲”最多 180 秒，点击后独立等待准备页 60 秒；前一阶段超时会退到桌面再切回游戏并结束任务 |
| Native V2（实验） | 默认关闭；先同时确认生命条与七轨判定标记，再使用时间制首音门控，禁止加载/歌曲信息/演奏场淡入冒充首音；速度 5.0 的首音检测带补偿采用真机录像基线，设备触控固定落在 `y=590` 判定线 |

“演出设置 → 实时演奏 Profile”中，Expert 与 Special 处于同一兼容等级，并都可兼容较低难度任务。单击表格行用于查看和编辑，双击可设为当前任务难度的 Profile；当前项用随主题变化的强调色单独标记。

Native 等待成本与启动延迟补偿已通过雷电真机验收，开发环境普通运行 `scripts/launch-mfa.ps1` 即会启用；仅在回归排查时使用 `-DisableNativeTimingCompensation` 临时关闭。正式安装目录不用于部署测试代码。

> [!TIP]
> 开演和实时演奏始终读取**本地谱面**。只有用户在 MFA 中主动点击“谱面同步”时才会联网更新。

## ⚙️ 谱面同步

启动 MFA 后进入：

`设置 → 演出设置 → 谱面辅助`

可以查看本地谱面清单并手动同步 Bestdori。

相关设计与数据格式见：

[📘 Bestdori 本地谱面仓库说明](docs/bestdori-chart-repository.md)

## 📦 Windows 便携版

维护者可从定制 MFAAvalonia 源码生成不包含本机配置的 Windows x64 发布包：

```powershell
.\scripts\setup.ps1

& '..\.tools\Miniconda3\envs\maabangdream\python.exe' `
  -m pip install -r .\requirements-release.txt

.\scripts\build-windows-release.ps1 -Version 1.3.7
```

发布包必须保持：

- 定制 MFAAvalonia 与对应 MaaFramework Core 一致
- 不携带本机用户配置
- 本地谱面、Agent 与运行时依赖完整
- 便携目录内路径可迁移，不依赖开发机绝对路径

## 🛠️ 源码开发

源码开发需要把两个公开仓库放在同一父目录：

```text
workplace/
├─ MaaBanGDream/
└─ MFAAvalonia/  # fix/native-realtime-ui-toggle
```

定制 MFA 源码：

[coatcn1/MFAAvalonia · fix/native-realtime-ui-toggle](https://github.com/coatcn1/MFAAvalonia/tree/fix/native-realtime-ui-toggle)

> [!WARNING]
> 不要用同版本官方 Core DLL 覆盖定制版本，否则会丢失“演出设置”和启动保护。

准备开发环境并运行验证：

```powershell
.\scripts\setup.ps1
.\scripts\verify.ps1
```

启动 MFAAvalonia：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch-mfa.ps1
```

## 🧭 Roadmap

- [x] Bestdori 本地谱面辅助
- [x] Windows x64 便携发布
- [x] 实时演奏诊断与录像
- [ ] ⚡ Native Realtime Engine V2
- [ ] 🔀 Pure Chart / Hybrid / Visual 多引擎模式
- [ ] 🖥️ 进一步优化低配电脑上的实时演奏性能

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。

开发约定：

- 新功能：`feature/<name>`
- Bug 修复：`fix/<name>`
- 不直接在 `main` 上开发
- 提交前运行：

```powershell
.\scripts\verify.ps1
git status --short
git diff --check
```

详细规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 📚 文档

| 文档 | 说明 |
| --- | --- |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更与项目进度 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [AGENTS.md](AGENTS.md) | AI / Codex 开发上下文 |
| [Bestdori 本地谱面仓库](docs/bestdori-chart-repository.md) | 谱面同步、格式、身份映射与离线门禁 |
| [runtime-compatibility.json](runtime-compatibility.json) | 固定运行时版本组合 |

## ⚠️ 使用须知

- 本项目仍在持续开发，实时演奏效果会受到模拟器性能、截图延迟、游戏设置与设备负载影响
- 使用前请确认模拟器分辨率、DPI 和项目 Profile 与当前环境一致
- 遇到实时演奏异常时，优先保留 Trace、录像和结果报告用于定位
- 请遵守游戏规则及相关服务条款，并自行评估自动化工具的使用风险

## 📝 许可证

本项目使用 [GPL-3.0-only](LICENSE) 许可证。

---

<p align="center">
  <strong>MaaBanGDream</strong><br>
  BanG Dream! automation powered by MaaFramework
</p>
