"""Validate a MaaBanGDream Windows release package without launching it."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.realtime.native_clock_assets import ABIS, ClockAssetError, validate_assets

CLOCK_ASSET_PREFIX = "agent/runtime/android_clock/"
CLOCK_ASSET_FILES = ("manifest.json", *(f"{abi}/mbd-clock" for abi in ABIS))


def validate_clock_directory(package_root):
    try:
        validate_assets(package_root / CLOCK_ASSET_PREFIX)
        return []
    except ClockAssetError:
        return ["Android observation clock assets missing, stale, or invalid"]


def validate_clock_archive(archive, prefix):
    # 只读固定五个成员，禁止任意解压路径；大小上限同时约束损坏归档的解压开销。
    names = archive.namelist()
    members = [(relative, prefix + CLOCK_ASSET_PREFIX + relative) for relative in CLOCK_ASSET_FILES]
    if any(names.count(name) != 1 for _, name in members):
        return ["Android observation clock archive assets missing or duplicated"]
    if any(archive.getinfo(name).file_size > (16384 if relative == 'manifest.json' else 4 * 1024 * 1024)
           for relative, name in members):
        return ["Android observation clock archive assets oversized"]
    scratch = ROOT / '.local'
    scratch.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='clock-package-', dir=scratch) as temporary:
            asset_root = Path(temporary)
            for relative, name in members:
                destination = asset_root / relative
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(archive.read(name))
            validate_assets(asset_root)
        return []
    except (ClockAssetError, OSError, RuntimeError):
        return ["Android observation clock archive assets stale or invalid"]


REQUIRED_PATHS = (
    "MaaBanGDream.exe",
    "MaaBanGDream.dll",
    "MaaBanGDream.deps.json",
    "MaaBanGDream.runtimeconfig.json",
    "MFAUpdater.exe",
    "libs/MFAAvalonia.Core.dll",
    "interface.json",
    "interface.template.json",
    "docs/about.md",
    "docs/contact.md",
    "docs/announcement.md",
    "docs/assets/maabangdream-logo-v1.png",
    "agent/server.py",
    "agent/profile_manager.py",
    "agent/realtime/native/maabangdream_realtime.pyd",
    "resource/pipeline/auto_live.json",
    "resource/Release.md",
    "resource/charts/manifest.json",
    "scripts/start-release.ps1",
    "scripts/restart-release.ps1",
    "scripts/normalize-release-directory.ps1",
    "scripts/sync_bestdori_catalog.py",
    "runtime/maabangdream-python.zip",
    "启动 MaaBanGDream.cmd",
    "BUILD-INFO.json",
    "LICENSE-MaaBanGDream.txt",
    "LICENSE-MFAAvalonia.txt",
    "LICENSE-MaaFramework-LGPL-3.0.md",
    "LICENSING-MaaBanGDream.md",
    "TRADEMARKS-MaaBanGDream.md",
    "THIRD-PARTY-NOTICES.md",
)
FORBIDDEN_TOP_LEVEL = (
    "MFAAvalonia.exe",
    "MFAAvalonia.dll",
    "MFAAvalonia.deps.json",
    "MFAAvalonia.runtimeconfig.json",
    "config",
    "logs",
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


def validate_release_archives(package_root: Path) -> list[str]:
    errors: list[str] = []
    archive_paths = (
        package_root.parent / f"{package_root.name}.zip",
        package_root.parent / f"{package_root.name}-update.zip",
    )
    for archive_path in archive_paths:
        if not archive_path.is_file():
            continue
        try:
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                is_update = archive_path.name.endswith("-update.zip")
                archive_root = "" if is_update else f"{package_root.name}/"
                errors.extend(f"{archive_path.name}: {error}"
                              for error in validate_clock_archive(archive, archive_root))
                launcher = f"{archive_root}启动 MaaBanGDream.cmd"
                if launcher not in names:
                    errors.append(
                        f"release archive has a corrupted or missing launcher name: "
                        f"{archive_path.name}"
                    )
                    continue
                if not archive.getinfo(launcher).flag_bits & 0x800:
                    errors.append(
                        f"release archive launcher is not marked UTF-8: "
                        f"{archive_path.name}"
                    )
                if is_update:
                    runtime = "runtime/maabangdream-python.zip"
                    charts = "resource/charts/"
                    if "interface.json" not in names:
                        errors.append("update archive has no root interface.json")
                    if runtime in names:
                        errors.append("update archive contains the Python runtime")
                    if any(name.startswith(charts) for name in names):
                        errors.append("update archive contains the chart catalog")
                release_note = f"{archive_root}resource/Release.md"
                if release_note not in names:
                    errors.append(
                        f"release archive has no packaged release notes: {archive_path.name}"
                    )
        except zipfile.BadZipFile as exc:
            errors.append(f"invalid release archive {archive_path.name}: {exc}")
    return errors


def validate(package_root: Path) -> list[str]:
    errors: list[str] = []
    errors.extend(validate_clock_directory(package_root))
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

    release_note_path = package_root / "resource/Release.md"
    if release_note_path.is_file():
        release_note = release_note_path.read_text(encoding="utf-8-sig")
        if not release_note.strip() or release_note.strip().casefold() == "placeholder":
            errors.append("packaged release notes are empty or placeholder")

    project_license_path = package_root / "LICENSE-MaaBanGDream.txt"
    if project_license_path.is_file():
        project_license = project_license_path.read_text(encoding="utf-8-sig")
        if "PolyForm Noncommercial License 1.0.0" not in project_license:
            errors.append("packaged MaaBanGDream license is not PolyForm Noncommercial 1.0.0")
        if not project_license.startswith("Required Notice:"):
            errors.append("packaged MaaBanGDream license has no Required Notice")

    maafw_license_path = package_root / "LICENSE-MaaFramework-LGPL-3.0.md"
    if maafw_license_path.is_file():
        maafw_license = maafw_license_path.read_text(encoding="utf-8-sig")
        if "GNU Lesser General Public License" not in maafw_license:
            errors.append("packaged MaaFramework LGPL text is invalid")

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
    errors.extend(validate_release_archives(package_root))
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
