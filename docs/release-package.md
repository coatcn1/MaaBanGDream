# MaaBanGDream Windows 版

这是 MaaBanGDream 的 Windows x64 完整运行包，已包含定制 MFAAvalonia、
MaaFramework 运行库、Python Agent、本地谱面和资源文件。

## 首次启动

1. 完整解压 ZIP，不要直接在压缩软件里运行。
2. 双击 `启动 MaaBanGDream.cmd`。
3. 首次启动会在当前目录的 `runtime` 内解压随包提供的固定 Python 环境。
4. 启动器会校验便携 Python、MaaFramework 和定制 MFA 的版本组合；不一致时拒绝启动。
5. MFA 打开后添加或选择 Android 模拟器，确认分辨率为 `1280×720`、
   DPI 为 `240`，然后再执行任务。

首次准备不下载安装器，也不要求电脑预装 Python、Miniconda、.NET 或开发工具。
普通演奏不联网；本地谱面只有在用户点击“演出设置 → 谱面辅助 → 同步”时联网。

如需验证“开演顺序门控”候选，请用
`scripts\start-release.ps1 -OrderedStartupTrial` 启动一次（仅本次进程生效，
普通双击启动保持默认行为）。Native 实时演奏仍需先在“演出设置”中打开开关。

## 用户数据

下列目录会在首次启动后生成，发布包本身不包含开发者的配置或设备信息：

- `config`：MFA、模拟器和任务选择配置；
- `profiles`：本机实时演奏 Profile；
- `debug`、`logs`、`screencap`：本机调试和日志；
- `runtime`：包内 Miniconda 环境。

客户端使用 MFA 原生 GitHub 更新入口检查和下载正式 Release。已存在便携 Python
运行库时优先下载约 148 MiB 的 runtime-free 更新包；运行库缺失时回退完整包。
下载支持断点续传和 SHA-256 校验，MFA 退出后由独立更新器覆盖程序文件，并保留
上述用户目录。谱面库继续通过“演出设置 → 谱面辅助 → 同步”独立更新。
每个完整包和更新包都携带当前版本的 `resource/Release.md`；“关于我们 → 显示公告”
与更新完成弹窗均读取这份本地文件，不会为了显示公告再次访问 GitHub。

## 注意事项

- MFA/MaaBanGDream 与 ALAS 等其他模拟器自动化工具不能同时运行。
- 实时演奏 Profile 与分辨率、DPI、帧率、画质、音符流速绑定；任一设置变化后
  必须重新校准。
- 只支持 Windows 10/11 x64；.NET 和 Python 运行时均已包含在发布包中。

## 源码与许可证

- MaaBanGDream：<https://github.com/coatcn1/MaaBanGDream>
- 定制 MFAAvalonia：
  <https://github.com/coatcn1/MFAAvalonia/tree/fix/speed-only-settings>

从 v1.4.0 起，MaaBanGDream 自有部分仅按随包
`LICENSE-MaaBanGDream.txt` 所示的 PolyForm Noncommercial 1.0.0 许可用于非商业
目的。收费软件、收费分发、收费部署或维护、商业服务及商业产品集成不在许可范围内。
名称与 Logo 使用规则见 `TRADEMARKS-MaaBanGDream.md`，完整许可边界见
`LICENSING-MaaBanGDream.md` 与 `THIRD-PARTY-NOTICES.md`。

定制 MFAAvalonia 继续使用 GPL-3.0，MaaFramework 继续使用 LGPL-3.0；对应许可证
均随包提供。其他第三方组件、游戏素材、谱面及模型继续适用各自权利条款。精确源码
提交记录在 `BUILD-INFO.json`。
