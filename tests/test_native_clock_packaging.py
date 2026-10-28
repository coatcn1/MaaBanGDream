import json
from pathlib import Path
import zipfile

import pytest

from scripts import build_android_clock, check_release_package
from test_native_clock_assets import assets
from agent.realtime.native_clock_assets import sha256_file


def test_ensure_reuses_all_valid_assets_without_accessing_ndk(tmp_path, monkeypatch):
    manifest = assets(tmp_path)
    monkeypatch.setattr(build_android_clock, 'build', lambda *_args, **_kwargs: pytest.fail('unexpected build'))
    assert build_android_clock.ensure(tmp_path / 'missing-ndk', output_root=tmp_path) == (manifest, False)


def test_ensure_rebuilds_invalid_assets_but_rejects_changed_source_first(tmp_path, monkeypatch):
    manifest = assets(tmp_path)
    (tmp_path / 'x86/mbd-clock').write_bytes(b'corrupt')
    calls=[]
    monkeypatch.setattr(build_android_clock, 'build', lambda ndk, **kwargs: calls.append((ndk,kwargs)) or manifest)
    assert build_android_clock.ensure('fixed-ndk', output_root=tmp_path) == (manifest, True)
    assert calls == [('fixed-ndk', dict(output_root=tmp_path))]
    source = tmp_path / 'native/android_clock/clock_rpc.c'
    source.parent.mkdir(parents=True)
    source.write_text('changed source', encoding='utf-8')
    monkeypatch.setattr(build_android_clock, 'ROOT', tmp_path)
    with pytest.raises(RuntimeError, match='source changed'):
        build_android_clock.ensure('fixed-ndk', output_root=tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize('prefix', ['', 'MaaBanGDream-v0.0.0-win-x64/'])
@pytest.mark.parametrize('problem', [None, 'missing', 'corrupt', 'source-pin', 'duplicate', 'missing-abi', 'wrong-abi'])
def test_archive_independently_validates_clock_assets(tmp_path, prefix, problem):
    asset_root=tmp_path/'assets'
    asset_root.mkdir()
    manifest=assets(asset_root)
    if problem == 'source-pin':
        manifest['source_sha256']='0'*64
        (asset_root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    elif problem == 'wrong-abi':
        path = asset_root/'x86/mbd-clock'
        path.write_bytes((asset_root/'arm64-v8a/mbd-clock').read_bytes())
        manifest['binaries'][0].update(bytes=path.stat().st_size, sha256=sha256_file(path))
        (asset_root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    archive_path=tmp_path/'package.zip'
    with zipfile.ZipFile(archive_path,'w') as archive:
        for relative in check_release_package.CLOCK_ASSET_FILES:
            if problem == 'missing' or problem == 'missing-abi' and relative == 'x86/mbd-clock':
                continue
            name=prefix+check_release_package.CLOCK_ASSET_PREFIX+relative
            data=(asset_root/relative).read_bytes()
            archive.writestr(name,b'corrupt' if problem == 'corrupt' and relative == 'x86/mbd-clock' else data)
        if problem == 'duplicate':
            with pytest.warns(UserWarning,match='Duplicate name'):
                archive.writestr(prefix+check_release_package.CLOCK_ASSET_PREFIX+'manifest.json',b'{}')
    with zipfile.ZipFile(archive_path) as archive:
        errors=check_release_package.validate_clock_archive(archive,prefix)
    assert bool(errors) == bool(problem)


@pytest.mark.parametrize('problem', ['corrupt', 'wrong-abi'])
def test_directory_rejects_stale_assets_even_with_all_files_present(tmp_path, problem):
    asset_root=tmp_path/check_release_package.CLOCK_ASSET_PREFIX
    asset_root.mkdir(parents=True)
    manifest = assets(asset_root)
    assert not check_release_package.validate_clock_directory(tmp_path)
    path = asset_root/'arm64-v8a/mbd-clock'
    path.write_bytes(b'corrupt' if problem == 'corrupt' else (asset_root/'x86/mbd-clock').read_bytes())
    if problem == 'wrong-abi':
        manifest['binaries'][-1].update(bytes=path.stat().st_size, sha256=sha256_file(path))
        (asset_root/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    assert check_release_package.validate_clock_directory(tmp_path)


def test_development_and_release_builders_explicitly_include_generated_clock_assets():
    root=Path(__file__).resolve().parents[1]
    launch=(root/'scripts/launch-mfa.ps1').read_text(encoding='utf-8-sig')
    release=(root/'scripts/build-windows-release.ps1').read_text(encoding='utf-8-sig')
    assert "'build_android_clock.py') --ensure" in launch
    assert "'build_android_clock.py')" in release
    assert 'agent/runtime/android_clock/$clockAsset' in release
    assert all(relative in release for relative in check_release_package.CLOCK_ASSET_FILES)
    startup=(root/'scripts/start-release.ps1').read_text(encoding='utf-8-sig')
    assert 'build_android_clock' not in startup and 'ndk' not in startup.lower()
