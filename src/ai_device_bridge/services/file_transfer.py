"""Pinned HTTPS upload client for confirmed file-transfer plans."""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path
from urllib.parse import quote

import httpx

from ai_device_bridge.services.peer_health import PeerHealthError, normalize_base_url


class FileTransferError(RuntimeError):
    """Raised when a transfer cannot be securely completed."""


def _chunks(path: Path, chunk_size: int = 1024 * 1024):
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            yield chunk


def send_file(
    source_path: str | Path,
    address: str,
    certificate_pem: str,
    receive_token: str,
    file_name: str,
    target_directory: str,
    expected_size: int,
    expected_sha256: str,
) -> dict[str, object]:
    source = Path(source_path)
    if not source.is_file():
        raise FileTransferError("源文件已不存在，请重新选择并生成传输计划。")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise FileTransferError("源文件内容已变化，请重新生成传输计划。")
    if not certificate_pem.strip() or not receive_token.strip():
        raise FileTransferError("请先为该设备保存 TLS 证书和接收授权码。")

    verify_context = ssl.create_default_context()
    verify_context.check_hostname = False
    try:
        verify_context.load_verify_locations(cadata=certificate_pem)
    except (ssl.SSLError, ValueError) as error:
        raise FileTransferError("已保存的接收设备证书无法读取，请重新配对。") from error
    try:
        base_url = normalize_base_url(address)
    except PeerHealthError as error:
        raise FileTransferError(str(error)) from error
    if not base_url.startswith("https://"):
        raise FileTransferError("文件传输只允许使用 HTTPS 地址。")
    url = f"{base_url}/api/v1/transfers"
    headers = {
        "Authorization": f"Bearer {receive_token}",
        "Content-Length": str(size),
        "X-File-Name": quote(file_name, safe=""),
        "X-Target-Directory": quote(target_directory, safe=""),
        "X-File-Size": str(size),
        "X-File-SHA256": expected_sha256,
    }
    try:
        with httpx.Client(
            verify=verify_context,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.put(url, headers=headers, content=_chunks(source))
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:300]
        raise FileTransferError(
            f"接收设备返回 HTTP {error.response.status_code}：{detail}"
        ) from error
    except httpx.RequestError as error:
        raise FileTransferError("HTTPS 连接失败或证书指纹不匹配，请检查配对证书和网络。") from error
    except ValueError as error:
        raise FileTransferError("接收设备返回了无法解析的响应。") from error
