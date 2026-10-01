"""Core domain objects for devices and file-transfer workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class PlanStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_CLARIFICATION = "needs_clarification"
    READY_FOR_REVIEW = "ready_for_review"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    DELETED = "deleted"


class SourceMode(StrEnum):
    MANUAL = "manual"
    NATURAL_LANGUAGE = "natural_language"


class TransferStatus(StrEnum):
    PREPARING = "preparing"
    WAITING_RECEIVER = "waiting_receiver"
    REJECTED = "rejected"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TransferDirection(StrEnum):
    SENT = "sent"
    RECEIVED = "received"


@dataclass(slots=True)
class DeviceProfile:
    device_id: UUID
    device_name: str
    public_key_fingerprint: str
    platform: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.device_name, "device_name")
        _require_text(self.public_key_fingerprint, "public_key_fingerprint")
        _require_text(self.platform, "platform")
        _require_aware_datetime(self.created_at, "created_at")


@dataclass(slots=True)
class FileQuery:
    keywords: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    modified_after: datetime | None = None
    modified_before: datetime | None = None

    def __post_init__(self) -> None:
        if self.modified_after is not None:
            _require_aware_datetime(self.modified_after, "modified_after")
        if self.modified_before is not None:
            _require_aware_datetime(self.modified_before, "modified_before")
        if (
            self.modified_after is not None
            and self.modified_before is not None
            and self.modified_after > self.modified_before
        ):
            raise ValueError("modified_after must not be later than modified_before")
        self.keywords = [_require_text(value, "keyword") for value in self.keywords]
        self.extensions = [_normalize_extension(value) for value in self.extensions]


@dataclass(slots=True)
class ParsedTransferIntent:
    file_query: FileQuery
    target_device_name: str | None = None
    target_directory_name: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None

    def __post_init__(self) -> None:
        if self.target_device_name is not None:
            self.target_device_name = _require_text(self.target_device_name, "target_device_name")
        if self.target_directory_name is not None:
            self.target_directory_name = _require_text(
                self.target_directory_name, "target_directory_name"
            )
        if self.clarification_question is not None:
            self.clarification_question = _require_text(
                self.clarification_question, "clarification_question"
            )
        if self.needs_clarification and self.clarification_question is None:
            raise ValueError("clarification_question is required when clarification is needed")


@dataclass(slots=True)
class TransferPlan:
    plan_id: UUID
    source_device_id: UUID
    source_path: str
    target_device_id: UUID
    target_directory: str
    source_mode: SourceMode
    file_name: str
    file_size_bytes: int
    status: PlanStatus
    created_at: datetime
    expires_at: datetime
    expected_sha256: str = ""

    def __post_init__(self) -> None:
        _require_text(self.source_path, "source_path")
        _require_text(self.target_directory, "target_directory")
        _require_text(self.file_name, "file_name")
        _require_nonnegative_int(self.file_size_bytes, "file_size_bytes")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.expected_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", self.expected_sha256):
            raise ValueError("expected_sha256 must contain exactly 64 hexadecimal characters")
        self.expected_sha256 = self.expected_sha256.lower()


@dataclass(slots=True)
class TransferTask:
    task_id: UUID
    plan_id: UUID
    sender_device_id: UUID
    receiver_device_id: UUID
    file_name: str
    file_size_bytes: int
    expected_sha256: str
    bytes_transferred: int
    status: TransferStatus
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.file_name, "file_name")
        _require_nonnegative_int(self.file_size_bytes, "file_size_bytes")
        _require_nonnegative_int(self.bytes_transferred, "bytes_transferred")
        if self.bytes_transferred > self.file_size_bytes:
            raise ValueError("bytes_transferred must not exceed file_size_bytes")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.expected_sha256):
            raise ValueError("expected_sha256 must contain exactly 64 hexadecimal characters")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")

    def update_progress(self, bytes_transferred: int) -> None:
        """Record monotonic byte progress while a task is transferring."""
        _require_nonnegative_int(bytes_transferred, "bytes_transferred")
        if self.status is not TransferStatus.TRANSFERRING:
            raise ValueError("progress can only change while the task is transferring")
        if bytes_transferred < self.bytes_transferred:
            raise ValueError("bytes_transferred cannot move backwards")
        if bytes_transferred > self.file_size_bytes:
            raise ValueError("bytes_transferred must not exceed file_size_bytes")
        self.bytes_transferred = bytes_transferred
        self.updated_at = _now_utc()

    def transition_to(self, status: TransferStatus) -> None:
        """Apply one permitted transfer-state transition."""
        allowed = {
            TransferStatus.PREPARING: {
                TransferStatus.WAITING_RECEIVER,
                TransferStatus.TRANSFERRING,
                TransferStatus.CANCELLED,
                TransferStatus.FAILED,
            },
            TransferStatus.WAITING_RECEIVER: {
                TransferStatus.REJECTED,
                TransferStatus.TRANSFERRING,
                TransferStatus.CANCELLED,
                TransferStatus.FAILED,
            },
            TransferStatus.TRANSFERRING: {
                TransferStatus.VERIFYING,
                TransferStatus.CANCELLED,
                TransferStatus.FAILED,
            },
            TransferStatus.VERIFYING: {
                TransferStatus.COMPLETED,
                TransferStatus.FAILED,
            },
        }
        if status not in allowed.get(self.status, set()):
            raise ValueError(f"invalid transfer transition: {self.status} -> {status}")
        self.status = status
        self.updated_at = _now_utc()


@dataclass(slots=True)
class TransferRecord:
    record_id: UUID
    task_id: UUID
    direction: TransferDirection
    peer_device_id: UUID
    file_name: str
    file_size_bytes: int
    status: TransferStatus
    started_at: datetime | None
    finished_at: datetime | None

    def __post_init__(self) -> None:
        _require_text(self.file_name, "file_name")
        _require_nonnegative_int(self.file_size_bytes, "file_size_bytes")
        if self.started_at is not None:
            _require_aware_datetime(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_aware_datetime(self.finished_at, "finished_at")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at must not be earlier than started_at")


@dataclass(slots=True)
class IncomingTransferAttempt:
    """Receiver-side history without inventing an authenticated sender identity."""

    attempt_id: UUID
    file_name: str
    target_directory: str
    file_size_bytes: int
    expected_sha256: str
    bytes_received: int
    status: TransferStatus
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.file_name, "file_name")
        _require_text(self.target_directory, "target_directory")
        _require_nonnegative_int(self.file_size_bytes, "file_size_bytes")
        _require_nonnegative_int(self.bytes_received, "bytes_received")
        if self.bytes_received > self.file_size_bytes:
            raise ValueError("bytes_received must not exceed file_size_bytes")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.expected_sha256):
            raise ValueError("expected_sha256 must contain exactly 64 hexadecimal characters")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        self.expected_sha256 = self.expected_sha256.lower()

    def update_progress(self, bytes_received: int) -> None:
        if self.status is not TransferStatus.TRANSFERRING:
            raise ValueError("receiver progress requires an active transfer")
        _require_nonnegative_int(bytes_received, "bytes_received")
        if bytes_received < self.bytes_received or bytes_received > self.file_size_bytes:
            raise ValueError("receiver progress must be monotonic and within the declared size")
        self.bytes_received = bytes_received
        self.updated_at = _now_utc()

    def transition_to(self, status: TransferStatus) -> None:
        allowed = {
            TransferStatus.TRANSFERRING: {TransferStatus.VERIFYING, TransferStatus.FAILED},
            TransferStatus.VERIFYING: {TransferStatus.COMPLETED, TransferStatus.FAILED},
        }
        if status not in allowed.get(self.status, set()):
            raise ValueError(f"invalid receiver transition: {self.status} -> {status}")
        self.status = status
        self.updated_at = _now_utc()


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")


def _normalize_extension(value: str) -> str:
    extension = _require_text(value, "extension").lower()
    if not extension.startswith("."):
        extension = f".{extension}"
    if extension == ".":
        raise ValueError("extension must contain characters after the dot")
    return extension


def _now_utc() -> datetime:
    return datetime.now(UTC)


def new_id() -> UUID:
    """Create a UUID for a new domain object."""
    return uuid4()
