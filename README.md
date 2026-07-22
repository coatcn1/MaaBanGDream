# MaaBanGDream

基于 MaaFramework 的 BanG Dream! 自动化项目。

- 当前版本：`v0.1.0` 开发预览
- 当前阶段：MaaFramework 最小页面闭环已完成，自动演出与实时演奏尚未迁移
- 项目仓库：https://github.com/coatcn1/MaaBanGDream

## 项目目标

将原 BanGDreamAutoScript 中仍有价值的能力逐步迁移到 MaaFramework，并使用 MFAAvalonia 作为通用图形界面。迁移采用小步验证方式，每项功能在独立 Git 分支完成，测试和真机验收通过后才允许合并与发布。

旧 Electron、PyWebIO 和自建调度器不在迁移范围内。本机 ADB 路径、设备序列号、日志、截图、Profile、虚拟环境和 MFAAvalonia 运行目录不会提交到仓库。

## 当前已实现

- Project Interface V2，可由 MFAAvalonia 加载。
- MaaFramework Core 与 Python Agent 版本固定为 5.10.2。
- 最小页面闭环：
  `HomeMarker → HomeLive → FreeLive → SongSelectMarker → BackToHome`
- 登录提示、通用关闭按钮和游戏启动处理。
- `CommonRecover` 故障恢复：
  - 未知页面持续 60 秒后开始恢复。
  - 每 1.5 秒发送一次 BACK，最多持续 30 秒。
  - 仍无法返回首页时重启游戏，最多重启两次。
- Pipeline 契约、资源完整性和恢复边界测试。
- 可重复的 MaaFramework/MFAAvalonia 版本组合检查。
- 环境安装、验证脚本及 Git 提交规范。

## 环境要求

- Windows 10/11
- Python 3.12
- MaaFramework Native Core / Python binding 5.10.2（PyPI 包名 `MaaFw`）
- MFAAvalonia 2.12.0（.NET Binding 5.8.0）
- .NET Desktop Runtime 10
- Android 设备分辨率 `1280×720`、DPI `240`

## 安装与验证

```powershell
.\scripts\setup.ps1
.\scripts\verify.ps1
```

将 `interface.json` 和 `resource` 复制到 MFAAvalonia 运行目录后启动 MFAAvalonia。发布前还必须完成真机连接、截图、点击、BACK、应用启停、最小页面闭环和故障恢复验收。

完整运行时版本检查需要显式提供本机 MFAAvalonia 目录：

```powershell
.\.venv\Scripts\python.exe scripts\check_runtime.py --mfa-root <MFAAvalonia目录>
```

已验收的精确组合记录在 `runtime-compatibility.json`。任一组件版本变化都视为新的组合，必须重新执行自动检查和真机验收，不能仅凭主版本相同推断兼容。

## 项目进度

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| 独立 MaaFramework 项目 | 已完成 | 与旧 BDAS 工作树分离，未带入旧仓库脏改动 |
| 基础运行环境 | 已完成 | Python 3.12、MaaFw 5.10.2、MFAAvalonia 2.12.0、.NET 10 |
| 运行时兼容性门禁 | 已完成 | 锁定并检查 Python、Core、MFA、.NET Binding 与 PI 组合 |
| 最小页面闭环 | 已完成 | 已在真实雷电模拟器上验收 |
| 故障恢复 | 已完成 | 已验证超时、BACK、重启及重启上限 |
| GitHub 首次预发布 | 已完成 | 公开发布 `v0.1.0` prerelease |
| 项目统一命名 | 已完成 | 仓库、本地目录、Interface 与发布名称统一为 MaaBanGDream |
| 自动演出 | 未开始 | 独立里程碑开发 |
| 实时演奏 | 未开始 | 独立 Python Agent 里程碑开发 |
| Profile 系统 | 未开始 | 配置校验及本机数据隔离 |
| 挑战演出 | 未开始 | 在自动演出基础上实现 |
| 每日调度 | 未开始 | 最后接入，不迁移旧调度器 |

## 进度与变更记录

### 2026-07-22

- 新增 `runtime-compatibility.json`，记录已真机验证的精确运行时组合。
- 新增 `scripts/check_runtime.py`，检查 MaaFw Python 包、requirements、Project Interface、MFAAvalonia、.NET Binding 和 Native Core。
- 将静态版本门禁接入 `scripts/verify.ps1`，并确保任一步失败都会中止；完整 MFA 检查通过 `--mfa-root` 或 `MFAA_ROOT` 启用。
- 增加版本解析、精确锁定和未验证组合拒绝测试；测试总数由 6 项增加至 10 项。

### 2026-07-21

- 从旧项目迁出独立 MaaFramework 工程，建立 `main` 分支。
- 安装并验证 Python 3.12、MaaFw 5.10.2、MFAAvalonia 2.12.0 和 .NET 10。
- 完成 Project Interface V2、Python Agent、Pipeline 与首批模板资源。
- 完成并真机验证最小页面闭环。
- 完成 `CommonRecover`，验证 60 秒触发、1.5 秒 BACK 间隔、30 秒恢复窗口和最多两次重启。
- 增加 Pipeline、PNG 资源、Interface 和恢复失败路径测试；当前共 6 项测试。
- 创建公开 GitHub 仓库并发布 `v0.1.0` 开发预览。
- 项目由 `BDAS-Maa` 统一更名为 `MaaBanGDream`，通过 PR #1 合并。
- 将 README 确立为项目进度、修改和后续计划的统一记录入口。

## 后续计划

按以下顺序分别建立独立里程碑和 Git 分支，不直接堆入 `main`：

1. 自动演出基础闭环
   - 迁移歌曲选择、队伍选择、演出开始和结算识别。
   - 增加失败恢复及真机回归场景。
2. 实时演奏 Agent
   - 重新评估旧识别与输入方案，不直接照搬旧实现。
   - 明确延迟、触点释放和停止任务后的清理保证。
3. Profile 系统
   - 定义可版本化的公共 schema。
   - 本机账户、设备和敏感配置继续保持忽略。
4. 挑战演出
   - 复用自动演出能力，补充挑战专属页面和恢复流程。
5. 每日调度
   - 使用 MaaFramework 任务组合重新实现。
   - 不迁移旧 Electron/PyWebIO 调度器。

## README 更新规则

以后每次代码修改都必须同步维护本文件：

1. 开始新里程碑时，更新“项目进度”和“后续计划”。
2. 功能、修复、依赖或运行方式发生变化时，更新对应章节。
3. 每次准备提交前，在“进度与变更记录”顶部追加日期和实际完成内容。
4. 只记录已经完成并验证的结果；未完成内容保留在“后续计划”。
5. README 未同步、测试未通过或真机门禁未满足时，不提交、不合并、不发布。

## Git 工作流

- 功能使用 `feature/*` 分支，修复使用 `fix/*` 分支。
- 提交前执行：

```powershell
git status --short
git diff --check
.\scripts\verify.ps1
```

- 发布前核对完整差异、提交历史、远端文件清单和敏感信息扫描。
- 只有验证通过且工作树干净时，才允许合并到 `main`、创建标签和发布。

更详细的规则见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [发布检查清单](docs/release-checklist.md)。

## 许可证

本项目采用 GPL-3.0-only。
