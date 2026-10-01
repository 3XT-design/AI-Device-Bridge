"""Exercise the bundled TLS server without opening the desktop UI."""

from __future__ import annotations

import os
import socket
import ssl
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

import httpx

from ai_device_bridge.infrastructure.node_server import NodeServer
from ai_device_bridge.infrastructure.sqlite_repository import SQLiteRepository
from ai_device_bridge.infrastructure.tls_identity import load_or_create_tls_identity


def run_node_smoke() -> int:
    error_path = Path(os.environ.get(
        "AI_DEVICE_BRIDGE_SMOKE_REPORT",
        str(Path(gettempdir()) / "ai-device-bridge-smoke-error.txt"),
    ))
    error_path.unlink(missing_ok=True)
    try:
        _run_node_smoke()
    except Exception:
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        return 1
    return 0


def _run_node_smoke() -> None:
    with TemporaryDirectory(prefix="ai-device-bridge-smoke-") as directory:
        root = Path(directory)
        repository = SQLiteRepository(root / "bridge.sqlite3")
        identity = load_or_create_tls_identity(root)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = NodeServer(
            host="127.0.0.1", port=port,
            certificate_path=identity.certificate_path,
            private_key_path=identity.private_key_path,
            certificate_fingerprint=identity.fingerprint,
            certificate_pem=identity.certificate_pem,
            receive_directory=root / "Received", repository=repository,
        )
        try:
            server.start()
            context = ssl.create_default_context(cafile=str(identity.certificate_path))
            context.check_hostname = False
            with httpx.Client(verify=context, trust_env=False, timeout=5.0) as client:
                response = client.get(f"https://127.0.0.1:{port}/api/v1/health")
                response.raise_for_status()
                if response.json().get("status") != "ok":
                    raise RuntimeError("健康检查没有返回 status=ok")
        finally:
            server.stop()


def run_motion_smoke() -> int:
    """Check that the packaged pose model loads and processes an image offline."""
    error_path = Path(os.environ.get(
        "AI_DEVICE_BRIDGE_SMOKE_REPORT",
        str(Path(gettempdir()) / "ai-device-bridge-smoke-error.txt"),
    ))
    error_path.unlink(missing_ok=True)
    try:
        import mediapipe as mp
        import numpy as np

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        with mp.solutions.pose.Pose(model_complexity=1) as pose:
            result = pose.process(image)
            if result is None:
                raise RuntimeError("姿态模型没有返回处理结果")
    except Exception:
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        return 1
    return 0
