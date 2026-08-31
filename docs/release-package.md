# MaaBanGDream Windows 版

这是 MaaBanGDream 的 Windows x64 完整运行包，已包含定制 MFAAvalonia、
MaaFramework 运行库、Python Agent、本地谱面和资源文件。

## 首次启动

1. 完整解压 ZIP，不要直接在压缩软件里运行。
2. 双击 `启动 MaaBanGDream.cmd`。
3. 首次启动会在当前目录的 `runtime` 内解压随包提供的固定 Python 环境。
4. MFA 打开后添加或选择 Android 模拟器，确认分辨率为 `1280×720`、
   DPI 为 `240`，然后再执行任务。

首次准备不下载安装器，也不要求电脑预装 Python、Miniconda、.NET 或开发工具。
普通演奏不联网；本地谱面只有在用户点击“演出设置 → 谱面辅助 → 同步”时联网。

## 用户数据

下列目录会在首次启动后生成，发布包本身不包含开发者的配置或设备信息：

- `config`：MFA、模拟器和任务选择配置；
- `profiles`：本机实时演奏 Profile；
- `debug`、`logs`、`screencap`：本机调试和日志；
- `runtime`：包内 Miniconda 环境。

更新时先退出 MFA，再将新版完整包覆盖到原目录；不要删除上述目录。

## 注意事项

- MFA/MaaBanGDream 与 ALAS 等其他模拟器自动化工具不能同时运行。
- 实时演奏 Profile 与分辨率、DPI、帧率、画质、音符流速绑定；任一设置变化后
  必须重新校准。
- 只支持 Windows 10/11 x64；.NET 和 Python 运行时均已包含在发布包中。

## 源码与许可证

- MaaBanGDream：<https://github.com/coatcn1/MaaBanGDream>
- 定制 MFAAvalonia：
  <https://github.com/coatcn1/MFAAvalonia/tree/feature/performance-visual-settings>

两个项目均按随包许可证文件所述以 GPL-3.0 发布。精确源码提交记录在
`BUILD-INFO.json`。
