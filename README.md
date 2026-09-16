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
  <a href="https://github.com/coatcn1/MaaBanGDream/releases"><img src="https://img.shields.io/badge/Version-v1.4.2-ff6f9f" alt="Version"></a>
  <img src="https://img.shields.io/badge/MaaFramework-5.10.2-4c8bf5" alt="MaaFramework">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4?logo=windows11&logoColor=white" alt="Windows">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-6f42c1" alt="License"></a>
</p>

---

## ✨ 功能

- [x] 🎮 **自动演出** — 当前曲目 / 随机选曲，五档难度，支持 1–99 轮连续执行
- [x] 🎹 **单人实时演奏** — 支持 TAP、FLICK、HOLD、Special 左右 Directional、双押、长条配对、判定线补救与残影抑制
- [x] 👥 **协力演出** — 普通四档房、好友邀请或六位私人房间号入房，支持同房续演；跳过设置页时复用最新准备页画面以缩短难度选择到“演出开始”的等待，同时保留点击送达确认
- [x] 🎼 **Bestdori 本地谱面辅助** — 809 首歌曲的 Hard / Expert / Special 谱面，谱面主导时序并由视觉持续校准
- [x] 🔄 **MFA 谱面同步** — 在“演出设置 → 谱面辅助”中手动增量同步 Bestdori，CN 封面缺失时依次回退 JP / EN
- [x] 🎯 **实时演奏校准** — 一次排练收敛时序、一次正式验证，生成 Profile 后启用
- [x] 🏆 **挑战演出** — 四档点数、五档难度、连续轮次
- [x] 🎼 **组曲演奏** — 按输入的 3 倍数完成自由巡演或课题巡演；每曲在自己的准备页识别，必要时由最终封面补全，支持断点续跑和逐首判定保存
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
“关于我们”采用居中项目卡片，展示专用 v1 Logo、介绍与项目链接；Logo、联系方式和许可证使用 MFA 原生玻璃卡片并随主题透明度变化，软件标志保留默认 Logo，相关素材随开发部署和发布包一起提供。“显示公告”读取当前安装版本随包携带的 `resource/Release.md`，不再访问 GitHub；下载继续使用原生进度提示，成功更新后展示同一份本地更新日志。
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

> [!NOTE]
> 实时演奏目前在 MuMu 模拟器上的测试样本较少，推荐使用已进行较多真机验证的
> [雷电模拟器 9（9.5.30.1）](https://ldstore.ldmnq.com/mngt/apk/arknights-ldinstaller-9.5.30.1.exe)。

## 🎼 演奏与谱面辅助

MaaBanGDream 的实时演奏并不是简单的固定坐标点击，而是由视觉识别、触控规划与本地谱面共同完成。

| 能力 | 作用 |
| --- | --- |
| 实时视觉识别 | 检测 TAP / FLICK / HOLD 等音符及演奏状态 |
| Bestdori 本地谱面 | 提供歌曲结构、时间轴和长条 / 滑条信息 |
| 谱面辅助 | 由谱面提供时序先验；Legacy 可用 TAP / FLICK / SKILL 投影及多节点 HOLD / Slide 头与换轨拓扑完成锁相 |
| Profile 校准 | 针对当前模拟器与游戏设置生成匹配参数 |
| FAST / SLOW 反馈 | 用于实时 Timing 调整与结果分析 |
| 音符流速复核 | 开关关闭时永不进入游戏设置页；开启时每次任务在离开主页前只读取、按需修正并复核音符流速，准备页不再打开设置 |
| 正式演出预检查 | 先把准备页的 `3D演出 / 动画MV` 循环切换到 `OFF`，再读取并按需关闭随后出现的“3D Cut in模式”，避免 3D 状态下把成员头像误当成复选框 |
| 调试 Trace / 录像 | 同一 run ID 关联准备证据、最终封面、演奏场门控、触控引擎、结算、清理、降级与重试决定；Native 首次快速掉血时异步保留前后约两秒、最多 21 张已有监控截图，辅助区分游戏判定异常与输入问题；协力准备后最多观察 60 秒并记录转场证据 |
| 最终封面门控 | 准备页歌曲标题、封面或等级缺失/冲突时，单人和组曲不提前结束，而是延迟预武装，用本局开场封面和实际标题重新确认；仍无法确认则不发送演奏触控。协力保留既有开场确认与 Legacy 降级规则；Special 缺少可信本地谱面时不盲打 |
| 有界失败重试 | “演出设置 → 任务安全”可设置 0–3 次；普通单人、校准与协力每次重试前都会释放会话并恢复到已识别页面；挑战演出不自动重试 |
| 生命终态监控 | 数值生命只用于确认演奏场、识别生命归零和触发协力跳车，不再提供低血量提前暂停 |
| 协力跳车 | 协力生命归零时停止演奏、确认 Native 触点已释放并切回游戏；若开演黑场漏检，则不启动引擎，只监控生命并在归零或超时后退到桌面再切回游戏。随后结束任务并提示用户手动断网跳车；Maa 不自动修改模拟器网络 |
| 协力准备恢复 | 入房后独立等待“不指定歌曲”最多 180 秒，点击后独立等待准备页 60 秒；前一阶段超时会退到桌面再切回游戏并结束任务 |
| 组曲演奏 | 自由巡演的歌曲选择页只选择难度，不读取歌曲身份；每曲进入自己的准备页后再识别，必要时由最终封面与实际标题补全。课题巡演只读取预设三曲和难度。三曲共用一个流速，第三曲后依次读取三张 PGGBM 并分别保存结果 |
| Native V2（实验） | 默认关闭；先同时确认生命条与七轨判定标记，再使用时间制首音门控，禁止加载/歌曲信息/演奏场淡入冒充首音；协力按固定缩放中心识别“其他成员正在准备中”，兼容不出现、只闪一帧或完整出现，并排除判定线附近的白底粉色双 FLICK；速度 5.0 的首音检测带补偿采用真机录像基线，设备触控固定落在 `y=590` 判定线 |

“演出设置 → 实时演奏 Profile”中，Expert 与 Special 处于同一兼容等级，并都可兼容较低难度任务。单击表格行用于查看和编辑，双击可设为当前任务难度的 Profile；当前项用随主题变化的强调色单独标记。协力、单人实时、挑战或自动演出选择 Special 时，如果当前歌曲没有可选的 Special 按钮，会显式回退 Expert 并按 Expert 的 Profile 与谱面继续；实时校准仍要求实际选中 Special。

Special 的难度策略不会静默冒充：按钮可选时必须实际选中 Special；按钮不可选时，协力、单人实时、挑战和自动演出才会显式回退 Expert。实时演奏与挑战会在日志和结果中同时保留请求难度和实际难度；实际 Special 必须命中可信本地谱面，Legacy 与 Native 都按谱面恢复 Left / Right，`width=1..7` 作为轨道跨度证据保留。雷电 Native 单人真机已覆盖普通 Special 与包含 Left/Right、`width=1..3` 的 Directional 谱面；新的单人/挑战回退和协力开演门控仍应在真实演出中分别验收。

Native 等待成本与启动延迟补偿已通过雷电真机验收，开发环境普通运行 `scripts/launch-mfa.ps1` 即会启用；仅在回归排查时使用 `-DisableNativeTimingCompensation` 临时关闭。正式安装目录不用于部署测试代码。

“演出设置 → 流速”中只保留“开演前自动设置并验证流速”。关闭时程序永不进入游戏设置页，直接信任各难度的目标流速；开启时，每次单人、协力、挑战和校准任务都会先在主页真实进入设置页，只读取、按需修正并复核音符流速，不保存跨任务跳过凭据。“一键实时演奏”在监听和开演前不会主动导航，开关开启时要求最近 15 分钟内已有流速读回；启动后等待开场封面与标题确认本地曲目，再按任务所选难度演奏一首，读取 PGGBM 并恢复主页后自动结束任务。Easy/Normal 等没有本地谱面的难度会整局使用视觉 Legacy。

所有演出任务在演出结束后统一循环“点击最右下角安全像素 `(1279,719)` → Android BACK → 再点击同一安全像素”。安全像素只用于加快页面动画，BACK 是唯一推进页面的输入；程序不会点击奖励、排名、活动、确定或下一步等可见按钮。普通演出读取一张 PGGBM，组曲按照会话断点读取总计三张，并在每次安全像素或 BACK 后先截图，防止越过刚出现的 PGGBM；之后只在最终剧情或主页等终点执行必要识别。歌曲身份沿用开演前及最终封面的确认结果，结算不再重新 OCR 标题、难度或等级来否决已完成的演奏；PGGBM 只用于读取稳定判定数字，组曲按第 1→2→3 首顺序保存。

其余演出设置不再由 MaaBanGDream 检查或修改，请用户在游戏内手动设为：

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

旧 Profile 与旧校准会话中的 `note_skin_type`、`tap_effect`、`judgement_assist_effect` 字段仍可读取，但只作为旧格式兼容数据保留，不再参与 Profile 匹配或校准续跑判定。

### 组曲演奏

“组曲演奏”使用正式实时演奏和现有 Profile，不提供排练模式。次数按歌曲计算：一首歌记 1 次；输入范围为 3–99 且必须是 3 的倍数，每完成一组三首并读取三张判定页后回主页开始下一组。当前不支持在第 1/2 首后把未完成的一组计为成功。

- **自由巡演**：选择“当前曲目”或“每首随机”，并设置统一难度。程序先确认每个槽位的歌曲选择页已连续稳定出现，点击未送达时最多重试三次；页面未确认前不发送随机或难度输入。选曲页只点击并复核难度，不读取标题、封面、等级或歌曲身份；连续选择到同一首歌是合法结果，不会被重复门禁提前拒绝。
- **课题巡演**：游戏已预设三首歌及难度，程序只读取封面、标题、等级和难度，不会点击修改。三首对应 Profile 的流速必须一致；流速自动检查开启时，会先读取阵容、回主页检查一次流速，再重新进入并确认阵容没有变化。
- **身份与演奏**：每曲先在自己的准备页读取封面、标题、等级和实际难度，并把身份原子写入组曲会话；准备页没有可信标题或完整身份时，点击开始后由最终封面与该页实际 OCR 标题补全。最终仍无法确认时会在发送演奏触控前停止。每首使用独立 run ID。
- **结算**：第三首完成后不区分组曲分数汇总、奖励、排名等中间页，只按统一安全像素/BACK节拍推进并依次读取三张 PGGBM，分别写入结果并沿用正式演奏的 timing offset 回写规则。不会点击任何可见按钮；超过有界次数后调用 `CommonRecover`，必要时重启游戏恢复主页。
- **续跑与启动恢复**：第 2/3 曲准备页只会在本地组曲会话与页面身份一致时继续；其余非主页页面会先按安全像素/BACK 节拍返回主页，再开始任务。会话缺失、选项变化或歌曲冲突均会停止输入。若第三曲后停止任务并由用户手动离开尚未收全的结算，缺失的 PGGBM 无法补读，旧会话会保留为不可恢复记录，下一次任务从第一曲开始新一组，不会被旧会话阻塞或把缺失结果算作完成；若三张均已原子保存，则只补记上一组完成状态，不会重打。会话单独保存在 `profiles/medley-sessions/`，不会改变旧 Profile、旧校准文件或校准会话的读取规则。

组曲中途不会打开设置页，也不会自动选择“休息”。请先为可能出现的三档实际难度选择已验收 Profile，并按上方推荐值手动配置流速以外的游戏内演出设置。

2026-09-14 已在雷电 Native 验收自由巡演“当前曲目”单组：三首相同歌曲均完成、三张判定页均保存并回到主页。2026-09-15 已验收课题巡演从第 2 曲断点续跑至 `3/3` 成功，以及普通非主页启动先恢复主页再开始新一组。随机歌曲和多组计数仍需分别验收。

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

.\scripts\build-windows-release.ps1 -Version 1.4.2
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
└─ MFAAvalonia/  # fix/speed-only-settings
```

定制 MFA 源码：

[coatcn1/MFAAvalonia · fix/speed-only-settings](https://github.com/coatcn1/MFAAvalonia/tree/fix/speed-only-settings)

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

欢迎提交 Issue 和 Pull Request。提交代码即表示贡献者有权提供该内容，并同意按
[PolyForm Noncommercial 1.0.0](LICENSE) 向项目用户许可该贡献；需要其他授权时会
另行取得贡献者的书面同意。

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
| [LICENSING.md](LICENSING.md) | 非商业许可范围、历史版本与第三方边界 |
| [TRADEMARKS.md](TRADEMARKS.md) | MaaBanGDream 名称与 Logo 使用规则 |
| [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) | 第三方组件、素材和数据权利说明 |
| [AGENTS.md](AGENTS.md) | AI / Codex 开发上下文 |
| [Bestdori 本地谱面仓库](docs/bestdori-chart-repository.md) | 谱面同步、格式、身份映射与离线门禁 |
| [runtime-compatibility.json](runtime-compatibility.json) | 固定运行时版本组合 |

## ⚠️ 使用须知

- 本项目仍在持续开发，实时演奏效果会受到模拟器性能、截图延迟、游戏设置与设备负载影响
- 使用前请确认模拟器分辨率、DPI 和项目 Profile 与当前环境一致
- 遇到实时演奏异常时，优先保留 Trace、录像和结果报告用于定位
- 请遵守游戏规则及相关服务条款，并自行评估自动化工具的使用风险

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
