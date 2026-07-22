from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[1]
LOCK_PATH = ROOT / "runtime-compatibility.json"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
INTERFACE_PATH = ROOT / "interface.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def native_directory(mfa_root: Path) -> Path:
    matches = list(mfa_root.glob("runtimes/*/native/MaaFramework.dll"))
    if len(matches) != 1:
        raise ValueError("unable to identify one MaaFramework.dll")
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
    return {
        "maafw_python": importlib.metadata.version("MaaFw"),
        "maafw_requirement": required_maafw_version(requirements),
        "project_interface": interface["interface_version"],
    }


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
    args = parser.parse_args()

    expected = load_json(LOCK_PATH)
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
