# Release checklist

- [ ] Work on a `feature/*` or `fix/*` branch.
- [ ] Review `git status --short` and the complete diff.
- [ ] Update the README project status, dated change log, and future plan.
- [ ] Run `scripts/verify.ps1` successfully.
- [ ] Run the full runtime compatibility check against the release MFAAvalonia directory.
- [ ] Confirm ADB device is online at 1280x720 and DPI 240.
- [ ] Run the minimal navigation task through MFAAvalonia.
- [ ] Verify the 60-second unknown-page timeout and bounded recovery.
- [ ] Scan tracked files for credentials and machine-local paths.
- [ ] Merge only verified changes to `main`.
- [ ] Tag from a clean `main` and publish release notes with honest scope.
