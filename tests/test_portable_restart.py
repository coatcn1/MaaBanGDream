from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows portable restart")


@pytest.mark.parametrize("fail", [False, True])
def test_hidden_restart_renames_and_reports_preparation_result(tmp_path: Path, fail: bool):
    parent = tmp_path / "中文 空格 ' 便携"
    install = parent / "MaaBanGDream-v0.0.0-win-x64"
    scripts = install / "scripts"
    scripts.mkdir(parents=True)
    (install / "update-manifest.json").write_text(
        json.dumps({"version": "7.8.9"}), encoding="utf-8"
    )
    for name in ("restart-release.ps1", "normalize-release-directory.ps1"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    preparation = "throw 'preparation failed'" if fail else (
        "[IO.File]::WriteAllText((Join-Path (Split-Path -Parent $PSScriptRoot) 'prepared.txt'), 'ready'); exit 0"
    )
    (scripts / "start-release.ps1").write_text(preparation, encoding="utf-8-sig")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    receipt = parent / "restart-result.json"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(scripts / "restart-release.ps1"), "-ResultPath", str(receipt)],
        cwd=parent, capture_output=True, timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    renamed = parent / "MaaBanGDream-v7.8.9-win-x64"
    assert renamed.is_dir() and not install.exists()
    assert result.returncode == (1 if fail else 0), result.stderr
    assert Path(json.loads(receipt.read_text(encoding="utf-8"))["install_root"]) == renamed
    assert (renamed / "prepared.txt").exists() is not fail
    transcript = renamed / "logs/updater-launch.log"
    assert transcript.is_file()
    if fail:
        assert "preparation failed" in transcript.read_text(encoding="utf-8-sig")
