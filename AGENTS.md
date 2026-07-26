# AI 上手指南

> 本文档为 AI 助手（Codex、Claude 等）提供项目上下文。人类读者请参阅 [README.md](README.md)。

## 项目身份

基于 MaaFramework 的 BanG Dream! 自动化项目。通过 MFAAvalonia GUI 加载 Python Agent，控制 Android 模拟器完成自动演出、实时触控演奏、校准和挑战演出。

- 仓库：`https://github.com/coatcn1/MaaBanGDream`
- 当前版本：`v0.6.0`
- 许可证：GPL-3.0-only

## 工作区布局

两个目录，不可混用：

| 目录 | 用途 |
| --- | --- |
| `D:\Documents\workplace\MaaBanGDream` | Git 仓库，唯一源码目录 |
| `D:\Documents\workplace\.tools\MFAAvalonia-profile-v3` | MFA 运行目录（Git 忽略），含 `interface.json` 和 `resource/` 部署副本 |

MFA 不会直接读仓库资源。修改代码后必须通过 `scripts/launch-mfa.ps1` 同步部署并重启 MFA。

## 固定运行环境

所有路径和版本都是硬约束，不可随意更改：

```
Conda:    D:\Documents\workplace\.tools\Miniconda3
环境名:   maabangdream
Python:   D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe (3.12.13)
```

| 组件 | 固定版本 |
| --- | --- |
| Miniconda | 26.5.3-1 (SHA-256: `60ab6c...de57a`) |
| Python | 3.12 |
| MaaFw (PyPI) | 5.10.2 |
| MFAAvalonia | 2.12.0 |
| .NET Binding | 5.8.0 |
| .NET Runtime | 10 |
| Conda 源 | `conda-forge` only, `nodefaults` |

精确组合记录：[runtime-compatibility.json](runtime-compatibility.json)

## 当前分支状态

```
main                               ← 主线，已发布 v0.6.0
feature/formal-calibration-challenge  ← realtime 开发主分支（29 commits，未合并）
fix/realtime-rescue-chord-pairing     ← 最新修复（基于 formal-calibration，已推远程）
```

realtime 功能链的所有 checkpoint 分支已清理。当前活跃开发在 `feature/formal-calibration-challenge` 和 `fix/realtime-rescue-chord-pairing` 上。

## 仓库结构

```
agent/                  # Python Agent（MFA 通过 server.py 调用）
  server.py             # Agent 入口
  realtime/             # 实时触控引擎核心
  profile_manager.py    # Profile 管理器（stdin JSON 接口）

resource/               # MaaFramework 任务资源
  pipeline/             # Pipeline JSON 定义
  image/                # 模板图片

scripts/                # 运维脚本
  setup.ps1             # 创建/修复 Conda 环境
  verify.ps1            # 运行 pytest + 运行时兼容检查
  launch-mfa.ps1        # 同步资源 → 部署 → 启动 MFA
  check_runtime.py      # 运行时兼容性检查
  replay_realtime_trace.py  # 离线重放 trace.jsonl

tests/                  # pytest 测试
  fixtures/             # 测试 fixture 数据

profiles/               # 实时演奏 Profile（Git 忽略）
docs/                   # 额外文档
```

## 关键约定

- **分支命名**：`feature/*` 做功能，`fix/*` 做修复
- **合并方式**：Squash Merge 到 `main`
- **PR 流程**：Draft PR → 检查通过 → Ready → Squash Merge
- **禁止提交**：ADB 路径、设备序列号、日志、截图、Profile、`.venv`、MFA 运行目录
- **发布门禁**：`verify.ps1` 全部通过 + 工作树干净 + 真机门禁满足
- **歌曲模式**：仅支持当前曲目和随机选曲，不支持按名称指定
- **不在迁移范围**：旧 BDAS 的 Electron、PyWebIO、自建调度器

## 测试与验证

```powershell
# 完整验证（pytest + 运行时兼容检查）
.\scripts\verify.ps1

# 仅运行时兼容检查
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe scripts\check_runtime.py --mfa-root D:\Documents\workplace\.tools\MFAAvalonia-profile-v3

# 仅 pytest
D:\Documents\workplace\.tools\Miniconda3\envs\maabangdream\python.exe -m pytest tests/ -v
```

pytest 临时目录固定在 `.local/pytest-<进程号>`（Git 忽略），不使用系统 AppData。

## 需要警惕的点

1. **修改代码后必须重新部署**：MFA 不会自动读取仓库资源。改 `resource/` 或 `agent/` 后务必跑 `launch-mfa.ps1`。
2. **MFA 与 ALAS 不能同时运行**：前台输入保护会阻止向同一模拟器发送输入，但不会阻止两个工具同时运行导致的竞争。
3. **Conda 环境是硬依赖**：不使用仓库 `.venv`。所有 Python 命令必须通过 Conda 环境的绝对路径执行。
4. **MaaFramework `max_hit` 陷阱**：MaaFramework 在同一个外层任务中保留节点命中计数，嵌套 `context.run_task()` 会复用计数器。校准等嵌套场景必须使用无 `max_hit` 的专用 Action。
5. **ctypes 回调异常**：MaaFw Python Binding 会吞掉回调中的 Python 异常。所有回调必须显式 try/except 并返回失败状态。
6. **Profile 环境签名**：分辨率、DPI、帧率、画质、音符流速五项中任一项变化都会使 Profile 失效。草稿 `accepted=false` 不能驱动正式演奏。
7. **离线重放优先**：触控逻辑修改先用 `trace.jsonl` 离线重放验证（`scripts/replay_realtime_trace.py`），再上真机。
