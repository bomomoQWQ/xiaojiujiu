"""Bridge settled user-model labels into the scoped Langchao outcome ledger.

Expected token amounts are prices.  Actual/correction tokens record observations for
future conditioning; callers must not add their realized amount on top of a fresh expected
forecast for the same outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .langchao_types import OutcomeStatus, OutcomeToken, SettlementType
from .user_model_v2_types import LabelStatus, Target, TargetLabelV2

_USER_KEYS = {Target.REPLY: "reply", Target.CONTINUE: "continuation", Target.NEGATIVE: "negative"}
_SETTLED = {
    LabelStatus.OBSERVED_POSITIVE: OutcomeStatus.CONFIRMED,
    LabelStatus.OBSERVED_NEGATIVE: OutcomeStatus.NOT_OBSERVED,
    LabelStatus.CENSORED: OutcomeStatus.CENSORED,
    LabelStatus.UNATTRIBUTABLE: OutcomeStatus.UNATTRIBUTABLE,
    LabelStatus.UNKNOWN: OutcomeStatus.CENSORED,
    LabelStatus.INVALIDATED: OutcomeStatus.CENSORED,
}


def _value(row: Any, name: str, index: int = 0) -> Any:
    return row[name] if isinstance(row, Mapping) else row[index]


@dataclass(slots=True)
class LangchaoUserOutcomeSettler:
    """Persist one Langchao result per active v2 label revision, exactly once."""

    live_repository: Any

    def settle_labels(self, labels: tuple[TargetLabelV2, ...]) -> tuple[OutcomeToken, ...]:
        written: list[OutcomeToken] = []
        for label in labels:
            if label.target not in _USER_KEYS or label.status is LabelStatus.PENDING:
                continue
            commit = self.live_repository.get_by_attempt(label.exposure_id)
            if commit is None or commit.scope_key != label.scope_key:
                continue
            expected = next(
                (token for token in commit.expected_tokens if token.outcome_key == _USER_KEYS[label.target]),
                None,
            )
            if expected is None:
                continue
            revision = self.live_repository.label_revision(
                exposure_id=label.exposure_id, target_name=label.target.value
            )
            if revision is None:
                continue
            token = self._token(expected, label=label, label_revision=revision)
            row = self.live_repository.outcomes.get_active_observation(
                reward_contract_id=commit.reward_contract_id,
                episode_id=expected.episode_id,
                outcome_key=expected.outcome_key,
            )
            corrects_token_id: str | None = None
            corrects_revision: int | None = None
            pointer_version = 0
            if row is not None:
                prior_label_revision = int(_value(row, "source_label_revision", -1))
                if prior_label_revision >= revision:
                    continue
                corrects_token_id = str(_value(row, "token_id"))
                corrects_revision = int(_value(row, "revision", 1))
                pointer_version = int(_value(row, "pointer_version", -2))
                token = replace(
                    token,
                    settlement_type=SettlementType.CORRECTION,
                    status=OutcomeStatus.CORRECTED,
                    corrects_token_id=corrects_token_id,
                )
            put_revision = getattr(
                self.live_repository.outcomes,
                "put_outcome_revision_in_transaction",
                self.live_repository.outcomes.put_outcome_revision,
            )
            put_revision(
                token,
                revision=1,
                reward_contract_id=commit.reward_contract_id,
                reward_contract_revision=commit.reward_revision,
                corrects_revision=corrects_revision,
            )
            if not self.live_repository.outcomes.activate_observation(
                reward_contract_id=commit.reward_contract_id,
                episode_id=expected.episode_id,
                outcome_key=expected.outcome_key,
                token_id=token.token_id,
                revision=1,
                source_exposure_id=label.exposure_id,
                source_label_revision=revision,
                expected_pointer_version=pointer_version,
            ):
                winner = self.live_repository.outcomes.get_active_observation(
                    reward_contract_id=commit.reward_contract_id,
                    episode_id=expected.episode_id,
                    outcome_key=expected.outcome_key,
                )
                if winner is None or int(_value(winner, "source_label_revision", -1)) < revision:
                    raise RuntimeError("Langchao user-outcome CAS lost to an older observation")
                continue
            written.append(token)
        return tuple(written)

    @staticmethod
    def _token(expected: OutcomeToken, *, label: TargetLabelV2, label_revision: int) -> OutcomeToken:
        status = _SETTLED[label.status]
        identity = f"{label.scope_key}:{label.exposure_id}:{label.target.value}:{label_revision}"
        return replace(
            expected,
            token_id=str(uuid5(NAMESPACE_URL, f"langchao-user-outcome:{identity}")),
            settlement_type=SettlementType.ACTUAL,
            status=status,
            base_amount=(expected.base_amount if label.value is True else 0.0),
            evidence_version="langchao.user-label.v1",
            idempotency_key=f"langchao-user-outcome:{identity}",
            evidence_refs=tuple(dict.fromkeys((*expected.evidence_refs, label.label_id, *label.source_event_ids))),
            observation_started_at=label.window_started_at,
            observation_ends_at=label.window_ends_at,
            corrects_token_id=None,
        )


__all__ = ["LangchaoUserOutcomeSettler"]
