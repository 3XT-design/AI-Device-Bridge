import hashlib
import socket

import pytest

from ai_device_bridge.infrastructure.node_server import NodeServer
from ai_device_bridge.infrastructure.tls_identity import load_or_create_tls_identity
from ai_device_bridge.services.file_transfer import FileTransferError, send_file
from ai_device_bridge.services.peer_health import check_peer_health


@pytest.fixture
def receiver_node(tmp_path):
    identity = load_or_create_tls_identity(tmp_path / "receiver-identity")
    receive_directory = tmp_path / "Received"
    token = "integration-test-token-long-enough-123456"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = NodeServer(
        host="127.0.0.1",
        port=port,
        certificate_path=identity.certificate_path,
        private_key_path=identity.private_key_path,
        certificate_fingerprint=identity.fingerprint,
        certificate_pem=identity.certificate_pem,
        receive_token=token,
        receive_directory=receive_directory,
    )
    server.start()
    try:
        yield {
            "address": f"127.0.0.1:{port}",
            "identity": identity,
            "receive_directory": receive_directory,
            "token": token,
        }
    finally:
        server.stop()


def test_tls_pinned_transfer_is_verified_and_published_atomically(tmp_path, receiver_node) -> None:
    identity = receiver_node["identity"]
    address = receiver_node["address"]
    token = receiver_node["token"]
    peer = check_peer_health(
        address,
        expected_fingerprint=identity.fingerprint,
        trusted_certificate_pem=identity.certificate_pem,
    )
    assert peer.certificate_fingerprint == identity.fingerprint

    source = tmp_path / "large-ish-file.bin"
    content = bytes(range(256)) * 32_768
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    response = send_file(
        source,
        address,
        identity.certificate_pem,
        token,
        source.name,
        "Integration/Inbox",
        len(content),
        digest,
    )

    received = receiver_node["receive_directory"] / "Integration" / "Inbox" / source.name
    assert response["status"] == "received"
    assert response["sha256"] == digest
    assert received.stat().st_size == len(content)
    assert hashlib.sha256(received.read_bytes()).hexdigest() == digest
    assert not list(received.parent.glob("*.part"))


def test_wrong_token_and_duplicate_name_do_not_replace_received_file(
    tmp_path, receiver_node
) -> None:
    identity = receiver_node["identity"]
    address = receiver_node["address"]
    source = tmp_path / "report.txt"
    source.write_text("trusted first content", encoding="utf-8")
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()

    with pytest.raises(FileTransferError, match="401"):
        send_file(
            source,
            address,
            identity.certificate_pem,
            "incorrect-token-but-long-enough-123456",
            source.name,
            "Inbox",
            len(content),
            digest,
        )

    destination = receiver_node["receive_directory"] / "Inbox" / source.name
    assert not destination.exists()
    send_file(
        source,
        address,
        identity.certificate_pem,
        receiver_node["token"],
        source.name,
        "Inbox",
        len(content),
        digest,
    )
    source.write_text("different content", encoding="utf-8")
    changed = source.read_bytes()
    with pytest.raises(FileTransferError, match="409"):
        send_file(
            source,
            address,
            identity.certificate_pem,
            receiver_node["token"],
            source.name,
            "Inbox",
            len(changed),
            hashlib.sha256(changed).hexdigest(),
        )
    assert destination.read_bytes() == content
