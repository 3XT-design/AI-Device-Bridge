import errno
import hashlib
import platform
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from ai_device_bridge import __version__
from ai_device_bridge.api.health import (
    API_VERSION,
    STAGING_DIRECTORY_NAME,
    TRANSFER_PROTOCOL_VERSION,
    create_app,
)
from ai_device_bridge.domain.models import DeviceProfile, TransferStatus
from ai_device_bridge.infrastructure.node_server import NodeServer, cleanup_orphaned_parts
from ai_device_bridge.infrastructure.sqlite_repository import SQLiteRepository
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
        "transfer_protocol_version": TRANSFER_PROTOCOL_VERSION,
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
    client = TestClient(create_app(
        receive_token=token, receive_directory=tmp_path, require_receiver_approval=False
    ))
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


def test_receive_transfer_reports_disk_full_without_publishing(tmp_path, monkeypatch) -> None:
    token = "example-receiver-token-that-is-long-enough"
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    client = TestClient(
        create_app(
            receive_token=token, receive_directory=tmp_path, repository=repository,
            require_receiver_approval=False,
        )
    )
    content = b"payload"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-File-Name": "report.txt",
        "X-Target-Directory": "Inbox",
        "X-File-Size": str(len(content)),
        "X-File-SHA256": hashlib.sha256(content).hexdigest(),
    }
    original_open = Path.open

    def disk_full_on_temporary_file(path, *args, **kwargs):
        if path.suffix == ".part":
            raise OSError(errno.ENOSPC, "No space left on device")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disk_full_on_temporary_file)
    response = client.put("/api/v1/transfers", headers=headers, content=content)
    assert response.status_code == 507
    assert not list(tmp_path.rglob("*.part"))
    assert not (tmp_path / "Inbox" / "report.txt").exists()
    assert repository.list_incoming_attempts()[0].status is TransferStatus.FAILED
    assert repository.list_incoming_attempts()[0].error_code == "http_507"


def test_startup_cleanup_only_removes_bridge_temporary_uploads(tmp_path) -> None:
    inbox = tmp_path / STAGING_DIRECTORY_NAME
    inbox.mkdir()
    orphan = inbox / f"{uuid4().hex}.part"
    unrelated = inbox / ".notes.part"
    ordinary_inbox = tmp_path / "Inbox"
    ordinary_inbox.mkdir()
    legitimate_file = ordinary_inbox / f".{uuid4().hex}.part"
    orphan.write_bytes(b"incomplete")
    unrelated.write_bytes(b"keep")
    legitimate_file.write_bytes(b"user file")

    assert cleanup_orphaned_parts(tmp_path) == 1
    assert not orphan.exists()
    assert unrelated.read_bytes() == b"keep"
    assert legitimate_file.read_bytes() == b"user file"


def test_node_start_reports_background_tls_error(tmp_path) -> None:
    server = NodeServer(
        host="127.0.0.1", port=0,
        certificate_path=tmp_path / "missing-cert.pem",
        private_key_path=tmp_path / "missing-key.pem",
        receive_directory=tmp_path / "Received",
    )
    with pytest.raises(RuntimeError, match="节点服务启动异常.*FileNotFoundError"):
        server.start(timeout_seconds=1)


def test_receive_transfer_rejects_bad_hash_traversal_and_overwrite(tmp_path) -> None:
    token = "example-receiver-token-that-is-long-enough"
    client = TestClient(create_app(
        receive_token=token, receive_directory=tmp_path, require_receiver_approval=False
    ))
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
    staging_directory = client.put(
        "/api/v1/transfers",
        headers={**headers, "X-Target-Directory": STAGING_DIRECTORY_NAME},
        content=content,
    )
    assert staging_directory.status_code == 400

    correct_headers = {**headers, "X-File-SHA256": hashlib.sha256(content).hexdigest()}
    assert (
        client.put("/api/v1/transfers", headers=correct_headers, content=content).status_code == 200
    )
    duplicate = client.put("/api/v1/transfers", headers=correct_headers, content=content)
    assert duplicate.status_code == 409
    assert (tmp_path / "Inbox" / "report.txt").read_bytes() == content


def test_strict_receiver_requires_paired_grant_and_one_time_approval(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    sender = DeviceProfile(uuid4(), "Sender", "fingerprint", "Windows", datetime.now(UTC))
    repository.save_peer(sender, "127.0.0.1:8765", certificate_pem="certificate")
    grant = repository.get_or_create_receive_grant(sender.device_id)
    legacy = "old-shared-receive-token"
    client = TestClient(create_app(
        receive_token=legacy, receive_directory=tmp_path / "Received",
        repository=repository, require_receiver_approval=True,
    ))
    content = b"confirmed payload"
    digest = hashlib.sha256(content).hexdigest()
    offer = {
        "file_name": "report.txt", "target_directory": "Inbox",
        "file_size_bytes": len(content), "sha256": digest,
    }
    old_auth = {"Authorization": f"Bearer {legacy}"}
    auth = {"Authorization": f"Bearer {grant}"}
    upload_headers = {
        **auth, "X-File-Name": "report.txt", "X-Target-Directory": "Inbox",
        "X-File-Size": str(len(content)), "X-File-SHA256": digest,
    }
    assert client.post(
        "/api/v1/transfers/requests", headers=old_auth, json=offer
    ).status_code == 401
    assert client.put(
        "/api/v1/transfers", headers=upload_headers, content=content
    ).status_code == 428
    requested = client.post("/api/v1/transfers/requests", headers=auth, json=offer)
    assert requested.status_code == 200
    request_id = requested.json()["request_id"]
    upload_url = f"/api/v1/transfers/{request_id}"
    assert client.put(upload_url, headers=upload_headers, content=content).status_code == 403
    assert not (tmp_path / "Received" / "Inbox" / "report.txt").exists()
    assert repository.list_incoming_attempts() == []
    assert repository.decide_receive_request(UUID(request_id), approve=False)
    assert client.get(f"/api/v1/transfers/requests/{request_id}", headers=auth).json() == {
        "status": "rejected"
    }
    assert client.put(upload_url, headers=upload_headers, content=content).status_code == 403
    assert any(
        entry.status is TransferStatus.REJECTED
        for entry in repository.list_history_entries()
    )

    requested = client.post("/api/v1/transfers/requests", headers=auth, json=offer)
    request_id = UUID(requested.json()["request_id"])
    assert repository.decide_receive_request(request_id, approve=True)
    upload_url = f"/api/v1/transfers/{request_id}"
    assert client.put(upload_url, headers=upload_headers, content=content).status_code == 200
    assert client.put(upload_url, headers=upload_headers, content=content).status_code == 403
    assert (tmp_path / "Received" / "Inbox" / "report.txt").read_bytes() == content

    new_grant = repository.rotate_receive_grant(sender.device_id)
    assert new_grant != grant
    assert client.post("/api/v1/transfers/requests", headers=auth, json=offer).status_code == 401
    new_auth = {"Authorization": f"Bearer {new_grant}"}
    requested = client.post("/api/v1/transfers/requests", headers=new_auth, json=offer)
    approved_id = UUID(requested.json()["request_id"])
    assert repository.decide_receive_request(approved_id, approve=True)
    newest_grant = repository.rotate_receive_grant(sender.device_id)
    assert repository.get_receive_request(approved_id).status == "rejected"
    assert client.put(
        f"/api/v1/transfers/{approved_id}",
        headers={**upload_headers, **new_auth}, content=content,
    ).status_code == 401
    new_auth = {"Authorization": f"Bearer {newest_grant}"}
    requested = client.post("/api/v1/transfers/requests", headers=new_auth, json=offer)
    cancelled_id = requested.json()["request_id"]
    assert client.delete(
        f"/api/v1/transfers/requests/{cancelled_id}", headers=new_auth
    ).json() == {"status": "cancelled"}
    assert repository.get_receive_request(UUID(cancelled_id)).status == "cancelled"
    requested = client.post("/api/v1/transfers/requests", headers=new_auth, json=offer)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE receive_requests SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE request_id = ?", (requested.json()["request_id"],),
        )
    assert client.get(
        f"/api/v1/transfers/requests/{requested.json()['request_id']}", headers=new_auth
    ).json() == {"status": "expired"}
    assert not repository.decide_receive_request(
        UUID(requested.json()["request_id"]), approve=True
    )
    assert repository.remove_device(sender.device_id)
    assert client.post(
        "/api/v1/transfers/requests", headers=new_auth, json=offer
    ).status_code == 401
