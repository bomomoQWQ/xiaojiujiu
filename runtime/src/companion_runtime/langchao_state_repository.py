"""Transactional persistence for isolated 「浪潮」 rounds and state snapshots.

No external action or message send is performed here.  Callers run the numerical
kernel outside the transaction, then atomically append its immutable result.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .langchao_engine import AdvanceResult, IntegrationStep
from .langchao_types import LANGCHAO_STATE_VERSION, LangchaoState, MotivationDirection


class LangchaoStateConflictError(RuntimeError):
    """Stored state differs, or the active-state CAS lost."""


class LangchaoStateReferenceError(RuntimeError):
    """An exact round, state, or candidate revision is unavailable."""


def _value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    return row[key] if isinstance(row, Mapping) else row[index]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical(value: Any) -> tuple[str, str]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _state_payload(state: LangchaoState) -> tuple[str, str]:
    return _canonical(state.to_dict())


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise LangchaoStateConflictError("state payload contains an invalid datetime")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _payload_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise LangchaoStateConflictError("state payload must be a JSON object")
    return dict(value)


def _rebuild_state(payload_value: Any) -> LangchaoState:
    """Strictly reconstruct the public DTO; its constructor revalidates all invariants."""
    payload = _payload_object(payload_value)
    expected = {
        "scope_key", "decision_round_id", "working_set", "readiness", "attraction", "attention",
        "advanced_at", "based_on_state_version", "event_cursor", "goal_snapshot_version",
        "reward_snapshot_version", "candidate_snapshot_version", "prediction_snapshot_version",
        "value_profile_version", "attention_version", "parameter_version", "permission_version",
        "revision", "state_version",
    }
    if set(payload) != expected:
        raise LangchaoStateConflictError("state payload fields do not exactly match LangchaoState")
    try:
        attention_obj = payload["attention"]
        if not isinstance(attention_obj, Mapping):
            raise TypeError("attention must be an object")
        readiness_obj = payload["readiness"]
        attraction_obj = payload["attraction"]
        if not isinstance(readiness_obj, Mapping) or not isinstance(attraction_obj, Mapping):
            raise TypeError("scores must be objects")
        working_set = tuple(payload["working_set"])
        return LangchaoState(
            scope_key=payload["scope_key"], decision_round_id=payload["decision_round_id"],
            working_set=working_set,
            readiness=tuple((candidate_id, readiness_obj[candidate_id]) for candidate_id in working_set),
            attraction=tuple((candidate_id, attraction_obj[candidate_id]) for candidate_id in working_set),
            attention=tuple((direction, attention_obj[direction.value]) for direction in MotivationDirection),
            advanced_at=_parse_datetime(payload["advanced_at"]),
            based_on_state_version=payload["based_on_state_version"], event_cursor=payload["event_cursor"],
            goal_snapshot_version=payload["goal_snapshot_version"],
            reward_snapshot_version=payload["reward_snapshot_version"],
            candidate_snapshot_version=payload["candidate_snapshot_version"],
            prediction_snapshot_version=payload["prediction_snapshot_version"],
            value_profile_version=payload["value_profile_version"], attention_version=payload["attention_version"],
            parameter_version=payload["parameter_version"], permission_version=payload["permission_version"],
            revision=payload["revision"], state_version=payload["state_version"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LangchaoStateConflictError("invalid persisted LangchaoState payload") from exc


class LangchaoStateRepository:
    """Single-scope repository for rounds, trajectories, and active state."""

    def __init__(self, connection: Any, *, scope_key: str) -> None:
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key must be a non-empty string")
        self.connection = connection
        self.scope_key = scope_key

    def _transaction(self) -> Any:
        transaction = getattr(self.connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    def _validate_state(self, state: LangchaoState) -> None:
        if not isinstance(state, LangchaoState):
            raise TypeError("state must be LangchaoState")
        if state.scope_key != self.scope_key:
            raise ValueError("state scope_key does not match repository scope_key")

    @staticmethod
    def _candidate_revisions(state: LangchaoState, revisions: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
        if not isinstance(revisions, Mapping) or set(revisions) != set(state.working_set):
            raise ValueError("candidate_revisions must exactly cover state.working_set")
        ordered: list[tuple[str, int]] = []
        for candidate_id in state.working_set:
            revision = revisions[candidate_id]
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ValueError("candidate revisions must be positive integers")
            ordered.append((candidate_id, revision))
        return tuple(ordered)

    def _insert_snapshot(self, state: LangchaoState, revisions: tuple[tuple[str, int], ...]) -> None:
        encoded, digest = _state_payload(state)
        existing = self.connection.execute(
            """SELECT payload_sha256 FROM langchao_state_snapshots
               WHERE scope_key = %s AND round_id = %s AND state_revision = %s""",
            (self.scope_key, state.decision_round_id, state.revision),
        ).fetchone()
        if existing is not None:
            if _value(existing, "payload_sha256") != digest:
                raise LangchaoStateConflictError("same round/state revision has different payload")
            return
        self.connection.execute(
            """INSERT INTO langchao_state_snapshots
               (scope_key, round_id, state_revision, payload, payload_sha256, advanced_at,
                event_cursor, based_on_state_version, goal_snapshot_version, reward_snapshot_version,
                candidate_snapshot_version, prediction_snapshot_version, value_profile_version,
                attention_version, parameter_version, permission_version, state_version)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (self.scope_key, state.decision_round_id, state.revision, encoded, digest, state.advanced_at,
             state.event_cursor, state.based_on_state_version, state.goal_snapshot_version,
             state.reward_snapshot_version, state.candidate_snapshot_version,
             state.prediction_snapshot_version, state.value_profile_version, state.attention_version,
             state.parameter_version, state.permission_version, state.state_version),
        )
        readiness, attraction = dict(state.readiness), dict(state.attraction)
        for ordinal, (candidate_id, candidate_revision) in enumerate(revisions):
            self.connection.execute(
                """INSERT INTO langchao_state_candidates
                   (scope_key, round_id, state_revision, ordinal, candidate_id, candidate_revision,
                    readiness, attraction) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (self.scope_key, state.decision_round_id, state.revision, ordinal, candidate_id,
                 candidate_revision, readiness[candidate_id], attraction[candidate_id]),
            )

    def _cas_active(self, state: LangchaoState, expected_pointer_version: int) -> int:
        if not isinstance(expected_pointer_version, int) or isinstance(expected_pointer_version, bool) or expected_pointer_version < 0:
            raise ValueError("expected_pointer_version must be a non-negative integer")
        self.connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"langchao:state:{self.scope_key}",),
        )
        row = self.connection.execute(
            "SELECT pointer_version FROM langchao_active_state WHERE scope_key = %s FOR UPDATE", (self.scope_key,),
        ).fetchone()
        actual = 0 if row is None else int(_value(row, "pointer_version"))
        if actual != expected_pointer_version:
            raise LangchaoStateConflictError(f"active state CAS failed: expected {expected_pointer_version}, found {actual}")
        next_version = actual + 1
        cursor = self.connection.execute(
            """INSERT INTO langchao_active_state (scope_key, round_id, state_revision, pointer_version)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (scope_key) DO UPDATE SET round_id = EXCLUDED.round_id,
                   state_revision = EXCLUDED.state_revision, pointer_version = EXCLUDED.pointer_version,
                   updated_at = CURRENT_TIMESTAMP
               WHERE langchao_active_state.pointer_version = %s""",
            (self.scope_key, state.decision_round_id, state.revision, next_version, expected_pointer_version),
        )
        if getattr(cursor, "rowcount", 1) != 1:
            raise LangchaoStateConflictError("active state CAS failed")
        return next_version

    def get_round_status(self, *, round_id: str) -> str | None:
        """Return the persisted lifecycle status for one scoped round."""
        if not isinstance(round_id, str) or not round_id.strip():
            raise ValueError("round_id must be a non-empty string")
        row = self.connection.execute(
            "SELECT status FROM langchao_rounds WHERE scope_key = %s AND round_id = %s",
            (self.scope_key, round_id),
        ).fetchone()
        return None if row is None else str(_value(row, "status"))

    def abort_open_round(
        self, *, round_id: str, ended_at: datetime, expected_pointer_version: int,
    ) -> bool:
        """Abort the exact active open round before replacing its working set.

        The active pointer is deliberately retained until ``begin_round`` CAS-publishes
        the replacement in the same outer transaction.
        """
        if not isinstance(round_id, str) or not round_id.strip():
            raise ValueError("round_id must be a non-empty string")
        if ended_at.tzinfo is None or ended_at.utcoffset() is None:
            raise ValueError("ended_at must be timezone-aware")
        if not isinstance(expected_pointer_version, int) or isinstance(expected_pointer_version, bool) or expected_pointer_version < 1:
            raise ValueError("expected_pointer_version must be a positive integer")
        with self._transaction():
            active = self.connection.execute(
                """SELECT round_id, pointer_version FROM langchao_active_state
                   WHERE scope_key = %s FOR UPDATE""",
                (self.scope_key,),
            ).fetchone()
            if active is None:
                raise LangchaoStateReferenceError("no active 浪潮 state")
            if (str(_value(active, "round_id")), int(_value(active, "pointer_version", 1))) != (
                round_id, expected_pointer_version,
            ):
                raise LangchaoStateConflictError("active round changed before abort")
            cursor = self.connection.execute(
                """UPDATE langchao_rounds SET status = 'aborted', ended_at = %s
                   WHERE scope_key = %s AND round_id = %s AND status = 'open'""",
                (ended_at, self.scope_key, round_id),
            )
            return getattr(cursor, "rowcount", 1) == 1

    def begin_round(
        self, state: LangchaoState, *, run_mode: str,
        candidate_revisions: Mapping[str, int], expected_pointer_version: int = 0,
        authority_revision: int | None = None,
    ) -> int:
        """Persist an open round and initial state, publishing it by CAS."""
        self._validate_state(state)
        if run_mode not in {"live", "shadow", "replay"}:
            raise ValueError("run_mode must be live, shadow, or replay")
        if authority_revision is not None and (not isinstance(authority_revision, int) or isinstance(authority_revision, bool) or authority_revision < 1):
            raise ValueError("authority_revision must be a positive integer or None")
        revisions = self._candidate_revisions(state, candidate_revisions)
        with self._transaction():
            existing = self.connection.execute(
                "SELECT run_mode, status, event_cursor FROM langchao_rounds WHERE scope_key = %s AND round_id = %s",
                (self.scope_key, state.decision_round_id),
            ).fetchone()
            if existing is not None:
                if (_value(existing, "run_mode"), _value(existing, "status"), _value(existing, "event_cursor")) != (run_mode, "open", state.event_cursor):
                    raise LangchaoStateConflictError("same round_id has different round metadata")
            else:
                self.connection.execute(
                    """INSERT INTO langchao_rounds
                       (scope_key, round_id, run_mode, status, started_at, event_cursor, authority_revision)
                       VALUES (%s, %s, %s, 'open', %s, %s, %s)""",
                    (self.scope_key, state.decision_round_id, run_mode, state.advanced_at,
                     state.event_cursor, authority_revision),
                )
            self._insert_snapshot(state, revisions)
            return self._cas_active(state, expected_pointer_version)

    @staticmethod
    def _step_payload(step: IntegrationStep) -> tuple[str, str]:
        return _canonical({
            "started_at": step.started_at.isoformat(), "ended_at": step.ended_at.isoformat(),
            "readiness_before": dict(step.readiness_before), "readiness_after": dict(step.readiness_after),
            "attraction": dict(step.attraction),
            "first_crossing_candidates": list(step.first_crossing_candidates), "step_version": step.step_version,
        })

    def append_advance(
        self, result: AdvanceResult, *, input_state: LangchaoState,
        candidate_revisions: Mapping[str, int], expected_pointer_version: int,
    ) -> int:
        """Atomically append steps/snapshot/candidates, CAS-publish, and finish the round."""
        if not isinstance(result, AdvanceResult):
            raise TypeError("result must be AdvanceResult")
        self._validate_state(input_state)
        self._validate_state(result.state)
        if result.state.decision_round_id != input_state.decision_round_id:
            raise ValueError("result and input state must belong to the same round")
        if result.state.revision <= input_state.revision:
            raise ValueError("result state revision must advance input state revision")
        revisions = self._candidate_revisions(result.state, candidate_revisions)
        if set(input_state.working_set) - set(result.state.working_set):
            raise ValueError("append_advance may not silently remove input candidates")
        input_encoded, input_digest = _state_payload(input_state)
        del input_encoded
        output_encoded, output_digest = _state_payload(result.state)
        del output_encoded
        with self._transaction():
            active = self.connection.execute(
                """SELECT a.round_id, a.state_revision, a.pointer_version, s.payload_sha256
                   FROM langchao_active_state a JOIN langchao_state_snapshots s
                     ON s.scope_key = a.scope_key AND s.round_id = a.round_id
                    AND s.state_revision = a.state_revision
                   WHERE a.scope_key = %s FOR UPDATE""", (self.scope_key,),
            ).fetchone()
            if active is None:
                raise LangchaoStateReferenceError("no active 浪潮 state")
            active_identity = (_value(active, "round_id"), int(_value(active, "state_revision")))
            expected_identity = (input_state.decision_round_id, input_state.revision)
            if active_identity != expected_identity or _value(active, "payload_sha256") != input_digest:
                # Exact replay after a successful commit is idempotent.
                existing = self.connection.execute(
                    """SELECT payload_sha256 FROM langchao_state_snapshots
                       WHERE scope_key = %s AND round_id = %s AND state_revision = %s""",
                    (self.scope_key, result.state.decision_round_id, result.state.revision),
                ).fetchone()
                if existing is not None and _value(existing, "payload_sha256") == output_digest and active_identity == (result.state.decision_round_id, result.state.revision):
                    return int(_value(active, "pointer_version"))
                raise LangchaoStateConflictError("active state is not the exact input snapshot")
            if int(_value(active, "pointer_version")) != expected_pointer_version:
                raise LangchaoStateConflictError("active state pointer version changed")
            round_row = self.connection.execute(
                "SELECT status FROM langchao_rounds WHERE scope_key = %s AND round_id = %s FOR UPDATE",
                (self.scope_key, input_state.decision_round_id),
            ).fetchone()
            if round_row is None or _value(round_row, "status") != "open":
                raise LangchaoStateConflictError("round is not open")
            self._insert_snapshot(result.state, revisions)
            for ordinal, step in enumerate(result.steps):
                encoded, digest = self._step_payload(step)
                payload = json.loads(encoded)
                self.connection.execute(
                    """INSERT INTO langchao_integration_steps
                       (scope_key, round_id, result_state_revision, ordinal, started_at, ended_at,
                        readiness_before, readiness_after, attraction, first_crossing_candidates,
                        payload_sha256, step_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                               %s::jsonb, %s, %s)""",
                    (self.scope_key, result.state.decision_round_id, result.state.revision, ordinal,
                     step.started_at, step.ended_at,
                     json.dumps(payload["readiness_before"], sort_keys=True, separators=(",", ":")),
                     json.dumps(payload["readiness_after"], sort_keys=True, separators=(",", ":")),
                     json.dumps(payload["attraction"], sort_keys=True, separators=(",", ":")),
                     json.dumps(payload["first_crossing_candidates"], separators=(",", ":")),
                     digest, step.step_version),
                )
            next_pointer = self._cas_active(result.state, expected_pointer_version)
            status = "decided" if result.decision_candidate_id is not None else ("deferred" if result.defer_reason is not None else "open")
            decision_revision = None if result.decision_candidate_id is None else dict(revisions)[result.decision_candidate_id]
            ended_at = result.decision_at if status == "decided" else (result.state.advanced_at if status == "deferred" else None)
            self.connection.execute(
                """UPDATE langchao_rounds SET status = %s, ended_at = %s, event_cursor = %s,
                       decision_candidate_id = %s, decision_candidate_revision = %s,
                       decision_at = %s, defer_reason = %s
                   WHERE scope_key = %s AND round_id = %s AND status = 'open'""",
                (status, ended_at, result.state.event_cursor, result.decision_candidate_id,
                 decision_revision, result.decision_at, result.defer_reason,
                 self.scope_key, result.state.decision_round_id),
            )
            return next_pointer

    def load_active_state(self) -> tuple[LangchaoState, int] | None:
        """Load only the active snapshot; callers resume from its advanced_at."""
        row = self.connection.execute(
            """SELECT s.payload, s.payload_sha256, a.pointer_version
               FROM langchao_active_state a JOIN langchao_state_snapshots s
                 ON s.scope_key = a.scope_key AND s.round_id = a.round_id
                AND s.state_revision = a.state_revision
               WHERE a.scope_key = %s""", (self.scope_key,),
        ).fetchone()
        if row is None:
            return None
        state = _rebuild_state(_value(row, "payload"))
        self._validate_state(state)
        _encoded, digest = _state_payload(state)
        if digest != _value(row, "payload_sha256"):
            raise LangchaoStateConflictError("active state payload hash mismatch")
        return state, int(_value(row, "pointer_version"))

    # Explicit recovery spelling for service startup code.
    recover_active_state = load_active_state


__all__ = [
    "LangchaoStateConflictError", "LangchaoStateReferenceError", "LangchaoStateRepository",
]
