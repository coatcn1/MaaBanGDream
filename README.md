# MaaBanGDream

BanG Dream! 自动化在 MaaFramework 下的独立重构项目。v0.1.0 是开发预览，仅包含最小页面闭环：

HomeMarker → HomeLive → FreeLive → SongSelectMarker → BackToHome

未知页面持续 60 秒后进入 CommonRecover：每 1.5 秒发送 BACK，30 秒仍未返回首页则重启游戏，最多重启两次。

## 环境

- Windows 10/11
- Python 3.12
- MaaFramework Core / Python binding 5.10.2（PyPI 包名 MaaFw）
- MFAAvalonia 2.12.0（其发行包内含 MaaFramework Runtime 5.10.2）
- .NET Desktop Runtime 10
- Android 设备：1280×720、DPI 240

## 开发运行

~~~powershell
.\\scripts\\setup.ps1
.\\.venv\\Scripts\\python.exe -m pytest tests
~~~

将 interface.json 与 resource 复制到 MFAAvalonia 的运行目录后启动 MFAAvalonia。ADB 可执行文件路径、设备序列号、日志、截图和本机 Profile 仅保存在本机，不提交仓库。

## 验证

~~~powershell
.\\scripts\\verify.ps1
~~~

发布前还必须完成真机连接、截图、点击、BACK、应用启停、最小页面闭环和故障恢复验收。

## 尚未迁移

自动演出、实时演奏 Agent、生命/结算识别、Profile 校验、挑战演出和每日调度尚未迁移。旧 Electron、PyWebIO 与自建调度器不在迁移范围内。

贡献与 Git 分支规则见 CONTRIBUTING.md。本项目采用 GPL-3.0-only。
