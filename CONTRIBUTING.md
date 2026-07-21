# Contributing

All source changes are tracked with Git. Use `feature/<name>` for features and
`fix/<name>` for fixes; do not develop directly on `main`.

Before committing, run:

```powershell
.\scripts\verify.ps1
git status --short
git diff --check
```

Review the complete diff and stage only the intended files. Local emulator paths,
credentials, logs, screenshots, profiles, virtual environments, and UI runtimes
must never be committed. Release tags are created only from a clean `main` after
the device acceptance test has passed.
