"""Application of candidate-pool operations: the pool manager's reducer half.

The strong semantic API may only propose ``ADD / UPDATE / RETIRE / REINTERPRET``.
This module validates and applies those proposals, and it is the *only* place
those mutations happen - both the rule-based generator and the semantic API path
funnel through :func:`apply_operations`.

Keeping it separate from :mod:`companion_runtime.runtime` avoids a circular import
while preserving the rule that the pool manager, not the model, owns the pool.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from . import candidate as candidate_module
from .config import RuntimeConfig
from .projections import Projections
from .typing import CandidateStatus, RuntimeState
from .utility import utcnow

LOGGER = logging.getLogger("companion_runtime.pool")


@dataclass(slots=True)
class PoolContext:
    """Everything the pool manager needs to apply an operation."""

    projections: Projections
    config: RuntimeConfig


def build_candidate_from_mapping(
    data: Mapping[str, object], *, now: datetime, config: RuntimeConfig
) -> candidate_module.CandidateIntent:
    """Build a candidate entity from a model-provided mapping.

    Kept public (rather than private to :mod:`candidate`) because the pool manager
    is the documented entry point for external proposals.
    """
    return candidate_module._candidate_from_mapping(data, now=now, config=config)


def apply_one(
    context: PoolContext,
    conn: sqlite3.Connection,
    operation: candidate_module.CandidateOperation,
    *,
    now: datetime | None = None,
    state: RuntimeState | None = None,
) -> candidate_module.PoolChange | None:
    """Validate and apply a single pool operation.

    Args:
        context: Projections and configuration.
        conn: Write connection inside the reducer transaction.
        operation: Operation to apply.
        now: Reference time.
        state: Current runtime state (used for interpretation versioning).

    Returns:
        The applied :class:`~companion_runtime.candidate.PoolChange`, or ``None``.

    Raises:
        ValueError: If the operation is malformed or the candidate is invalid.
        KeyError: If the referenced candidate does not exist.
    """
    stamp = now or utcnow()
    projections = context.projections
    op = operation.op

    if op == "add":
        if not operation.candidate:
            raise ValueError("add requires a candidate payload")
        candidate = build_candidate_from_mapping(
            operation.candidate, now=stamp, config=context.config
        )
        reason = candidate_module.validate_candidate(candidate)
        if reason:
            raise ValueError(reason)
        projections.candidates.upsert(conn, candidate)
        return candidate_module.PoolChange(
            op=op, candidate_id=candidate.candidate_id, detail=candidate.intent
        )

    if op == "update":
        if not operation.candidate_id:
            raise ValueError("update requires candidate_id")
        candidate = projections.candidates.get(operation.candidate_id)
        if candidate is None:
            raise KeyError(f"unknown candidate: {operation.candidate_id}")
        allowed = {
            "type",
            "intent",
            "goal",
            "target",
            "sources",
            "constraints",
            "preconditions",
            "invalidate_when",
            "confidence",
            "status",
            "internal_need",
            "unfinished_relevance",
            "emotion_relevance",
            "expires_at",
        }
        for key, value in (operation.patch or {}).items():
            if key not in allowed:
                LOGGER.warning("Ignoring non-writable candidate field %s", key)
                continue
            setattr(candidate, key, value)
        candidate.updated_at = stamp
        projections.candidates.upsert(conn, candidate)
        return candidate_module.PoolChange(op=op, candidate_id=candidate.candidate_id)

    if op == "retire":
        if not operation.candidate_id:
            raise ValueError("retire requires candidate_id")
        if projections.candidates.get(operation.candidate_id) is None:
            # Retiring a candidate that does not exist is a protocol error, not a
            # silent success: it usually means a stale identifier was passed in.
            raise KeyError(f"unknown candidate: {operation.candidate_id}")
        projections.candidates.set_status(
            conn,
            operation.candidate_id,
            CandidateStatus.RETIRED.value,
            reason=operation.reason or "retired",
        )
        return candidate_module.PoolChange(
            op=op, candidate_id=operation.candidate_id, detail=operation.reason or "retired"
        )

    if op == "reinterpret":
        if not operation.candidate_id:
            raise ValueError("reinterpret requires candidate_id")
        candidate = projections.candidates.get(operation.candidate_id)
        if candidate is None:
            raise KeyError(f"unknown candidate: {operation.candidate_id}")
        projections.interpretations.add_version(
            conn,
            target_kind="candidate",
            target_id=candidate.candidate_id,
            content=operation.interpretation or operation.reason or "reinterpreted",
            confidence=float((operation.patch or {}).get("confidence", candidate.confidence)),
            source_version=state.version if state else 0,
            source_event_ids=operation.sources,
        )
        return candidate_module.PoolChange(
            op=op, candidate_id=candidate.candidate_id, detail=operation.interpretation
        )

    raise ValueError(f"unknown operation: {op}")


def apply_operations(
    context: PoolContext,
    conn: sqlite3.Connection,
    operations: Sequence[candidate_module.CandidateOperation],
    *,
    now: datetime | None = None,
    state: RuntimeState | None = None,
) -> candidate_module.PoolApplyResult:
    """Apply a batch of operations, rejecting (not raising on) bad ones.

    A malformed operation must never take down the reducer, so each is applied
    inside its own savepoint and failures are collected for the API response.

    Args:
        context: Projections and configuration.
        conn: Write connection.
        operations: Operations to apply.
        now: Reference time.
        state: Current runtime state.

    Returns:
        A :class:`~companion_runtime.candidate.PoolApplyResult`.
    """
    stamp = now or utcnow()
    result = candidate_module.PoolApplyResult()
    for operation in operations:
        try:
            with context.projections.db.transaction():
                change = apply_one(context, conn, operation, now=stamp, state=state)
        except Exception as exc:  # noqa: BLE001 - protocol boundary: reject, never crash
            LOGGER.warning("Rejected candidate operation %s: %s", operation.op, exc)
            result.rejected.append({"op": operation.op, "reason": str(exc)})
            continue
        if change is not None:
            result.changes.append(change)
    return result
