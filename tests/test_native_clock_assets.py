import json
from pathlib import Path

import pytest

from agent.realtime.native_clock_assets import (
    ABIS, CURRENT_SOURCE_SHA256, NDK_REVISION, ClockAssetError, sha256_file, validate_assets,
)


def assets(tmp_path):
    rows = []
    for abi, target in ABIS.items():
        path = tmp_path / abi / "mbd-clock"
        path.parent.mkdir()
        elf_class, machine = {"x86": (1, 3), "x86_64": (2, 62),
                              "armeabi-v7a": (1, 40), "arm64-v8a": (2, 183)}[abi]
        header = bytearray(52 if elf_class == 1 else 64)
        header[:7] = b"\x7fELF" + bytes((elf_class, 1, 1))
        header[18:20] = machine.to_bytes(2, "little")
        path.write_bytes(header)
        rows.append(dict(abi=abi, target=target, file=f"{abi}/mbd-clock", bytes=path.stat().st_size,
                         sha256=sha256_file(path), needed=["libc.so"]))
    manifest = dict(schema=1, wire_version=1, wire_size=128, minimum_api=21, ndk_revision=NDK_REVISION,
                    source_sha256=CURRENT_SOURCE_SHA256, binaries=rows)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_runtime_source_pin_tracks_actual_c_source():
    root = Path(__file__).resolve().parents[1]
    assert sha256_file(root / "native/android_clock/clock_rpc.c") == CURRENT_SOURCE_SHA256


@pytest.mark.parametrize("problem", [None, "stale-source", "wrong-file", "traversal", "missing-abi", "private-field",
                                     "binary-corrupt", "wrong-ndk", "wrong-needed"])
def test_assets_are_relocatable_and_reject_stale_or_corrupted_builds(tmp_path, problem):
    manifest = assets(tmp_path)
    if problem == "stale-source":
        manifest["source_sha256"] = "0" * 64
    elif problem == "wrong-file":
        manifest["binaries"][0]["file"] = "other/mbd-clock"
    elif problem == "traversal":
        manifest["binaries"][0]["file"] = "../outside"
    elif problem == "missing-abi":
        manifest["binaries"].pop()
    elif problem == "private-field":
        manifest["compiler_path"] = "private path"
    elif problem == "binary-corrupt":
        (tmp_path / "x86/mbd-clock").write_bytes(b"bad")
    elif problem == "wrong-ndk":
        manifest["ndk_revision"] = "28.0"
    elif problem == "wrong-needed":
        manifest["binaries"][0]["needed"].append("libstdc++.so")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if problem:
        with pytest.raises(ClockAssetError):
            validate_assets(tmp_path)
    else:
        assert validate_assets(tmp_path) == manifest


@pytest.mark.parametrize("abi", list(ABIS))
@pytest.mark.parametrize("problem", ["machine", "class", "endian", "magic", "truncated"])
def test_resigned_wrong_elf_content_is_rejected_without_ndk(tmp_path, abi, problem):
    manifest = assets(tmp_path)
    path = tmp_path / abi / "mbd-clock"
    header = bytearray(path.read_bytes())
    if problem == "machine":
        header[18:20] = (183 if abi != "arm64-v8a" else 3).to_bytes(2, "little")
    elif problem == "class":
        header[4] = 2 if header[4] == 1 else 1
    elif problem == "endian":
        header[5] = 2
    elif problem == "magic":
        header[0] = 0
    else:
        header = header[:-1]
    path.write_bytes(header)
    row = next(row for row in manifest["binaries"] if row["abi"] == abi)
    row.update(bytes=len(header), sha256=sha256_file(path))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ClockAssetError, match="ELF ABI mismatch"):
        validate_assets(tmp_path)
