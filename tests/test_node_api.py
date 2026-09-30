import hashlib
import platform
import socket
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ai_device_bridge import __version__
from ai_device_bridge.api.health import API_VERSION, create_app
from ai_device_bridge.services.peer_health import PeerHealthError, normalize_base_url


def test_health_endpoint_returns_node_metadata_and_tls_identity() -> None:
    device_id = uuid4()
    client = TestClient(create_app(device_id))

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "device_id": str(device_id),
        "device_name": socket.gethostname(),
        "platform": platform.system(),
        "app_version": __version__,
        "api_version": API_VERSION,
        "certificate_fingerprint": "",
        "certificate_pem": "",
    }


def test_normalize_base_url_adds_https_scheme() -> None:
    assert normalize_base_url("192.168.1.20:8765") == "https://192.168.1.20:8765"
    assert normalize_base_url("https://192.168.1.20:8765/") == "https://192.168.1.20:8765"


def test_normalize_base_url_rejects_paths_and_invalid_schemes() -> None:
    with pytest.raises(PeerHealthError):
        normalize_base_url("ftp://example.test")

    with pytest.raises(PeerHealthError):
        normalize_base_url("http://example.test/api")


def test_receive_transfer_requires_token_and_checks_hash(tmp_path) -> None:
    token = "example-receiver-token-that-is-long-enough"
    client = TestClient(create_app(receive_token=token, receive_directory=tmp_path))
    content = b"verified file contents"
    digest = hashlib.sha256(content).hexdigest()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-File-Name": "report.txt",
        "X-Target-Directory": "Inbox",
        "X-File-Size": str(len(content)),
        "X-File-SHA256": digest,
    }

    unauthorized = client.put(
        "/api/v1/transfers",
        headers={**headers, "Authorization": "Bearer wrong"},
        content=content,
    )
    assert unauthorized.status_code == 401
    assert not list(tmp_path.rglob("report.txt"))

    response = client.put("/api/v1/transfers", headers=headers, content=content)
    assert response.status_code == 200
    assert response.json()["sha256"] == digest
    assert (tmp_path / "Inbox" / "report.txt").read_bytes() == content


def test_receive_transfer_rejects_bad_hash_traversal_and_overwrite(tmp_path) -> None:
    token = "example-receiver-token-that-is-long-enough"
    client = TestClient(create_app(receive_token=token, receive_directory=tmp_path))
    content = b"payload"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-File-Name": "report.txt",
        "X-Target-Directory": "Inbox",
        "X-File-Size": str(len(content)),
        "X-File-SHA256": "0" * 64,
    }

    bad_hash = client.put("/api/v1/transfers", headers=headers, content=content)
    assert bad_hash.status_code == 400
    assert not list(tmp_path.rglob("*.part"))

    traversal = client.put(
        "/api/v1/transfers",
        headers={**headers, "X-File-Name": "%2E%2E%2Fescape.txt"},
        content=content,
    )
    assert traversal.status_code == 400
    directory_traversal = client.put(
        "/api/v1/transfers",
        headers={**headers, "X-Target-Directory": "%2E%2E%2Fescape"},
        content=content,
    )
    assert directory_traversal.status_code == 400

    correct_headers = {**headers, "X-File-SHA256": hashlib.sha256(content).hexdigest()}
    assert (
        client.put("/api/v1/transfers", headers=correct_headers, content=content).status_code == 200
    )
    duplicate = client.put("/api/v1/transfers", headers=correct_headers, content=content)
    assert duplicate.status_code == 409
    assert (tmp_path / "Inbox" / "report.txt").read_bytes() == content
