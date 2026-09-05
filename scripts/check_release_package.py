"""Validate a MaaBanGDream Windows release package without launching it."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path


REQUIRED_PATHS = (
    "MFAAvalonia.exe",
    "MFAAvalonia.deps.json",
    "libs/MFAAvalonia.Core.dll",
    "interface.json",
    "interface.template.json",
    "agent/server.py",
    "agent/profile_manager.py",
    "agent/realtime/native/maabangdream_realtime.pyd",
    "resource/pipeline/auto_live.json",
    "resource/charts/manifest.json",
    "scripts/start-release.ps1",
    "scripts/sync_bestdori_catalog.py",
    "runtime/maabangdream-python.zip",
    "启动 MaaBanGDream.cmd",
    "BUILD-INFO.json",
    "LICENSE-MaaBanGDream.txt",
    "LICENSE-MFAAvalonia.txt",
)
FORBIDDEN_TOP_LEVEL = (
    "config",
    "logs",
    "profiles",
    "debug",
    "screencap",
    ".maabangdream-backup",
    "profile-manager.json",
    "appsettings.json",
)
TEXT_SUFFIXES = {".json", ".ps1", ".cmd", ".md", ".txt", ".py"}
FORBIDDEN_TEXT = (
    r"D:\Documents\workplace",
    r"C:\Users\Lenovo",
    "MFAAvalonia-profile-v3",
)


def validate(package_root: Path) -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED_PATHS:
        if not (package_root / relative).is_file():
            errors.append(f"missing required file: {relative}")
    for relative in FORBIDDEN_TOP_LEVEL:
        if (package_root / relative).exists():
            errors.append(f"private/runtime state included: {relative}")

    interface_path = package_root / "interface.json"
    if interface_path.is_file():
        interface = json.loads(interface_path.read_text(encoding="utf-8-sig"))
        if interface["agent"]["child_exec"] != "python":
            errors.append("unconfigured interface must use portable child_exec=python")
        if interface["resource"][0]["path"] != ["./resource"]:
            errors.append("unconfigured interface must use ./resource")

    runtime_archive = package_root / "runtime/maabangdream-python.zip"
    if runtime_archive.is_file():
        with zipfile.ZipFile(runtime_archive) as archive:
            unpack_script = archive.read("Scripts/conda-unpack-script.py")
        unpack_text = unpack_script.decode("utf-8", errors="replace")
        for forbidden in FORBIDDEN_TEXT:
            if forbidden.casefold() in unpack_text.casefold():
                errors.append(
                    f"local path marker {forbidden!r} found in portable runtime"
                )

    native_pyd = package_root / "agent/realtime/native/maabangdream_realtime.pyd"
    if native_pyd.is_file():
        native_dir = str(native_pyd.parent)
        if native_dir not in sys.path:
            sys.path.insert(0, native_dir)
        try:
            import maabangdream_realtime as _native_realtime

            if not str(getattr(_native_realtime, "version", lambda: "")()):
                errors.append("native realtime extension version self-check failed")
        except Exception as exc:
            errors.append(f"native realtime extension import failed: {exc}")

    for path in package_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        for forbidden in FORBIDDEN_TEXT:
            if forbidden.casefold() in text.casefold():
                errors.append(
                    f"local path marker {forbidden!r} found in "
                    f"{path.relative_to(package_root)}"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package_root", type=Path)
    args = parser.parse_args()
    package_root = args.package_root.resolve()
    errors = validate(package_root)
    if errors:
        for error in errors:
            print(f"release_error={error}")
        return 1
    files = [path for path in package_root.rglob("*") if path.is_file()]
    print(f"release_package={package_root}")
    print(f"release_files={len(files)}")
    print(f"release_bytes={sum(path.stat().st_size for path in files)}")
    print("release_validation=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
