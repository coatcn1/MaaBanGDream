# MaaBanGDream Windows 版

Windows 10/11 x64 完整运行包包含定制 MFAAvalonia、MaaFramework、Python/.NET 运行库、Agent、本地谱面和资源，无需预装开发工具。

## 启动

1. 完整解压 ZIP，不要在压缩软件内运行。
2. 双击 `启动 MaaBanGDream.cmd`；首次自动解压便携 Python 到 `runtime`，校验 Python、MaaFramework 和定制 MFA 版本组合，不一致则拒绝启动。
3. MFA 打开后选择 Android 模拟器，确认 `1280×720`、DPI `240`，再执行任务。程序入口为 `MaaBanGDream.exe`，建议始终使用上述启动器。

“设置 → 性能设置 → 任务运行时阻止息屏”只在任务执行期间阻止息屏和休眠；完成、停止、失败、关闭开关或退出 MFA 均释放请求。偏好可保存，空闲时不阻止系统休眠。

定制 MFA 默认核验 Windows 上已声明雷电设备的 ADB 端口与进程身份。同端口冲突时，只在监听归属证明可用时选择独立回环地址；PID 缺失或无效、归属无法确认时拒绝连接，不改保存的设备序列号。该核验只覆盖现有雷电 `emulator-*` 配置范围，前台应用保护继续保留。截图连接必须与主控制器路由一致；路由变化需重新连接，独立地址连接失败或主路由不明时不重启共享 ADB。
开发启动脚本仍兼容 `-VerifyAdbEndpoint`；诊断时显式 `MFA_VERIFY_ADB_ENDPOINT=0` 可禁用，未设置则默认开启。

## 本机数据

发布包不含开发者配置或设备信息；更新保留下列目录：

| 目录 | 用途 |
| --- | --- |
| `config` | MFA、模拟器和任务配置 |
| `profiles` | 实时演奏 Profile |
| `debug`、`logs`、`screencap` | 调试、日志和截图 |
| `runtime` | 便携 Python 环境 |
| `resource/charts` | 本地谱面、封面和同步索引 |

## 更新与维护

**程序更新：** 在 MFA 的 GitHub 更新入口检查正式 Release；已有便携 Python 时优先使用 runtime-free 包，缺失则回退完整包。
下载支持断点续传和 SHA-256 校验，退出 MFA 后替换文件，完整应用成功后才记录新版本。
当前版本说明随包保存在 `resource/Release.md`，可从“关于我们 → 更新日志”查看。
公告通过 `interface.json` 的 `welcome` 地址获取，内容变化才提醒；断网使用缓存，首次离线使用随包 `docs/announcement.md`。

**谱面更新：** 普通演奏不联网。在独立的“设置 → 谱面更新”分类手动同步或定时维护，默认开启、每 24 小时检查，可关闭或设为 1–720 小时。
按上次成功检查时间判断到期，仅在所有实例没有执行或排队任务时联网；失败至少退避 15 分钟，界面显示上次成功、下次检查及最近错误。
新任务可取消同步等待；关闭自动更新只取消自动同步，手动同步仍可独立使用，退出客户端终止同步子进程。
校验有效资源后复用，按需下载新增、缺失、损坏或索引音符数变化的资源；
同音符数的远端内容变更无法由索引检查发现。失败或中断保留旧清单引用的可用资源。

**清理缓存：** “关于我们 → 清理缓存”只处理 `debug`、`logs`、`screencap` 及部署 sidecar 的 `realtime_recordings`、`result_captures`、`maafw_debug`、`mfa_logs` 白名单，保留配置、Profile 和谱面。
任何实例任务或谱面维护期间拒绝清理；结果显示实际文件数、释放字节和残留原因。
占用日志保留，重解析点和越界路径拒绝递归。

## 注意事项

- MFA/MaaBanGDream 与 ALAS 等模拟器自动化工具不能同时运行。
- Profile 与分辨率、DPI、帧率、画质、流速和引擎绑定；改变任一设置后重新校准。Native 需在“演出设置”中显式启用。
- 开发试验“开演顺序门控”默认关闭；`scripts\start-release.ps1 -OrderedStartupTrial` 仅对本次进程生效。

## 源码与许可证

- MaaBanGDream：<https://github.com/coatcn1/MaaBanGDream>
- 定制 MFAAvalonia：<https://github.com/coatcn1/MFAAvalonia/tree/fix/native-feedback-pr94-description>
- 精确源码提交见随包 `BUILD-INFO.json`。

v1.4.0 起，项目自有部分按 `LICENSE-MaaBanGDream.txt` 中的 PolyForm Noncommercial 1.0.0 许可，仅限非商业用途；收费软件、分发、部署维护、商业服务和商业产品集成不在许可范围内。
完整边界见 `LICENSING-MaaBanGDream.md`、`THIRD-PARTY-NOTICES.md`，名称与 Logo 规则见 `TRADEMARKS-MaaBanGDream.md`。

定制 MFAAvalonia 为 GPL-3.0，MaaFramework 为 LGPL-3.0，许可证均随包提供；其他组件、游戏素材、谱面和模型适用各自权利条款。
