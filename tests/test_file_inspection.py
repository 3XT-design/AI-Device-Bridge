import hashlib

import pytest

from ai_device_bridge.services.file_inspection import inspect_file


def test_inspect_file_returns_size_and_sha256(tmp_path) -> None:
    source = tmp_path / "sample.txt"
    source.write_bytes(b"AI Device Bridge")

    result = inspect_file(source, chunk_size=4)

    assert result.file_name == "sample.txt"
    assert result.file_size_bytes == len(b"AI Device Bridge")
    assert result.sha256 == hashlib.sha256(b"AI Device Bridge").hexdigest()


def test_inspect_file_rejects_missing_paths_and_directories(tmp_path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        inspect_file(tmp_path / "missing.txt")
    with pytest.raises(ValueError, match="文件夹"):
        inspect_file(tmp_path)
