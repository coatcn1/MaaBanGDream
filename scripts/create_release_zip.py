"""Create MaaBanGDream release ZIP files with UTF-8 entry names."""

from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path
from typing import Iterable


def _normalize_excludes(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        value.replace("\\", "/").strip("/")
        for value in values
        if value.strip("/\\")
    )


def _is_excluded(relative: str, excludes: tuple[str, ...]) -> bool:
    return any(
        relative == prefix or relative.startswith(f"{prefix}/")
        for prefix in excludes
    )


def create_release_zip(
    source_root: Path,
    output_path: Path,
    *,
    excludes: Iterable[str] = (),
    include_root: bool = True,
) -> None:
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError(f"release source is not a directory: {source_root}")

    output_path = output_path.resolve()
    try:
        output_path.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("release ZIP output cannot be inside its source directory")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f"{output_path.name}.tmp")
    temporary_path.unlink(missing_ok=True)
    normalized_excludes = _normalize_excludes(excludes)

    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=False,
        ) as archive:
            # 显式按相对路径排序，使同一输入树生成稳定的条目顺序；zipfile 会为
            # 中文名称设置 UTF-8 标志，避免 Windows tar 按本地代码页写入乱码。
            paths = sorted(
                (path for path in source_root.rglob("*") if path.is_file()),
                key=lambda path: path.relative_to(source_root).as_posix().casefold(),
            )
            for path in paths:
                relative = path.relative_to(source_root).as_posix()
                if _is_excluded(relative, normalized_excludes):
                    continue
                archive_name = (
                    f"{source_root.name}/{relative}" if include_root else relative
                )
                archive.write(path, archive_name)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--flat-root", action="store_true")
    args = parser.parse_args()
    create_release_zip(
        args.source,
        args.output,
        excludes=args.exclude,
        include_root=not args.flat_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
