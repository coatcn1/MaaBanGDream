# MaaBanGDream

基于 [MaaFramework](https://github.com/MaaAssistantArknights/MaaFramework) 的《BanG Dream! 少女乐团派对！》自动化项目。

当前版本 `v0.9.0` · [GitHub](https://github.com/coatcn1/MaaBanGDream)

## 功能

- **自动演出** — 当前曲目/随机选曲，五档难度，1–99 轮全自动
- **实时演奏** — 机器人排练/正式演奏，触控引擎支持判定线首现补救、双押分别判定、双绿条配对与残影抑制
- **本地谱面辅助** — 809 首歌曲的 Hard/Expert/Special 本地谱面；封面、标题文字和等级联合匹配，谱面主导时序并由视觉持续校准
- **MFA 谱面同步** — 在“演出设置 → 谱面辅助”手动增量同步 Bestdori；CN 封面缺失时依次回退 JP/EN
- **实时演奏校准** — 三排练一正式自动校准，生成 Profile 后启用
- **挑战演出** — 四档点数、五档难度、连续轮次
- **调试记录** — 完全关闭、轻度 Trace 或完整记录；完整模式输出 JSONL + 60 FPS MJPG/MKV

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

启动后可在 MFA 的“设置 → 演出设置 → 谱面辅助”查看本地清单并手动同步。
同步只在用户点击时联网；开演和实时演奏始终只读本地谱面。

## 文档

| 文档 | 说明 |
| --- | --- |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录与项目进度 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [AGENTS.md](AGENTS.md) | AI 助手上手上下文 |
| [Bestdori 本地谱面仓库](docs/bestdori-chart-repository.md) | 谱面同步、格式、身份映射与离线门禁 |

## 许可证

[GPL-3.0-only](LICENSE)
