# Contributing

All source changes are tracked with Git. Use `feature/<name>` for features and
`fix/<name>` for fixes; do not develop directly on `main`.

Merge pull requests into `main` using squash to comply with the repository's
linear-history rules. Do not create merge commits or rewrite shared history.

## 本地开发

将 MaaBanGDream 和定制 MFAAvalonia 放在同一父目录；界面源码使用 [fix/speed-only-settings](https://github.com/coatcn1/MFAAvalonia/tree/fix/speed-only-settings) 分支。

```text
workplace/
├─ MaaBanGDream/
└─ MFAAvalonia/
```

Python、MaaFramework、MFA 和 .NET 的版本以 [runtime-compatibility.json](runtime-compatibility.json) 为准。从 MaaBanGDream 源码目录运行：

```powershell
.\scripts\setup.ps1
.\scripts\launch-mfa.ps1
```

Agent 和资源改动通过启动脚本同步到开发 MFA；不要用官方 Core DLL 覆盖定制版本，也不要删除配置或 Profile 来修复界面。

维护者构建 Windows 完整包：

```powershell
& '..\.tools\Miniconda3\envs\maabangdream\python.exe' `
  -m pip install -r .\requirements-release.txt
.\scripts\build-windows-release.ps1 -Version 1.5.0
```

发布门禁见 [AGENTS.md](AGENTS.md)，运行包约定见 [Windows 运行包](docs/release-package.md)。

## 提交检查

Before committing, run:

```powershell
.\scripts\verify.ps1
git status --short
git diff --check
```

Every user-visible code, dependency, workflow, or behavior change must keep the
concise overview in `README.md` and the dated record in `CHANGELOG.md` in sync
with the verified result. Do not copy detailed investigation logs into the
README; preserve durable engineering constraints in `AGENTS.md` instead.

Review the complete diff and stage only the intended files. Local emulator paths,
credentials, logs, screenshots, profiles, virtual environments, and UI runtimes
must never be committed. Release tags are created only from a clean `main` after
the device acceptance test has passed.

By submitting a contribution, you represent that you have the right to provide
it and agree to license it to MaaBanGDream users under the repository's current
PolyForm Noncommercial License 1.0.0. Any different or additional relicensing
requires separate written consent from the affected copyright holders.
