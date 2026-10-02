"""Scoped PostgreSQL repository for the isolated 「浪潮」 result-token ledger.

It persists immutable outcome revisions and their correction lineage only; it is not
connected to Runtime, sending, or any reward side effect.
"""

from __future__ import annotations

import json
import math
from contextlib import nullcontext
from typing import Any, Mapping, Sequence

from .langchao_repository import (
    LangchaoReferenceError,
    LangchaoRevisionConflictError,
    _canonical_payload,
    _value,
)
from .langchao_types import MotivationDirection, OutcomeToken, SettlementType


class LangchaoOutcomeConflictError(LangchaoRevisionConflictError):
    """An idempotency key or immutable outcome identity was reused differently."""


class LangchaoOutcomeReferenceError(LangchaoReferenceError):
    """An exact reward/outcome revision reference is missing or incompatible."""


class LangchaoOutcomeRepository:
    """A single-scope repository for result identities, revisions and ledgers."""

    def __init__(self, connection: Any, *, scope_key: str) -> None:
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key must be a non-empty string")
        self.connection = connection
        self.scope_key = scope_key

    def _transaction(self) -> Any:
        transaction = getattr(self.connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    @staticmethod
    def _positive_revision(name: str, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    def _require_scope(self, outcome: OutcomeToken) -> None:
        if outcome.scope_key != self.scope_key:
            raise ValueError("OutcomeToken scope_key does not match repository scope_key")

    @staticmethod
    def _normalized_weights(outcome: OutcomeToken) -> str:
        """Return the eight-key storage representation without changing its scale."""
        supplied = dict(outcome.direction_weights)
        values = {direction.value: float(supplied.get(direction, 0.0)) for direction in MotivationDirection}
        if any(not math.isfinite(value) or value < 0.0 for value in values.values()):
            raise ValueError("direction_weights must be finite and non-negative")
        return json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _reward_revision(self, reward_contract_id: str, reward_contract_revision: int) -> Any:
        row = self.connection.execute(
            """SELECT goal_id, payload, total_cap
               FROM langchao_reward_revisions
               WHERE scope_key = %s AND reward_contract_id = %s AND revision = %s""",
            (self.scope_key, reward_contract_id, reward_contract_revision),
        ).fetchone()
        if row is None:
            raise LangchaoOutcomeReferenceError("exact reward contract revision does not exist in repository scope")
        return row

    def put_outcome_revision(
        self,
        outcome: OutcomeToken,
        *,
        revision: int,
        reward_contract_id: str,
        reward_contract_revision: int,
        corrects_revision: int | None = None,
    ) -> Any:
        """Append one immutable revision in a repository-owned transaction."""
        with self._transaction():
            return self.put_outcome_revision_in_transaction(
                outcome, revision=revision, reward_contract_id=reward_contract_id,
                reward_contract_revision=reward_contract_revision,
                corrects_revision=corrects_revision,
            )

    def put_outcome_revision_in_transaction(
        self,
        outcome: OutcomeToken,
        *,
        revision: int,
        reward_contract_id: str,
        reward_contract_revision: int,
        corrects_revision: int | None = None,
    ) -> Any:
        """Transaction-neutral append for a caller-owned settlement transaction."""

        if not isinstance(outcome, OutcomeToken):
            raise TypeError("outcome must be OutcomeToken")
        self._require_scope(outcome)
        self._positive_revision("revision", revision)
        self._positive_revision("reward_contract_revision", reward_contract_revision)
        if not isinstance(reward_contract_id, str) or not reward_contract_id.strip():
            raise ValueError("reward_contract_id must be a non-empty string")
        if corrects_revision is not None:
            self._positive_revision("corrects_revision", corrects_revision)
        if outcome.settlement_type is SettlementType.CORRECTION and corrects_revision is None:
            raise ValueError("correction outcomes require corrects_revision")
        if outcome.settlement_type is not SettlementType.CORRECTION and corrects_revision is not None:
            raise ValueError("corrects_revision is only valid for correction outcomes")

        _payload, encoded, digest = _canonical_payload(outcome)
        weights_json = self._normalized_weights(outcome)
        with nullcontext():
            # Idempotency precedes identity insertion so a conflicting replay leaves no
            # orphan identity when a real transaction rolls back.
            idem = self.connection.execute(
                """SELECT *, payload_sha256 = %s AS payload_matches
                   FROM langchao_outcome_revisions
                   WHERE scope_key = %s AND idempotency_key = %s""",
                (digest, self.scope_key, outcome.idempotency_key),
            ).fetchone()
            if idem is not None:
                if not bool(_value(idem, "payload_matches", -1)):
                    raise LangchaoOutcomeConflictError("same idempotency_key has different payload")
                if (
                    str(_value(idem, "token_id")) != outcome.token_id
                    or int(_value(idem, "revision", 1)) != revision
                    or str(_value(idem, "reward_contract_id", 2)) != reward_contract_id
                    or int(_value(idem, "reward_contract_revision", 3)) != reward_contract_revision
                ):
                    raise LangchaoOutcomeConflictError("idempotency replay changed immutable revision coordinates")
                return idem

            reward = self._reward_revision(reward_contract_id, reward_contract_revision)
            reward_payload = _value(reward, "payload", 1)
            if isinstance(reward_payload, str):
                reward_payload = json.loads(reward_payload)
            if isinstance(reward_payload, Mapping):
                if reward_payload.get("goal_id") != outcome.goal_id or reward_payload.get("episode_id") != outcome.episode_id:
                    raise LangchaoOutcomeReferenceError("outcome goal/episode does not match reward revision")

            self.connection.execute(
                """INSERT INTO langchao_outcome_identities
                   (scope_key, token_id, reward_contract_id, outcome_key)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (scope_key, token_id) DO NOTHING""",
                (self.scope_key, outcome.token_id, reward_contract_id, outcome.outcome_key),
            )
            identity = self.connection.execute(
                """SELECT reward_contract_id, outcome_key FROM langchao_outcome_identities
                   WHERE scope_key = %s AND token_id = %s""",
                (self.scope_key, outcome.token_id),
            ).fetchone()
            if identity is None or (
                _value(identity, "reward_contract_id") != reward_contract_id
                or _value(identity, "outcome_key", 1) != outcome.outcome_key
            ):
                raise LangchaoOutcomeConflictError("scoped token identity changed reward contract or outcome key")

            existing = self.connection.execute(
                """SELECT *, payload_sha256 = %s AS payload_matches
                   FROM langchao_outcome_revisions
                   WHERE scope_key = %s AND token_id = %s AND revision = %s""",
                (digest, self.scope_key, outcome.token_id, revision),
            ).fetchone()
            if existing is not None:
                if not bool(_value(existing, "payload_matches", -1)):
                    raise LangchaoOutcomeConflictError("same token/revision has different payload")
                return existing

            if outcome.settlement_type is SettlementType.CORRECTION:
                if outcome.corrects_token_id == outcome.token_id:
                    raise LangchaoOutcomeReferenceError("an outcome revision cannot correct its own token")
                corrected = self.connection.execute(
                    """SELECT r.payload, i.reward_contract_id, i.outcome_key
                       FROM langchao_outcome_revisions AS r
                       JOIN langchao_outcome_identities AS i
                         ON i.scope_key = r.scope_key AND i.token_id = r.token_id
                       WHERE r.scope_key = %s AND r.token_id = %s AND r.revision = %s""",
                    (self.scope_key, outcome.corrects_token_id, corrects_revision),
                ).fetchone()
                if corrected is None:
                    raise LangchaoOutcomeReferenceError("exact corrected outcome revision does not exist in scope")
                corrected_payload = _value(corrected, "payload")
                if isinstance(corrected_payload, str):
                    corrected_payload = json.loads(corrected_payload)
                if not isinstance(corrected_payload, Mapping) or (
                    corrected_payload.get("scope_key") != self.scope_key
                    or corrected_payload.get("goal_id") != outcome.goal_id
                    or corrected_payload.get("episode_id") != outcome.episode_id
                    or corrected_payload.get("outcome_key") != outcome.outcome_key
                ):
                    raise LangchaoOutcomeReferenceError(
                        "correction must match corrected scope, goal, episode, and outcome_key"
                    )

            return self.connection.execute(
                """INSERT INTO langchao_outcome_revisions
                   (scope_key, token_id, revision, token_version, reward_contract_id,
                    reward_contract_revision, settlement_type, status, base_amount,
                    direction_weights, evidence_version, idempotency_key, milestone_id,
                    observation_started_at, observation_ends_at, corrects_token_id,
                    corrects_revision, payload, payload_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                           %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                   RETURNING *""",
                (
                    self.scope_key, outcome.token_id, revision, outcome.token_version,
                    reward_contract_id, reward_contract_revision, outcome.settlement_type.value,
                    outcome.status.value, outcome.base_amount, weights_json,
                    outcome.evidence_version, outcome.idempotency_key, outcome.milestone_id,
                    outcome.observation_started_at, outcome.observation_ends_at,
                    outcome.corrects_token_id, corrects_revision, encoded, digest,
                ),
            ).fetchone()

    def activate_outcome(self, *, token_id: str, revision: int, expected_pointer_version: int) -> bool:
        self._positive_revision("revision", revision)
        if not isinstance(expected_pointer_version, int) or isinstance(expected_pointer_version, bool) or expected_pointer_version < 0:
            raise ValueError("expected_pointer_version must be a non-negative integer")
        with self._transaction():
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"langchao:outcome:{self.scope_key}:{token_id}",),
            )
            target = self.connection.execute(
                """SELECT 1 FROM langchao_outcome_revisions
                   WHERE scope_key = %s AND token_id = %s AND revision = %s""",
                (self.scope_key, token_id, revision),
            ).fetchone()
            if target is None:
                raise LangchaoOutcomeReferenceError("target outcome revision does not exist in repository scope")
            current = self.connection.execute(
                """SELECT pointer_version FROM langchao_outcome_active
                   WHERE scope_key = %s AND token_id = %s FOR UPDATE""",
                (self.scope_key, token_id),
            ).fetchone()
            actual = 0 if current is None else int(_value(current, "pointer_version"))
            if actual != expected_pointer_version:
                return False
            cursor = self.connection.execute(
                """INSERT INTO langchao_outcome_active
                   (scope_key, token_id, revision, pointer_version)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (scope_key, token_id) DO UPDATE
                   SET revision = EXCLUDED.revision,
                       pointer_version = EXCLUDED.pointer_version,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE langchao_outcome_active.pointer_version = %s""",
                (self.scope_key, token_id, revision, actual + 1, expected_pointer_version),
            )
            return getattr(cursor, "rowcount", 1) == 1

    def bind_reward_outcomes(
        self,
        *,
        reward_contract_id: str,
        reward_contract_revision: int,
        outcome_revisions: Sequence[tuple[str, int]],
    ) -> tuple[Any, ...]:
        """Bind exact *expected* revisions and enforce cap in base-amount units.

        The cap is compared with ``sum(base_amount)`` once per token.  Direction
        weights are a normalized allocation of that amount and are never summed as
        eight independent awards.  Actual and correction rows are result-ledger facts,
        not members of this expected ledger.
        """

        self._positive_revision("reward_contract_revision", reward_contract_revision)
        refs = tuple(outcome_revisions)
        if len({token_id for token_id, _ in refs}) != len(refs):
            raise ValueError("outcome_revisions must not contain duplicate token ids")
        with self._transaction():
            reward = self._reward_revision(reward_contract_id, reward_contract_revision)
            total_cap = float(_value(reward, "total_cap", 2))
            rows: list[Any] = []
            total_base = 0.0
            for token_id, revision in refs:
                self._positive_revision("outcome revision", revision)
                row = self.connection.execute(
                    """SELECT r.*, i.reward_contract_id AS identity_reward_contract_id
                       FROM langchao_outcome_revisions AS r
                       JOIN langchao_outcome_identities AS i
                         ON i.scope_key = r.scope_key AND i.token_id = r.token_id
                       WHERE r.scope_key = %s AND r.token_id = %s AND r.revision = %s""",
                    (self.scope_key, token_id, revision),
                ).fetchone()
                if row is None:
                    raise LangchaoOutcomeReferenceError("exact outcome revision does not exist in scope")
                if _value(row, "identity_reward_contract_id", -1) != reward_contract_id:
                    raise LangchaoOutcomeReferenceError("outcome identity belongs to a different reward contract")
                if _value(row, "settlement_type") != SettlementType.EXPECTED.value:
                    raise LangchaoOutcomeReferenceError("actual/correction outcomes cannot enter the expected ledger")
                total_base += float(_value(row, "base_amount"))
                rows.append(row)
            if not math.isfinite(total_base) or total_base > total_cap + 1e-12:
                raise LangchaoOutcomeConflictError("expected outcome base amounts exceed reward total_cap")

            existing = self.connection.execute(
                """SELECT token_id, outcome_revision, ordinal
                   FROM langchao_reward_outcomes
                   WHERE scope_key = %s AND reward_contract_id = %s
                     AND reward_contract_revision = %s
                   ORDER BY ordinal""",
                (self.scope_key, reward_contract_id, reward_contract_revision),
            ).fetchall()
            existing_refs = tuple(
                (str(_value(row, "token_id")), int(_value(row, "outcome_revision", 1))) for row in existing
            )
            if existing_refs:
                if existing_refs != refs:
                    raise LangchaoOutcomeConflictError("reward revision already has different exact outcome membership")
                return tuple(existing)
            for ordinal, (token_id, revision) in enumerate(refs):
                self.connection.execute(
                    """INSERT INTO langchao_reward_outcomes
                       (scope_key, reward_contract_id, reward_contract_revision,
                        token_id, outcome_revision, ordinal)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (self.scope_key, reward_contract_id, reward_contract_revision, token_id, revision, ordinal),
                )
            return tuple(rows)

    def list_settled_user_observations(self) -> tuple[Any, ...]:
        """Return active user outcome facts for future forecast conditioning.

        Only active semantic pointers are read, so corrections replace rather than
        duplicate earlier evidence.  The consumer still decides which terminal
        statuses are statistically usable.
        """
        cursor = self.connection.execute(
            """SELECT r.token_id, r.revision, r.settlement_type, r.status,
                      r.base_amount, i.outcome_key,
                      rr.payload->>'template_key' AS template_key
               FROM langchao_user_outcome_active AS a
               JOIN langchao_outcome_revisions AS r
                 ON r.scope_key=a.scope_key AND r.token_id=a.token_id
                AND r.revision=a.revision
               JOIN langchao_outcome_identities AS i
                 ON i.scope_key=r.scope_key AND i.token_id=r.token_id
               JOIN langchao_reward_revisions AS rr
                 ON rr.scope_key=r.scope_key
                AND rr.reward_contract_id=r.reward_contract_id
                AND rr.revision=r.reward_contract_revision
               WHERE a.scope_key=%s
               ORDER BY r.token_id, r.revision""",
            (self.scope_key,),
        )
        return tuple(cursor.fetchall())

    def get_active_observation(
        self, *, reward_contract_id: str, episode_id: str, outcome_key: str
    ) -> Any:
        """Return the latest settled user fact for one semantic reward outcome."""
        return self.connection.execute(
            """SELECT r.*, a.pointer_version, a.source_exposure_id,
                      a.source_label_revision
               FROM langchao_user_outcome_active AS a
               JOIN langchao_outcome_revisions AS r
                 ON r.scope_key=a.scope_key AND r.token_id=a.token_id
                AND r.revision=a.revision
               WHERE a.scope_key=%s AND a.reward_contract_id=%s
                 AND a.episode_id=%s AND a.outcome_key=%s""",
            (self.scope_key, reward_contract_id, episode_id, outcome_key),
        ).fetchone()

    def activate_observation(
        self, *, reward_contract_id: str, episode_id: str, outcome_key: str,
        token_id: str, revision: int, source_exposure_id: str,
        source_label_revision: int, expected_pointer_version: int,
    ) -> bool:
        """CAS the semantic user-outcome pointer, preserving correction lineage."""
        self._positive_revision("revision", revision)
        self._positive_revision("source_label_revision", source_label_revision)
        if expected_pointer_version < 0:
            raise ValueError("expected_pointer_version must be non-negative")
        cursor = self.connection.execute(
            """INSERT INTO langchao_user_outcome_active
               (scope_key,reward_contract_id,episode_id,outcome_key,token_id,revision,
                source_exposure_id,source_label_revision,pointer_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (scope_key,reward_contract_id,episode_id,outcome_key) DO UPDATE
               SET token_id=EXCLUDED.token_id, revision=EXCLUDED.revision,
                   source_exposure_id=EXCLUDED.source_exposure_id,
                   source_label_revision=EXCLUDED.source_label_revision,
                   pointer_version=EXCLUDED.pointer_version, updated_at=CURRENT_TIMESTAMP
               WHERE langchao_user_outcome_active.pointer_version=%s
                 AND langchao_user_outcome_active.source_label_revision < EXCLUDED.source_label_revision""",
            (self.scope_key, reward_contract_id, episode_id, outcome_key, token_id,
             revision, source_exposure_id, source_label_revision,
             expected_pointer_version + 1, expected_pointer_version),
        )
        return getattr(cursor, "rowcount", 1) == 1

    def get_active_outcome(self, *, token_id: str) -> Any:
        return self.connection.execute(
            """SELECT r.*, a.pointer_version
               FROM langchao_outcome_active AS a
               JOIN langchao_outcome_revisions AS r
                 ON r.scope_key = a.scope_key AND r.token_id = a.token_id
                AND r.revision = a.revision
               WHERE a.scope_key = %s AND a.token_id = %s""",
            (self.scope_key, token_id),
        ).fetchone()

    def get_ledger_lineage(self, *, token_id: str, revision: int) -> tuple[Any, ...]:
        """Return the exact correction ancestry, newest row first."""

        self._positive_revision("revision", revision)
        cursor = self.connection.execute(
            """WITH RECURSIVE lineage AS (
                   SELECT r.*, 0 AS lineage_depth
                   FROM langchao_outcome_revisions AS r
                   WHERE r.scope_key = %s AND r.token_id = %s AND r.revision = %s
                   UNION ALL
                   SELECT parent.*, lineage.lineage_depth + 1
                   FROM lineage
                   JOIN langchao_outcome_revisions AS parent
                     ON parent.scope_key = lineage.scope_key
                    AND parent.token_id = lineage.corrects_token_id
                    AND parent.revision = lineage.corrects_revision
               )
               SELECT * FROM lineage ORDER BY lineage_depth""",
            (self.scope_key, token_id, revision),
        )
        return tuple(cursor.fetchall())


__all__ = [
    "LangchaoOutcomeConflictError",
    "LangchaoOutcomeReferenceError",
    "LangchaoOutcomeRepository",
]
