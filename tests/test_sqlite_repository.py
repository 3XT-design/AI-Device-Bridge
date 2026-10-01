import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_device_bridge.domain.models import (
    AIPlanEvidence,
    DeviceProfile,
    IncomingTransferAttempt,
    PlanStatus,
    SourceMode,
    TransferDirection,
    TransferPlan,
    TransferRecord,
    TransferStatus,
    TransferTask,
)
from ai_device_bridge.infrastructure.sqlite_repository import SCHEMA_VERSION, SQLiteRepository


def test_sqlite_repository_round_trips_core_entities(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    now = datetime.now(UTC)
    sender = DeviceProfile(uuid4(), "Laptop", "sha256:laptop-key", "Windows", now)
    receiver = DeviceProfile(uuid4(), "Desktop", "sha256:desktop-key", "Windows", now)
    plan = TransferPlan(
        plan_id=uuid4(),
        source_device_id=sender.device_id,
        source_path="C:/work/report.txt",
        target_device_id=receiver.device_id,
        target_directory="D:/BridgeInbox",
        source_mode=SourceMode.MANUAL,
        file_name="report.txt",
        file_size_bytes=128,
        status=PlanStatus.CONFIRMED,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        expected_sha256="b" * 64,
    )
    task = TransferTask(
        task_id=uuid4(),
        plan_id=plan.plan_id,
        sender_device_id=sender.device_id,
        receiver_device_id=receiver.device_id,
        file_name="report.txt",
        file_size_bytes=128,
        expected_sha256="a" * 64,
        bytes_transferred=128,
        status=TransferStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    record = TransferRecord(
        record_id=uuid4(),
        task_id=task.task_id,
        direction=TransferDirection.SENT,
        peer_device_id=receiver.device_id,
        file_name="report.txt",
        file_size_bytes=128,
        status=TransferStatus.COMPLETED,
        started_at=now,
        finished_at=now,
    )

    repository.save_device(sender)
    repository.save_device(receiver)
    repository.save_plan(plan)
    repository.save_task(task)
    repository.save_record(record)

    assert repository.get_device(sender.device_id) == sender
    assert repository.list_devices() == [receiver, sender]
    assert repository.get_plan(plan.plan_id) == plan
    assert repository.get_task(task.task_id) == task
    assert repository.list_records() == [record]


def test_transfer_attempt_is_atomic_and_listed_after_restart(tmp_path) -> None:
    database_path = tmp_path / "bridge.sqlite3"
    repository = SQLiteRepository(database_path)
    now = datetime.now(UTC)
    sender = DeviceProfile(uuid4(), "Sender", "sender-key", "Windows", now)
    receiver = DeviceProfile(uuid4(), "Receiver", "receiver-key", "Windows", now)
    repository.save_device(sender)
    repository.save_device(receiver)
    plan = TransferPlan(
        uuid4(),
        sender.device_id,
        "C:/report.txt",
        receiver.device_id,
        "Inbox",
        SourceMode.MANUAL,
        "report.txt",
        6,
        PlanStatus.CONFIRMED,
        now,
        now + timedelta(minutes=5),
        "a" * 64,
    )
    repository.save_plan(plan)
    task = TransferTask(
        uuid4(),
        plan.plan_id,
        sender.device_id,
        receiver.device_id,
        "report.txt",
        6,
        "a" * 64,
        0,
        TransferStatus.WAITING_RECEIVER,
        now,
        now,
    )
    record = TransferRecord(
        uuid4(),
        task.task_id,
        TransferDirection.SENT,
        uuid4(),
        "report.txt",
        6,
        TransferStatus.WAITING_RECEIVER,
        now,
        None,
    )

    with pytest.raises(sqlite3.IntegrityError):
        repository.save_transfer_attempt(task, record)
    assert repository.get_task(task.task_id) is None

    record.peer_device_id = receiver.device_id
    repository.save_transfer_attempt(task, record)
    restarted = SQLiteRepository(database_path)
    assert restarted.list_tasks() == [task]
    assert restarted.list_records() == [record]


def test_incoming_history_recovers_incomplete_attempt_without_false_success(tmp_path) -> None:
    database_path = tmp_path / "bridge.sqlite3"
    repository = SQLiteRepository(database_path)
    now = datetime.now(UTC)
    interrupted = IncomingTransferAttempt(
        uuid4(),
        "large.bin",
        "Inbox",
        100,
        "a" * 64,
        40,
        TransferStatus.TRANSFERRING,
        now,
        now,
    )
    completed = IncomingTransferAttempt(
        uuid4(),
        "done.bin",
        "Inbox",
        2,
        "b" * 64,
        2,
        TransferStatus.COMPLETED,
        now,
        now,
    )
    repository.save_incoming_attempt(interrupted)
    repository.save_incoming_attempt(completed)

    restarted = SQLiteRepository(database_path)
    assert restarted.reconcile_incomplete_incoming() == 1
    assert restarted.reconcile_incomplete_incoming() == 0
    records = {row.attempt_id: row for row in restarted.list_incoming_attempts()}
    assert records[interrupted.attempt_id].status is TransferStatus.FAILED
    assert records[interrupted.attempt_id].error_code == "outcome_unknown"
    assert records[interrupted.attempt_id].bytes_received == 40
    assert records[completed.attempt_id].status is TransferStatus.COMPLETED


def test_local_node_id_is_stable_across_repository_restarts(tmp_path) -> None:
    database_path = tmp_path / "bridge.sqlite3"
    first_id = SQLiteRepository(database_path).get_or_create_node_id()
    second_id = SQLiteRepository(database_path).get_or_create_node_id()

    assert first_id == second_id


def test_local_device_profile_is_persisted_and_updates_name(tmp_path) -> None:
    database_path = tmp_path / "bridge.sqlite3"
    repository = SQLiteRepository(database_path)
    first = repository.get_or_create_local_device("Laptop", "Windows")
    restarted = SQLiteRepository(database_path)
    updated = restarted.get_or_create_local_device("Laptop-2", "Windows")

    assert first.device_id == updated.device_id
    assert updated.device_name == "Laptop-2"


def test_peer_address_is_saved_and_removed_with_peer(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    peer = DeviceProfile(uuid4(), "Desktop", "not-configured", "Windows", datetime.now(UTC))

    repository.save_peer(peer, "192.168.1.20:8765")
    assert repository.get_device(peer.device_id) == peer
    assert repository.list_paired_devices() == [peer]
    assert repository.get_peer_address(peer.device_id) == "192.168.1.20:8765"

    assert repository.remove_device(peer.device_id)
    assert repository.get_device(peer.device_id) is None
    assert repository.list_paired_devices() == []
    assert repository.get_peer_address(peer.device_id) is None


def test_unpair_preserves_device_referenced_by_transfer_plan(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    now = datetime.now(UTC)
    sender = repository.get_or_create_local_device("Laptop", "Windows")
    receiver = DeviceProfile(uuid4(), "Desktop", "a" * 64, "Windows", now)
    repository.save_peer(
        receiver,
        "192.168.1.20:8765",
        certificate_pem="certificate",
        transfer_token="token",
    )
    plan = TransferPlan(
        plan_id=uuid4(),
        source_device_id=sender.device_id,
        source_path="C:/work/report.txt",
        target_device_id=receiver.device_id,
        target_directory="Inbox",
        source_mode=SourceMode.MANUAL,
        file_name="report.txt",
        file_size_bytes=12,
        status=PlanStatus.CONFIRMED,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        expected_sha256="b" * 64,
    )
    repository.save_plan(plan)

    assert repository.remove_device(receiver.device_id)

    assert repository.get_plan(plan.plan_id) == plan
    assert repository.get_device(receiver.device_id) == receiver
    assert repository.list_paired_devices() == []
    assert repository.get_peer_address(receiver.device_id) is None
    assert repository.get_peer_security(receiver.device_id) == ("", "")


def test_peer_security_and_receive_token_are_persisted(tmp_path) -> None:
    database_path = tmp_path / "bridge.sqlite3"
    repository = SQLiteRepository(database_path)
    peer = DeviceProfile(uuid4(), "Desktop", "a" * 64, "Windows", datetime.now(UTC))
    repository.save_peer(peer, "192.168.1.20:8765", "peer certificate", "peer token")

    assert repository.get_peer_security(peer.device_id) == ("peer token", "peer certificate")
    assert (
        repository.get_or_create_receive_token()
        == SQLiteRepository(database_path).get_or_create_receive_token()
    )


def test_repository_migrates_existing_plan_table_for_file_hash(tmp_path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE devices (
                device_id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                public_key_fingerprint TEXT NOT NULL,
                platform TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY,
                source_device_id TEXT NOT NULL REFERENCES devices(device_id),
                source_path TEXT NOT NULL,
                target_device_id TEXT NOT NULL REFERENCES devices(device_id),
                target_directory TEXT NOT NULL,
                source_mode TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )

    SQLiteRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(plans)")}
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert "expected_sha256" in columns
    assert "ai_plan_evidence" in tables
    assert SQLiteRepository(database_path).schema_version() == SCHEMA_VERSION


def test_m4_unversioned_database_upgrades_without_losing_user_data(tmp_path) -> None:
    path = tmp_path / "bridge.sqlite3"
    previous = SQLiteRepository(path)
    now = datetime.now(UTC)
    local = previous.get_or_create_local_device("Laptop", "Windows")
    peer = DeviceProfile(uuid4(), "Desktop", "fingerprint", "Windows", now)
    previous.save_peer(peer, "10.0.0.2:8765", "certificate", "saved-peer-token")
    token = previous.get_or_create_receive_token()
    plan = TransferPlan(
        uuid4(), local.device_id, "C:/report.txt", peer.device_id, "Inbox",
        SourceMode.MANUAL, "report.txt", 1, PlanStatus.CONFIRMED,
        now, now + timedelta(minutes=15), "a" * 64,
    )
    previous.save_plan(plan)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 0")

    upgraded = SQLiteRepository(path)
    assert upgraded.schema_version() == SCHEMA_VERSION
    assert upgraded.get_or_create_receive_token() == token
    assert upgraded.get_peer_security(peer.device_id) == ("saved-peer-token", "certificate")
    assert upgraded.get_plan(plan.plan_id) == plan
    assert SQLiteRepository(path).schema_version() == SCHEMA_VERSION


def test_m5_upgrade_preserves_data_without_trusting_old_shared_token(tmp_path) -> None:
    path = tmp_path / "bridge.sqlite3"
    previous = SQLiteRepository(path)
    sender = DeviceProfile(uuid4(), "Sender", "fingerprint", "Windows", datetime.now(UTC))
    previous.save_peer(sender, "10.0.0.2:8765", "certificate", "outbound-token")
    old_shared = previous.get_or_create_receive_token()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE receive_requests")
        connection.execute("DROP TABLE receive_grants")
        connection.execute("PRAGMA user_version = 3")

    upgraded = SQLiteRepository(path)
    assert upgraded.schema_version() == SCHEMA_VERSION
    assert upgraded.get_or_create_receive_token() == old_shared
    assert upgraded.get_peer_security(sender.device_id) == ("outbound-token", "certificate")
    assert upgraded.authorized_sender(old_shared) is None
    assert upgraded.list_receive_grants() == []
    grant = upgraded.get_or_create_receive_grant(sender.device_id)
    assert upgraded.authorized_sender(grant) == sender.device_id

    changed = DeviceProfile(
        sender.device_id, "Sender", "changed-fingerprint", "Windows", datetime.now(UTC)
    )
    upgraded.save_peer(changed, "10.0.0.2:8765", "new-certificate")
    assert upgraded.authorized_sender(grant) is None
    assert upgraded.get_peer_security(sender.device_id) == ("", "new-certificate")


def test_newer_schema_is_rejected_without_modifying_database(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="高于程序支持"):
        SQLiteRepository(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999


def test_cleanup_removes_only_old_terminal_history_and_keeps_files_and_plans(tmp_path) -> None:
    path = tmp_path / "bridge.sqlite3"
    repository = SQLiteRepository(path)
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    source = tmp_path / "keep.txt"
    source.write_text("keep", encoding="utf-8")
    local = repository.get_or_create_local_device("Laptop", "Windows")
    peer = DeviceProfile(uuid4(), "Desktop", "fingerprint", "Windows", now)
    repository.save_peer(peer, "10.0.0.2:8765", "certificate", "token")
    plan = TransferPlan(
        uuid4(), local.device_id, str(source), peer.device_id, "Inbox",
        SourceMode.MANUAL, source.name, 4, PlanStatus.CONFIRMED,
        old, now + timedelta(minutes=15), "a" * 64,
    )
    repository.save_plan(plan)
    for status, when in (
        (TransferStatus.COMPLETED, old),
        (TransferStatus.FAILED, now),
        (TransferStatus.TRANSFERRING, old),
    ):
        task = TransferTask(
            uuid4(), plan.plan_id, local.device_id, peer.device_id,
            source.name, 4, "a" * 64, 0, status, when, when,
        )
        record = TransferRecord(
            uuid4(), task.task_id, TransferDirection.SENT, peer.device_id,
            source.name, 4, status, when, when,
        )
        repository.save_transfer_attempt(task, record)
        incoming = IncomingTransferAttempt(
            uuid4(), source.name, "Inbox", 4, "a" * 64, 0,
            status, when, when,
        )
        repository.save_incoming_attempt(incoming)
    assert len(repository.list_history_entries()) == 6

    assert repository.prune_terminal_history(now - timedelta(days=30)) == (1, 1)
    restarted = SQLiteRepository(path)
    assert len(restarted.list_history_entries()) == 4
    assert {row.status for row in restarted.list_tasks()} == {
        TransferStatus.FAILED, TransferStatus.TRANSFERRING,
    }
    assert restarted.get_plan(plan.plan_id) == plan
    assert restarted.get_peer_security(peer.device_id) == ("token", "certificate")
    assert source.read_text(encoding="utf-8") == "keep"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.prune_terminal_history(datetime.now())


def test_ai_plan_evidence_survives_restart_and_hidden_plan(tmp_path) -> None:
    database_path = tmp_path / "bridge.sqlite3"
    repository = SQLiteRepository(database_path)
    now = datetime.now(UTC)
    sender = DeviceProfile(uuid4(), "Laptop", "sender", "Windows", now)
    receiver = DeviceProfile(uuid4(), "Desktop", "receiver", "Windows", now)
    repository.save_device(sender)
    repository.save_peer(receiver, "192.168.1.20:8765")
    plan = TransferPlan(
        uuid4(), sender.device_id, "C:/allowed/report.pdf", receiver.device_id,
        "Inbox", SourceMode.NATURAL_LANGUAGE, "report.pdf", 12, PlanStatus.CONFIRMED,
        now, now + timedelta(minutes=15), "a" * 64,
    )
    evidence = AIPlanEvidence(
        plan.plan_id, "找上周的报告", "C:/allowed",
        (("F00001", "report.pdf"), ("F00002", "other-report.pdf")), "F00001", now,
    )
    repository.save_ai_plan(plan, evidence)

    restarted = SQLiteRepository(database_path)
    assert restarted.get_plan(plan.plan_id) == plan
    assert restarted.get_ai_plan_evidence(plan.plan_id) == evidence
    assert restarted.delete_plan(plan.plan_id)
    assert restarted.get_ai_plan_evidence(plan.plan_id) == evidence

    with pytest.raises(ValueError, match="natural-language"):
        plan.source_mode = SourceMode.MANUAL
        repository.save_ai_plan(plan, evidence)


def test_delete_plan_hides_from_recent_list_and_preserves_transfer_history(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    now = datetime.now(UTC)
    sender = DeviceProfile(uuid4(), "Laptop", "sender", "Windows", now)
    receiver = DeviceProfile(uuid4(), "Desktop", "receiver", "Windows", now)
    plan = TransferPlan(
        plan_id=uuid4(),
        source_device_id=sender.device_id,
        source_path="C:/work/report.txt",
        target_device_id=receiver.device_id,
        target_directory="Inbox",
        source_mode=SourceMode.MANUAL,
        file_name="report.txt",
        file_size_bytes=7,
        status=PlanStatus.COMPLETED,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        expected_sha256="c" * 64,
    )
    task = TransferTask(
        task_id=uuid4(),
        plan_id=plan.plan_id,
        sender_device_id=sender.device_id,
        receiver_device_id=receiver.device_id,
        file_name=plan.file_name,
        file_size_bytes=plan.file_size_bytes,
        expected_sha256=plan.expected_sha256,
        bytes_transferred=7,
        status=TransferStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    repository.save_device(sender)
    repository.save_device(receiver)
    repository.save_plan(plan)
    repository.save_task(task)

    assert repository.delete_plan(plan.plan_id)

    assert repository.list_plans() == []
    assert repository.get_plan(plan.plan_id).status is PlanStatus.DELETED
    assert repository.get_task(task.task_id) == task
    assert not repository.delete_plan(plan.plan_id)
