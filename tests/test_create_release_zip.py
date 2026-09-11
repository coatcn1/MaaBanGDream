from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.create_release_zip import create_release_zip


def test_release_zip_preserves_utf8_names_and_excludes_subtrees(tmp_path: Path):
    source = tmp_path / "MaaBanGDream-v1.3.7-win-x64"
    (source / "docs").mkdir(parents=True)
    (source / "runtime").mkdir()
    (source / "resource/charts").mkdir(parents=True)
    (source / "启动 MaaBanGDream.cmd").write_text("start", encoding="utf-8")
    (source / "docs/关于.md").write_text("about", encoding="utf-8")
    (source / "runtime/maabangdream-python.zip").write_bytes(b"runtime")
    (source / "resource/charts/manifest.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "release.zip"

    create_release_zip(
        source,
        output,
        excludes=("runtime/maabangdream-python.zip", "resource/charts"),
    )

    root = source.name
    launcher = f"{root}/启动 MaaBanGDream.cmd"
    about = f"{root}/docs/关于.md"
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert launcher in names
        assert about in names
        assert archive.getinfo(launcher).flag_bits & 0x800
        assert archive.getinfo(about).flag_bits & 0x800
        assert f"{root}/runtime/maabangdream-python.zip" not in names
        assert not any(name.startswith(f"{root}/resource/charts/") for name in names)

        extracted = tmp_path / "extracted"
        archive.extractall(extracted)

    assert (extracted / launcher).read_text(encoding="utf-8") == "start"
    assert (extracted / about).read_text(encoding="utf-8") == "about"


def test_update_zip_places_interface_at_archive_root(tmp_path: Path):
    source = tmp_path / "MaaBanGDream-v1.3.7-win-x64"
    (source / "resource").mkdir(parents=True)
    (source / "interface.json").write_text("{}", encoding="utf-8")
    (source / "resource/pipeline.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "release-update.zip"

    create_release_zip(source, output, include_root=False)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())

    assert "interface.json" in names
    assert "resource/pipeline.json" in names
    assert not any(name.startswith(f"{source.name}/") for name in names)
