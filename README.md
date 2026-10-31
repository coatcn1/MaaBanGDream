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
  <a href="https://github.com/coatcn1/MaaBanGDream/releases"><img src="https://img.shields.io/badge/Version-v1.5.0-ff6f9f" alt="Version"></a>
  <img src="https://img.shields.io/badge/MaaFramework-5.10.2-4c8bf5" alt="MaaFramework">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4?logo=windows11&logoColor=white" alt="Windows">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-6f42c1" alt="License"></a>
</p>

v1.5.0 新增交换所抽箱，并改进 Native 反馈与协力首拍识别。更新内容见[发行说明](docs/release-notes-v1.5.0.md)，开发变更见 [CHANGELOG](CHANGELOG.md)。

## ✨ 功能

- 🎮 **自动演出** — 当前曲目或随机选曲，五档难度，支持连续运行
- 🎹 **单人实时演奏** — TAP、FLICK、HOLD / Slide、双押与 Special 方向音符
- 👥 **协力演出** — 普通房、好友邀请、私人房间，支持筛歌与同房续演
- ⚔️ **竞演演出** — 活动 / 自由房型、满员开演及竞演结算
- 🎼 **Bestdori 本地谱面辅助** — Hard / Expert / Special 谱面结构与时序
- 🔄 **MFA 谱面同步** — 定时增量更新或手动同步，空闲时维护本地曲库
- 🎯 **实时演奏校准** — 一次排练、一次正式验证，生成匹配的 Profile
- 🎲 **随机 GREAT（实验）** — 比例与难度加成可配置，默认关闭
- 🏆 **挑战演出** — 四档点数、五档难度，支持连续轮次
- 🎼 **组曲演奏** — 自由巡演 / 课题巡演，支持断点续跑与逐首结果
- 🔋 **主页体力补充** — 按任务选择小火罐、大火罐或智能补充
- ⏩ **结算容错与可选检查** — 结果读取异常保留完成状态，可跳过成绩数字检查
- 🧪 **调试记录** — 开场截图、轻量 Trace 或完整记录，关联整局证据
- 📹 **手动流程录像** — 录制界面操作，生成可逐帧定位的 MKV 与首末帧
- 🎁 **交换所抽箱** — 默认预览，按抽数或活动币预算规划抽取与换箱

## 🚀 快速开始

1. 前往 [Releases](https://github.com/coatcn1/MaaBanGDream/releases) 下载最新 Windows x64 完整包。
2. **完整解压**，双击 `启动 MaaBanGDream.cmd`。
3. 连接 Android 设备或模拟器，设置为 **1280 × 720、DPI 240**。
4. 按[使用指南](docs/usage.md)配置游戏，实时演奏前完成校准并选择匹配的 Profile，再添加任务运行。

> [!IMPORTANT]
> 完整包已包含运行环境，无需额外安装 Python、Miniconda 或 .NET；首次启动会展开便携 Python，请耐心等待。

安装、更新与本机数据说明见 [Windows 运行包](docs/release-package.md)。

## 🖥️ 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11 x64 |
| Android 画面 | 1280 × 720，DPI 240 |
| 演出环境 | 帧率、画质、流速及引擎与所选 Profile 一致 |

Native 为默认关闭的实验功能；雷电和 MuMu 须分别使用匹配的 Profile。固定运行时组合见 [runtime-compatibility.json](runtime-compatibility.json)。

## 📚 文档

| 文档 | 内容 |
| --- | --- |
| [使用指南](docs/usage.md) | 游戏设置、任务配置、随机 GREAT 与故障处理 |
| [实时演奏 Profile](docs/realtime-profile.md) | 环境匹配、校准与本机数据 |
| [Bestdori 本地谱面仓库](docs/bestdori-chart-repository.md) | 谱面同步、格式与身份映射 |
| [Windows 运行包](docs/release-package.md) | 安装、更新与数据保留 |
| [稳定性验收](docs/realtime-stability-validation.md) | Trace、截图基准与离线验证 |
| [Native 引擎](native/realtime/README.md) | 构建、会话边界与执行证据 |
| [CHANGELOG](CHANGELOG.md) | 版本变更、验证记录与历史档案 |

## 🛠️ 源码开发

- **开发与构建** — 环境准备、启动命令、验证及打包步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- **项目约束** — 开发门禁与文档边界见 [AGENTS.md](AGENTS.md)。
- **定制界面** — 配套 [MFAAvalonia 的 fix/speed-only-settings 分支](https://github.com/coatcn1/MFAAvalonia/tree/fix/speed-only-settings)，与本项目放在同一父目录。

## 🧭 后续计划

- [ ] ⚡ 完善 Native 引擎稳定性与各模式验收
- [ ] 🔀 探索 Pure Chart / Hybrid / Visual 多引擎模式
- [ ] 🖥️ 优化低配电脑的实时演奏性能

具体目标见[引擎设计与验收标准](docs/realtime-engine-v2-goals.md)。

## ⚠️ 使用须知

- 实时效果受设备性能、截图延迟和游戏设置影响，使用前确认环境与 Profile 一致。
- 异常请保留版本、任务、环境及 run ID，提交 [Issue](https://github.com/coatcn1/MaaBanGDream/issues) 前隐去账号和设备信息。
- 避免多个自动化工具同时控制同一设备，并自行评估游戏规则及服务条款下的使用风险。

## 📝 许可证

自有代码采用 [PolyForm Noncommercial 1.0.0](LICENSE)，属于**源码可用（source-available）**，允许非商业用途，不授权收费软件、收费分发、收费部署或维护等商业用途。
历史版本与第三方边界见 [LICENSING.md](LICENSING.md) 和 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。

公开修改版须使用明显不同的名称和 Logo，不得暗示官方认可；规则见 [TRADEMARKS.md](TRADEMARKS.md)。

<p align="center">
  <strong>MaaBanGDream</strong><br>
  BanG Dream! automation powered by MaaFramework
</p>
