# MaaBanGDream

基于 [MaaFramework](https://github.com/MaaAssistantArknights/MaaFramework) 的《BanG Dream! 少女乐团派对！》自动化项目。

当前版本 `v0.8.2` · [GitHub](https://github.com/coatcn1/MaaBanGDream)

## 功能

- **自动演出** — 当前曲目/随机选曲，五档难度，1–99 轮全自动
- **实时演奏** — 机器人排练/正式演奏，触控引擎支持判定线首现补救、双押分别判定、双绿条配对与残影抑制
- **本地谱面辅助** — 收藏 3 的 12 首代表歌曲、53 个现有难度；谱面主导时序并由视觉持续校准，失配整局回退纯视觉
- **实时演奏校准** — 三排练一正式自动校准，生成 Profile 后启用
- **挑战演出** — 四档点数、五档难度、连续轮次
- **调试记录** — 可选整曲 trace 录制，输出 JSONL + AVI 用于离线回放分析

## 环境要求

| 组件 | 版本 |
| --- | --- |
| Windows | 10/11 |
| Miniconda 环境 (`maabangdream`) | 26.5.3-1 / Python 3.12 |
| MaaFramework | 5.10.2 |
| MFAAvalonia | 2.12.0 |
| .NET Desktop Runtime | 10 |
| Android 设备 | 1280×720 / DPI 240 |

精确版本组合记录在 [runtime-compatibility.json](runtime-compatibility.json)。

## 快速开始

```powershell
.\scripts\setup.ps1     # 创建 Conda 环境并安装依赖
.\scripts\verify.ps1    # 运行验证
```

启动 MFAAvalonia：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch-mfa.ps1
```

## 文档

| 文档 | 说明 |
| --- | --- |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录与项目进度 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [AGENTS.md](AGENTS.md) | AI 助手上手上下文 |
| [Bestdori 本地谱面仓库](docs/bestdori-chart-repository.md) | 谱面同步、格式、身份映射与离线门禁 |

## 许可证

[GPL-3.0-only](LICENSE)
# MaaBanGDream
