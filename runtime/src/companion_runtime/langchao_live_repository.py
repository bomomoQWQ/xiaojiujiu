"""Persistence and exactly-once terminal settlement for 「浪潮」 live sends."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .langchao_outcome_repository import LangchaoOutcomeRepository
from .langchao_types import MotivationDirection, OutcomeStatus, OutcomeToken, SettlementType
from .user_model_v2_service import canonical_exposure_id


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
    terminal_ack_id: str | None = None
    terminal_ack_kind: str | None = None


class LangchaoLiveRepository:
    def __init__(self, connection: Any, *, scope_key: str) -> None:
        self.connection = connection
        self.scope_key = scope_key
        self.outcomes = LangchaoOutcomeRepository(connection, scope_key=scope_key)

    def save_commit(self, commit: LangchaoLiveCommit) -> None:
        if commit.scope_key != self.scope_key:
            raise ValueError("live commit belongs to a different scope")
        snapshot = {
            "version": "langchao.live-commit.v1",
            "tokens": [token.to_dict() for token in commit.expected_tokens],
        }
        cursor = self.connection.execute(
            """INSERT INTO langchao_live_commits
               (scope_key, round_id, langchao_candidate_id, candidate_revision,
                source_candidate_id, reward_contract_id, reward_revision,
                expected_token_ids, snapshot, attempt_id, render_outbox_id, claim_id, committed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
               ON CONFLICT (scope_key, round_id) DO NOTHING RETURNING round_id""",
            (self.scope_key, commit.round_id, commit.langchao_candidate_id,
             commit.candidate_revision, commit.source_candidate_id,
             commit.reward_contract_id, commit.reward_revision,
             json.dumps([token.token_id for token in commit.expected_tokens]),
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             commit.attempt_id, commit.render_outbox_id, commit.claim_id, commit.committed_at),
        )
        if cursor.fetchone() is None:
            existing = self.get(commit.round_id)
            if existing != commit:
                raise ValueError("conflicting langchao live commit snapshot")

    def get(self, round_id: str) -> LangchaoLiveCommit | None:
        row = self.connection.execute(
            """SELECT scope_key,round_id,langchao_candidate_id,candidate_revision,
                      source_candidate_id,reward_contract_id,reward_revision,snapshot,
                      attempt_id,render_outbox_id,claim_id,committed_at,terminal_ack_id,terminal_ack_kind
               FROM langchao_live_commits WHERE scope_key=%s AND round_id=%s""",
            (self.scope_key, round_id),
        ).fetchone()
        return None if row is None else self._decode(row)

    def get_by_attempt(self, exposure_id: str) -> LangchaoLiveCommit | None:
        """Resolve a v2 exposure to the exact Langchao attempt in this scope."""
        rows = self.connection.execute(
            """SELECT scope_key,round_id,langchao_candidate_id,candidate_revision,
                      source_candidate_id,reward_contract_id,reward_revision,snapshot,
                      attempt_id,render_outbox_id,claim_id,committed_at,terminal_ack_id,terminal_ack_kind
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
        return None if row is None else int(row["revision"] if isinstance(row, Mapping) else row[0])

    def pending(self) -> tuple[LangchaoLiveCommit, ...]:
        rows = self.connection.execute(
            """SELECT scope_key,round_id,langchao_candidate_id,candidate_revision,
                      source_candidate_id,reward_contract_id,reward_revision,snapshot,
                      attempt_id,render_outbox_id,claim_id,committed_at,terminal_ack_id,terminal_ack_kind
               FROM langchao_live_commits
               WHERE scope_key=%s AND terminal_ack_kind IS NULL ORDER BY committed_at,round_id""",
            (self.scope_key,),
        ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def settle_terminal(self, *, round_id: str, attempt_id: str, ack_id: str,
                        sent: bool, acknowledged_at: datetime) -> tuple[OutcomeToken, ...]:
        """Claim one terminal ack and append only locally observable execution facts."""
        commit = self.get(round_id)
        if commit is None:
            return ()
        if commit.attempt_id != attempt_id:
            raise ValueError("send acknowledgement attempt does not match langchao live commit")
        kind = "sent" if sent else "failed"
        if commit.terminal_ack_kind is not None:
            if (commit.terminal_ack_id, commit.terminal_ack_kind) != (ack_id, kind):
                raise ValueError("langchao live commit already has a different terminal acknowledgement")
            return ()
        cursor = self.connection.execute(
            """UPDATE langchao_live_commits
               SET terminal_ack_id=%s, terminal_ack_kind=%s, terminal_acknowledged_at=%s
               WHERE scope_key=%s AND round_id=%s AND attempt_id=%s
                 AND terminal_ack_kind IS NULL RETURNING round_id""",
            (ack_id, kind, acknowledged_at, self.scope_key, round_id, attempt_id),
        )
        if cursor.fetchone() is None:
            winner = self.get(round_id)
            if winner is None or (winner.terminal_ack_id, winner.terminal_ack_kind) != (ack_id, kind):
                raise ValueError("langchao live terminal acknowledgement conflicts")
            return ()

        written: list[OutcomeToken] = []
        for expected in commit.expected_tokens:
            # User outcomes remain unobserved. Only explicit local execution tokens settle.
            if expected.outcome_key not in {"delivery", "expression_delivered", "rest_realized"}:
                continue
            status = OutcomeStatus.CONFIRMED if sent else OutcomeStatus.CENSORED
            actual = replace(
                expected,
                token_id=str(uuid5(NAMESPACE_URL, f"langchao-live:{attempt_id}:{expected.token_id}:{kind}")),
                settlement_type=SettlementType.ACTUAL,
                status=status,
                base_amount=expected.base_amount if sent else 0.0,
                evidence_version="langchao.live-ack.v1",
                idempotency_key=f"langchao-live:{attempt_id}:{expected.token_id}:{kind}",
                evidence_refs=tuple(dict.fromkeys((*expected.evidence_refs, ack_id, kind))),
                observation_started_at=None,
                observation_ends_at=None,
            )
            self.outcomes.put_outcome_revision(
                actual, revision=1, reward_contract_id=commit.reward_contract_id,
                reward_contract_revision=commit.reward_revision,
            )
            self.outcomes.activate_outcome(
                token_id=actual.token_id, revision=1, expected_pointer_version=0
            )
            written.append(actual)
        return tuple(written)

    def _decode(self, row: Any) -> LangchaoLiveCommit:
        def value(name: str, index: int) -> Any:
            return row[name] if isinstance(row, Mapping) else row[index]
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
            claim_id=str(value("claim_id", 10)), committed_at=value("committed_at", 11),
            terminal_ack_id=value("terminal_ack_id", 12),
            terminal_ack_kind=value("terminal_ack_kind", 13),
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
