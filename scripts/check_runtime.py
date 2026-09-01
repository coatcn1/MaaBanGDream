from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[1]
LOCK_PATH = ROOT / "runtime-compatibility.json"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
INTERFACE_PATH = ROOT / "interface.json"


def load_json(path: Path) -> dict[str, Any]:
    # Windows PowerShell 5.1 writes a UTF-8 BOM for ``-Encoding utf8``.
    # Accept it so an older portable launcher can repair its generated files
    # instead of becoming permanently unable to pass the next startup check.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def required_maafw_version(requirements: str) -> str:
    match = re.search(r"(?im)^MaaFw==([^\s;]+)\s*$", requirements)
    if not match:
        raise ValueError("requirements.txt must pin MaaFw with ==")
    return match.group(1)


def deps_versions(deps: dict[str, Any]) -> tuple[str, str]:
    libraries = deps.get("libraries", {})
    mfa_versions = [
        key.removeprefix("MFAAvalonia.Core.Reference/").removesuffix(".0")
        for key in libraries
        if key.startswith("MFAAvalonia.Core.Reference/")
    ]
    binding_versions = [
        key.removeprefix("Maa.Framework.Binding/")
        for key in libraries
        if key.startswith("Maa.Framework.Binding/")
    ]
    if len(mfa_versions) != 1 or len(binding_versions) != 1:
        raise ValueError("unable to identify one MFAAvalonia and Maa binding version")
    return mfa_versions[0], binding_versions[0]


def current_runtime_identifier(
    platform_name: str | None = None,
    machine: str | None = None,
) -> str:
    platform_key = platform_name or sys.platform
    machine_key = (machine or platform.machine()).lower()
    operating_system = {
        "win32": "win",
        "linux": "linux",
        "darwin": "osx",
    }.get(platform_key)
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine_key)
    if operating_system is None or architecture is None:
        raise ValueError(
            f"unsupported runtime platform: {platform_key}/{machine_key}"
        )
    return f"{operating_system}-{architecture}"


def native_directory(mfa_root: Path) -> Path:
    runtime_id = current_runtime_identifier()
    current = mfa_root / "runtimes" / runtime_id / "native" / "MaaFramework.dll"
    if current.is_file():
        return current.parent
    matches = list(mfa_root.glob("runtimes/*/native/MaaFramework.dll"))
    if len(matches) != 1:
        raise ValueError(
            f"unable to identify MaaFramework.dll for {runtime_id}"
        )
    return matches[0].parent


def native_core_version(mfa_root: Path) -> str:
    framework = native_directory(mfa_root) / "MaaFramework.dll"
    versions = {
        match.decode().removeprefix("v")
        for match in re.findall(rb"v\d+\.\d+\.\d+", framework.read_bytes())
    }
    if len(versions) != 1:
        raise ValueError("unable to identify one MaaFramework Core version marker")
    return versions.pop()


def static_versions() -> dict[str, str | int]:
    interface = load_json(INTERFACE_PATH)
    requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    prefix = Path(sys.prefix)
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "environment_manager": (
            "conda" if (prefix / "conda-meta").is_dir() else "not-conda"
        ),
        "conda_environment": prefix.name,
        "maafw_python": importlib.metadata.version("MaaFw"),
        "maafw_requirement": required_maafw_version(requirements),
        "project_interface": interface["interface_version"],
    }


def expected_versions(
    locked: dict[str, Any],
    *,
    portable: bool = False,
    prefix: Path | None = None,
) -> dict[str, Any]:
    expected = dict(locked)
    if not portable:
        return expected
    runtime_prefix = prefix or Path(sys.prefix)
    ready_marker = runtime_prefix / ".maabangdream-ready"
    if runtime_prefix.name != "python" or not ready_marker.is_file():
        raise RuntimeError(
            "portable runtime must be the prepared package-local runtime/python"
        )
    expected["conda_environment"] = "python"
    return expected


def mfa_versions(mfa_root: Path) -> dict[str, str]:
    deps_path = mfa_root / "MFAAvalonia.deps.json"
    if not deps_path.is_file():
        raise FileNotFoundError(f"missing {deps_path}")
    mfa, binding = deps_versions(load_json(deps_path))
    return {
        "mfaavalonia": mfa,
        "mfa_dotnet_binding": binding,
        "maafw_core": native_core_version(mfa_root),
    }


def verify(actual: dict[str, str | int], expected: dict[str, Any]) -> None:
    aliases = {"maafw_requirement": "maafw_python"}
    failures = []
    for key, value in actual.items():
        expected_key = aliases.get(key, key)
        expected_value = expected.get(expected_key)
        if value != expected_value:
            failures.append(f"{key}: expected {expected_value}, got {value}")
    if failures:
        raise RuntimeError("runtime compatibility check failed: " + "; ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the tested Maa runtime tuple")
    parser.add_argument(
        "--mfa-root",
        type=Path,
        default=os.environ.get("MFAA_ROOT"),
        help="MFAAvalonia directory; defaults to MFAA_ROOT",
    )
    parser.add_argument(
        "--portable",
        action="store_true",
        help="verify the prepared package-local runtime/python environment",
    )
    args = parser.parse_args()

    expected = expected_versions(
        load_json(LOCK_PATH),
        portable=args.portable,
    )
    actual = static_versions()
    if args.mfa_root:
        actual.update(mfa_versions(args.mfa_root.resolve()))
    verify(actual, expected)

    for key in sorted(actual):
        print(f"{key}={actual[key]}")
    if not args.mfa_root:
        print("mfa_runtime=skipped (set MFAA_ROOT or pass --mfa-root for full check)")


if __name__ == "__main__":
    main()
