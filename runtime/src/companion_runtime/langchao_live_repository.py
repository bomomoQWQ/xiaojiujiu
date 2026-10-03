"""Persistence and exactly-once terminal settlement for 「浪潮」 live sends."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from contextlib import nullcontext
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .langchao_no_send import NoSendResult
from .langchao_no_send_repository import LangchaoNoSendRepository
from .langchao_outcome_repository import LangchaoOutcomeRepository
from .langchao_runtime_adapter import (
    COLD_START_DRIVE_OUTCOME,
    COLD_START_EXPLORATION_LIMIT,
    COLD_START_EXPLORATION_MIN_SPACING,
    COLD_START_EXPLORATION_WINDOW,
)
from .langchao_types import MotivationDirection, OutcomeStatus, OutcomeToken, SettlementType
from .exposure_identity import canonical_exposure_id


class LangchaoColdStartBudget:
    """Durable budget over cold-start outreach already committed in this scope.

    The v2 engine bounds exploratory contact with a limit per window and a minimum
    spacing. Langchao reuses those exact numbers so one policy governs both engines, and
    counts the durable live commits whose reward contract carries the cold-start drive
    token. Without this the numerical kernel would reach out every couple of hours, which
    is precisely the uncontrolled volume the bound exists to prevent.
    """

    def __init__(self, connection: Any, *, scope_key: str) -> None:
        self.connection = connection
        self.scope_key = scope_key

    def usage(self, *, since: datetime) -> tuple[int, datetime | None]:
        """Return how many cold-start messages were *delivered* since ``since``.

        The bound exists to limit how often the character actually reaches out, so it
        counts acknowledgements rather than commits: a round whose delivery never left the
        building -- a failed render, a refused dispatch -- reached nobody and must not
        consume the allowance.

        Args:
            since: Start of the counting window.

        Returns:
            ``(count, last_delivered_at)``; the timestamp is ``None`` when none exist.
        """
        row = self.connection.execute(
            """SELECT count(*) AS used, max(committed_at) AS last_at
                 FROM langchao_live_commits
                WHERE scope_key = %s AND committed_at >= %s
                  AND terminal_ack_kind = 'sent'
                  AND EXISTS (
                        SELECT 1 FROM jsonb_array_elements(snapshot->'tokens') AS token
                         WHERE token->>'outcome_key' = %s)""",
            (self.scope_key, since, COLD_START_DRIVE_OUTCOME),
        ).fetchone()
        if row is None:
            return 0, None
        used = row["used"] if isinstance(row, Mapping) else row[0]
        last_at = row["last_at"] if isinstance(row, Mapping) else row[1]
        return int(used or 0), last_at

    def available(
        self,
        *,
        now: datetime,
        limit: int = COLD_START_EXPLORATION_LIMIT,
        window: timedelta = COLD_START_EXPLORATION_WINDOW,
        min_spacing: timedelta = COLD_START_EXPLORATION_MIN_SPACING,
    ) -> bool:
        """Return whether another cold-start send may be committed at ``now``.

        Args:
            now: Decision time.
            limit: Maximum cold-start sends per window; zero disables the drive.
            window: Length of the counting window.
            min_spacing: Minimum gap between two cold-start sends.

        Returns:
            ``True`` when a cold-start drive may be granted for this round.
        """
        if limit <= 0:
            return False
        used, last_at = self.usage(since=now - window)
        if used >= limit:
            return False
        if min_spacing > timedelta(0) and last_at is not None and now - last_at < min_spacing:
            return False
        return True


@dataclass(frozen=True, slots=True, kw_only=True)
class LangchaoLiveCommit:
    scope_key: str
    round_id: str
    langchao_candidate_id: str
    candidate_revision: int
    source_candidate_id: str
    reward_contract_id: str
    reward_revision: int
    expected_tokens: tuple[OutcomeToken, ...]
    attempt_id: str
    render_outbox_id: str
    claim_id: str
    committed_at: datetime
    semantic_dispatch_id: str | None = None
    authority_revision: int | None = None
    terminal_ack_id: str | None = None
    terminal_ack_kind: str | None = None


class LangchaoLiveRepository:
    def __init__(self, connection: Any, *, scope_key: str) -> None:
        self.connection = connection
        self.scope_key = scope_key
        self.outcomes = LangchaoOutcomeRepository(connection, scope_key=scope_key)
        self.no_sends = LangchaoNoSendRepository(connection, scope_key=scope_key)

    def save_no_send(self, result: NoSendResult, *, recorded_at: datetime | None = None) -> NoSendResult:
        return self.no_sends.save(result, recorded_at=recorded_at)

    def get_no_send(self, round_id: str) -> NoSendResult | None:
        return self.no_sends.get(round_id)

    def list_no_sends(self, *, reason: Any | None = None, limit: int = 100) -> tuple[NoSendResult, ...]:
        return self.no_sends.list(reason=reason, limit=limit)

    def save_commit(self, commit: LangchaoLiveCommit) -> None:
        if commit.scope_key != self.scope_key:
            raise ValueError("live commit belongs to a different scope")
        snapshot = {
            "version": "langchao.live-commit.v2",
            "tokens": [token.to_dict() for token in commit.expected_tokens],
        }
        # v26 is rolling-compatible: old writers may leave the new witness pair NULL
        # while upgraded writers dual-write it. The composite FK rejects wrong/partial pairs.
        cursor = self.connection.execute(
            """INSERT INTO langchao_live_commits
               (scope_key, round_id, langchao_candidate_id, candidate_revision,
                source_candidate_id, reward_contract_id, reward_revision,
                expected_token_ids, snapshot, attempt_id, render_outbox_id, claim_id,
                semantic_dispatch_id, authority_revision, committed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (scope_key, round_id) DO NOTHING RETURNING round_id""",
            (self.scope_key, commit.round_id, commit.langchao_candidate_id,
             commit.candidate_revision, commit.source_candidate_id,
             commit.reward_contract_id, commit.reward_revision,
             json.dumps([token.token_id for token in commit.expected_tokens]),
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             commit.attempt_id, commit.render_outbox_id, commit.claim_id,
             commit.semantic_dispatch_id, commit.authority_revision, commit.committed_at),
        )
        if cursor.fetchone() is None:
            existing = self.get(commit.round_id)
            if existing != commit:
                raise ValueError("conflicting langchao live commit snapshot")

    def get(self, round_id: str) -> LangchaoLiveCommit | None:
        row = self.connection.execute(
            """SELECT scope_key,round_id,langchao_candidate_id,candidate_revision,
                      source_candidate_id,reward_contract_id,reward_revision,snapshot,
                      attempt_id,render_outbox_id,claim_id,semantic_dispatch_id,authority_revision,
                       committed_at,terminal_ack_id,terminal_ack_kind
               FROM langchao_live_commits WHERE scope_key=%s AND round_id=%s""",
            (self.scope_key, round_id),
        ).fetchone()
        return None if row is None else self._decode(row)

    def get_by_attempt(self, exposure_id: str) -> LangchaoLiveCommit | None:
        """Resolve a v2 exposure to the exact Langchao attempt in this scope."""
        rows = self.connection.execute(
            """SELECT scope_key,round_id,langchao_candidate_id,candidate_revision,
                      source_candidate_id,reward_contract_id,reward_revision,snapshot,
                      attempt_id,render_outbox_id,claim_id,semantic_dispatch_id,authority_revision,
                       committed_at,terminal_ack_id,terminal_ack_kind
               FROM langchao_live_commits
               WHERE scope_key=%s AND terminal_ack_kind='sent'""",
            (self.scope_key,),
        ).fetchall()
        matches = tuple(
            self._decode(row) for row in rows
            if canonical_exposure_id(self.scope_key, str(row["attempt_id"] if isinstance(row, Mapping) else row[8]))
            == exposure_id
        )
        if len(matches) > 1:
            raise RuntimeError("multiple Langchao attempts resolve to one exposure")
        return matches[0] if matches else None

    def label_revision(self, *, exposure_id: str, target_name: str) -> int | None:
        row = self.connection.execute(
            """SELECT a.pointer_version
               FROM user_model_active_labels_v2 AS a
               WHERE a.scope_key=%s AND a.exposure_id=%s AND a.target_name=%s""",
            (self.scope_key, exposure_id, target_name),
        ).fetchone()
        return None if row is None else int(row["pointer_version"] if isinstance(row, Mapping) else row[0])

    def pending(self) -> tuple[LangchaoLiveCommit, ...]:
        rows = self.connection.execute(
            """SELECT scope_key,round_id,langchao_candidate_id,candidate_revision,
                      source_candidate_id,reward_contract_id,reward_revision,snapshot,
                      attempt_id,render_outbox_id,claim_id,semantic_dispatch_id,authority_revision,
                       committed_at,terminal_ack_id,terminal_ack_kind
               FROM langchao_live_commits
               WHERE scope_key=%s AND terminal_ack_kind IS NULL ORDER BY committed_at,round_id""",
            (self.scope_key,),
        ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def _transaction(self) -> Any:
        transaction = getattr(self.connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    def mark_exposure_recovered(self, *, round_id: str, recovered_at: datetime) -> bool:
        cursor = self.connection.execute(
            """UPDATE langchao_live_commits
               SET exposure_recovered_at=%s
               WHERE scope_key=%s AND round_id=%s
                 AND exposure_recovery_required AND exposure_recovered_at IS NULL""",
            (recovered_at, self.scope_key, round_id),
        )
        return getattr(cursor, "rowcount", 1) == 1

    def pending_exposure_recovery(self) -> tuple[LangchaoLiveCommit, ...]:
        rows = self.connection.execute(
            """SELECT scope_key,round_id,langchao_candidate_id,candidate_revision,
                      source_candidate_id,reward_contract_id,reward_revision,snapshot,
                      attempt_id,render_outbox_id,claim_id,semantic_dispatch_id,authority_revision,
                      committed_at,terminal_ack_id,terminal_ack_kind
               FROM langchao_live_commits
               WHERE scope_key=%s AND exposure_recovery_required AND exposure_recovered_at IS NULL
               ORDER BY committed_at,round_id""",
            (self.scope_key,),
        ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def settle_terminal(self, *, round_id: str, attempt_id: str, ack_id: str,
                        sent: bool, acknowledged_at: datetime) -> tuple[OutcomeToken, ...]:
        """Settle ACK, local outcome tokens and recovery marker in one transaction.

        ``terminal_ack_*`` is not the completion marker.  A retry that finds an exact ACK
        with ``settlement_status='pending'`` resumes the idempotent token writes and marks
        settlement complete, so an interrupted old rollout cannot permanently lose reward
        or exposure recovery.
        """
        with self._transaction():
            row = self.connection.execute(
                """SELECT scope_key,round_id,langchao_candidate_id,candidate_revision,
                          source_candidate_id,reward_contract_id,reward_revision,snapshot,
                          attempt_id,render_outbox_id,claim_id,semantic_dispatch_id,authority_revision,
                          committed_at,terminal_ack_id,terminal_ack_kind,settlement_status
                   FROM langchao_live_commits
                   WHERE scope_key=%s AND round_id=%s FOR UPDATE""",
                (self.scope_key, round_id),
            ).fetchone()
            if row is None:
                return ()
            commit = self._decode(row)
            if commit.attempt_id != attempt_id:
                raise ValueError("send acknowledgement attempt does not match langchao live commit")
            kind = "sent" if sent else "failed"
            if isinstance(row, Mapping):
                existing_ack = row.get("terminal_ack_id")
                existing_kind = row.get("terminal_ack_kind")
                status = row.get("settlement_status", "pending")
            else:
                existing_ack = row[14] if len(row) > 14 else row[12]
                existing_kind = row[15] if len(row) > 15 else row[13]
                status = row[16] if len(row) > 16 else "pending"
            if existing_kind is not None and (existing_ack, existing_kind) != (ack_id, kind):
                raise ValueError("langchao live commit already has a different terminal acknowledgement")
            if status == "completed":
                return ()
            # Pre-v26 rows and lightweight compatibility stores do not expose the
            # settlement marker. In that shape an exact terminal ACK still means the
            # old implementation completed its work; v26 PostgreSQL rows explicitly
            # carry settlement_status='pending' when repair is required.
            if "settlement_status" not in row and existing_kind is not None:
                return ()
            if existing_kind is None:
                self.connection.execute(
                    """UPDATE langchao_live_commits
                       SET terminal_ack_id=%s, terminal_ack_kind=%s, terminal_acknowledged_at=%s
                       WHERE scope_key=%s AND round_id=%s AND attempt_id=%s""",
                    (ack_id, kind, acknowledged_at, self.scope_key, round_id, attempt_id),
                )

            written: list[OutcomeToken] = []
            for expected in commit.expected_tokens:
                if expected.outcome_key not in {"delivery", "expression_delivered", "rest_realized"}:
                    continue
                actual = replace(
                    expected,
                    token_id=str(uuid5(NAMESPACE_URL, f"langchao-live:{attempt_id}:{expected.token_id}:{kind}")),
                    settlement_type=SettlementType.ACTUAL,
                    status=(OutcomeStatus.CONFIRMED if sent else OutcomeStatus.CENSORED),
                    base_amount=expected.base_amount if sent else 0.0,
                    evidence_version="langchao.live-ack.v1",
                    idempotency_key=f"langchao-live:{attempt_id}:{expected.token_id}:{kind}",
                    evidence_refs=tuple(dict.fromkeys((*expected.evidence_refs, ack_id, kind))),
                    observation_started_at=None,
                    observation_ends_at=None,
                )
                writer = getattr(self.outcomes, "put_outcome_revision_in_transaction", None)
                if writer is None:  # compatibility for repository fakes and old implementations
                    writer = self.outcomes.put_outcome_revision
                writer(
                    actual, revision=1, reward_contract_id=commit.reward_contract_id,
                    reward_contract_revision=commit.reward_revision,
                )
                activator = getattr(self.outcomes, "activate_outcome", None)
                if callable(activator):
                    activated = activator(
                        token_id=actual.token_id, revision=1, expected_pointer_version=0
                    )
                    if not activated:
                        active = self.connection.execute(
                            """SELECT revision FROM langchao_outcome_active
                               WHERE scope_key=%s AND token_id=%s""",
                            (self.scope_key, actual.token_id),
                        ).fetchone()
                        if active is None or int(active["revision"] if isinstance(active, Mapping) else active[0]) != 1:
                            raise RuntimeError("outcome activation CAS lost to a different revision")
                written.append(actual)

            self.connection.execute(
                """UPDATE langchao_live_commits
                   SET settlement_status='completed', settlement_completed_at=%s,
                       exposure_recovery_required=%s
                   WHERE scope_key=%s AND round_id=%s AND settlement_status='pending'""",
                (acknowledged_at, sent, self.scope_key, round_id),
            )
            return tuple(written)

    def _decode(self, row: Any) -> LangchaoLiveCommit:
        def value(name: str, index: int, default: Any = None) -> Any:
            if isinstance(row, Mapping):
                return row.get(name, default)
            return row[index] if index < len(row) else default
        raw = value("snapshot", 7)
        snapshot = json.loads(raw) if isinstance(raw, str) else raw
        tokens = tuple(_token_from_dict(item) for item in snapshot["tokens"])
        return LangchaoLiveCommit(
            scope_key=str(value("scope_key", 0)), round_id=str(value("round_id", 1)),
            langchao_candidate_id=str(value("langchao_candidate_id", 2)),
            candidate_revision=int(value("candidate_revision", 3)),
            source_candidate_id=str(value("source_candidate_id", 4)),
            reward_contract_id=str(value("reward_contract_id", 5)),
            reward_revision=int(value("reward_revision", 6)), expected_tokens=tokens,
            attempt_id=str(value("attempt_id", 8)), render_outbox_id=str(value("render_outbox_id", 9)),
            claim_id=str(value("claim_id", 10)),
            semantic_dispatch_id=value("semantic_dispatch_id", 11),
            authority_revision=(None if value("authority_revision", 12) is None
                                else int(value("authority_revision", 12))),
            committed_at=value("committed_at", 13, value("committed_at", 11)),
            terminal_ack_id=value("terminal_ack_id", 14, value("terminal_ack_id", 12)),
            terminal_ack_kind=value("terminal_ack_kind", 15, value("terminal_ack_kind", 13)),
        )


def _token_from_dict(data: Mapping[str, Any]) -> OutcomeToken:
    def dt(value: Any) -> datetime | None:
        return None if value is None else datetime.fromisoformat(str(value))
    return OutcomeToken(
        token_id=str(data["token_id"]), scope_key=str(data["scope_key"]),
        goal_id=str(data["goal_id"]), episode_id=str(data["episode_id"]),
        outcome_key=str(data["outcome_key"]), settlement_type=SettlementType(data["settlement_type"]),
        status=OutcomeStatus(data["status"]), base_amount=float(data["base_amount"]),
        direction_weights=tuple((MotivationDirection(key), float(value))
                                for key, value in data["direction_weights"].items()),
        evidence_version=str(data["evidence_version"]), idempotency_key=str(data["idempotency_key"]),
        evidence_refs=tuple(data.get("evidence_refs", ())), milestone_id=data.get("milestone_id"),
        observation_started_at=dt(data.get("observation_started_at")),
        observation_ends_at=dt(data.get("observation_ends_at")),
        corrects_token_id=data.get("corrects_token_id"),
    )


__all__ = ["LangchaoLiveCommit", "LangchaoLiveRepository"]
