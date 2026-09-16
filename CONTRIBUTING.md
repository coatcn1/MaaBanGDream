# Contributing

All source changes are tracked with Git. Use `feature/<name>` for features and
`fix/<name>` for fixes; do not develop directly on `main`.

Merge pull requests into `main` using squash to comply with the repository's
linear-history rules. Do not create merge commits or rewrite shared history.

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
