"""Scoped persistence and query API for terminal Langchao non-send results."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from .langchao_no_send import NoSendReason, NoSendResult


class LangchaoNoSendConflictError(RuntimeError):
    """One round already has a different terminal non-send result."""


class LangchaoNoSendRepository:
    def __init__(self, connection: Any, *, scope_key: str) -> None:
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key is required")
        self.connection = connection
        self.scope_key = scope_key

    def save(self, result: NoSendResult, *, recorded_at: datetime | None = None) -> NoSendResult:
        if not isinstance(result, NoSendResult):
            raise TypeError("result must be NoSendResult")
        if result.round_id is None:
            raise ValueError("persisted no-send result requires round_id")
        cursor = self.connection.execute(
            """INSERT INTO langchao_no_send_results
               (scope_key, round_id, reason, stage, candidate_id, permission_version,
                details, recorded_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,COALESCE(%s,CURRENT_TIMESTAMP))
               ON CONFLICT (scope_key, round_id) DO NOTHING RETURNING round_id""",
            (self.scope_key, result.round_id, result.reason.value, result.stage,
             result.candidate_id, result.permission_version,
             json.dumps(dict(result.details), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")), recorded_at),
        )
        if cursor.fetchone() is None:
            existing = self.get(result.round_id)
            if existing != result:
                raise LangchaoNoSendConflictError(
                    "round already has a different terminal no-send result"
                )
        return result

    def get(self, round_id: str) -> NoSendResult | None:
        row = self.connection.execute(
            """SELECT reason,stage,round_id,candidate_id,permission_version,details
               FROM langchao_no_send_results
               WHERE scope_key=%s AND round_id=%s""",
            (self.scope_key, round_id),
        ).fetchone()
        return None if row is None else self._decode(row)

    def list(self, *, reason: NoSendReason | str | None = None,
             limit: int = 100) -> tuple[NoSendResult, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in [1, 1000]")
        normalized = None if reason is None else (
            reason if isinstance(reason, NoSendReason) else NoSendReason(reason)
        )
        rows = self.connection.execute(
            """SELECT reason,stage,round_id,candidate_id,permission_version,details
               FROM langchao_no_send_results
               WHERE scope_key=%s AND (%s IS NULL OR reason=%s)
               ORDER BY recorded_at DESC, round_id DESC LIMIT %s""",
            (self.scope_key, None if normalized is None else normalized.value,
             None if normalized is None else normalized.value, limit),
        ).fetchall()
        return tuple(self._decode(row) for row in rows)

    @staticmethod
    def _decode(row: Any) -> NoSendResult:
        def value(name: str, index: int) -> Any:
            return row[name] if isinstance(row, Mapping) else row[index]
        raw = value("details", 5)
        details = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(details, Mapping):
            raise TypeError("persisted no-send details must be an object")
        return NoSendResult(
            reason=NoSendReason(str(value("reason", 0))), stage=str(value("stage", 1)),
            round_id=str(value("round_id", 2)), candidate_id=value("candidate_id", 3),
            permission_version=value("permission_version", 4),
            details=tuple(sorted((str(key), str(item)) for key, item in details.items())),
        )


__all__ = ["LangchaoNoSendConflictError", "LangchaoNoSendRepository"]
