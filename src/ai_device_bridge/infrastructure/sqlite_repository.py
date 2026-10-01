"""SQLite persistence, migrations, peer grants and transfer history."""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

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

SCHEMA_VERSION = 4


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    direction: TransferDirection
    file_name: str
    status: TransferStatus
    occurred_at: datetime
    error_message: str | None


@dataclass(frozen=True, slots=True)
class ReceiveRequest:
    request_id: UUID
    sender_device_id: UUID
    file_name: str
    target_directory: str
    file_size_bytes: int
    expected_sha256: str
    status: str
    expires_at: datetime


class SQLiteRepository:
    """Store devices, plans, tasks, and transfer history in a local SQLite file."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}；请使用较新版本。"
                )
            connection.execute("BEGIN IMMEDIATE")
            if version < 1:
                _execute_schema(connection,
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL,
                    public_key_fingerprint TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_metadata (
                    metadata_key TEXT PRIMARY KEY,
                    metadata_value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS peer_addresses (
                    device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
                    address TEXT NOT NULL,
                    transfer_token TEXT NOT NULL DEFAULT '',
                    certificate_pem TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    source_device_id TEXT NOT NULL REFERENCES devices(device_id),
                    source_path TEXT NOT NULL,
                    target_device_id TEXT NOT NULL REFERENCES devices(device_id),
                    target_directory TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    expected_sha256 TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
                    sender_device_id TEXT NOT NULL REFERENCES devices(device_id),
                    receiver_device_id TEXT NOT NULL REFERENCES devices(device_id),
                    file_name TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
                    expected_sha256 TEXT NOT NULL,
                    bytes_transferred INTEGER NOT NULL CHECK (bytes_transferred >= 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    CHECK (bytes_transferred <= file_size_bytes)
                );

                CREATE TABLE IF NOT EXISTS transfer_records (
                    record_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    direction TEXT NOT NULL,
                    peer_device_id TEXT NOT NULL REFERENCES devices(device_id),
                    file_name TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
                    status TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS incoming_transfers (
                    attempt_id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    target_directory TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
                    expected_sha256 TEXT NOT NULL,
                    bytes_received INTEGER NOT NULL CHECK (bytes_received >= 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    CHECK (bytes_received <= file_size_bytes)
                );

                CREATE INDEX IF NOT EXISTS idx_plans_created_at
                    ON plans(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_created_at
                    ON tasks(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_records_finished_at
                    ON transfer_records(finished_at DESC);
                CREATE INDEX IF NOT EXISTS idx_incoming_created_at
                    ON incoming_transfers(created_at DESC);
                """
                )
                plan_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(plans)")
                }
                if "expected_sha256" not in plan_columns:
                    connection.execute(
                        "ALTER TABLE plans ADD COLUMN expected_sha256 TEXT NOT NULL DEFAULT ''"
                    )
                peer_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(peer_addresses)")
                }
                if "transfer_token" not in peer_columns:
                    connection.execute(
                        "ALTER TABLE peer_addresses ADD COLUMN transfer_token "
                        "TEXT NOT NULL DEFAULT ''"
                    )
                if "certificate_pem" not in peer_columns:
                    connection.execute(
                        "ALTER TABLE peer_addresses ADD COLUMN certificate_pem "
                        "TEXT NOT NULL DEFAULT ''"
                    )
                connection.execute("PRAGMA user_version = 1")
            if version < 2:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS ai_plan_evidence (
                        plan_id TEXT PRIMARY KEY REFERENCES plans(plan_id),
                        request_text TEXT NOT NULL,
                        authorized_root TEXT NOT NULL,
                        candidates_json TEXT NOT NULL,
                        selected_candidate_id TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                connection.execute("PRAGMA user_version = 2")
            if version < 3:
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_status_updated "
                    "ON tasks(status, updated_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_incoming_status_updated "
                    "ON incoming_transfers(status, updated_at)"
                )
                connection.execute("PRAGMA user_version = 3")
            if version < 4:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS receive_grants (
                        device_id TEXT PRIMARY KEY REFERENCES peer_addresses(device_id)
                            ON DELETE CASCADE,
                        token TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS receive_requests (
                        request_id TEXT PRIMARY KEY,
                        sender_device_id TEXT NOT NULL REFERENCES devices(device_id),
                        file_name TEXT NOT NULL,
                        target_directory TEXT NOT NULL,
                        file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
                        expected_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_receive_requests_status_expiry "
                    "ON receive_requests(status, expires_at)"
                )
                connection.execute("PRAGMA user_version = 4")

    def schema_version(self) -> int:
        with self._connection() as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]

    def get_or_create_node_id(self) -> UUID:
        """Return this installation's stable node ID, creating it on first run."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT metadata_value FROM app_metadata WHERE metadata_key = 'node_id'"
            ).fetchone()
            if row is not None:
                return UUID(row["metadata_value"])
            node_id = uuid4()
            connection.execute(
                "INSERT INTO app_metadata (metadata_key, metadata_value) VALUES ('node_id', ?)",
                (str(node_id),),
            )
        return node_id

    def get_or_create_receive_token(self) -> str:
        """Return a random bearer token that the owner can share with a sender."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT metadata_value FROM app_metadata WHERE metadata_key = 'receive_token'"
            ).fetchone()
            if row is not None:
                return row["metadata_value"]
            token = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO app_metadata (metadata_key, metadata_value) "
                "VALUES ('receive_token', ?)",
                (token,),
            )
        return token

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT metadata_value FROM app_metadata WHERE metadata_key = ?", (key,)
            ).fetchone()
        return default if row is None else row["metadata_value"]

    def save_setting(self, key: str, value: str) -> None:
        if not key.strip():
            raise ValueError("setting key must be non-empty")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO app_metadata (metadata_key, metadata_value) VALUES (?, ?) "
                "ON CONFLICT(metadata_key) DO UPDATE SET metadata_value=excluded.metadata_value",
                (key, value),
            )

    def get_or_create_local_device(self, device_name: str, platform: str) -> DeviceProfile:
        """Return the stable local device profile, updating its display metadata."""
        device_id = self.get_or_create_node_id()
        existing = self.get_device(device_id)
        if existing is None:
            existing = DeviceProfile(
                device_id=device_id,
                device_name=device_name,
                public_key_fingerprint="local-installation",
                platform=platform,
                created_at=datetime.now(UTC),
            )
        else:
            existing.device_name = device_name
            existing.platform = platform
        self.save_device(existing)
        return existing

    def save_device(self, device: DeviceProfile) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO devices VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     device_name=excluded.device_name,
                     public_key_fingerprint=excluded.public_key_fingerprint,
                     platform=excluded.platform,
                     created_at=excluded.created_at""",
                (
                    str(device.device_id),
                    device.device_name,
                    device.public_key_fingerprint,
                    device.platform,
                    _datetime_to_text(device.created_at),
                ),
            )

    def save_peer(
        self,
        device: DeviceProfile,
        address: str,
        certificate_pem: str = "",
        transfer_token: str = "",
    ) -> None:
        """Save a confirmed peer and its last known network address atomically."""
        normalized_address = address.strip()
        if not normalized_address:
            raise ValueError("address must be a non-empty string")
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT public_key_fingerprint FROM devices WHERE device_id = ?",
                (str(device.device_id),),
            ).fetchone()
            if existing is not None and existing["public_key_fingerprint"] != (
                device.public_key_fingerprint
            ):
                connection.execute(
                    "DELETE FROM receive_grants WHERE device_id = ?", (str(device.device_id),)
                )
                connection.execute(
                    "UPDATE receive_requests SET status = 'rejected' "
                    "WHERE sender_device_id = ? AND status IN ('pending', 'approved')",
                    (str(device.device_id),),
                )
                connection.execute(
                    "UPDATE peer_addresses SET transfer_token = '', certificate_pem = '' "
                    "WHERE device_id = ?", (str(device.device_id),)
                )
            connection.execute(
                """INSERT INTO devices VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     device_name=excluded.device_name,
                     public_key_fingerprint=excluded.public_key_fingerprint,
                     platform=excluded.platform,
                     created_at=excluded.created_at""",
                (
                    str(device.device_id),
                    device.device_name,
                    device.public_key_fingerprint,
                    device.platform,
                    _datetime_to_text(device.created_at),
                ),
            )
            connection.execute(
                """INSERT INTO peer_addresses
                       (device_id, address, transfer_token, certificate_pem)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     address=excluded.address,
                     transfer_token=CASE WHEN excluded.transfer_token = ''
                         THEN peer_addresses.transfer_token ELSE excluded.transfer_token END,
                     certificate_pem=CASE WHEN excluded.certificate_pem = ''
                         THEN peer_addresses.certificate_pem ELSE excluded.certificate_pem END""",
                (str(device.device_id), normalized_address, transfer_token, certificate_pem),
            )

    def get_peer_address(self, device_id: UUID) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT address FROM peer_addresses WHERE device_id = ?",
                (str(device_id),),
            ).fetchone()
        return None if row is None else row["address"]

    def get_peer_security(self, device_id: UUID) -> tuple[str, str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT transfer_token, certificate_pem FROM peer_addresses WHERE device_id = ?",
                (str(device_id),),
            ).fetchone()
        return ("", "") if row is None else (row["transfer_token"], row["certificate_pem"])

    def save_peer_token(self, device_id: UUID, transfer_token: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE peer_addresses SET transfer_token = ? WHERE device_id = ?",
                (transfer_token.strip(), str(device_id)),
            )
            if cursor.rowcount == 0:
                raise ValueError("请先保存该配对设备及其地址。")

    def get_or_create_receive_grant(self, device_id: UUID) -> str:
        """Issue a receiver-side secret only for an actively paired sender."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT token FROM receive_grants WHERE device_id = ?", (str(device_id),)
            ).fetchone()
            if row is not None:
                return row["token"]
            if connection.execute(
                "SELECT 1 FROM peer_addresses WHERE device_id = ?", (str(device_id),)
            ).fetchone() is None:
                raise ValueError("请先与该发送设备完成配对。")
            token = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO receive_grants VALUES (?, ?, ?)",
                (str(device_id), token, _datetime_to_text(datetime.now(UTC))),
            )
        return token

    def rotate_receive_grant(self, device_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE receive_grants SET token = ?, created_at = ? WHERE device_id = ?",
                (token, _datetime_to_text(datetime.now(UTC)), str(device_id)),
            )
            if cursor.rowcount == 0:
                raise ValueError("该设备尚无接收授权码，请先配对。")
            connection.execute(
                "UPDATE receive_requests SET status = 'rejected' "
                "WHERE sender_device_id = ? AND status IN ('pending', 'approved')",
                (str(device_id),),
            )
        return token

    def list_receive_grants(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT token FROM receive_grants").fetchall()
        return [row["token"] for row in rows]

    def authorized_sender(self, token: str) -> UUID | None:
        if not token:
            return None
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT device_id, token FROM receive_grants"
            ).fetchall()
        for row in rows:
            if hmac.compare_digest(row["token"], token):
                return UUID(row["device_id"])
        return None

    def create_receive_request(
        self,
        sender_device_id: UUID,
        file_name: str,
        target_directory: str,
        file_size_bytes: int,
        expected_sha256: str,
    ) -> ReceiveRequest:
        now = datetime.now(UTC)
        request = ReceiveRequest(
            uuid4(), sender_device_id, file_name, target_directory,
            file_size_bytes, expected_sha256, "pending", now + timedelta(seconds=90),
        )
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM receive_grants WHERE device_id = ?", (str(sender_device_id),)
            ).fetchone() is None:
                raise ValueError("发送设备未获授权。")
            connection.execute(
                "INSERT INTO receive_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(request.request_id), str(sender_device_id), file_name,
                    target_directory, file_size_bytes, expected_sha256,
                    request.status, _datetime_to_text(now),
                    _datetime_to_text(request.expires_at),
                ),
            )
        return request

    def expire_receive_requests(self, *, all_pending: bool = False) -> int:
        with self._connection() as connection:
            if all_pending:
                cursor = connection.execute(
                    "UPDATE receive_requests SET status = 'expired' "
                    "WHERE status IN ('pending', 'approved')"
                )
            else:
                cursor = connection.execute(
                    "UPDATE receive_requests SET status = 'expired' "
                    "WHERE status IN ('pending', 'approved') AND expires_at <= ?",
                    (_datetime_to_text(datetime.now(UTC)),),
                )
        return cursor.rowcount

    def get_receive_request(self, request_id: UUID) -> ReceiveRequest | None:
        self.expire_receive_requests()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM receive_requests WHERE request_id = ?", (str(request_id),)
            ).fetchone()
        return None if row is None else _row_to_receive_request(row)

    def list_pending_receive_requests(self) -> list[ReceiveRequest]:
        self.expire_receive_requests()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM receive_requests WHERE status = 'pending' "
                "ORDER BY created_at LIMIT 20"
            ).fetchall()
        return [_row_to_receive_request(row) for row in rows]

    def decide_receive_request(self, request_id: UUID, *, approve: bool) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE receive_requests SET status = ? "
                "WHERE request_id = ? AND status = 'pending' AND expires_at > ? "
                "AND sender_device_id IN (SELECT device_id FROM receive_grants)",
                (
                    "approved" if approve else "rejected", str(request_id),
                    _datetime_to_text(datetime.now(UTC)),
                ),
            )
        return cursor.rowcount == 1

    def cancel_receive_request(self, request_id: UUID, sender_device_id: UUID) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE receive_requests SET status = 'rejected' "
                "WHERE request_id = ? AND sender_device_id = ? "
                "AND status IN ('pending', 'approved')",
                (str(request_id), str(sender_device_id)),
            )
        return cursor.rowcount == 1

    def consume_receive_request(
        self,
        request_id: UUID,
        sender_device_id: UUID,
        file_name: str,
        target_directory: str,
        file_size_bytes: int,
        expected_sha256: str,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE receive_requests SET status = 'consumed' "
                "WHERE request_id = ? AND sender_device_id = ? AND file_name = ? "
                "AND target_directory = ? AND file_size_bytes = ? AND expected_sha256 = ? "
                "AND status = 'approved' AND expires_at > ? "
                "AND sender_device_id IN (SELECT device_id FROM receive_grants)",
                (
                    str(request_id), str(sender_device_id), file_name, target_directory,
                    file_size_bytes, expected_sha256,
                    _datetime_to_text(datetime.now(UTC)),
                ),
            )
        return cursor.rowcount == 1

    def get_device(self, device_id: UUID) -> DeviceProfile | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_id = ?", (str(device_id),)
            ).fetchone()
        if row is None:
            return None
        return DeviceProfile(
            device_id=UUID(row["device_id"]),
            device_name=row["device_name"],
            public_key_fingerprint=row["public_key_fingerprint"],
            platform=row["platform"],
            created_at=_text_to_datetime(row["created_at"]),
        )

    def list_devices(self) -> list[DeviceProfile]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM devices ORDER BY device_name COLLATE NOCASE"
            ).fetchall()
        return [
            DeviceProfile(
                device_id=UUID(row["device_id"]),
                device_name=row["device_name"],
                public_key_fingerprint=row["public_key_fingerprint"],
                platform=row["platform"],
                created_at=_text_to_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def list_paired_devices(self) -> list[DeviceProfile]:
        """List only devices that still have an active pairing address and credentials."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT d.* FROM devices AS d "
                "INNER JOIN peer_addresses AS p ON p.device_id = d.device_id "
                "ORDER BY d.device_name COLLATE NOCASE"
            ).fetchall()
        return [
            DeviceProfile(
                device_id=UUID(row["device_id"]),
                device_name=row["device_name"],
                public_key_fingerprint=row["public_key_fingerprint"],
                platform=row["platform"],
                created_at=_text_to_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def remove_device(self, device_id: UUID) -> bool:
        """Unpair a device, preserving its profile when transfer history references it."""
        with self._connection() as connection:
            connection.execute(
                "UPDATE receive_requests SET status = 'rejected' "
                "WHERE sender_device_id = ? AND status IN ('pending', 'approved')",
                (str(device_id),),
            )
            cursor = connection.execute(
                "DELETE FROM peer_addresses WHERE device_id = ?", (str(device_id),)
            )
            if cursor.rowcount == 0:
                return False
            device_key = str(device_id)
            referenced = any(
                connection.execute(
                    f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",
                    (device_key,),
                ).fetchone()
                is not None
                for table, column in (
                    ("plans", "source_device_id"),
                    ("plans", "target_device_id"),
                    ("tasks", "sender_device_id"),
                    ("tasks", "receiver_device_id"),
                    ("transfer_records", "peer_device_id"),
                    ("receive_requests", "sender_device_id"),
                )
            )
            if not referenced:
                connection.execute("DELETE FROM devices WHERE device_id = ?", (device_key,))
            return True

    def save_plan(self, plan: TransferPlan) -> None:
        with self._connection() as connection:
            _upsert_plan(connection, plan)

    def save_ai_plan(self, plan: TransferPlan, evidence: AIPlanEvidence) -> None:
        """Persist a reviewed AI plan and its metadata-only provenance atomically."""
        if plan.source_mode is not SourceMode.NATURAL_LANGUAGE or plan.plan_id != evidence.plan_id:
            raise ValueError("AI evidence must belong to a natural-language plan")
        with self._connection() as connection:
            _upsert_plan(connection, plan)
            connection.execute(
                """INSERT INTO ai_plan_evidence (
                       plan_id, request_text, authorized_root, candidates_json,
                       selected_candidate_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(plan_id) DO UPDATE SET
                     request_text=excluded.request_text,
                     authorized_root=excluded.authorized_root,
                     candidates_json=excluded.candidates_json,
                     selected_candidate_id=excluded.selected_candidate_id,
                     created_at=excluded.created_at""",
                (
                    str(evidence.plan_id),
                    evidence.request_text,
                    evidence.authorized_root,
                    json.dumps(evidence.candidates, ensure_ascii=False),
                    evidence.selected_candidate_id,
                    _datetime_to_text(evidence.created_at),
                ),
            )

    def get_ai_plan_evidence(self, plan_id: UUID) -> AIPlanEvidence | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM ai_plan_evidence WHERE plan_id = ?", (str(plan_id),)
            ).fetchone()
        if row is None:
            return None
        return AIPlanEvidence(
            plan_id=UUID(row["plan_id"]),
            request_text=row["request_text"],
            authorized_root=row["authorized_root"],
            candidates=tuple(tuple(item) for item in json.loads(row["candidates_json"])),
            selected_candidate_id=row["selected_candidate_id"],
            created_at=_text_to_datetime(row["created_at"]),
        )

    def get_plan(self, plan_id: UUID) -> TransferPlan | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (str(plan_id),)
            ).fetchone()
        return None if row is None else _row_to_plan(row)

    def list_plans(self, limit: int = 50) -> list[TransferPlan]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM plans WHERE status <> 'deleted' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_plan(row) for row in rows]

    def delete_plan(self, plan_id: UUID) -> bool:
        """Hide a plan from the recent list while preserving its audit row and references."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE plans SET status = 'deleted' WHERE plan_id = ? AND status <> 'deleted'",
                (str(plan_id),),
            )
            return cursor.rowcount > 0

    def save_task(self, task: TransferTask) -> None:
        with self._connection() as connection:
            _save_task(connection, task)

    def save_transfer_attempt(self, task: TransferTask, record: TransferRecord) -> None:
        """Commit a task and its history row together so they cannot diverge."""
        if record.task_id != task.task_id or record.status != task.status:
            raise ValueError("transfer record must match the task ID and status")
        with self._connection() as connection:
            _save_task(connection, task)
            _save_record(connection, record)

    def list_tasks(self, limit: int = 50) -> list[TransferTask]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_incomplete_tasks(self) -> list[TransferTask]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?, ?, ?) ORDER BY created_at",
                (
                    TransferStatus.PREPARING.value,
                    TransferStatus.WAITING_RECEIVER.value,
                    TransferStatus.TRANSFERRING.value,
                    TransferStatus.VERIFYING.value,
                ),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def get_task(self, task_id: UUID) -> TransferTask | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)
            ).fetchone()
        return None if row is None else _row_to_task(row)

    def save_record(self, record: TransferRecord) -> None:
        with self._connection() as connection:
            _save_record(connection, record)

    def get_record_for_task(self, task_id: UUID) -> TransferRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM transfer_records WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
                (str(task_id),),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def list_records(self) -> list[TransferRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM transfer_records ORDER BY rowid DESC"
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_history_entries(self, limit: int = 2000) -> list[HistoryEntry]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT direction, file_name, status, occurred_at, error_message FROM (
                    SELECT r.direction, r.file_name, r.status,
                           COALESCE(r.finished_at, r.started_at, t.created_at) AS occurred_at,
                           t.error_message
                    FROM transfer_records AS r JOIN tasks AS t ON t.task_id = r.task_id
                    UNION ALL
                    SELECT 'received', file_name, status, created_at, error_message
                    FROM incoming_transfers
                ) ORDER BY occurred_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            HistoryEntry(
                direction=TransferDirection(row["direction"]),
                file_name=row["file_name"],
                status=TransferStatus(row["status"]),
                occurred_at=_text_to_datetime(row["occurred_at"]),
                error_message=row["error_message"],
            )
            for row in rows
        ]

    def prune_terminal_history(self, before: datetime) -> tuple[int, int]:
        """Delete old finished task/receive rows; retain plans, devices and disk files."""
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must be timezone-aware")
        cutoff = _datetime_to_text(before)
        terminal = tuple(status.value for status in (
            TransferStatus.COMPLETED, TransferStatus.FAILED,
            TransferStatus.CANCELLED, TransferStatus.REJECTED,
        ))
        with self._connection() as connection:
            matching = (
                "task_id IN (SELECT task_id FROM tasks "
                "WHERE status IN (?, ?, ?, ?) AND julianday(updated_at) < julianday(?))"
            )
            connection.execute(
                f"DELETE FROM transfer_records WHERE {matching}", (*terminal, cutoff)
            )
            sent = connection.execute(
                "DELETE FROM tasks WHERE status IN (?, ?, ?, ?) "
                "AND julianday(updated_at) < julianday(?)",
                (*terminal, cutoff),
            ).rowcount
            received = connection.execute(
                "DELETE FROM incoming_transfers WHERE status IN (?, ?, ?, ?) "
                "AND julianday(updated_at) < julianday(?)",
                (*terminal, cutoff),
            ).rowcount
            connection.execute(
                "DELETE FROM receive_requests WHERE status IN ('rejected', 'expired', 'consumed') "
                "AND julianday(expires_at) < julianday(?)",
                (cutoff,),
            )
        return sent, received

    def save_incoming_attempt(self, attempt: IncomingTransferAttempt) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO incoming_transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(attempt_id) DO UPDATE SET
                     bytes_received=excluded.bytes_received,
                     status=excluded.status,
                     updated_at=excluded.updated_at,
                     error_code=excluded.error_code,
                     error_message=excluded.error_message""",
                (
                    str(attempt.attempt_id),
                    attempt.file_name,
                    attempt.target_directory,
                    attempt.file_size_bytes,
                    attempt.expected_sha256,
                    attempt.bytes_received,
                    attempt.status.value,
                    _datetime_to_text(attempt.created_at),
                    _datetime_to_text(attempt.updated_at),
                    attempt.error_code,
                    attempt.error_message,
                ),
            )

    def list_incoming_attempts(self, limit: int = 50) -> list[IncomingTransferAttempt]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM incoming_transfers ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_incoming_attempt(row) for row in rows]

    def reconcile_incomplete_incoming(self) -> int:
        """After a process restart, never present an unconfirmed receive as complete."""
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE incoming_transfers
                   SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                   WHERE status IN (?, ?)""",
                (
                    TransferStatus.FAILED.value,
                    "outcome_unknown",
                    "接收过程被中断；请检查接收目录中的正式文件。",
                    _datetime_to_text(datetime.now(UTC)),
                    TransferStatus.TRANSFERRING.value,
                    TransferStatus.VERIFYING.value,
                ),
            )
            return cursor.rowcount


def _execute_schema(connection: sqlite3.Connection, script: str) -> None:
    """Execute migration DDL inside the caller's transaction."""
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _save_task(connection: sqlite3.Connection, task: TransferTask) -> None:
    connection.execute(
        """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     plan_id=excluded.plan_id,
                     sender_device_id=excluded.sender_device_id,
                     receiver_device_id=excluded.receiver_device_id,
                     file_name=excluded.file_name,
                     file_size_bytes=excluded.file_size_bytes,
                     expected_sha256=excluded.expected_sha256,
                     bytes_transferred=excluded.bytes_transferred,
                     status=excluded.status,
                     created_at=excluded.created_at,
                     updated_at=excluded.updated_at,
                     error_code=excluded.error_code,
                     error_message=excluded.error_message""",
        (
            str(task.task_id),
            str(task.plan_id),
            str(task.sender_device_id),
            str(task.receiver_device_id),
            task.file_name,
            task.file_size_bytes,
            task.expected_sha256.lower(),
            task.bytes_transferred,
            task.status.value,
            _datetime_to_text(task.created_at),
            _datetime_to_text(task.updated_at),
            task.error_code,
            task.error_message,
        ),
    )


def _save_record(connection: sqlite3.Connection, record: TransferRecord) -> None:
    connection.execute(
        """INSERT INTO transfer_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(record_id) DO UPDATE SET
                     task_id=excluded.task_id,
                     direction=excluded.direction,
                     peer_device_id=excluded.peer_device_id,
                     file_name=excluded.file_name,
                     file_size_bytes=excluded.file_size_bytes,
                     status=excluded.status,
                     started_at=excluded.started_at,
                     finished_at=excluded.finished_at""",
        (
            str(record.record_id),
            str(record.task_id),
            record.direction.value,
            str(record.peer_device_id),
            record.file_name,
            record.file_size_bytes,
            record.status.value,
            _optional_datetime_to_text(record.started_at),
            _optional_datetime_to_text(record.finished_at),
        ),
    )


def _datetime_to_text(value: datetime) -> str:
    return value.isoformat()


def _optional_datetime_to_text(value: datetime | None) -> str | None:
    return None if value is None else _datetime_to_text(value)


def _text_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _upsert_plan(connection: sqlite3.Connection, plan: TransferPlan) -> None:
    connection.execute(
        """INSERT INTO plans (
               plan_id, source_device_id, source_path, target_device_id,
               target_directory, source_mode, file_name, file_size_bytes,
               status, created_at, expires_at, expected_sha256
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(plan_id) DO UPDATE SET
             source_device_id=excluded.source_device_id,
             source_path=excluded.source_path,
             target_device_id=excluded.target_device_id,
             target_directory=excluded.target_directory,
             source_mode=excluded.source_mode,
             file_name=excluded.file_name,
             file_size_bytes=excluded.file_size_bytes,
             status=excluded.status,
             created_at=excluded.created_at,
             expires_at=excluded.expires_at,
             expected_sha256=excluded.expected_sha256""",
        _plan_to_values(plan),
    )


def _plan_to_values(plan: TransferPlan) -> tuple[object, ...]:
    return (
        str(plan.plan_id),
        str(plan.source_device_id),
        plan.source_path,
        str(plan.target_device_id),
        plan.target_directory,
        plan.source_mode.value,
        plan.file_name,
        plan.file_size_bytes,
        plan.status.value,
        _datetime_to_text(plan.created_at),
        _datetime_to_text(plan.expires_at),
        plan.expected_sha256,
    )


def _row_to_plan(row: sqlite3.Row) -> TransferPlan:
    return TransferPlan(
        plan_id=UUID(row["plan_id"]),
        source_device_id=UUID(row["source_device_id"]),
        source_path=row["source_path"],
        target_device_id=UUID(row["target_device_id"]),
        target_directory=row["target_directory"],
        source_mode=SourceMode(row["source_mode"]),
        file_name=row["file_name"],
        file_size_bytes=row["file_size_bytes"],
        status=PlanStatus(row["status"]),
        created_at=_text_to_datetime(row["created_at"]),
        expires_at=_text_to_datetime(row["expires_at"]),
        expected_sha256=row["expected_sha256"],
    )


def _row_to_task(row: sqlite3.Row) -> TransferTask:
    return TransferTask(
        task_id=UUID(row["task_id"]),
        plan_id=UUID(row["plan_id"]),
        sender_device_id=UUID(row["sender_device_id"]),
        receiver_device_id=UUID(row["receiver_device_id"]),
        file_name=row["file_name"],
        file_size_bytes=row["file_size_bytes"],
        expected_sha256=row["expected_sha256"],
        bytes_transferred=row["bytes_transferred"],
        status=TransferStatus(row["status"]),
        created_at=_text_to_datetime(row["created_at"]),
        updated_at=_text_to_datetime(row["updated_at"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _row_to_record(row: sqlite3.Row) -> TransferRecord:
    return TransferRecord(
        record_id=UUID(row["record_id"]),
        task_id=UUID(row["task_id"]),
        direction=TransferDirection(row["direction"]),
        peer_device_id=UUID(row["peer_device_id"]),
        file_name=row["file_name"],
        file_size_bytes=row["file_size_bytes"],
        status=TransferStatus(row["status"]),
        started_at=(None if row["started_at"] is None else _text_to_datetime(row["started_at"])),
        finished_at=(None if row["finished_at"] is None else _text_to_datetime(row["finished_at"])),
    )


def _row_to_incoming_attempt(row: sqlite3.Row) -> IncomingTransferAttempt:
    return IncomingTransferAttempt(
        attempt_id=UUID(row["attempt_id"]),
        file_name=row["file_name"],
        target_directory=row["target_directory"],
        file_size_bytes=row["file_size_bytes"],
        expected_sha256=row["expected_sha256"],
        bytes_received=row["bytes_received"],
        status=TransferStatus(row["status"]),
        created_at=_text_to_datetime(row["created_at"]),
        updated_at=_text_to_datetime(row["updated_at"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _row_to_receive_request(row: sqlite3.Row) -> ReceiveRequest:
    return ReceiveRequest(
        request_id=UUID(row["request_id"]),
        sender_device_id=UUID(row["sender_device_id"]),
        file_name=row["file_name"],
        target_directory=row["target_directory"],
        file_size_bytes=row["file_size_bytes"],
        expected_sha256=row["expected_sha256"],
        status=row["status"],
        expires_at=_text_to_datetime(row["expires_at"]),
    )
