"""Scoped PostgreSQL persistence for immutable 「浪潮」 contract revisions.

This module is deliberately not connected to Runtime or sending.  A repository is
bound to exactly one ``scope_key`` at construction, stores DTO ``to_dict`` payloads,
and selects active revisions only through independently CAS-versioned pointers.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from typing import Any, Mapping

from .langchao_types import ActionCandidateContract, GoalContract, RewardContract


class LangchaoRevisionConflictError(RuntimeError):
    """The same scoped identity/revision was presented with different content."""


class LangchaoReferenceError(RuntimeError):
    """A required exact scoped parent revision is not active or does not exist."""


def _canonical_payload(dto: Any) -> tuple[dict[str, Any], str, str]:
    to_dict = getattr(dto, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("浪潮 persistence inputs must be DTOs with to_dict()")
    payload = to_dict()
    if not isinstance(payload, dict):
        raise TypeError("DTO to_dict() must return a dict")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return payload, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    return row[key] if isinstance(row, Mapping) else row[index]


class LangchaoRepository:
    """A configuration-bound, single-scope 「浪潮」 PostgreSQL repository."""

    def __init__(self, connection: Any, *, scope_key: str) -> None:
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key must be a non-empty string")
        self.connection = connection
        self.scope_key = scope_key

    def _transaction(self) -> Any:
        transaction = getattr(self.connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    def _require_scope(self, dto: Any) -> None:
        if getattr(dto, "scope_key", None) != self.scope_key:
            raise ValueError("DTO scope_key does not match repository scope_key")

    def _put_identity(self, table: str, id_column: str, identity: str, semantic_key: str) -> None:
        self.connection.execute(
            f"""INSERT INTO {table} (scope_key, {id_column}, semantic_key)
                VALUES (%s, %s, %s)
                ON CONFLICT (scope_key, {id_column}) DO NOTHING""",
            (self.scope_key, identity, semantic_key),
        )
        row = self.connection.execute(
            f"SELECT semantic_key FROM {table} WHERE scope_key = %s AND {id_column} = %s",
            (self.scope_key, identity),
        ).fetchone()
        if row is None or _value(row, "semantic_key") != semantic_key:
            raise LangchaoRevisionConflictError("scoped identity has a different semantic_key")

    def _existing_or_conflict(self, table: str, id_column: str, identity: str, revision: int, digest: str) -> Any | None:
        row = self.connection.execute(
            f"""SELECT *, payload_sha256 = %s AS payload_matches FROM {table}
                WHERE scope_key = %s AND {id_column} = %s AND revision = %s""",
            (digest, self.scope_key, identity, revision),
        ).fetchone()
        if row is None:
            return None
        if not bool(_value(row, "payload_matches", -1)):
            raise LangchaoRevisionConflictError("same identity/revision has different payload")
        return row

    def put_goal_revision(self, goal: GoalContract) -> Any:
        if not isinstance(goal, GoalContract):
            raise TypeError("goal must be GoalContract")
        self._require_scope(goal)
        _payload, encoded, digest = _canonical_payload(goal)
        with self._transaction():
            self._put_identity("langchao_goal_identities", "goal_id", goal.goal_id, goal.semantic_key)
            existing = self._existing_or_conflict("langchao_goal_revisions", "goal_id", goal.goal_id, goal.revision, digest)
            if existing is not None:
                return existing
            return self.connection.execute(
                """INSERT INTO langchao_goal_revisions
                   (scope_key, goal_id, revision, payload, payload_sha256, kind, ownership,
                    status, parent_goal_id, reward_contract_id, contract_created_at, contract_updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (self.scope_key, goal.goal_id, goal.revision, encoded, digest, goal.kind.value,
                 goal.ownership.value, goal.status.value, goal.parent_goal_id,
                 goal.reward_contract_id, goal.created_at, goal.updated_at),
            ).fetchone()

    def put_reward_revision(self, reward: RewardContract) -> Any:
        if not isinstance(reward, RewardContract):
            raise TypeError("reward must be RewardContract")
        self._require_scope(reward)
        _payload, encoded, digest = _canonical_payload(reward)
        semantic_key = reward.template_key
        with self._transaction():
            self._put_identity("langchao_reward_identities", "reward_contract_id", reward.reward_contract_id, semantic_key)
            existing = self._existing_or_conflict(
                "langchao_reward_revisions", "reward_contract_id", reward.reward_contract_id, reward.revision, digest
            )
            if existing is not None:
                return existing
            return self.connection.execute(
                """INSERT INTO langchao_reward_revisions
                   (scope_key, reward_contract_id, revision, payload, payload_sha256, goal_id,
                    total_cap, contract_created_at, contract_updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (self.scope_key, reward.reward_contract_id, reward.revision, encoded, digest,
                 reward.goal_id, reward.total_cap, reward.created_at, reward.updated_at),
            ).fetchone()

    def put_candidate_revision(self, candidate: ActionCandidateContract) -> Any:
        if not isinstance(candidate, ActionCandidateContract):
            raise TypeError("candidate must be ActionCandidateContract")
        self._require_scope(candidate)
        _payload, encoded, digest = _canonical_payload(candidate)
        revision = candidate.semantic_revision
        with self._transaction():
            self._put_identity(
                "langchao_candidate_identities", "candidate_id", candidate.candidate_id, candidate.semantic_key
            )
            existing = self._existing_or_conflict(
                "langchao_candidate_revisions", "candidate_id", candidate.candidate_id, revision, digest
            )
            if existing is not None:
                return existing
            reward_row = self.connection.execute(
                """SELECT revision FROM langchao_reward_active
                   WHERE scope_key = %s AND reward_contract_id = %s""",
                (self.scope_key, candidate.reward_contract_ref),
            ).fetchone()
            if reward_row is None:
                raise LangchaoReferenceError(
                    f"reward {candidate.reward_contract_ref!r} has no active revision in scope"
                )
            reward_revision = int(_value(reward_row, "revision"))
            goal_revisions: list[tuple[str, int]] = []
            for goal_id in candidate.goal_refs:
                row = self.connection.execute(
                    """SELECT revision FROM langchao_goal_active
                       WHERE scope_key = %s AND goal_id = %s""",
                    (self.scope_key, goal_id),
                ).fetchone()
                if row is None:
                    raise LangchaoReferenceError(f"goal {goal_id!r} has no active revision in scope")
                goal_revisions.append((goal_id, int(_value(row, "revision"))))
            inserted = self.connection.execute(
                """INSERT INTO langchao_candidate_revisions
                   (scope_key, candidate_id, revision, payload, payload_sha256, kind, state,
                    retirement_reason, reward_contract_id, reward_revision, resource_budget,
                    available_from, expires_at, contract_created_at, contract_updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (self.scope_key, candidate.candidate_id, revision, encoded, digest,
                 candidate.kind.value, candidate.state.value,
                 candidate.retirement_reason.value if candidate.retirement_reason else None,
                 candidate.reward_contract_ref, reward_revision, candidate.resource_budget,
                 candidate.available_from, candidate.expires_at, candidate.created_at,
                 candidate.updated_at),
            ).fetchone()
            for ordinal, (goal_id, goal_revision) in enumerate(goal_revisions):
                self.connection.execute(
                    """INSERT INTO langchao_candidate_goal_refs
                       (scope_key, candidate_id, candidate_revision, goal_id, goal_revision, ordinal)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (self.scope_key, candidate.candidate_id, revision, goal_id, goal_revision, ordinal),
                )
            return inserted

    def _activate(self, kind: str, identity: str, revision: int, expected_pointer_version: int) -> bool:
        if not isinstance(expected_pointer_version, int) or isinstance(expected_pointer_version, bool) or expected_pointer_version < 0:
            raise ValueError("expected_pointer_version must be a non-negative integer")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("revision must be a positive integer")
        definitions = {
            "goal": ("goal_id", "langchao_goal_revisions", "langchao_goal_active"),
            "reward": ("reward_contract_id", "langchao_reward_revisions", "langchao_reward_active"),
            "candidate": ("candidate_id", "langchao_candidate_revisions", "langchao_candidate_active"),
        }
        id_column, revision_table, active_table = definitions[kind]
        with self._transaction():
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"langchao:{kind}:{self.scope_key}:{identity}",),
            )
            target = self.connection.execute(
                f"SELECT 1 FROM {revision_table} WHERE scope_key = %s AND {id_column} = %s AND revision = %s",
                (self.scope_key, identity, revision),
            ).fetchone()
            if target is None:
                raise LangchaoReferenceError("target revision does not exist in repository scope")
            current = self.connection.execute(
                f"SELECT pointer_version FROM {active_table} WHERE scope_key = %s AND {id_column} = %s FOR UPDATE",
                (self.scope_key, identity),
            ).fetchone()
            actual = 0 if current is None else int(_value(current, "pointer_version"))
            if actual != expected_pointer_version:
                return False
            next_version = actual + 1
            cursor = self.connection.execute(
                f"""INSERT INTO {active_table} (scope_key, {id_column}, revision, pointer_version)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (scope_key, {id_column}) DO UPDATE
                    SET revision = EXCLUDED.revision,
                        pointer_version = EXCLUDED.pointer_version,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE {active_table}.pointer_version = %s""",
                (self.scope_key, identity, revision, next_version, expected_pointer_version),
            )
            return getattr(cursor, "rowcount", 1) == 1

    def activate_goal(self, *, goal_id: str, revision: int, expected_pointer_version: int) -> bool:
        return self._activate("goal", goal_id, revision, expected_pointer_version)

    def activate_reward(self, *, reward_contract_id: str, revision: int, expected_pointer_version: int) -> bool:
        return self._activate("reward", reward_contract_id, revision, expected_pointer_version)

    def activate_candidate(self, *, candidate_id: str, revision: int, expected_pointer_version: int) -> bool:
        return self._activate("candidate", candidate_id, revision, expected_pointer_version)

    def _get_active(self, kind: str, identity: str) -> Any:
        definitions = {
            "goal": ("goal_id", "langchao_goal_revisions", "langchao_goal_active"),
            "reward": ("reward_contract_id", "langchao_reward_revisions", "langchao_reward_active"),
            "candidate": ("candidate_id", "langchao_candidate_revisions", "langchao_candidate_active"),
        }
        id_column, revision_table, active_table = definitions[kind]
        return self.connection.execute(
            f"""SELECT r.*, a.pointer_version
                FROM {active_table} AS a
                JOIN {revision_table} AS r
                  ON r.scope_key = a.scope_key AND r.{id_column} = a.{id_column}
                 AND r.revision = a.revision
                WHERE a.scope_key = %s AND a.{id_column} = %s""",
            (self.scope_key, identity),
        ).fetchone()

    def get_active_goal(self, *, goal_id: str) -> Any:
        return self._get_active("goal", goal_id)

    def get_active_reward(self, *, reward_contract_id: str) -> Any:
        return self._get_active("reward", reward_contract_id)

    def get_active_candidate(self, *, candidate_id: str) -> Any:
        return self._get_active("candidate", candidate_id)


__all__ = [
    "LangchaoReferenceError", "LangchaoRepository", "LangchaoRevisionConflictError",
]
