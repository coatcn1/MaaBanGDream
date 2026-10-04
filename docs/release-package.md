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

程序入口使用 `MaaBanGDream.exe`，仍建议通过上述启动器准备并验证运行环境。
“设置 → 性能设置 → 任务运行时阻止息屏”开启后，仅在任务执行期间阻止自动息屏
和休眠；任务完成、停止或失败后自动解除，关闭开关或退出 MFA 也会释放请求。
保存的开关偏好在重启后恢复，软件空闲时不阻止系统息屏或休眠。

首次准备不下载安装器，也不要求电脑预装 Python、Miniconda、.NET 或开发工具。
普通演奏不联网；本地谱面通过“演出设置 → 谱面辅助”的定时增量更新或手动同步维护。
定时更新默认开启、间隔 24 小时，可关闭或设置 1–720 小时；启动及运行期间按上次成功检查时间
判断是否到期，仅在所有实例没有执行或排队任务时联网。失败至少等待 15 分钟重试，界面显示
上次成功、下次检查及最近错误。同步时新任务可取消等待，关闭开关取消自动同步，退出客户端
终止同步子进程。谱面和封面校验通过后复用，新增、缺失、损坏或索引音符数变化的资源按需下载；
音符数相同的远端内容变化不属于本次元数据增量校验范围。失败或中断保留旧清单引用的可用资源。

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
上述用户目录。谱面库继续通过“演出设置 → 谱面辅助”的定时或手动同步独立更新。
每个完整包和更新包都携带当前版本的 `resource/Release.md`，“关于我们 → 更新日志”
读取这份本地版本说明。独立公告通过 `interface.json` 的 `welcome` 地址获取，内容变化
才自动提醒；断网时使用缓存，首次离线启动则使用随包的 `docs/announcement.md`。
维护者可单独修改主分支的该文件发布公告，不必创建 Release。

更新时显示半透明进度窗口，文件替换和便携环境准备在后台执行，不通过 CMD 重启。
更新后只展示一次版本说明，不叠出启动公告；失败时保留错误和日志入口。
Windows 不支持透明效果或关闭系统透明效果时，更新器使用实色背景。

“关于我们 → 清理缓存”只处理 `debug`、`logs`、`screencap` 和部署 sidecar 中的
`realtime_recordings`、`result_captures`、`maafw_debug`、`mfa_logs` 白名单目录，
不会删除配置、Profile 或谱面。全部实例任务或谱面维护期间拒绝清理；结果显示实际文件数、
释放字节和残留原因，被占用日志保留，重解析点或越界路径拒绝递归。本次功能已部署开发 Maa，
一次真实空闲自动同步已通过且既有资源、配置与 Profile 保持不变，界面已由用户查看确认；
修改频率、关闭开关与清理按钮尚未逐项实测，也未等待完整 24 小时周期。

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
