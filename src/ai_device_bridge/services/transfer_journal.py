"""Persist one transfer attempt and its user-visible history consistently."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from ai_device_bridge.domain.models import (
    TransferDirection,
    TransferPlan,
    TransferRecord,
    TransferStatus,
    TransferTask,
)
from ai_device_bridge.infrastructure.sqlite_repository import SQLiteRepository

TERMINAL_STATUSES = {
    TransferStatus.COMPLETED,
    TransferStatus.FAILED,
    TransferStatus.CANCELLED,
    TransferStatus.REJECTED,
}


@dataclass(slots=True)
class TransferJournal:
    repository: SQLiteRepository
    task: TransferTask
    record: TransferRecord

    @classmethod
    def begin_sent(cls, repository: SQLiteRepository, plan: TransferPlan) -> TransferJournal:
        now = datetime.now(UTC)
        task = TransferTask(
            task_id=uuid4(),
            plan_id=plan.plan_id,
            sender_device_id=plan.source_device_id,
            receiver_device_id=plan.target_device_id,
            file_name=plan.file_name,
            file_size_bytes=plan.file_size_bytes,
            expected_sha256=plan.expected_sha256,
            bytes_transferred=0,
            status=TransferStatus.PREPARING,
            created_at=now,
            updated_at=now,
        )
        record = TransferRecord(
            record_id=uuid4(),
            task_id=task.task_id,
            direction=TransferDirection.SENT,
            peer_device_id=plan.target_device_id,
            file_name=plan.file_name,
            file_size_bytes=plan.file_size_bytes,
            status=task.status,
            started_at=now,
            finished_at=None,
        )
        repository.save_transfer_attempt(task, record)
        return cls(repository, task, record)

    def transition(self, status: TransferStatus) -> None:
        self.task.transition_to(status)
        self.record.status = status
        if status in TERMINAL_STATUSES:
            self.record.finished_at = self.task.updated_at
        self.repository.save_transfer_attempt(self.task, self.record)

    def progress(self, bytes_transferred: int, *, persist: bool = True) -> None:
        self.task.update_progress(bytes_transferred)
        if persist:
            self.repository.save_transfer_attempt(self.task, self.record)

    def fail(self, code: str, message: str) -> None:
        self.task.error_code = code
        self.task.error_message = message
        self.transition(TransferStatus.FAILED)

    @classmethod
    def reconcile_interrupted(cls, repository: SQLiteRepository) -> int:
        """Mark attempts left active by a crash as unconfirmed, never successful."""
        count = 0
        for task in repository.list_incomplete_tasks():
            record = repository.get_record_for_task(task.task_id)
            if record is None:
                continue
            cls(repository, task, record).fail(
                "outcome_unknown",
                "程序中断，未收到接收端完成确认；请检查接收端文件后再重试。",
            )
            count += 1
        return count
