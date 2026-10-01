"""Small local diagnostic log with secret redaction."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER = re.compile(r"(?i)(bearer\s+)[^\s,;]+")
_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;]+")
_SECRET_FIELD = re.compile(
    r"(?i)((?:receive_token|transfer_token|access_token|password|secret)\s*[:=]\s*)[^\s,;]+"
)


class DiagnosticLog:
    def __init__(self, data_dir: Path, secrets: tuple[str, ...] = ()) -> None:
        self.path = data_dir / "logs" / "bridge.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secrets = {value for value in secrets if value}
        self._lock = Lock()

    def add_secret(self, value: str) -> None:
        if value:
            with self._lock:
                self._secrets.add(value)

    def redact(self, value: str) -> str:
        with self._lock:
            secrets = tuple(sorted(self._secrets, key=len, reverse=True))
        result = _KEY_BLOCK.sub("[PRIVATE KEY REDACTED]", str(value))
        result = _BEARER.sub(r"\1[REDACTED]", result)
        result = _AUTH_HEADER.sub(r"\1[REDACTED]", result)
        result = _SECRET_FIELD.sub(r"\1[REDACTED]", result)
        for secret in secrets:
            result = result.replace(secret, "[REDACTED]")
        return result

    def event(self, code: str, detail: str = "") -> None:
        safe_code = re.sub(r"[^A-Z0-9_]", "_", code.upper())[:48]
        safe_detail = self.redact(detail).replace("\r", " ").replace("\n", " ")[:2000]
        line = f"{datetime.now(UTC).isoformat()} {safe_code} {safe_detail}\n"
        with self._lock:
            if self.path.exists() and self.path.stat().st_size > 1_000_000:
                previous = self.path.with_suffix(".log.1")
                self.path.replace(previous)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)

    def report(self, *, version: str, schema_version: int, service: str, issue: str) -> str:
        return self.redact(
            f"AI Device Bridge {version}\n"
            f"数据库版本：{schema_version}\n"
            f"本机服务：{service}\n"
            f"数据目录：{self.path.parent.parent}\n"
            f"诊断日志：{self.path}\n"
            f"最近错误：{issue or '无'}"
        )
