"""Unauthenticated, non-sensitive health endpoint for LAN reachability checks."""

import errno
import hashlib
import hmac
import os
import platform
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from socket import gethostname
from urllib.parse import unquote
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from ai_device_bridge import __version__
from ai_device_bridge.domain.models import IncomingTransferAttempt, TransferStatus
from ai_device_bridge.infrastructure.sqlite_repository import SQLiteRepository

API_VERSION = "v1"
STAGING_DIRECTORY_NAME = ".bridge-staging"


class HealthResponse(BaseModel):
    status: str
    device_id: UUID
    device_name: str
    platform: str
    app_version: str
    api_version: str
    certificate_fingerprint: str
    certificate_pem: str


def create_app(
    device_id: UUID | None = None,
    certificate_fingerprint: str = "",
    certificate_pem: str = "",
    receive_token: str = "",
    receive_directory: str | Path = "received",
    repository: SQLiteRepository | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Device Bridge Node", version=__version__)
    stable_device_id = device_id or uuid4()
    inbox_root = Path(receive_directory).resolve()

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            device_id=stable_device_id,
            device_name=gethostname(),
            platform=platform.system(),
            app_version=__version__,
            api_version=API_VERSION,
            certificate_fingerprint=certificate_fingerprint,
            certificate_pem=certificate_pem,
        )

    @app.put("/api/v1/transfers")
    async def receive_transfer(
        request: Request,
        authorization: str | None = Header(default=None),
        x_file_name: str = Header(default=""),
        x_target_directory: str = Header(default=""),
        x_file_size: str = Header(default=""),
        x_file_sha256: str = Header(default=""),
    ) -> dict[str, object]:
        expected_authorization = f"Bearer {receive_token}"
        if (
            not receive_token
            or not authorization
            or not hmac.compare_digest(authorization, expected_authorization)
        ):
            raise HTTPException(status_code=401, detail="接收授权码无效。")

        file_name = unquote(x_file_name)
        target_directory = unquote(x_target_directory)
        if (
            not file_name
            or file_name in {".", ".."}
            or "/" in file_name
            or "\\" in file_name
            or any(ord(character) < 32 for character in file_name)
        ):
            raise HTTPException(status_code=400, detail="文件名无效。")
        directory_parts = target_directory.replace("\\", "/").split("/")
        if (
            not target_directory
            or any(
                part in {"", ".", ".."}
                or ":" in part
                or any(ord(character) < 32 for character in part)
                for part in directory_parts
            )
            or directory_parts[0].casefold() == STAGING_DIRECTORY_NAME.casefold()
        ):
            raise HTTPException(status_code=400, detail="目标目录无效。")
        try:
            expected_size = int(x_file_size)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="文件大小无效。") from error
        if expected_size < 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", x_file_sha256):
            raise HTTPException(status_code=400, detail="文件大小或 SHA-256 无效。")

        target_directory_path = inbox_root.joinpath(*directory_parts).resolve()
        if not target_directory_path.is_relative_to(inbox_root):
            raise HTTPException(status_code=400, detail="目标目录超出接收目录范围。")
        resolved_parts = target_directory_path.relative_to(inbox_root).parts
        if resolved_parts and resolved_parts[0].casefold() == STAGING_DIRECTORY_NAME.casefold():
            raise HTTPException(status_code=400, detail="目标目录无效。")
        target_path = target_directory_path / file_name
        staging_root = inbox_root / STAGING_DIRECTORY_NAME
        temporary_path = staging_root / f"{uuid4().hex}.part"
        digest = hashlib.sha256()
        received_size = 0
        last_saved_bytes = 0
        now = datetime.now(UTC)
        attempt = (
            IncomingTransferAttempt(
                attempt_id=uuid4(),
                file_name=file_name,
                target_directory=target_directory,
                file_size_bytes=expected_size,
                expected_sha256=x_file_sha256,
                bytes_received=0,
                status=TransferStatus.TRANSFERRING,
                created_at=now,
                updated_at=now,
            )
            if repository is not None
            else None
        )
        published = False

        def fail_attempt(code: str, message: str) -> None:
            if attempt is None or repository is None or published:
                return
            if received_size <= expected_size and received_size > attempt.bytes_received:
                attempt.update_progress(received_size)
            attempt.error_code = code
            attempt.error_message = message
            attempt.transition_to(TransferStatus.FAILED)
            try:
                repository.save_incoming_attempt(attempt)
            except sqlite3.Error:
                # Preserve the original transfer error if the same disk is full.
                pass

        try:
            if attempt is not None and repository is not None:
                repository.save_incoming_attempt(attempt)
            staging_root.mkdir(parents=True, exist_ok=True)
            if staging_root.is_symlink():
                raise HTTPException(status_code=500, detail="接收暂存目录配置无效。")
            target_directory_path.mkdir(parents=True, exist_ok=True)
            with temporary_path.open("xb") as output:
                async for chunk in request.stream():
                    received_size += len(chunk)
                    if received_size > expected_size:
                        raise HTTPException(status_code=413, detail="接收数据超过声明大小。")
                    digest.update(chunk)
                    output.write(chunk)
                    if (
                        attempt is not None
                        and repository is not None
                        and received_size - last_saved_bytes >= 4 * 1024 * 1024
                    ):
                        attempt.update_progress(received_size)
                        repository.save_incoming_attempt(attempt)
                        last_saved_bytes = received_size
            if attempt is not None and repository is not None:
                attempt.update_progress(received_size)
                attempt.transition_to(TransferStatus.VERIFYING)
                repository.save_incoming_attempt(attempt)
            if received_size != expected_size:
                raise HTTPException(status_code=400, detail="接收数据大小不匹配。")
            if not hmac.compare_digest(digest.hexdigest(), x_file_sha256.lower()):
                raise HTTPException(status_code=400, detail="SHA-256 校验失败。")
            try:
                os.link(temporary_path, target_path)
            except FileExistsError as error:
                raise HTTPException(status_code=409, detail="目标目录已有同名文件。") from error
            published = True
            if attempt is not None and repository is not None:
                attempt.transition_to(TransferStatus.COMPLETED)
                repository.save_incoming_attempt(attempt)
        except ClientDisconnect as error:
            fail_attempt("connection_lost", "发送连接中断，文件未完成接收。")
            raise HTTPException(status_code=400, detail="发送连接中断。") from error
        except HTTPException as error:
            fail_attempt(f"http_{error.status_code}", str(error.detail))
            raise
        except OSError as error:
            response_error = _storage_http_error(error)
            fail_attempt(f"http_{response_error.status_code}", str(response_error.detail))
            raise response_error from error
        except sqlite3.Error as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    "文件可能已接收，但历史保存失败；请检查接收目录。"
                    if published
                    else "接收端任务记录失败，文件未完成接收。"
                ),
            ) from error
        finally:
            temporary_path.unlink(missing_ok=True)

        return {
            "status": "received",
            "device_id": str(stable_device_id),
            "file_name": file_name,
            "file_size_bytes": received_size,
            "sha256": digest.hexdigest(),
        }

    return app


def _storage_http_error(error: OSError) -> HTTPException:
    if error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return HTTPException(status_code=507, detail="接收设备磁盘空间不足。")
    return HTTPException(status_code=500, detail="接收设备无法写入文件。")
