# MaaBanGDream v1.3.8

本次公告覆盖从 v1.3.7 到 v1.3.8 的全部已合并改动，而不只包含最后提交的组曲演奏。

## ✨ 新功能

- 🎼 新增“组曲演奏”：支持自由巡演与课题巡演，一首歌计一次，输入 3–99 且必须为 3 的倍数。自由巡演支持当前曲目或每首随机并允许歌曲重复；课题巡演读取页面预设歌曲和难度。
- 💾 组曲使用独立原子会话记录三首歌的身份、Profile、流速、进度和延迟结算状态。第 2/3 曲及结算阶段只有在现场与会话一致时才续跑；每曲使用独立 run ID，并分别保存 PGGBM 与 timing offset。
- 🎼 完整支持 Special 谱面的 Directional Left/Right 与 `width=1..7` 语义。单人、挑战和协力会贯穿记录请求难度与实际难度；允许的模式在 Special 不可选时显式回退 Expert，实际 Special 则必须在触控前确认可信本地谱面。
- 🎧 “一键实时演奏”改为等待歌曲开场封面与标题，确认本地曲目后按任务所选难度演奏一首，并在确认完成后自动结束，不再从歌曲中途误启动或持续监听下一首。

## ⚙️ 演出流程调整

- 🎛️ 游戏设置自动化收缩为仅检查音符流速：开关开启时，每次任务在主页读取、按需修正并复核；关闭时不截图、不读取、不点击设置页。组曲中途及其他模式准备页都不会补做设置检查。
- 🧩 旧 Profile 和校准会话中的演出效果字段继续兼容读取，但不再参与 Profile 匹配或续跑判定。
- 🤝 协力准备阶段复用本轮未改动的准备页截图，保留难度点击送达、准备按钮消失、成员退出和开演转场门禁；雷电实测从难度点击到“演出开始”的平均耗时由约 3.57 秒降至约 0.59 秒。
- 🪟 协力“其他成员正在准备中”检测改按固定缩放中心识别，兼容弹窗不出现、只闪一帧或完整出现，并排除判定线附近白底粉色双 FLICK 对首音门控的干扰。

## 🐛 修复

- 🪟 组曲第一、二曲后会识别“达成报酬一览”弹窗并只发送一次 Android BACK；第三曲后识别组曲分数汇总页，等待页面真实离开后再读取三张 PGGBM，避免连续 BACK 跳过结果页或在主页触发退出确认。
- 🔁 已知结算页使用 Android BACK 与可见右下角按钮推进；持续未知页面交替尝试两种输入，每次重新识别。达到有界次数仍无法恢复时保存现场并调用 `CommonRecover`，必要时重启一次游戏回主页，同时保留任务失败事实。
- ✅ 修复组曲次数选项覆盖不完整、导致失败分支被错误报告为成功的问题。
- 🔎 一键演奏新增有界识别诊断，并修复难度与 Trace 选项整块覆盖、无参数节点收到 JSON `null`、任务完成后重新监听，以及 `Little Busters!` 实拍封面被严格 8 bit 门槛拒绝的问题。宽松封面匹配只有在标题已独立收窄候选后才启用。
- 🎯 修复 Special→Expert 回退后的 Profile、谱面、流速门禁和结果证据仍可能使用请求难度的问题；实际难度现在贯穿完整演奏链路。
- 📦 runtime-free 更新 ZIP 改为直接在归档根目录提供 `interface.json`、`resource/` 和程序文件；完整安装包继续保留版本目录外壳，修复更新下载及校验成功后仍提示缺少 `interface.json` 的问题。
- 📢 完整包和更新包都会携带当前版本的 `resource/Release.md`，客户端显示公告和更新完成弹窗读取本地文件，不再为了公告正文访问 GitHub。
- 🖥️ 发布构建脚本使用兼容 Windows PowerShell 5.1 的 UTF-8 BOM，并验证 ZIP 中央目录的 UTF-8 标记，修复中文启动器文件名在构建或解压后损坏的问题。

## 📚 文档与维护

- 🕹️ README 补充雷电模拟器 9.5.30.1 下载入口；由于当前 MuMu 实机样本较少，实时演奏优先推荐已有更多验证记录的雷电模拟器。
- 🖼️ 开发文档明确按任务复杂度选择图片分析工具：普通附图可直接分析，简单 OCR/批量初筛可使用 vision，复杂页面、空间关系和交互定位允许优先使用原生图片查看或 Computer Use。

## 📦 更新方式

- 🔄 v1.3.7 可通过 MFA 原生更新入口检查并下载 v1.3.8；已有 Python runtime 时优先使用较小的 `-update.zip`，运行库缺失时使用完整包。
- 📁 默认版本目录更新后改名为 `MaaBanGDream-v1.3.8-win-x64`；自定义目录名保持不变。
- 🎼 本地谱面继续通过“演出设置 → 谱面辅助 → 同步”独立维护，不进入常规更新包。

## ✅ 验证

- 🧪 主项目最终完整验证：`1100 passed / 6 skipped`；固定 Python 3.12、MaaFw 5.10.2、.NET Binding 5.8.0 与定制 MFAAvalonia 2.12.0 兼容检查通过。
- 📦 Native C++：`1827 checks passed`。完整包和 runtime-free 更新包均通过归档结构、中文文件名、隐私路径、本机状态、更新包排除项及 SHA-256 校验。
- 🎮 Special：雷电 Native 单人五局均为 MISS 0，GREAT 依次为 4/0/11/5/1；其中两张谱面覆盖 Left/Right Directional 与 `width=1..3`。协力 Special 与新的协力首音门控仍保留独立真机验收要求。
- 🎮 一键实时演奏：用户已完成本轮开场识别与单曲结束行为复测并同意提交。
- 🎮 组曲：雷电 Native 已完成一组自由巡演“当前曲目”闭环，三首均完成、三张 PGGBM 均保存并回到主页，任务以 3/3 成功结束；另一次课题巡演续跑完成剩余第 2/3 曲。
- 🎮 组曲分数汇总页识别与通用未知页面兜底使用当前实拍模板完成自动化与开发部署验证，尚未再次完整跑完最终结算。

## 🔒 已知限制

- ⚠️ 自由巡演随机歌曲、课题巡演完整新开局、多组计数及不同中断阶段仍需分别积累真机验收。
- ⚠️ 协力 Special→Expert 回退和新的首音弹窗门控不能由单人结果外推为已验收。
- ⚠️ MuMu Native 仍受逐局变化的客户机时钟偏斜影响，当前建议雷电使用 Native、MuMu 使用 Legacy。

## 📝 v1.3.7 以来的合并提交

- [`a15c956`](https://github.com/coatcn1/MaaBanGDream/commit/a15c9564a2029de0bd531f251a365bd0dfcaa7c6) — [#49](https://github.com/coatcn1/MaaBanGDream/pull/49) `fix(release): flatten runtime-free update archive`
- [`232030a`](https://github.com/coatcn1/MaaBanGDream/commit/232030aeaf84d257d721d6c0b090edbc6ca74c26) — [#50](https://github.com/coatcn1/MaaBanGDream/pull/50) `fix: package local release notes`
- [`92befb6`](https://github.com/coatcn1/MaaBanGDream/commit/92befb6f052c03a5594bf31acfaa3ce29758211f) — [#51](https://github.com/coatcn1/MaaBanGDream/pull/51) `fix: preserve release launcher filename on Windows PowerShell`
- [`ff7a344`](https://github.com/coatcn1/MaaBanGDream/commit/ff7a344439757117d844c6bf93994a692ffc8b27) — [#53](https://github.com/coatcn1/MaaBanGDream/pull/53) `feat: add Special support and one-key live startup recognition`
- [`7c0e585`](https://github.com/coatcn1/MaaBanGDream/commit/7c0e58517f67b3f00bc834e37807261ee284ec9b) — [#54](https://github.com/coatcn1/MaaBanGDream/pull/54) `feat: add medley live mode`

完整逐日记录见 [`CHANGELOG.md`](https://github.com/coatcn1/MaaBanGDream/blob/main/CHANGELOG.md)。
