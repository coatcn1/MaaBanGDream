# MaaBanGDream v1.4.0

本次版本发布新的非商业许可与品牌政策，并包含 v1.3.9 之后已经完成真机验收的
准备页和组曲流程修复。

## 📜 许可与品牌

- 🔒 **非商业许可**：从 v1.4.0 起，MaaBanGDream 自有部分改用 PolyForm
  Noncommercial 1.0.0。允许为非商业目的查看、克隆、运行、研究、修改和分发；
  不授权收费软件、收费分发、收费部署或维护、商业服务、商业产品集成及其他预期
  商业应用。
- 🏷️ **保留官方名称**：官方项目继续使用 MaaBanGDream。公开发布的修改版或派生
  项目须使用明显不同的名称和 Logo，不得用项目名、Logo 或作者身份宣传收费服务
  或暗示官方认可；正常讨论、教程、引用和“基于 MaaBanGDream”的事实来源说明
  不受限制。
- 🧾 **明确许可边界**：v1.3.9 及更早版本继续适用原 GPL；MFAAvalonia、
  MaaFramework、minitouch、nlohmann/json、OCR 模型、游戏素材和谱面继续适用各自
  许可证或权利条款。发布包补齐对应许可与第三方说明。

## 🐛 修复

- 🎞️ **准备页正确关闭 3D 演出**：修复 `EXPOSE 'Burn out!!!'` 等准备页处于
  3D 演出状态时，旧流程把粉色成员头像当成 3D Cut-in 开关并持续点击无效位置的
  问题。现在先按 `3D演出 → 动画MV → OFF` 切换并确认，再处理 Cut-in 复选框。
- 🎼 **组曲从任意页面可靠启动**：旧“待结算”会话不再把普通非主页页面误当成
  结算续跑；流程会先按统一安全节拍恢复主页，再从第一曲开始新一组。合法的第 2/3
  曲准备页仍按会话身份约束继续演出。
- ⏱️ **消除曲间负等待崩溃**：修复组曲曲间等待偶发
  `ValueError: sleep length must be non-negative`；正式演出预检查中的同型时间边界
  也一并加固。

## 📦 更新方式

- 🔄 已安装旧版的用户可使用软件内更新；存在便携 Python 运行库时优先下载不含
  运行库和本地谱面的更新包。
- 📥 首次安装请下载完整 Windows x64 压缩包并完整解压。
- 📢 更新后“显示公告”和更新完成弹窗均读取本包携带的这份说明。

## ✅ 验证

- 🧪 自动化：`scripts/verify.ps1` 为 `1117 passed / 6 skipped`；固定运行时检查确认
  Python 3.12、MaaFw 5.10.2、MFAAvalonia 2.12.0 与 .NET Binding 5.8.0 匹配。
- 📦 发布包：完整包与 runtime-free 更新包均通过结构检查，并分别附带 SHA-256
  校验文件；包内版本、公告、许可文件和构建提交保持一致。
- 🎮 雷电真机：已验证组曲从第 2 曲断点续跑，三首结果全部保存并以 `3/3`
  返回主页；随后从普通非主页页面启动，两次 BACK 恢复主页并开始新一组。准备页
  3D 演出修复已通过自动化回归，本轮发布前未另做一局独立真机复测。

## 🧾 v1.3.9 以来的提交

- [`85a5a1b`](https://github.com/coatcn1/MaaBanGDream/commit/85a5a1b32fe65b4c252c8891d8d9fc875ec3508a) `fix(preflight): disable 3d mode before cut-in (#58)`
- [`e567d9f`](https://github.com/coatcn1/MaaBanGDream/commit/e567d9f70bd85d2761a9c3311e69735286a38a76) `fix(medley): recover unrelated startup pages (#59)`
- [查看 v1.3.9 到 v1.4.0 的完整提交对比](https://github.com/coatcn1/MaaBanGDream/compare/v1.3.9...v1.4.0)
