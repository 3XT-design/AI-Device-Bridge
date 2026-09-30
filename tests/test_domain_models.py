from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_device_bridge.domain.models import (
    DeviceProfile,
    PlanStatus,
    SourceMode,
    TransferPlan,
    TransferStatus,
    TransferTask,
)


def make_task(status: TransferStatus = TransferStatus.WAITING_RECEIVER) -> TransferTask:
    now = datetime.now(UTC)
    return TransferTask(
        task_id=uuid4(),
        plan_id=uuid4(),
        sender_device_id=uuid4(),
        receiver_device_id=uuid4(),
        file_name="report.txt",
        file_size_bytes=100,
        expected_sha256="a" * 64,
        bytes_transferred=0,
        status=status,
        created_at=now,
        updated_at=now,
    )


def test_transfer_task_follows_allowed_state_transitions() -> None:
    task = make_task()

    task.transition_to(TransferStatus.TRANSFERRING)
    task.update_progress(50)
    task.transition_to(TransferStatus.VERIFYING)
    task.transition_to(TransferStatus.COMPLETED)

    assert task.status is TransferStatus.COMPLETED
    assert task.bytes_transferred == 50
    with pytest.raises(ValueError, match="invalid transfer transition"):
        task.transition_to(TransferStatus.FAILED)


def test_transfer_progress_cannot_move_backwards_or_exceed_size() -> None:
    task = make_task(TransferStatus.TRANSFERRING)
    task.update_progress(50)

    with pytest.raises(ValueError, match="move backwards"):
        task.update_progress(20)
    with pytest.raises(ValueError, match="must not exceed"):
        task.update_progress(101)


def test_task_rejects_invalid_hash_and_naive_timestamp() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="64 hexadecimal"):
        TransferTask(
            task_id=uuid4(),
            plan_id=uuid4(),
            sender_device_id=uuid4(),
            receiver_device_id=uuid4(),
            file_name="report.txt",
            file_size_bytes=100,
            expected_sha256="not-a-hash",
            bytes_transferred=0,
            status=TransferStatus.WAITING_RECEIVER,
            created_at=now,
            updated_at=now,
        )


def test_plan_requires_expiration_after_creation() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="later than created_at"):
        TransferPlan(
            plan_id=uuid4(),
            source_device_id=uuid4(),
            source_path="C:/data/report.txt",
            target_device_id=uuid4(),
            target_directory="D:/received",
            source_mode=SourceMode.MANUAL,
            file_name="report.txt",
            file_size_bytes=10,
            status=PlanStatus.READY_FOR_REVIEW,
            created_at=now,
            expires_at=now - timedelta(seconds=1),
        )


def test_device_requires_non_empty_identity_fields() -> None:
    with pytest.raises(ValueError, match="device_name"):
        DeviceProfile(
            device_id=uuid4(),
            device_name=" ",
            public_key_fingerprint="fingerprint",
            platform="Windows",
            created_at=datetime.now(UTC),
        )
