"""Concrete PostgreSQL repository for the Runtime-v2 coordinator.

Predictions are produced by the v2 prediction service from a freshly encoded
candidate/context snapshot.  Repeat history, matter resets, settleable prepared
exposures and decision audits are read/written only through v2 PostgreSQL tables.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from .repeat_v2 import (
    SendAcknowledgedExposureV2,
    UserMatterEventKind,
    UserMatterEventV2,
)
from .runtime_v2 import CandidateV2, PredictionSetV2
from .user_model_v2_features import FeatureSnapshotV2
from .user_model_v2_prediction import UserModelV2PredictionService
from .user_model_v2_repository import canonical_json
from .user_model_v2_service import PreparedExposureV2
from .user_model_v2_service_repository import PostgresUserModelV2ServiceRepository
from .user_model_v2_types import Target

ContextProvider = Callable[[CandidateV2, datetime], Mapping[str, Any]]
StateVersionProvider = Callable[[], int]


class PostgresV2RuntimeRepository:
    """Implement ``V2RuntimeRepository`` over one schema-bound PG connection."""

    def __init__(
        self,
        connection: Any,
        *,
        prediction_service: UserModelV2PredictionService,
        service_repository: PostgresUserModelV2ServiceRepository,
        scope_key: str,
        context_provider: ContextProvider | None = None,
        state_version_provider: StateVersionProvider | None = None,
    ) -> None:
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key is required")
        self.connection = connection
        self.prediction_service = prediction_service
        self.service_repository = service_repository
        self.scope_key = scope_key
        self.context_provider = context_provider or (lambda _candidate, _now: {})
        self.state_version_provider = state_version_provider or (lambda: 0)

    def prediction_for(
        self, *, scope_key: str, candidate: CandidateV2, now: datetime
    ) -> PredictionSetV2:
        self._scope(scope_key)
        feature_id = str(
            uuid5(
                NAMESPACE_URL,
                f"runtime-v2-prediction-feature:{scope_key}:{candidate.candidate_id}:{now.isoformat()}",
            )
        )
        features = FeatureSnapshotV2(
            scope_key=scope_key,
            exposure_id=feature_id,
            action_json=candidate.action,
            context_json=self.context_provider(candidate, now),
            context_cutoff_at=now,
            created_at=now,
        )
        envelope = self.prediction_service.predict(
            features=features,
            predicted_at=now,
            based_on_state_version=int(self.state_version_provider()),
            source_event_ids=candidate.source_event_ids,
        )
        by_target = {item.target: item for item in envelope.predictions}
        parameter_ids = tuple(
            identifier
            for _target, identifier in envelope.parameter_snapshot_ids
            if identifier is not None
        )
        return PredictionSetV2(
            snapshot_id=envelope.envelope_id,
            parameter_version=("+".join(parameter_ids) if parameter_ids else "prior-or-unavailable"),
            reply=by_target[Target.REPLY],
            continuation=by_target[Target.CONTINUE],
            negative=by_target[Target.NEGATIVE],
        )

    def acknowledged_exposures(
        self, *, scope_key: str, now: datetime
    ) -> Sequence[SendAcknowledgedExposureV2]:
        self._scope(scope_key)
        rows = self.connection.execute(
            """SELECT exposure_id, acknowledged_at, concern_id, action_goal_id
               FROM runtime_v2_exposure_metadata
               WHERE scope_key = %s AND acknowledged_at <= %s
               ORDER BY acknowledged_at, exposure_id""",
            (scope_key, now),
        ).fetchall()
        return tuple(
            SendAcknowledgedExposureV2(
                exposure_id=str(_row(row, "exposure_id", 0)),
                acknowledged_at_utc=_datetime(_row(row, "acknowledged_at", 1)),
                concern_id=_optional_text(_row(row, "concern_id", 2)),
                action_goal_id=_optional_text(_row(row, "action_goal_id", 3)),
            )
            for row in rows
        )

    def user_matter_events(
        self, *, scope_key: str, now: datetime
    ) -> Sequence[UserMatterEventV2]:
        self._scope(scope_key)
        rows = self.connection.execute(
            """SELECT event_id, occurred_at, kind, concern_id, action_goal_id
               FROM runtime_v2_user_matter_events
               WHERE scope_key = %s AND occurred_at <= %s
               ORDER BY occurred_at, event_id""",
            (scope_key, now),
        ).fetchall()
        return tuple(
            UserMatterEventV2(
                event_id=str(_row(row, "event_id", 0)),
                occurred_at_utc=_datetime(_row(row, "occurred_at", 1)),
                kind=UserMatterEventKind(str(_row(row, "kind", 2))),
                concern_id=_optional_text(_row(row, "concern_id", 3)),
                action_goal_id=_optional_text(_row(row, "action_goal_id", 4)),
            )
            for row in rows
        )

    def settleable_exposures(
        self,
        *,
        scope_key: str,
        observations: Sequence[Any],
        as_of: datetime,
    ) -> Sequence[PreparedExposureV2]:
        self._scope(scope_key)
        exposure_ids = tuple(
            dict.fromkeys(
                exposure_id
                for observation in observations
                for exposure_id in observation.candidate_exposure_ids
            )
        )
        if not exposure_ids:
            return ()
        rows = self.connection.execute(
            """SELECT idempotency_key
               FROM interaction_exposures_v2
               WHERE scope_key = %s AND exposure_id = ANY(%s)
                 AND occurred_at <= %s
               ORDER BY occurred_at, exposure_id""",
            (scope_key, list(exposure_ids), as_of),
        ).fetchall()
        prepared: list[PreparedExposureV2] = []
        for row in rows:
            item = self.service_repository.get_prepared_exposure(
                scope_key=scope_key,
                idempotency_key=str(_row(row, "idempotency_key", 0)),
            )
            if item is not None:
                prepared.append(item)
        return tuple(prepared)

    def append_user_matter_events(
        self, *, scope_key: str, events: Sequence[UserMatterEventV2]
    ) -> None:
        self._scope(scope_key)
        for event in events:
            self.connection.execute(
                """INSERT INTO runtime_v2_user_matter_events
                   (scope_key, event_id, occurred_at, kind, concern_id, action_goal_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (scope_key, event_id) DO NOTHING""",
                (
                    scope_key,
                    event.event_id,
                    event.occurred_at_utc,
                    event.kind.value,
                    event.concern_id,
                    event.action_goal_id,
                ),
            )

    def save_decision_audit(self, *, decision_id: str, audit: Mapping[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO runtime_v2_decision_audits
               (decision_id, scope_key, audit, updated_at)
               VALUES (%s, %s, %s::jsonb, CURRENT_TIMESTAMP)
               ON CONFLICT (decision_id) DO UPDATE
               SET audit = EXCLUDED.audit, updated_at = CURRENT_TIMESTAMP
               WHERE runtime_v2_decision_audits.scope_key = EXCLUDED.scope_key""",
            (decision_id, self.scope_key, canonical_json(audit)),
        )

    def record_acknowledged_exposure(
        self,
        *,
        exposure_id: str,
        acknowledged_at: datetime,
        concern_id: str | None = None,
        action_goal_id: str | None = None,
    ) -> None:
        """Attach repeat-policy identity to an already prepared v2 exposure."""

        self.connection.execute(
            """INSERT INTO runtime_v2_exposure_metadata
               (scope_key, exposure_id, acknowledged_at, concern_id, action_goal_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (scope_key, exposure_id) DO NOTHING""",
            (
                self.scope_key,
                exposure_id,
                acknowledged_at,
                _optional_text(concern_id),
                _optional_text(action_goal_id),
            ),
        )

    def _scope(self, scope_key: str) -> None:
        if scope_key != self.scope_key:
            raise ValueError("runtime repository cannot cross its configured scope")


def _row(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def _datetime(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stored runtime-v2 timestamp must be timezone-aware")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


V2RuntimeRepositoryAdapter = PostgresV2RuntimeRepository

__all__ = ["PostgresV2RuntimeRepository", "V2RuntimeRepositoryAdapter"]
