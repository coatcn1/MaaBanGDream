from __future__ import annotations

import numpy as np

from agent.realtime.vision_io import imread_unicode, imwrite_unicode


def test_image_roundtrip_through_non_ascii_path(tmp_path):
    """便携版 cv2 在中文路径下读写会失败，字节级读写必须能往返。"""
    target = tmp_path / "下载目录"
    target.mkdir()
    path = target / "证据.png"
    image = np.arange(0, 8 * 8 * 3, dtype=np.uint8).reshape(8, 8, 3)

    assert imwrite_unicode(path, image) is True
    assert path.is_file() and path.stat().st_size > 0

    loaded = imread_unicode(path)
    assert loaded is not None
    assert loaded.shape == (8, 8, 3)
    assert np.array_equal(loaded, image)


def test_read_missing_or_empty_file_returns_none(tmp_path):
    assert imread_unicode(tmp_path / "不存在.png") is None

    empty = tmp_path / "空文件.png"
    empty.write_bytes(b"")
    assert imread_unicode(empty) is None


def test_write_without_extension_is_rejected(tmp_path):
    import pytest

    from agent.realtime.vision_io import imwrite_unicode

    with pytest.raises(ValueError, match="扩展名"):
        imwrite_unicode(tmp_path / "无后缀", np.zeros((2, 2, 3), dtype=np.uint8))
