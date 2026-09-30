"""Unauthenticated, non-sensitive health endpoint for LAN reachability checks."""

import hashlib
import hmac
import os
import platform
import re
from pathlib import Path
from socket import gethostname
from urllib.parse import unquote
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from ai_device_bridge import __version__

API_VERSION = "v1"


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
        if not target_directory or any(
            part in {"", ".", ".."} or ":" in part or any(ord(character) < 32 for character in part)
            for part in directory_parts
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
        target_directory_path.mkdir(parents=True, exist_ok=True)
        target_path = target_directory_path / file_name
        temporary_path = target_directory_path / f".{uuid4().hex}.part"
        digest = hashlib.sha256()
        received_size = 0
        try:
            with temporary_path.open("xb") as output:
                async for chunk in request.stream():
                    received_size += len(chunk)
                    if received_size > expected_size:
                        raise HTTPException(status_code=413, detail="接收数据超过声明大小。")
                    digest.update(chunk)
                    output.write(chunk)
            if received_size != expected_size:
                raise HTTPException(status_code=400, detail="接收数据大小不匹配。")
            if not hmac.compare_digest(digest.hexdigest(), x_file_sha256.lower()):
                raise HTTPException(status_code=400, detail="SHA-256 校验失败。")
            try:
                os.link(temporary_path, target_path)
            except FileExistsError as error:
                raise HTTPException(status_code=409, detail="目标目录已有同名文件。") from error
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
