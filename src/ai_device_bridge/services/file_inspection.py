"""Local file metadata and checksum calculation for transfer-plan review."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileInspection:
    path: str
    file_name: str
    file_size_bytes: int
    sha256: str


def inspect_file(path: str | Path, chunk_size: int = 1024 * 1024) -> FileInspection:
    """Read a regular file in chunks and return its size and SHA-256 digest."""
    source = Path(path).expanduser()
    if not source.exists():
        raise ValueError("所选文件不存在。")
    if not source.is_file():
        raise ValueError("请选择单个文件，不能选择文件夹。")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    digest = hashlib.sha256()
    try:
        with source.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
        size = source.stat().st_size
    except OSError as error:
        raise ValueError(f"无法读取所选文件：{error}") from error

    return FileInspection(
        path=str(source.resolve()),
        file_name=source.name,
        file_size_bytes=size,
        sha256=digest.hexdigest(),
    )
