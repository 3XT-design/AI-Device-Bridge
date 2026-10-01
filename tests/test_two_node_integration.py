import hashlib
import socket
import time
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

import pytest

from ai_device_bridge.app import FileTransferWorker
from ai_device_bridge.domain.models import (
    DeviceProfile,
    PlanStatus,
    SourceMode,
    TransferPlan,
    TransferStatus,
)
from ai_device_bridge.infrastructure.node_server import NodeServer
from ai_device_bridge.infrastructure.sqlite_repository import SQLiteRepository
from ai_device_bridge.infrastructure.tls_identity import load_or_create_tls_identity
from ai_device_bridge.services.file_transfer import FileTransferError, TransferCancelled, send_file
from ai_device_bridge.services.peer_health import check_peer_health


@pytest.fixture
def receiver_node(tmp_path):
    identity = load_or_create_tls_identity(tmp_path / "receiver-identity")
    receive_directory = tmp_path / "Received"
    repository = SQLiteRepository(tmp_path / "receiver.sqlite3")
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
        repository=repository,
        require_receiver_approval=False,
    )
    server.start()
    try:
        yield {
            "address": f"127.0.0.1:{port}",
            "identity": identity,
            "receive_directory": receive_directory,
            "token": token,
            "repository": repository,
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
        require_receiver_approval=False,
    )

    received = receiver_node["receive_directory"] / "Integration" / "Inbox" / source.name
    assert response["status"] == "received"
    assert response["sha256"] == digest
    assert received.stat().st_size == len(content)
    assert hashlib.sha256(received.read_bytes()).hexdigest() == digest
    assert not list(receiver_node["receive_directory"].rglob("*.part"))
    incoming = receiver_node["repository"].list_incoming_attempts()
    assert len(incoming) == 1
    assert incoming[0].status is TransferStatus.COMPLETED
    assert incoming[0].bytes_received == len(content)


def test_strict_receiver_waits_for_local_approval_before_streaming(tmp_path) -> None:
    identity = load_or_create_tls_identity(tmp_path / "identity")
    repository = SQLiteRepository(tmp_path / "receiver.sqlite3")
    sender = DeviceProfile(uuid4(), "Sender", "fingerprint", "Windows", datetime.now(UTC))
    repository.save_peer(sender, "127.0.0.1:8765", certificate_pem="certificate")
    grant = repository.get_or_create_receive_grant(sender.device_id)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = NodeServer(
        host="127.0.0.1", port=port,
        certificate_path=identity.certificate_path,
        private_key_path=identity.private_key_path,
        certificate_fingerprint=identity.fingerprint,
        certificate_pem=identity.certificate_pem,
        receive_token="old-global-token", receive_directory=tmp_path / "Received",
        repository=repository,
    )
    source = tmp_path / "confirmed.txt"
    source.write_bytes(b"receiver approved")
    results = []
    errors = []

    def send() -> None:
        try:
            results.append(send_file(
                source, f"127.0.0.1:{port}", identity.certificate_pem,
                grant, source.name, "Inbox", source.stat().st_size,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                require_receiver_approval=True,
            ))
        except Exception as error:
            errors.append(error)

    server.start()
    try:
        worker = Thread(target=send)
        worker.start()
        pending = []
        for _ in range(100):
            pending = repository.list_pending_receive_requests()
            if pending:
                break
            time.sleep(0.05)
        assert len(pending) == 1
        target = tmp_path / "Received" / "Inbox" / source.name
        assert not target.exists()
        assert not list((tmp_path / "Received").rglob("*.part"))
        assert repository.decide_receive_request(pending[0].request_id, approve=True)
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert errors == []
        assert results[0]["status"] == "received"
        assert target.read_bytes() == source.read_bytes()
    finally:
        server.stop()


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
            require_receiver_approval=False,
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
        require_receiver_approval=False,
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
            require_receiver_approval=False,
        )
    assert destination.read_bytes() == content
    incoming = receiver_node["repository"].list_incoming_attempts()
    assert [attempt.status for attempt in incoming] == [
        TransferStatus.FAILED,
        TransferStatus.COMPLETED,
    ]
    assert incoming[0].error_code == "http_409"


def test_upload_progress_cancel_cleans_temporary_file(tmp_path, receiver_node) -> None:
    source = tmp_path / "cancel.bin"
    source.write_bytes(b"x" * (4 * 1024 * 1024))
    identity = receiver_node["identity"]
    cancelled = Event()
    progress = []
    phases = []

    def on_progress(sent: int) -> None:
        progress.append(sent)
        cancelled.set()

    with pytest.raises(TransferCancelled):
        send_file(
            source,
            receiver_node["address"],
            identity.certificate_pem,
            receiver_node["token"],
            source.name,
            "Inbox",
            source.stat().st_size,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            on_progress=on_progress,
            on_phase=phases.append,
            cancel_event=cancelled,
            require_receiver_approval=False,
        )

    assert progress == [1024 * 1024]
    assert phases == ["checking", "transferring"]
    destination = receiver_node["receive_directory"] / "Inbox"
    for _ in range(50):
        if not list(receiver_node["receive_directory"].rglob("*.part")):
            break
        time.sleep(0.02)
    assert not list(receiver_node["receive_directory"].rglob("*.part"))
    assert not (destination / source.name).exists()
    for _ in range(50):
        incoming = receiver_node["repository"].list_incoming_attempts()
        if incoming and incoming[0].status is TransferStatus.FAILED:
            break
        time.sleep(0.02)
    assert incoming[0].status is TransferStatus.FAILED
    assert incoming[0].error_code == "connection_lost"


def test_desktop_transfer_worker_records_failure_and_successful_retry(
    tmp_path, receiver_node
) -> None:
    source = tmp_path / "retry.txt"
    source.write_bytes(b"verified content")
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    now = datetime.now(UTC)
    sender = DeviceProfile(uuid4(), "sender", "sender-key", "Windows", now)
    receiver = DeviceProfile(uuid4(), "receiver", "receiver-key", "Windows", now)
    repository.save_device(sender)
    repository.save_device(receiver)
    plan = TransferPlan(
        uuid4(),
        sender.device_id,
        str(source),
        receiver.device_id,
        "Inbox",
        SourceMode.MANUAL,
        source.name,
        source.stat().st_size,
        PlanStatus.CONFIRMED,
        now,
        now + timedelta(minutes=5),
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    repository.save_plan(plan)
    identity = receiver_node["identity"]
    failures = []
    first = FileTransferWorker(
        plan,
        receiver_node["address"],
        identity.certificate_pem,
        "incorrect-token-but-long-enough-123456",
        repository,
        require_receiver_approval=False,
    )
    first.failed.connect(failures.append)
    first.run()
    assert len(failures) == 1 and "401" in failures[0]
    assert repository.list_records()[0].status is TransferStatus.FAILED

    results = []
    progress = []
    second = FileTransferWorker(
        plan,
        receiver_node["address"],
        identity.certificate_pem,
        receiver_node["token"],
        repository,
        require_receiver_approval=False,
    )
    second.succeeded.connect(results.append)
    second.progress_changed.connect(progress.append)
    second.run()
    assert results[0]["status"] == "received"
    assert progress[-1] == source.stat().st_size
    restarted = SQLiteRepository(tmp_path / "history.sqlite3")
    records = restarted.list_records()
    assert [record.status for record in records] == [
        TransferStatus.COMPLETED,
        TransferStatus.FAILED,
    ]
    assert restarted.get_task(records[0].task_id).bytes_transferred == source.stat().st_size

    cancellations = []
    third = FileTransferWorker(
        plan,
        receiver_node["address"],
        identity.certificate_pem,
        receiver_node["token"],
        repository,
    )
    third.cancelled.connect(cancellations.append)
    third.cancel()
    third.run()
    assert len(cancellations) == 1
    assert repository.list_records()[0].status is TransferStatus.CANCELLED
