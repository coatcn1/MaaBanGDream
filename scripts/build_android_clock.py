"""用已有固定 NDK 构建设备旁路时钟；不下载、不部署、不修改环境变量。"""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.realtime.native_clock_assets import (
    ABIS, ASSET_ROOT, CURRENT_SOURCE_SHA256, NDK_REVISION, ClockAssetError, sha256_file, validate_assets,
)


def build(ndk_root, *, output_root=ASSET_ROOT):
    ndk_root = Path(ndk_root).resolve()
    properties = (ndk_root / "source.properties").read_text(encoding="utf-8")
    if re.search(r"^Pkg\.Revision\s*=\s*" + re.escape(NDK_REVISION) + r"\s*$", properties, re.M) is None:
        raise RuntimeError("Android clock build requires NDK r27d")
    toolchain = ndk_root / "toolchains" / "llvm" / "prebuilt" / "windows-x86_64" / "bin"
    compiler, inspect = toolchain / "clang.exe", toolchain / "llvm-readelf.exe"
    if not compiler.is_file() or not inspect.is_file():
        raise RuntimeError("required existing NDK Windows toolchain is unavailable")
    source = ROOT / "native" / "android_clock" / "clock_rpc.c"
    source_sha = sha256_file(source)
    if source_sha != CURRENT_SOURCE_SHA256:
        raise RuntimeError("clock source changed; update the reviewed runtime source pin before building")
    build_root = source.parent / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    # 先完成并核验全部 ABI；失败不覆盖此前可用资产，也不静默复制旧候选。
    with tempfile.TemporaryDirectory(prefix="clock-", dir=build_root) as temporary:
        staging = Path(temporary)
        rows = []
        for abi, target in ABIS.items():
            binary = staging / abi / "mbd-clock"
            binary.parent.mkdir()
            subprocess.run([str(compiler), f"--target={target}", "-std=c11", "-O2", "-fPIE", "-pie",
                "-fstack-protector-strong", "-D_FORTIFY_SOURCE=2", "-Wall", "-Wextra", "-Werror",
                "-Wl,-z,relro", "-Wl,-z,now", "-Wl,--as-needed", "-Wl,--no-undefined",
                f"-ffile-prefix-map={ROOT}=.", str(source), "-o", str(binary)], check=True, timeout=120)
            result = subprocess.run([str(inspect), "-d", str(binary)], capture_output=True,
                                    text=True, check=True, timeout=10)
            needed = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", result.stdout)
            if needed != ["libc.so"]:
                raise RuntimeError("clock helper has unsupported dynamic dependencies")
            rows.append(dict(abi=abi, target=target, file=f"{abi}/mbd-clock", bytes=binary.stat().st_size,
                             sha256=sha256_file(binary), needed=needed))
        manifest = dict(schema=1, wire_version=1, wire_size=128, minimum_api=21,
                        ndk_revision=NDK_REVISION, source_sha256=source_sha, binaries=rows)
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        validate_assets(staging)
        output_root = Path(output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        for row in rows:
            destination = output_root / row["file"]
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(staging / row["file"], destination)
        # manifest 最后替换；读到中间状态的启动方因 hash 不符而 fail-closed。
        manifest_temporary = output_root / "manifest.json.part"
        shutil.copy2(staging / "manifest.json", manifest_temporary)
        manifest_temporary.replace(output_root / "manifest.json")
    validate_assets(output_root)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ndk-root", type=Path, default=ROOT.parent / ".tools" / "android-ndk-r27d")
    parser.add_argument("--output-root", type=Path, default=ASSET_ROOT)
    parser.add_argument("--ensure", action="store_true", help="Reuse current validated assets, otherwise build locally")
    args = parser.parse_args()
    manifest, built = ensure(args.ndk_root, output_root=args.output_root) if args.ensure else (
        build(args.ndk_root, output_root=args.output_root), True)
    print(json.dumps(dict(built=built, abi_count=len(manifest["binaries"]), source_sha256=manifest["source_sha256"])))


def ensure(ndk_root, *, output_root=ASSET_ROOT):
    # 即使旧资产彼此 hash 一致，也必须先确认本次源码对应已审查的 pin。
    if sha256_file(ROOT / "native/android_clock/clock_rpc.c") != CURRENT_SOURCE_SHA256:
        raise RuntimeError("clock source changed; update the reviewed runtime source pin before building")
    try:
        return validate_assets(output_root), False
    except ClockAssetError:
        return build(ndk_root, output_root=output_root), True


if __name__ == "__main__":
    main()
