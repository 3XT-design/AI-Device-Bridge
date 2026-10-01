"""Pinned HTTPS upload client for confirmed file-transfer plans."""

from __future__ import annotations

import hashlib
import ssl
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from urllib.parse import quote

import httpx

from ai_device_bridge.services.peer_health import PeerHealthError, normalize_base_url


class FileTransferError(RuntimeError):
    """Raised when a transfer cannot be securely completed."""

    def __init__(self, message: str, code: str = "transfer_failed") -> None:
        super().__init__(message)
        self.code = code


class TransferCancelled(FileTransferError):
    def __init__(self) -> None:
        super().__init__("传输已取消；如接收端已有同名文件，请先核对其哈希。", "cancelled")


def _check_cancel(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TransferCancelled()


def _chunks(
    path: Path,
    on_progress: Callable[[int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    cancel_event: Event | None = None,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    sent = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            _check_cancel(cancel_event)
            sent += len(chunk)
            yield chunk
            if on_progress is not None:
                on_progress(sent)
    if on_phase is not None:
        on_phase("verifying")


def send_file(
    source_path: str | Path,
    address: str,
    certificate_pem: str,
    receive_token: str,
    file_name: str,
    target_directory: str,
    expected_size: int,
    expected_sha256: str,
    *,
    on_progress: Callable[[int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    cancel_event: Event | None = None,
) -> dict[str, object]:
    source = Path(source_path)
    if not source.is_file():
        raise FileTransferError("源文件已不存在，请重新选择并生成传输计划。", "source_missing")
    _check_cancel(cancel_event)
    if on_phase is not None:
        on_phase("checking")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                _check_cancel(cancel_event)
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise FileTransferError(
            "读取源文件失败，请检查文件或磁盘。", "source_read_failed"
        ) from error
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise FileTransferError("源文件内容已变化，请重新生成传输计划。", "source_changed")
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
    _check_cancel(cancel_event)
    if on_phase is not None:
        on_phase("transferring")
    try:
        with httpx.Client(
            verify=verify_context,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.put(
                url,
                headers=headers,
                content=_chunks(source, on_progress, on_phase, cancel_event),
            )
            response.raise_for_status()
            result = response.json()
            if (
                not isinstance(result, dict)
                or result.get("status") != "received"
                or result.get("file_size_bytes") != size
                or result.get("sha256") != expected_sha256
            ):
                raise FileTransferError(
                    "接收端确认信息与传输计划不一致，请检查接收文件。", "bad_receipt"
                )
            return result
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:300]
        raise FileTransferError(
            f"接收设备返回 HTTP {error.response.status_code}：{detail}",
            f"http_{error.response.status_code}",
        ) from error
    except httpx.TimeoutException as error:
        raise FileTransferError(
            "连接或等待接收端确认超时；请先检查接收端文件，再决定是否重试。",
            "timeout_unknown_result",
        ) from error
    except httpx.RequestError as error:
        raise FileTransferError(
            "HTTPS 连接失败或证书指纹不匹配；接收端结果可能未知，请检查文件和网络。",
            "connection_failed",
        ) from error
    except ValueError as error:
        raise FileTransferError("接收设备返回了无法解析的响应。", "bad_receipt") from error
    except OSError as error:
        raise FileTransferError(
            "读取源文件失败，请检查文件或磁盘。", "source_read_failed"
        ) from error
