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
  <a href="https://github.com/coatcn1/MaaBanGDream/releases"><img src="https://img.shields.io/badge/Version-v1.4.8-ff6f9f" alt="Version"></a>
  <img src="https://img.shields.io/badge/MaaFramework-5.10.2-4c8bf5" alt="MaaFramework">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4?logo=windows11&logoColor=white" alt="Windows">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-6f42c1" alt="License"></a>
</p>

---

v1.4.8 纳入已合并修复与 PR #82 / #83 修正版：Native 掉血判定纠偏和等待抖动过滤提供默认关闭的实验开关，诊断写盘与输入回执隔离，挑战生命归零后保留现场。验证范围与限制见[本版本发行说明](docs/release-notes-v1.4.8.md)。

## ✨ 功能

- [x] 🎮 **自动演出** — 当前曲目 / 随机选曲，五档难度，连续运行
- [x] 🎹 **单人实时演奏** — TAP、FLICK、HOLD / Slide、双押与 Special 方向音符
- [x] 👥 **协力演出** — 普通房、好友邀请、私人房间，支持筛歌与同房续演
- [x] 🎼 **Bestdori 本地谱面辅助** — Hard / Expert / Special 谱面提供歌曲结构与时序
- [x] 🔄 **MFA 谱面同步** — 定时增量更新或手动同步，空闲时维护本地曲库
- [x] 🎯 **实时演奏校准** — 一次排练、一次正式验证，生成并选择匹配的 Profile
- [x] 🏆 **挑战演出** — 四档点数、五档难度、连续轮次；生命归零后停止并保留现场
- [x] 🎼 **组曲演奏** — 自由巡演 / 课题巡演，支持断点续跑与逐首结果
- [x] ⏩ **结算容错与可选检查** — 结果读取异常保留完成状态，可跳过成绩数字检查
- [x] 🧪 **调试记录** — 轻度 Trace 或完整记录，关联开演、输入、结算与清理证据
- [x] 📹 **手动流程录像** — 录制界面操作，生成可逐帧定位的 MKV 与首末帧

“演出设置 → Native”提供默认关闭的**判定反馈微调**和**等待抖动过滤**实验开关，下局生效。
正式演奏开始后，微调每 3 秒以 20Hz 采样 0.8 秒，无需掉血；12 秒内三个可信同向窗口建立趋势，
确认生效后按新反馈继续请求每次 2ms 的未来输入调整。生命与终点平时保持约 5Hz，窗口结束额外复核一次；
排练、校准与 Legacy 不使用，本次续调机制仍待真机复验。
Native 停止会确认完整触点释放序列；凌晨 20 局协议释放均完整，已有遗留触点仍需重启模拟器清除。
Native jlog 诊断异步批写，慢盘或写入失败只报告证据缺失；生命截图与短窗判定区域保留帧时序，
便于区分取帧陈旧与游戏判定变化，最新死亡原因仍待真实游戏复验。首音触发时独立保存两帧判定带与门控计算证据，
不依赖调试或微调开关；后台保存失败只报告证据警告，不改变首音锚点。

## 🚀 快速开始

### 普通用户

1. 前往 [Releases](https://github.com/coatcn1/MaaBanGDream/releases) 下载最新的 `MaaBanGDream-v*-win-x64.zip`。
2. **完整解压**，双击 `启动 MaaBanGDream.cmd`。
3. 连接 Android 设备或模拟器，设置为 **1280 × 720、DPI 240**。
4. 实时演奏前按[使用指南](docs/usage.md)配置游戏、校准并选择匹配的 Profile，再添加任务运行。

定制 MFA 默认核验所选雷电的 Windows ADB 端口进程归属；同端口冲突时仅选择可证明归属的地址，
身份不明则拒绝连接。保存的设备序列号保持不变。

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
> 雷电使用 Native 时须显式启用并选择对应 Profile；MuMu 继续使用 Legacy。
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

“演出设置 → 实时演奏 Profile”中，单击行查看和编辑，双击设为当前任务难度的 Profile。
Special 只有确认不可选时才允许显式回退 Expert；实时校准要求精确选择 Special。
实际 Special 缺少可信本地谱面时，不发送演奏触控。

最终封面从第一张有效画面读取并保存实读标题；标题切换为目标得分时，沿用同一封面此前的歌名。
最终封面默认读取完整标题条，避免长标题两端被截断；额外宽框补全与协力准备超时恢复仍为默认关闭的开发候选。
标题匹配容忍同音平片假名混读，长短同前缀标题用完整文字复核歧义；目标得分页保留颜色与数字布局检查并补偿淡入亮度。这些从 [PR #87](https://github.com/coatcn1/MaaBanGDream/pull/87) 拆分吸收的调整目前仅完成离线验证，仍待真机验收。
协力准备页也保留完整标题裁切。《わちゃ・もちゃ・ぺったん行進曲》Expert 已在额外宽框候选关闭时完成单人完整演奏验收；协力房间、其他曲目和难度仍待验收。确认失败时，即使关闭普通录像，也保存决策原图和标题判定记录。
完整曲库唯一封面、标题一致且展示等级差不超过 1 级时允许确认；共享封面、标题冲突或更大差异仍保留保护。
具体任务规则、失败重试与模式验收范围见[使用指南](docs/usage.md)。

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

进入 `设置 → 演出设置 → 谱面辅助`，可查看清单、手动同步或设置定时增量更新。
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
.\scripts\build-windows-release.ps1 -Version 1.4.8
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

最近同曲 Expert / Native 挑战在轻量 Trace 开启时完成 20/20，全部动作执行、触点释放与 jlog 保存完整；11 局 AP，其余 9 局合计 24 GREAT、1 MISS。未达稳定 AP 目标，本批未触发实际掉血纠偏，两项实验继续默认关闭。

最终封面长标题及协力房间恢复修正版已通过替代 PR #82 / #83 合入 main。窄框优先、缺失身份才允许可信连续标题补全；房间准备超时有界恢复，停止优先，完成一局重置连续失败预算。两个进程候选开关默认关闭；随 v1.4.8 打包时，实际长标题与协力恢复尚未完成专项真机验收。
