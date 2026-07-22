# MaaBanGDream

基于 MaaFramework 的《BanG Dream! 少女乐团派对！》自动化项目。

- 当前开发版本：`0.2.0`
- 最新已发布版本：`v0.1.0` 开发预览
- 项目仓库：https://github.com/coatcn1/MaaBanGDream

## 当前能力

- Project Interface V2，可由 MFAAvalonia 加载。
- MaaFramework Core 与 Python Agent 固定为 5.10.2。
- 页面连通性测试：`主页 → 演出 → 自由演出 → 选曲页 → 主页`。
- 单轮自动演出：使用游戏当前选中的歌曲，确认自动演出已开启后开始，完成结算并返回主页。
- 自动演出次数耗尽时停止任务，不点击开关或开始按钮。
- `CommonRecover` 可优先点击安全节点，随后按 1.5 秒间隔发送 BACK；超时后最多重启游戏两次。
- 登录提示、通用关闭按钮、结算、奖励和剧情跳过处理。
- Pipeline 契约、PNG 完整性与来源哈希、恢复边界和运行时版本组合检查。

当前不支持指定歌曲、多轮重演、Profile、挑战演出、每日调度或实时演奏。旧项目的 Electron、PyWebIO 和自建调度器不在迁移范围内。

## 环境要求

- Windows 10/11
- Python 3.12
- MaaFramework Native Core / Python binding 5.10.2（PyPI 包名 `MaaFw`）
- MFAAvalonia 2.12.0、.NET Binding 5.8.0
- .NET Desktop Runtime 10
- Android 设备分辨率 `1280×720`、DPI `240`

已验证的精确组合记录在 `runtime-compatibility.json`。任一组件版本变化都必须重新执行自动检查和真机验收，不能只依据主版本相同推断兼容。

## 安装与验证

```powershell
.\scripts\setup.ps1
.\scripts\verify.ps1
```

完整运行时检查需要提供本机 MFAAvalonia 目录：

```powershell
.\.venv\Scripts\python.exe scripts\check_runtime.py --mfa-root <MFAAvalonia目录>
```

把 `interface.json` 和 `resource` 部署到 MFAAvalonia 项目目录后即可启动。发布前还必须完成连接、截图、点击、BACK、应用启停、相关页面闭环及停止任务安全性真机验收。本机 ADB 路径、设备序列号、日志、截图、Profile、虚拟环境和 MFAAvalonia 运行目录均不得提交。

## 项目进度

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| 独立 MaaFramework 项目 | 已完成 | 与旧 BDAS 工作树分离，不带入旧仓库脏改动 |
| 基础运行环境 | 已完成 | Python 3.12、MaaFw 5.10.2、MFAAvalonia 2.12.0、.NET 10 |
| 运行时兼容门禁 | 已完成 | 锁定 Python、Core、MFA、.NET Binding 与 PI 组合 |
| 最小页面闭环 | 已完成 | 已在真实雷电模拟器验收 |
| 故障恢复 | 已完成 | 已覆盖安全节点、BACK、停止检测及重启上限 |
| GitHub 首次预发布 | 已完成 | 公开发布 `v0.1.0` prerelease |
| 单轮自动演出 | 已完成 | 当前歌曲、单轮、结算后返回主页；已通过关闭与开启状态真机验收 |
| 指定歌曲与多轮 | 未开始 | 下一里程碑 |
| 实时演奏 | 未开始 | 独立 Python Agent 里程碑 |
| Profile 系统 | 未开始 | 配置校验及本机数据隔离 |
| 挑战演出 | 未开始 | 在自动演出基础上实现 |
| 每日调度 | 未开始 | 最后接入，不迁移旧调度器 |

## 进度与变更记录

### 2026-07-22

- 在 `feature/auto-live-foundation` 分支新增当前歌曲单轮自动演出 Pipeline。
- 从旧仓库 `HEAD` 导入准备、自动演出状态、开始、结算、奖励和剧情模板，并记录 SHA-256 来源清单；未复制旧仓库工作区修改。
- 开始按钮只能从“自动演出已开启”节点到达；耗尽状态优先并直接停止；关闭状态最多尝试三次。
- 演出结果等待上限设为 300 秒，正常演出等待不会触发 60 秒未知页恢复。
- 扩展 `CommonRecover`，支持安全节点点击、逐次新截图和每项控制操作前的停止检测。
- 新增自动演出 Pipeline 契约、模板来源哈希和停止安全性测试；当前共 15 项自动测试。
- 修正选曲确认点击区域，并在真实雷电模拟器完成两轮验收：自动演出关闭时先开启再开始，已开启时直接开始；两轮均识别结算并返回主页。
- 验收日志确认演出等待期间没有 BACK，BACK 仅在结算恢复阶段发出；停止失败验收后没有继续控制游戏。
- 次数耗尽状态未在真机构造，本版本仅通过来源模板回放约束和 Pipeline 契约验证，发布说明保留此限制。
- 新增 v0.2.0 发布说明。
- 新增精确运行时组合检查并接入 `scripts/verify.ps1`。

### 2026-07-21

- 从旧项目迁出独立 MaaFramework 工程并建立 `main` 分支。
- 安装并验证 Python 3.12、MaaFw 5.10.2、MFAAvalonia 2.12.0 和 .NET 10。
- 完成 Project Interface V2、Python Agent、Pipeline、最小页面闭环和基础 `CommonRecover`。
- 创建公开 GitHub 仓库并发布 `v0.1.0` 开发预览。
- 项目由 `BDAS-Maa` 统一更名为 `MaaBanGDream`，通过 PR #1 合并。
- 将 README 确立为项目进度、修改和后续计划的统一记录入口。

## 后续计划

1. 指定歌曲与多轮自动演出
   - 提供歌曲选择和次数参数。
   - 处理重复演出、次数不足及中途失败的可恢复状态。
2. 实时演奏 Agent
   - 重新评估识别、输入延迟、触点释放和停止后的清理保证，不直接照搬旧实现。
3. Profile 系统
   - 定义可版本化 schema，本机账户、设备和敏感配置继续保持忽略。
4. 挑战演出
   - 复用自动演出能力，补充挑战专属页面和恢复流程。
5. 每日调度
   - 使用 MaaFramework 任务组合重新实现，不迁移旧 Electron/PyWebIO 调度器。

以上各项分别使用独立 `feature/*` 分支和 Pull Request，不直接堆入 `main`。

## README 更新规则

以后每次代码修改都必须同步维护本文件：

1. 开始新里程碑时更新“项目进度”和“后续计划”。
2. 功能、修复、依赖或运行方式变化时更新对应章节。
3. 提交前在“进度与变更记录”顶部追加实际完成内容。
4. 只把已完成且验证过的结果标记为完成，未完成内容保留在计划中。
5. README 未同步、测试未通过或真机门禁未满足时，不提交、不合并、不发布。

## Git 工作流

- 功能使用 `feature/*` 分支，修复使用 `fix/*` 分支。
- 分支推送后先创建 Draft PR；检查通过后转为 Ready，再 Squash Merge 到 `main`。
- 提交前执行：

```powershell
git status --short
git diff --check
.\scripts\verify.ps1
```

- 发布前核对 `git diff main...HEAD`、提交历史、远端文件清单和敏感信息扫描。
- 只有验证通过且工作树干净时，才允许合并到 `main`、创建 annotated tag 和 prerelease。

详见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [发布检查清单](docs/release-checklist.md)。

## 许可证

本项目采用 GPL-3.0-only。
