"""Concrete PostgreSQL repository for the Runtime-v2 coordinator.

Predictions are produced by the v2 prediction service from a freshly encoded
candidate/context snapshot.  Repeat history, matter resets, settleable prepared
exposures and decision audits are read/written only through v2 PostgreSQL tables.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from .emotion_v2_interface import (
    EmotionDomainEventV2,
    EmotionEvidenceOriginV2,
    EmotionInputV2,
)
from .expectations_v2 import (
    ExpectationSettlementV2,
    OutcomeCompletenessV2,
    SettlementDispositionV2,
)
from .maintenance_v2 import ActiveParameterPointerV2, DueExpectationV2
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
from .user_model_v2_types import (
    ExpectationV2,
    PredictionEnvelopeV2,
    LabelStatus,
    SupportStatus,
    Target,
    TargetLabelV2,
    TargetPredictionV2,
)

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
        cold_start_exploration: bool = False,
    ) -> None:
        """Attach repeat-policy identity to an already prepared v2 exposure.

        ``cold_start_exploration`` marks that this delivery was authorised by the
        bounded cold-start exploration allowance rather than by a positive estimate,
        so it can be counted against that allowance later.
        """

        self.connection.execute(
            """INSERT INTO runtime_v2_exposure_metadata
               (scope_key, exposure_id, acknowledged_at, concern_id, action_goal_id,
                cold_start_exploration)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (scope_key, exposure_id) DO NOTHING""",
            (
                self.scope_key,
                exposure_id,
                acknowledged_at,
                _optional_text(concern_id),
                _optional_text(action_goal_id),
                bool(cold_start_exploration),
            ),
        )

    def count_cold_start_explorations(self, *, scope_key: str, since: datetime) -> int:
        """Count acknowledged proactive deliveries that spent exploration budget."""

        self._scope(scope_key)
        row = self.connection.execute(
            """SELECT count(*)
               FROM runtime_v2_exposure_metadata
               WHERE scope_key = %s
                 AND acknowledged_at >= %s
                 AND cold_start_exploration""",
            (scope_key, since),
        ).fetchone()
        if row is None:
            return 0
        value = _row(row, "count", 0)
        return int(value or 0)

    def list_due_pending_exposures(
        self, *, scope_key: str, as_of: datetime, limit: int
    ) -> Sequence[PreparedExposureV2]:
        """Return distinct prepared exposures having an expired active pending label."""

        self._scope(scope_key)
        rows = self.connection.execute(
            """SELECT DISTINCT e.idempotency_key, e.occurred_at, e.exposure_id
               FROM user_model_active_labels_v2 AS a
               JOIN interaction_target_labels_v2 AS l
                 ON l.scope_key = a.scope_key
                AND l.target_label_id = a.target_label_id
               JOIN interaction_exposures_v2 AS e
                 ON e.scope_key = a.scope_key AND e.exposure_id = a.exposure_id
               WHERE a.scope_key = %s
                 AND l.target_value->>'status' = 'pending'
                 AND (l.target_value->>'window_ends_at')::timestamptz <= %s
               ORDER BY e.occurred_at, e.exposure_id
               LIMIT %s""",
            (scope_key, as_of, int(limit)),
        ).fetchall()
        results: list[PreparedExposureV2] = []
        for row in rows:
            prepared = self.service_repository.get_prepared_exposure(
                scope_key=scope_key,
                idempotency_key=str(_row(row, "idempotency_key", 0)),
            )
            if prepared is not None:
                results.append(prepared)
        return tuple(results)

    def list_due_expectations(
        self, *, scope_key: str, as_of: datetime, limit: int
    ) -> Sequence[DueExpectationV2]:
        """Read expectations whose active target label is no longer pending."""

        self._scope(scope_key)
        rows = self.connection.execute(
            """SELECT e.expectation, l.target_value, a.pointer_version,
                      previous.settlement
               FROM expectations_v2 AS e
               JOIN user_model_active_labels_v2 AS a
                 ON a.scope_key = e.scope_key
                AND a.exposure_id = (e.expectation->>'exposure_id')::uuid
               JOIN interaction_target_labels_v2 AS l
                 ON l.scope_key = a.scope_key
                AND l.target_label_id = a.target_label_id
               LEFT JOIN LATERAL (
                   SELECT s.settlement
                   FROM runtime_v2_expectation_settlements AS s
                   WHERE s.scope_key = e.scope_key
                     AND s.expectation_id = e.expectation_id
                     AND s.target_name = a.target_name
                   ORDER BY s.label_revision DESC LIMIT 1
               ) AS previous ON TRUE
               WHERE e.scope_key = %s
                 AND e.status = 'pending'
                 AND e.due_at <= %s
                 AND l.target_value->>'status' <> 'pending'
               ORDER BY e.due_at, e.expectation_id, a.target_name
               LIMIT %s""",
            (scope_key, as_of, int(limit)),
        ).fetchall()
        return tuple(
            DueExpectationV2(
                expectation=_expectation(_row(row, "expectation", 0)),
                label=self.service_repository.get_active_label(
                    scope_key=scope_key,
                    exposure_id=str(_json(_row(row, "expectation", 0))["exposure_id"]),
                    target=Target(str(_json(_row(row, "target_value", 1))["target"])),
                )[0],
                label_revision=int(_row(row, "pointer_version", 2)),
                previous=(
                    None
                    if _row(row, "settlement", 3) is None
                    else _settlement(_row(row, "settlement", 3))
                ),
            )
            for row in rows
        )

    def write_expectation_revision_once(
        self,
        *,
        scope_key: str,
        settlement: ExpectationSettlementV2,
        emotion_shadow: EmotionInputV2,
        settled_at: datetime,
    ) -> bool:
        """Append one immutable revision; the revision key is the exactly-once guard."""

        self._scope(scope_key)
        cursor = self.connection.execute(
            """INSERT INTO runtime_v2_expectation_settlements
               (scope_key, revision_key, settlement_key, expectation_id, exposure_id,
                target_name, label_revision, settlement, emotion_shadow, settled_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
               ON CONFLICT (scope_key, revision_key) DO NOTHING
               RETURNING revision_key""",
            (
                scope_key,
                settlement.revision_key,
                settlement.settlement_key,
                settlement.expectation_id,
                settlement.exposure_id,
                settlement.target.value,
                settlement.label_revision,
                canonical_json(_settlement_payload(settlement)),
                canonical_json(_emotion_payload(emotion_shadow)),
                settled_at,
            ),
        )
        inserted = cursor.fetchone() is not None
        if inserted:
            # A target-independent expectation may have four revisions.  It becomes
            # resolved only once no active target label remains pending.
            self.connection.execute(
                """UPDATE expectations_v2 AS e
                   SET status = 'met', resolved_at = %s,
                       resolution = jsonb_build_object('maintenance_v2', true)
                   WHERE e.scope_key = %s AND e.expectation_id = %s
                     AND NOT EXISTS (
                       SELECT 1 FROM user_model_active_labels_v2 AS a
                       JOIN interaction_target_labels_v2 AS l
                         ON l.scope_key = a.scope_key AND l.target_label_id = a.target_label_id
                       WHERE a.scope_key = e.scope_key
                         AND a.exposure_id = (e.expectation->>'exposure_id')::uuid
                         AND l.target_value->>'status' = 'pending')""",
                (settled_at, scope_key, settlement.expectation_id),
            )
        return inserted

    def get_active_parameter_pointer(self, *, scope_key: str) -> ActiveParameterPointerV2:
        self._scope(scope_key)
        row = self.connection.execute(
            """SELECT parameter_snapshot_id, activation_version
               FROM user_model_active_parameters_v2
               WHERE scope_key = %s AND deactivated_at IS NULL""",
            (scope_key,),
        ).fetchone()
        if row is None:
            return ActiveParameterPointerV2(snapshot_id=None, activation_version=0)
        return ActiveParameterPointerV2(
            snapshot_id=str(_row(row, "parameter_snapshot_id", 0)),
            activation_version=int(_row(row, "activation_version", 1)),
        )

    def save_parameter_snapshot_and_activate(
        self,
        *,
        scope_key: str,
        snapshot_id: str,
        expected_snapshot_id: str | None,
        parameter_version: int,
        activation_version: int,
        effective_at: datetime,
        parameters: Mapping[str, object],
        provenance: Mapping[str, object],
        idempotency_key: str,
    ) -> bool:
        """Persist a fitted snapshot and CAS active within one DB transaction."""

        self._scope(scope_key)
        transaction = getattr(self.connection, "transaction", None)
        with (transaction() if transaction is not None else nullcontext()):
            saved = self.service_repository.repository.insert_parameter_snapshot(
                scope_key=scope_key,
                parameter_snapshot_id=snapshot_id,
                effective_at=effective_at,
                parameters=parameters,
                provenance=provenance,
                parameter_version=parameter_version,
                parent_snapshot_id=expected_snapshot_id,
                idempotency_key=idempotency_key,
            )
            return self.service_repository.repository.compare_and_swap_active_parameters(
                scope_key=scope_key,
                expected_snapshot_id=expected_snapshot_id,
                new_snapshot_id=str(saved),
                activation_version=activation_version,
                activation_context={"kind": "maintenance_v2", "effective_at": effective_at.isoformat()},
                idempotency_key=f"{idempotency_key}:active",
            )

    def read_blackbox_evidence(self, *, scope_key: str) -> Mapping[str, Any]:
        """Return normalized, scope-bound evidence for the public black-box API."""
        self._scope(scope_key)
        def maps(rows: Sequence[Any]) -> list[Any]:
            return [dict(row) if isinstance(row, Mapping) else row for row in rows]
        events = self.connection.execute(
            "SELECT event_id, event_type AS kind, conversation_id AS scope_key, timestamp "
            "FROM raw_events WHERE conversation_id = %s ORDER BY timestamp", (scope_key,)
        ).fetchall()
        exposures = self.connection.execute(
            "SELECT exposure_id, scope_key, occurred_at, action, context, exposure_payload "
            "FROM interaction_exposures_v2 WHERE scope_key = %s ORDER BY occurred_at",
            (scope_key,),
        ).fetchall()
        labels = self.connection.execute(
            "SELECT l.target_label_id, l.scope_key, l.exposure_id, l.target_name, "
            "l.target_value, l.label_version FROM interaction_target_labels_v2 l "
            "JOIN user_model_active_labels_v2 a ON a.scope_key=l.scope_key "
            "AND a.target_label_id=l.target_label_id WHERE l.scope_key=%s ORDER BY l.labelled_at",
            (scope_key,),
        ).fetchall()
        parameters = self.connection.execute(
            "SELECT s.parameter_snapshot_id,s.scope_key,s.parameter_version,s.parameters "
            "FROM user_model_parameter_snapshots_v2 s JOIN user_model_active_parameters_v2 a "
            "ON a.scope_key=s.scope_key AND a.parameter_snapshot_id=s.parameter_snapshot_id "
            "WHERE s.scope_key=%s AND a.deactivated_at IS NULL", (scope_key,)
        ).fetchall()
        return {"events": maps(events), "exposures": maps(exposures), "labels": maps(labels),
                "parameter_snapshots": maps(parameters)}

    def list_decision_audits(self, *, scope_key: str) -> Sequence[Mapping[str, Any]]:
        self._scope(scope_key)
        rows = self.connection.execute(
            "SELECT decision_id,scope_key,audit,updated_at FROM runtime_v2_decision_audits "
            "WHERE scope_key=%s ORDER BY updated_at", (scope_key,)
        ).fetchall()
        return tuple(dict(row) if isinstance(row, Mapping) else row for row in rows)

    def _scope(self, scope_key: str) -> None:
        if scope_key != self.scope_key:
            raise ValueError("runtime repository cannot cross its configured scope")


def _json(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("stored runtime-v2 payload must be a JSON object")
    return value


def _prediction(payload: Any) -> TargetPredictionV2:
    item = _json(payload)
    return TargetPredictionV2(
        prediction_id=str(item["prediction_id"]),
        scope_key=str(item["scope_key"]),
        target=Target(str(item["target"])),
        point=item.get("point"), lower=item.get("lower"), upper=item.get("upper"),
        interval_level=item.get("interval_level"), interval_kind=item.get("interval_kind"),
        support=SupportStatus(str(item["support"])),
        predicted_at=_datetime(item["predicted_at"]),
        created_at=_datetime(item["created_at"]), updated_at=_datetime(item["updated_at"]),
        source_event_ids=tuple(str(value) for value in item.get("source_event_ids", ())),
        contract_version=str(item["contract_version"]),
        feature_version=str(item["feature_version"]),
        target_contract_version=str(item["target_contract_version"]),
    )


def _expectation(payload: Any) -> ExpectationV2:
    item = _json(payload)
    envelope_payload = _json(item["envelope"])
    parameter_ids = _json(envelope_payload.get("parameter_snapshot_ids", {}))
    envelope = PredictionEnvelopeV2(
        envelope_id=str(envelope_payload["envelope_id"]), scope_key=str(envelope_payload["scope_key"]),
        predictions=tuple(_prediction(value) for value in envelope_payload["predictions"]),
        predicted_at=_datetime(envelope_payload["predicted_at"]),
        based_on_state_version=int(envelope_payload["based_on_state_version"]),
        created_at=_datetime(envelope_payload["created_at"]), updated_at=_datetime(envelope_payload["updated_at"]),
        parameter_snapshot_ids=tuple((target, parameter_ids.get(target.value)) for target in Target),
        source_event_ids=tuple(str(value) for value in envelope_payload.get("source_event_ids", ())),
        contract_version=str(envelope_payload["contract_version"]),
        feature_version=str(envelope_payload["feature_version"]),
        target_contract_version=str(envelope_payload["target_contract_version"]),
    )
    return ExpectationV2(
        expectation_id=str(item["expectation_id"]), exposure_id=str(item["exposure_id"]),
        envelope=envelope, scope_key=str(item["scope_key"]), fixed_at=_datetime(item["fixed_at"]),
        window_started_at=_datetime(item["window_started_at"]), window_ends_at=_datetime(item["window_ends_at"]),
        horizon_seconds=int(item["horizon_seconds"]), created_at=_datetime(item["created_at"]),
        updated_at=_datetime(item["updated_at"]),
        source_event_ids=tuple(str(value) for value in item.get("source_event_ids", ())),
        contract_version=str(item["contract_version"]), feature_version=str(item["feature_version"]),
        target_contract_version=str(item["target_contract_version"]),
    )


def _settlement(payload: Any) -> ExpectationSettlementV2:
    item = _json(payload)
    return ExpectationSettlementV2(
        settlement_key=str(item["settlement_key"]), revision_key=str(item["revision_key"]),
        expectation_id=str(item["expectation_id"]), exposure_id=str(item["exposure_id"]),
        target=Target(str(item["target"])), label_id=str(item["label_id"]),
        label_revision=int(item["label_revision"]), expected_point=float(item["expected_point"]),
        actual_outcome=item.get("actual_outcome"), completeness=OutcomeCompletenessV2(str(item["completeness"])),
        residual=item.get("residual"), support=SupportStatus(str(item["support"])),
        source_event_ids=tuple(str(value) for value in item.get("source_event_ids", ())),
        disposition=SettlementDispositionV2(str(item.get("disposition", "initial"))),
        supersedes_revision_key=item.get("supersedes_revision_key"),
        intervention_source_event_ids=tuple(str(value) for value in item.get("intervention_source_event_ids", ())),
    )


def _settlement_payload(item: ExpectationSettlementV2) -> dict[str, Any]:
    return {
        "settlement_key": item.settlement_key, "revision_key": item.revision_key,
        "expectation_id": item.expectation_id, "exposure_id": item.exposure_id,
        "target": item.target.value, "label_id": item.label_id,
        "label_revision": item.label_revision, "expected_point": item.expected_point,
        "actual_outcome": item.actual_outcome, "completeness": item.completeness.value,
        "residual": item.residual, "support": item.support.value,
        "source_event_ids": list(item.source_event_ids), "disposition": item.disposition.value,
        "supersedes_revision_key": item.supersedes_revision_key,
        "intervention_source_event_ids": list(item.intervention_source_event_ids),
    }


def _emotion_payload(item: EmotionInputV2) -> dict[str, Any]:
    return {
        "event_key": item.event_key, "domain_event": item.domain_event.value,
        "prior_expectation": item.prior_expectation, "actual_outcome": item.actual_outcome,
        "completeness": item.completeness.value, "source_event_ids": list(item.source_event_ids),
        "support": item.support.value, "origin": item.origin.value, "shadow": item.shadow,
        "supersedes_event_key": item.supersedes_event_key,
    }


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
