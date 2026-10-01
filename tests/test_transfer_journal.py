from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ai_device_bridge.domain.models import (
    DeviceProfile,
    PlanStatus,
    SourceMode,
    TransferPlan,
    TransferStatus,
)
from ai_device_bridge.infrastructure.sqlite_repository import SQLiteRepository
from ai_device_bridge.services.transfer_journal import TransferJournal


def test_attempt_progress_and_crash_reconciliation_survive_restart(tmp_path) -> None:
    database_path = tmp_path / "bridge.sqlite3"
    repository = SQLiteRepository(database_path)
    now = datetime.now(UTC)
    sender = DeviceProfile(uuid4(), "sender", "sender-key", "Windows", now)
    receiver = DeviceProfile(uuid4(), "receiver", "receiver-key", "Windows", now)
    repository.save_device(sender)
    repository.save_device(receiver)
    plan = TransferPlan(
        uuid4(),
        sender.device_id,
        "C:/file.bin",
        receiver.device_id,
        "Inbox",
        SourceMode.MANUAL,
        "file.bin",
        10,
        PlanStatus.CONFIRMED,
        now,
        now + timedelta(minutes=5),
        "a" * 64,
    )
    repository.save_plan(plan)

    journal = TransferJournal.begin_sent(repository, plan)
    journal.transition(TransferStatus.TRANSFERRING)
    journal.progress(6)
    restarted = SQLiteRepository(database_path)
    assert restarted.get_task(journal.task.task_id).bytes_transferred == 6
    assert TransferJournal.reconcile_interrupted(restarted) == 1
    assert TransferJournal.reconcile_interrupted(restarted) == 0
    task = restarted.get_task(journal.task.task_id)
    record = restarted.get_record_for_task(journal.task.task_id)
    assert task.status is record.status is TransferStatus.FAILED
    assert task.error_code == "outcome_unknown"
    assert record.finished_at is not None


def test_completed_attempt_is_not_reclassified_on_restart(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bridge.sqlite3")
    now = datetime.now(UTC)
    sender = DeviceProfile(uuid4(), "sender", "sender-key", "Windows", now)
    receiver = DeviceProfile(uuid4(), "receiver", "receiver-key", "Windows", now)
    repository.save_device(sender)
    repository.save_device(receiver)
    plan = TransferPlan(
        uuid4(),
        sender.device_id,
        "C:/file.bin",
        receiver.device_id,
        "Inbox",
        SourceMode.MANUAL,
        "file.bin",
        10,
        PlanStatus.CONFIRMED,
        now,
        now + timedelta(minutes=5),
        "a" * 64,
    )
    repository.save_plan(plan)
    journal = TransferJournal.begin_sent(repository, plan)
    journal.transition(TransferStatus.TRANSFERRING)
    journal.progress(10)
    journal.transition(TransferStatus.VERIFYING)
    journal.transition(TransferStatus.COMPLETED)

    assert TransferJournal.reconcile_interrupted(repository) == 0
    assert repository.get_task(journal.task.task_id).status is TransferStatus.COMPLETED
