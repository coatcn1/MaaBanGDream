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

---

v1.5.0 新增交换所抽箱，并改进 Native 反馈与协力首拍识别。
更新内容与验证范围见[发行说明](docs/release-notes-v1.5.0.md)，最新开发记录见 [CHANGELOG](CHANGELOG.md)。

## ✨ 功能

- [x] 🎮 **自动演出** — 当前曲目 / 随机选曲，五档难度，连续运行
- [x] 🎹 **单人实时演奏** — TAP、FLICK、HOLD / Slide、双押与 Special 方向音符
- [x] 👥 **协力演出** — 普通房、邀请好友、私人房间号，支持筛歌与同房续演
- [x] ⚔️ **竞演演出** — 活动 / 自由房型、满员开演及竞演结算
- [x] 🎼 **Bestdori 本地谱面辅助** — Hard / Expert / Special 谱面提供歌曲结构与时序
- [x] 🔄 **MFA 谱面同步** — 定时增量更新或手动同步，空闲时维护本地曲库
- [x] 🎯 **实时演奏校准** — 一次排练、一次正式验证，生成并选择匹配的 Profile
- [x] 🎲 **随机 GREAT（实验）** — 比例与难度加成可配置，默认关闭；见[使用说明](docs/usage.md#随机-great实验)
- [x] 🏆 **挑战演出** — 四档点数、五档难度、连续轮次；生命归零后停止并保留现场
- [x] 🎼 **组曲演奏** — 自由巡演 / 课题巡演，支持断点续跑与逐首结果
- [x] 🔋 **主页体力补充** — 按任务选择小火罐、大火罐或智能补充
- [x] ⏩ **结算容错与可选检查** — 结果读取异常保留完成状态，可跳过成绩数字检查
- [x] 🧪 **调试记录** — 开场截图、轻量 Trace 或完整记录，关联整局证据
- [x] 📹 **手动流程录像** — 录制界面操作，生成可逐帧定位的 MKV 与首末帧
- [x] 🎁 **交换所抽箱** — 默认预览，按抽数或活动币预算规划抽取与换箱

任务配置、演出设置与实验选项见[使用指南](docs/usage.md)。

## 🚀 快速开始

### 普通用户

1. 前往 [Releases](https://github.com/coatcn1/MaaBanGDream/releases) 下载最新的 `MaaBanGDream-v*-win-x64.zip`。
2. **完整解压**，双击 `启动 MaaBanGDream.cmd`。
3. 连接 Android 设备或模拟器，设置为 **1280 × 720、DPI 240**。
4. 实时演奏前按[使用指南](docs/usage.md)配置游戏、校准并选择匹配的 Profile，再添加任务运行。

定制 MFA 默认核验所选雷电的 Windows ADB 端口进程归属；同端口冲突时仅选择可证明归属的地址，
身份不明则拒绝连接。保存的设备序列号保持不变。
MuMu 与雷电双开后连接失效时，可在任务停止后重新连接或刷新设备；已验证路由变化时会重建连接及截图上下文。
任务执行期间不允许手动重连，避免打断正在进行的输入。

> [!IMPORTANT]
> Windows 便携包已经包含定制 MFAAvalonia、MaaFramework、本地谱面、Agent、便携 Python 与 .NET 运行时。<br>
> 普通用户不需要额外安装 Python、Miniconda、.NET 或开发工具。

> [!NOTE]
> 首次启动会在解压目录内展开便携 Python，因此可能比之后稍慢。

程序更新使用 MFA 的 GitHub 更新入口，保留配置、Profile、日志、调试记录和谱面；谱面单独同步。
主分支可能包含尚未发布的修改，随包版本说明见 `resource/Release.md`，近期改动见 [CHANGELOG](CHANGELOG.md)。

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

> [!NOTE]
> 雷电和 MuMu 都可显式选择 Native，并使用匹配的 Profile；按设备分别保留实际演奏证据。
> Native 为实验功能，默认关闭，验收范围见[使用指南](docs/usage.md)。

## 🎼 演奏与谱面辅助

实时演奏由视觉识别、触控规划与本地谱面共同完成，使用与当前设备、流速和引擎匹配的 Profile。

| 能力 | 作用 |
| --- | --- |
| 实时视觉识别 | 检测音符与演奏状态 |
| Bestdori 本地谱面 | 提供歌曲时间轴、长条 / 滑条及方向信息 |
| Profile 校准 | 一次排练收敛时序、一次正式验证后启用 |
| 音符流速复核 | 开启时离开主页前检查并按需修正；关闭时不打开设置页 |
| 最终封面门控 | 以本局封面、实读标题和实际难度确认身份，避免使用旧谱面 |
| 协力成员加载页保护 | 默认开启；表情图标仍在时继续等待最终封面 |
| 结算与调试 | 安全推进结算，保存 PGGBM、Trace 和录像证据 |

在“演出设置 → Profile”查看、编辑和选择当前任务的 Profile。
歌曲身份在准备页与最终封面核验；Special 无可信本地谱面时不发送演奏触控。
任务模式、难度回退、结果检查和故障处理见[使用指南](docs/usage.md)。

流速以外的游戏设置请手动配置：

| 游戏内设置 | 推荐值 |
| --- | --- |
| 镜像 | 关闭 |
| 判定辅助 | 关闭 |
| 连击数量显示 | 开启 |
| 连击数量横向位置 | 右 |
| 连击数量纵向位置 | 上 |
| FAST / SLOW 表示 | 开启 |
| NOTE TYPE | 1 |
| TAP EFFECT | 4 |

### 组曲演奏

自由巡演可用当前曲目或每首随机；课题巡演沿用游戏预设三曲和难度。
次数按歌曲计算，有限次数为 **3–999 且是 3 的倍数**，填 **0** 则无限循环完整三首。
每曲在自己的准备页确认身份，必要时由最终封面补全；三曲 Profile 流速须一致，第三曲后按曲序保存结果。
断点续跑、完整三首重试和成绩缺失处理见[组曲说明](docs/usage.md#组曲)。

> [!TIP]
> 有谱面时读取已校验的**本地谱面**，演奏过程中不联网下载。定时或手动同步只在全部实例没有执行或排队任务时联网。

## ⚙️ 谱面同步

进入 `设置 → 谱面更新`，可查看清单、手动同步或设置定时增量更新。
自动更新默认每 **24 小时**检查，可关闭或设为 **1–720 小时**；失败至少退避 15 分钟，失败或中断保留旧资源。
关闭自动更新不影响手动同步；同音符数的远端谱面修订无法仅靠索引检查发现。

“关于我们 → 清理缓存”清理诊断文件，保留配置、Profile 和谱面；任务或谱面维护期间拒绝清理，并显示实际释放量与残留原因。

详细说明：[📘 Bestdori 本地谱面仓库说明](docs/bestdori-chart-repository.md)。

## 📦 Windows 便携版

维护者可从定制 MFAAvalonia 源码生成不包含本机配置的 Windows x64 发布包：

```powershell
.\scripts\setup.ps1
& '..\.tools\Miniconda3\envs\maabangdream\python.exe' `
  -m pip install -r .\requirements-release.txt
.\scripts\build-windows-release.ps1 -Version 1.5.0
```

发布包须保持定制 MFA 与 Core 一致、依赖完整、路径可迁移，并排除本机用户配置。
安装、更新与本机数据说明见 [Windows 运行包](docs/release-package.md)；发布流程见[发布文档](docs/release-notes-template.md)。

## 🛠️ 源码开发

两个仓库放在同一父目录，定制 MFA 使用 [fix/speed-only-settings](https://github.com/coatcn1/MFAAvalonia/tree/fix/speed-only-settings) 分支：

```text
workplace/
├─ MaaBanGDream/
└─ MFAAvalonia/  # fix/speed-only-settings
```

> [!WARNING]
> 不要用同版本官方 Core DLL 覆盖定制版本，否则会丢失“演出设置”和启动保护。

```powershell
.\scripts\setup.ps1
.\scripts\verify.ps1
.\scripts\launch-mfa.ps1
```

Agent 和资源改动通过启动脚本同步到开发 MFA。开发规则见 [CONTRIBUTING](CONTRIBUTING.md) 和 [AGENTS](AGENTS.md)。

## 🧭 Roadmap

- [x] Bestdori 本地谱面辅助
- [x] Windows x64 便携发布
- [x] 实时演奏诊断与录像
- [ ] ⚡ 完善 Native Realtime Engine V2 的稳定性与模式验收
- [ ] 🔀 探索 Pure Chart / Hybrid / Visual 多引擎模式
- [ ] 🖥️ 进一步优化低配电脑上的实时演奏性能

Native 的设计与验收标准见[引擎目标](docs/realtime-engine-v2-goals.md)，已完成的版本改动见 [CHANGELOG](CHANGELOG.md)。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。提交代码即表示有权提供该内容，并同意按 [PolyForm Noncommercial 1.0.0](LICENSE) 许可贡献；其他授权须另行取得书面同意。

- 新功能使用 `feature/<name>`，修复使用 `fix/<name>`，不直接在 `main` 开发。
- 提交前运行 `scripts/verify.ps1` 和 `git diff --check`，检查工作树及本机数据。
- 详细规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 📚 文档

| 文档 | 说明 |
| --- | --- |
| [使用指南](docs/usage.md) | 游戏设置、任务规则、身份确认与故障处理 |
| [实时演奏 Profile](docs/realtime-profile.md) | 环境匹配、校准与本机数据 |
| [Bestdori 本地谱面仓库](docs/bestdori-chart-repository.md) | 谱面同步、格式与身份映射 |
| [稳定性验收](docs/realtime-stability-validation.md) | Trace、截图基准与离线验证 |
| [Native 引擎](native/realtime/README.md) | 构建、会话边界与执行证据 |
| [CHANGELOG.md](CHANGELOG.md) | 近期变更与历史档案 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [LICENSING.md](LICENSING.md) | 非商业许可、历史版本与第三方边界 |
| [TRADEMARKS.md](TRADEMARKS.md) | 名称与 Logo 使用规则 |
| [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) | 第三方组件、素材与数据权利 |
| [AGENTS.md](AGENTS.md) | 开发约束 |
| [runtime-compatibility.json](runtime-compatibility.json) | 固定运行时版本组合 |

## ⚠️ 使用须知

- 实时效果受模拟器性能、截图延迟、游戏设置与设备负载影响；使用前确认分辨率、DPI 和 Profile 与当前环境一致。
- 异常请保留 Trace、录像和结果报告，提交 [Issue](https://github.com/coatcn1/MaaBanGDream/issues) 时附版本、任务、环境和 run ID，隐去账号及设备信息。
- MFA 与其他自动化工具应避免同时控制同一设备；请遵守游戏规则及服务条款，自行评估使用风险。

## 📝 许可证

从 `v1.4.0` 起，MaaBanGDream 自有部分采用
[PolyForm Noncommercial License 1.0.0](LICENSE)：允许为非商业目的查看、克隆、
运行、研究、修改和分发，但不授权收费软件、收费分发、收费部署或维护、商业服务、
商业产品集成及其他预期商业应用。本项目属于**源码可用（source-available）**，
不再宣称为 OSI 定义下的开源软件。

`v1.3.9` 及更早标签继续适用各版本随附的 GPL-3.0-only 许可证。第三方组件、游戏
素材、谱面及模型继续适用各自权利条款。完整边界见 [LICENSING.md](LICENSING.md)
与 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。

官方项目继续使用 **MaaBanGDream** 名称。公开发布的修改版或派生项目必须使用明显
不同的名称和 Logo，不得利用 MaaBanGDream、官方 Logo 或作者身份宣传收费服务、
暗示官方认可；事实性的讨论、教程、引用和来源说明不受限制。详见
[TRADEMARKS.md](TRADEMARKS.md)。

---

<p align="center">
  <strong>MaaBanGDream</strong><br>
  BanG Dream! automation powered by MaaFramework
</p>
