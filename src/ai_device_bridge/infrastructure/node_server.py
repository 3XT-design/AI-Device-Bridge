"""Background Uvicorn server embedded in the desktop application."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn

from ai_device_bridge.api.health import create_app


class NodeServer:
    """Start and stop the local HTTP node service from the desktop UI."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        device_id: UUID | None = None,
        certificate_path: str | Path | None = None,
        private_key_path: str | Path | None = None,
        certificate_fingerprint: str = "",
        certificate_pem: str = "",
        receive_token: str = "",
        receive_directory: str | Path = "received",
    ) -> None:
        self.host = host
        self.port = port
        self.device_id = device_id or uuid4()
        self.certificate_path = certificate_path
        self.private_key_path = private_key_path
        self.certificate_fingerprint = certificate_fingerprint
        self.certificate_pem = certificate_pem
        self.receive_token = receive_token
        self.receive_directory = receive_directory
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return bool(self._server and self._server.started)

    def start(self, timeout_seconds: float = 5.0) -> None:
        if self.is_running:
            return
        if self._thread and self._thread.is_alive():
            raise RuntimeError("节点服务仍在启动或关闭，请稍后重试。")

        if not self.certificate_path or not self.private_key_path:
            raise RuntimeError("未配置节点 TLS 证书，拒绝启动明文传输服务。")

        config = uvicorn.Config(
            create_app(
                self.device_id,
                self.certificate_fingerprint,
                self.certificate_pem,
                self.receive_token,
                self.receive_directory,
            ),
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            ssl_certfile=str(self.certificate_path),
            ssl_keyfile=str(self.private_key_path),
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            name="ai-device-bridge-node",
            daemon=True,
        )
        self._thread.start()

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.05)

        self.stop()
        raise RuntimeError(
            f"无法在端口 {self.port} 启动节点服务；请检查端口是否被占用或被系统策略阻止。"
        )

    def stop(self, timeout_seconds: float = 3.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout_seconds)
        self._server = None
        self._thread = None
