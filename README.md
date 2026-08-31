<p align="center">
  <img src="docs/assets/maabangdream-logo.png" alt="MaaBanGDream Logo" width="320">
</p>

<h1 align="center">MaaBanGDream</h1>

基于 [MaaFramework](https://github.com/MaaAssistantArknights/MaaFramework) 的《BanG Dream! 少女乐团派对！》自动化项目。

当前版本 `v0.9.1-rc.2` · [GitHub](https://github.com/coatcn1/MaaBanGDream)

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

## 普通用户安装

前往 [Releases](https://github.com/coatcn1/MaaBanGDream/releases) 下载最新的
`MaaBanGDream-v*-win-x64.zip`，完整解压后双击 `启动 MaaBanGDream.cmd`。

完整包已经包含定制 MFAAvalonia、MaaFramework、本地谱面和 Agent。首次启动
会在解压目录内展开固定版本的便携 Python 环境；.NET 运行时也已经包含，
不需要克隆源码，也不需要预装 Python、Miniconda、.NET 或开发工具。

## 源码开发

源码开发需要把两个公开仓库放在同一父目录：

```text
workplace/
├─ MaaBanGDream/
└─ MFAAvalonia/  # feature/performance-visual-settings
```

定制 MFA 源码：
[coatcn1/MFAAvalonia](https://github.com/coatcn1/MFAAvalonia/tree/feature/performance-visual-settings)。
不要用同版本官方 Core DLL 覆盖定制版本，否则会丢失“演出设置”和启动保护。

准备开发环境：

```powershell
.\scripts\setup.ps1
.\scripts\verify.ps1    # 运行验证
```

启动 MFAAvalonia：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch-mfa.ps1
```

启动后可在 MFA 的“设置 → 演出设置 → 谱面辅助”查看本地清单并手动同步。
同步只在用户点击时联网；开演和实时演奏始终只读本地谱面。

维护者可用下列命令从定制 MFA 源码生成不含本机配置的 Windows 发布包：

```powershell
.\scripts\setup.ps1
& '..\.tools\Miniconda3\envs\maabangdream\python.exe' `
  -m pip install -r .\requirements-release.txt
.\scripts\build-windows-release.ps1 -Version 0.9.1-rc.2
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
