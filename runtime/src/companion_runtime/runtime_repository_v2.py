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
from uuid import NAMESPACE_URL, UUID, uuid5

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
from .motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2
from .repeat_v2 import RepeatSubjectV2
from .runtime_v2 import (
    COMMITTED_DECISION_SNAPSHOT_VERSION,
    CandidateV2,
    CommittedDecisionV2,
    PredictionSetV2,
)
from .user_model_v2_features import FeatureSnapshotV2
from .user_model_v2_prediction import UserModelV2PredictionService
from .user_model_v2_repository import canonical_json
from .user_model_v2_service import PreparedExposureV2, UserModelV2Service
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
        # Once a delivery placeholder exists, keep its recovery copy at the same
        # append-only audit frontier (notably the rendered stage).  The candidate and
        # prediction witness remain immutable.
        self.connection.execute(
            """UPDATE runtime_v2_committed_decisions
               SET audit_snapshot = %s::jsonb
               WHERE decision_id = %s AND scope_key = %s AND terminal_status IS NULL""",
            (canonical_json(audit), decision_id, self.scope_key),
        )

    def save_committed_decision(self, *, committed: CommittedDecisionV2) -> None:
        """Append the immutable decision/delivery placeholder, rejecting conflicts."""

        self._scope(committed.scope_key)
        payload = _committed_candidate_payload(committed)
        cursor = self.connection.execute(
            """INSERT INTO runtime_v2_committed_decisions
               (decision_id, scope_key, snapshot_version, candidate_snapshot,
                cold_start_exploration, audit_snapshot, audit_version,
                attempt_id, render_outbox_id, committed_at)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s)
               ON CONFLICT (decision_id) DO NOTHING
               RETURNING decision_id""",
            (
                committed.decision_id,
                committed.scope_key,
                committed.snapshot_version,
                canonical_json(payload),
                committed.cold_start_exploration,
                canonical_json(committed.audit),
                str(committed.audit.get("audit_contract_version", "")),
                committed.attempt_id,
                committed.render_outbox_id,
                committed.committed_at,
            ),
        )
        if cursor.fetchone() is None:
            existing = self.recover_committed_decision(
                scope_key=committed.scope_key, decision_id=committed.decision_id
            )
            if existing != committed:
                raise ValueError("conflicting committed decision snapshot")

    def recover_committed_decision(
        self, *, scope_key: str, decision_id: str
    ) -> CommittedDecisionV2 | None:
        self._scope(scope_key)
        row = self.connection.execute(
            """SELECT decision_id, scope_key, snapshot_version, candidate_snapshot,
                      cold_start_exploration, audit_snapshot, attempt_id,
                      render_outbox_id, committed_at, terminal_ack_id, terminal_status
               FROM runtime_v2_committed_decisions
               WHERE decision_id = %s""",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        stored_scope = str(_row(row, "scope_key", 1))
        if stored_scope != scope_key:
            raise ValueError("committed decision belongs to a different scope")
        version = int(_row(row, "snapshot_version", 2))
        if version != COMMITTED_DECISION_SNAPSHOT_VERSION:
            raise ValueError("unsupported committed decision snapshot version")
        snapshot = _json(_row(row, "candidate_snapshot", 3))
        return CommittedDecisionV2(
            decision_id=str(_row(row, "decision_id", 0)),
            scope_key=stored_scope,
            snapshot_version=version,
            candidate=_candidate_from_snapshot(snapshot),
            predictions=_predictions_from_snapshot(snapshot),
            cold_start_exploration=bool(_row(row, "cold_start_exploration", 4)),
            audit=_json(_row(row, "audit_snapshot", 5)),
            attempt_id=str(_row(row, "attempt_id", 6)),
            render_outbox_id=str(_row(row, "render_outbox_id", 7)),
            committed_at=_datetime(_row(row, "committed_at", 8)),
            terminal_ack_id=_optional_text(_row(row, "terminal_ack_id", 9)),
            terminal_status=_optional_text(_row(row, "terminal_status", 10)),
        )

    def mark_committed_decision_ack_once(
        self,
        *,
        scope_key: str,
        decision_id: str,
        ack_id: str,
        status: str,
        acknowledged_at: datetime,
    ) -> bool:
        """Claim a terminal acknowledgement exactly once, including failures."""

        self._scope(scope_key)
        if status not in {"sent", "failed"}:
            raise ValueError("terminal acknowledgement status must be sent or failed")
        cursor = self.connection.execute(
            """UPDATE runtime_v2_committed_decisions
               SET terminal_ack_id = %s, terminal_status = %s, terminal_acknowledged_at = %s
               WHERE decision_id = %s AND scope_key = %s AND terminal_status IS NULL
               RETURNING decision_id""",
            (ack_id, status, acknowledged_at, decision_id, scope_key),
        )
        return cursor.fetchone() is not None

    def get_acknowledged_prepared_exposure(
        self, *, scope_key: str, idempotency_key: str
    ) -> PreparedExposureV2 | None:
        """Recover the committed ack result after duplicate delivery or process restart."""

        self._scope(scope_key)
        row = self.connection.execute(
            """SELECT 1
               FROM expectations_v2 AS x
               JOIN interaction_exposures_v2 AS e
                 ON e.scope_key = x.scope_key AND e.exposure_id = x.exposure_id
               WHERE e.scope_key = %s AND e.idempotency_key = %s""",
            (scope_key, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return self.service_repository.get_prepared_exposure(
            scope_key=scope_key, idempotency_key=idempotency_key
        )

    def prepare_exposure_and_expectation(
        self,
        *,
        user_model: UserModelV2Service,
        scope_key: str,
        exposure_id: str,
        idempotency_key: str,
        occurred_at: datetime,
        action: Mapping[str, Any],
        context_provider: Callable[[], Mapping[str, Any]],
        horizons: Mapping[Target, int],
        delivery_basis: Any,
        source_event_ids: tuple[str, ...],
        concern_id: str | None = None,
        action_goal_id: str | None = None,
        cold_start_exploration: bool = False,
    ) -> PreparedExposureV2:
        """Atomically create/recover an ack exposure and its frozen prediction."""

        self._scope(scope_key)
        transaction = getattr(self.connection, "transaction", None)
        with (transaction() if transaction is not None else nullcontext()):
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"runtime-v2-send-ack:{scope_key}:{idempotency_key}",),
            )
            existing = self.service_repository.get_prepared_exposure(
                scope_key=scope_key, idempotency_key=idempotency_key
            )
            if existing is not None:
                row = self.connection.execute(
                    "SELECT 1 FROM expectations_v2 WHERE scope_key = %s AND exposure_id = %s",
                    (scope_key, existing.exposure.exposure_id),
                ).fetchone()
                if row is not None:
                    return existing
                prepared = existing
            else:
                prepared = user_model.prepare_exposure(
                    scope_key=scope_key,
                    exposure_id=exposure_id,
                    idempotency_key=idempotency_key,
                    occurred_at=occurred_at,
                    action=action,
                    context_provider=context_provider,
                    delivery_confirmed=True,
                    horizons=horizons,
                    delivery_basis=delivery_basis,
                    source_event_ids=source_event_ids,
                )
                if prepared is None:  # delivery_confirmed is true; defensive contract guard
                    raise RuntimeError("confirmed send did not prepare an exposure")
            envelope = self.prediction_service.predict(
                features=prepared.features,
                predicted_at=prepared.exposure.occurred_at,
                based_on_state_version=int(self.state_version_provider()),
                source_event_ids=prepared.exposure.source_event_ids,
            )
            self.freeze_acknowledged_expectation(
                prepared=prepared,
                envelope=envelope,
                idempotency_key=f"{idempotency_key}:expectation",
                concern_id=concern_id,
                action_goal_id=action_goal_id,
                cold_start_exploration=cold_start_exploration,
            )
            return prepared

    def freeze_acknowledged_expectation(
        self,
        *,
        prepared: PreparedExposureV2,
        envelope: PredictionEnvelopeV2,
        idempotency_key: str,
        concern_id: str | None = None,
        action_goal_id: str | None = None,
        cold_start_exploration: bool = False,
    ) -> ExpectationV2:
        """Freeze the send-time envelope and repeat metadata exactly once.

        The exposure is already inserted by ``put_prepared_exposure`` in the caller's
        transaction.  An advisory transaction lock serializes duplicate acknowledgements;
        the exposure unique index is the durable replay guard after restart.  The winning
        JSON envelope is returned on every replay and is never recomputed from later active
        parameters.
        """

        self._scope(prepared.exposure.scope_key)
        if envelope.scope_key != self.scope_key:
            raise ValueError("prediction envelope and exposure must use the same scope")
        if envelope.predicted_at > prepared.exposure.occurred_at:
            raise ValueError("send-time prediction cannot be made after acknowledgement")
        exposure_id = prepared.exposure.exposure_id
        expectation_id = str(
            uuid5(NAMESPACE_URL, f"runtime-v2-expectation:{self.scope_key}:{exposure_id}")
        )
        expectation = ExpectationV2(
            expectation_id=expectation_id,
            exposure_id=exposure_id,
            envelope=envelope,
            scope_key=self.scope_key,
            fixed_at=prepared.exposure.occurred_at,
            window_started_at=prepared.exposure.window_started_at,
            window_ends_at=prepared.exposure.window_ends_at,
            horizon_seconds=prepared.exposure.horizon_seconds,
            created_at=prepared.exposure.created_at,
            updated_at=prepared.exposure.updated_at,
            source_event_ids=prepared.exposure.source_event_ids,
        )
        transaction = getattr(self.connection, "transaction", None)
        with (transaction() if transaction is not None else nullcontext()):
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"runtime-v2-expectation:{self.scope_key}:{exposure_id}",),
            )
            row = self.connection.execute(
                """SELECT expectation FROM expectations_v2
                   WHERE scope_key = %s AND exposure_id = %s""",
                (self.scope_key, exposure_id),
            ).fetchone()
            if row is not None:
                return _expectation(_row(row, "expectation", 0))
            self.connection.execute(
                """INSERT INTO expectations_v2
                   (expectation_id, scope_key, idempotency_key, prediction_snapshot_id,
                    exposure_id, due_at, expectation, expected_value, tolerance,
                    expectation_version)
                   VALUES (%s, %s, %s, NULL, %s, %s, %s::jsonb, %s, 0.0, 1)""",
                (
                    UUID(expectation_id), self.scope_key, idempotency_key, exposure_id,
                    expectation.window_ends_at, canonical_json(expectation.to_dict()),
                    0.0,
                ),
            )
            self.record_acknowledged_exposure(
                exposure_id=exposure_id,
                acknowledged_at=prepared.exposure.occurred_at,
                concern_id=concern_id,
                action_goal_id=action_goal_id,
                cold_start_exploration=cold_start_exploration,
            )
        return expectation

    def record_acknowledged_exposure(
        self,
        *,
        exposure_id: str,
        acknowledged_at: datetime,
        concern_id: str | None = None,
        action_goal_id: str | None = None,
        cold_start_exploration: bool = False,
    ) -> None:
        """Attach repeat-policy identity to an already prepared v2 exposure."""

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

    def last_cold_start_exploration_at(self, *, scope_key: str) -> datetime | None:
        """Return when this scope last spent exploration budget, if ever."""

        self._scope(scope_key)
        row = self.connection.execute(
            """SELECT max(acknowledged_at)
               FROM runtime_v2_exposure_metadata
               WHERE scope_key = %s AND cold_start_exploration""",
            (scope_key,),
        ).fetchone()
        if row is None:
            return None
        value = _row(row, "max", 0)
        return None if value is None else _datetime(value)

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
                   SET status = 'settled', resolved_at = %s,
                       resolution = jsonb_build_object(
                           'maintenance_v2', true,
                           'semantics', 'target outcomes stored separately; status is not goal satisfaction')
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


def _committed_candidate_payload(committed: CommittedDecisionV2) -> dict[str, Any]:
    candidate = committed.candidate
    return {
        "candidate": {
            "candidate_id": candidate.candidate_id,
            "action": dict(candidate.action),
            "internal_utility": candidate.internal_utility,
            "coefficients": {
                "v_reply": candidate.coefficients.v_reply,
                "v_continue": candidate.coefficients.v_continue,
                "c_negative": candidate.coefficients.c_negative,
            },
            "policy": {
                "low_pressure": candidate.policy.low_pressure,
                "low_frequency": candidate.policy.low_frequency,
                "easy_to_ignore": candidate.policy.easy_to_ignore,
                "continuous_follow_up": candidate.policy.continuous_follow_up,
                "sensitive": candidate.policy.sensitive,
            },
            "repeat_subject": {
                "concern_id": candidate.repeat_subject.concern_id,
                "action_goal_id": candidate.repeat_subject.action_goal_id,
            },
            "source_event_ids": list(candidate.source_event_ids),
        },
        "prediction_witness": {
            "snapshot_id": committed.predictions.snapshot_id,
            "parameter_version": committed.predictions.parameter_version,
            "reply": committed.predictions.reply.to_dict(),
            "continuation": committed.predictions.continuation.to_dict(),
            "negative": committed.predictions.negative.to_dict(),
        },
    }


def _candidate_from_snapshot(payload: Mapping[str, Any]) -> CandidateV2:
    item = _json(payload.get("candidate"))
    coefficients = _json(item.get("coefficients"))
    policy = _json(item.get("policy"))
    repeat = _json(item.get("repeat_subject"))
    action = item.get("action")
    if not isinstance(action, Mapping):
        raise ValueError("committed candidate action must be a JSON object")
    return CandidateV2(
        candidate_id=str(item["candidate_id"]),
        action=dict(action),
        internal_utility=float(item["internal_utility"]),
        coefficients=UserUtilityCoefficientsV2(
            v_reply=coefficients["v_reply"],
            v_continue=coefficients["v_continue"],
            c_negative=coefficients["c_negative"],
        ),
        policy=CandidatePolicyV2(**{name: bool(policy[name]) for name in (
            "low_pressure", "low_frequency", "easy_to_ignore",
            "continuous_follow_up", "sensitive"
        )}),
        repeat_subject=RepeatSubjectV2(
            concern_id=_optional_text(repeat.get("concern_id")),
            action_goal_id=_optional_text(repeat.get("action_goal_id")),
        ),
        source_event_ids=tuple(str(value) for value in item.get("source_event_ids", ())),
    )


def _predictions_from_snapshot(payload: Mapping[str, Any]) -> PredictionSetV2:
    witness = _json(payload.get("prediction_witness"))
    return PredictionSetV2(
        snapshot_id=str(witness["snapshot_id"]),
        parameter_version=str(witness["parameter_version"]),
        reply=_prediction(witness["reply"]),
        continuation=_prediction(witness["continuation"]),
        negative=_prediction(witness["negative"]),
    )


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
        label_revision=int(item["label_revision"]),
        expected_point=(None if item.get("expected_point") is None else float(item["expected_point"])),
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
