"""设备旁路时钟资产校验；构建机路径及设备信息不进入可复制 manifest。"""

import hashlib
import json
from pathlib import Path
import re


ABIS = {"x86": "i686-linux-android21", "x86_64": "x86_64-linux-android21",
        "armeabi-v7a": "armv7a-linux-androideabi21", "arm64-v8a": "aarch64-linux-android21"}
NDK_REVISION = "27.3.13750724"
CURRENT_SOURCE_SHA256 = "be768498897a47d6e6f6ea2161ef4e2448340f06ef272d131e7d3a149a5e3925"
ASSET_ROOT = Path(__file__).resolve().parents[1] / "runtime" / "android_clock"
ELF_ABIS = {"x86": (1, 3), "x86_64": (2, 62), "armeabi-v7a": (1, 40), "arm64-v8a": (2, 183)}


class ClockAssetError(RuntimeError):
    pass


def sha256_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _validate_elf_abi(path, abi):
    # manifest 标签及散列只能证明文件一致；固定长度读取真实 ELF 头，避免错架构资产被重新签名放行。
    with path.open("rb") as stream:
        header = stream.read(64)
    elf_class, machine = ELF_ABIS[abi]
    header_size = 52 if elf_class == 1 else 64
    if (len(header) < header_size or header[:4] != b"\x7fELF"
            or header[4] != elf_class or header[5] != 1 or header[6] != 1
            or int.from_bytes(header[18:20], "little") != machine):
        raise ClockAssetError("clock asset ELF ABI mismatch: " + abi)


def validate_assets(root=ASSET_ROOT):
    root = Path(root).resolve()
    try:
        manifest_path = root / "manifest.json"
        if manifest_path.stat().st_size > 16384:
            raise ClockAssetError("clock asset manifest oversized")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (set(manifest) != {"schema", "wire_version", "wire_size", "minimum_api", "ndk_revision",
                             "source_sha256", "binaries"}
                or manifest["schema"] != 1 or manifest["wire_version"] != 1 or manifest["wire_size"] != 128
                or manifest["minimum_api"] != 21 or manifest["ndk_revision"] != NDK_REVISION
                or manifest["source_sha256"] != CURRENT_SOURCE_SHA256):
            raise ClockAssetError("clock asset manifest schema mismatch")
        binaries = manifest["binaries"]
        if not isinstance(binaries, list) or len(binaries) != len(ABIS) or {row["abi"] for row in binaries} != set(ABIS):
            raise ClockAssetError("clock asset ABI set mismatch")
        for row in binaries:
            abi = row["abi"]
            if (set(row) != {"abi", "target", "file", "bytes", "sha256", "needed"}
                    or row["target"] != ABIS[abi] or row["file"] != f"{abi}/mbd-clock"
                    or row["needed"] != ["libc.so"] or type(row["bytes"]) is not int or row["bytes"] <= 0
                    or not re.fullmatch("[0-9a-f]{64}", row["sha256"])):
                raise ClockAssetError("clock asset entry mismatch")
            path = (root / row["file"]).resolve()
            if (not path.is_relative_to(root) or path.stat().st_size != row["bytes"]
                    or sha256_file(path) != row["sha256"]):
                raise ClockAssetError("clock asset hash/size mismatch")
            _validate_elf_abi(path, abi)
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ClockAssetError("clock runtime assets unavailable or invalid; build Android clock assets") from exc
