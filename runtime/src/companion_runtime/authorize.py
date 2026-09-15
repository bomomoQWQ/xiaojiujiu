"""Authorization: the hard gate between an intention and a visible message.

Authorization is the last line of defence. Independently of what the motivational
game decided, a message may only leave the Runtime when:

* no active boundary forbids proactive contact (or the action is a reply to a user
  message, which re-opens reply permission);
* the daily and per-window contact budgets are respected;
* the attempt exists, is in a sendable state, and its text is not empty;
* the attempt was not resolved or aborted during rendering.

The authorizer also produces the *constraints* that are handed to the renderer, so
the main LLM knows what it must not do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from . import action as action_module
from . import boundaries as boundary_module
from .config import RuntimeConfig
from .projections import Projections
from .typing import (
    ActionAttempt,
    AttemptState,
    AuthorizeResult,
    RuntimeState,
)
from .utility import ensure_aware, isoformat, utcnow

LOGGER = logging.getLogger("companion_runtime.auth")


@dataclass(slots=True)
class AuthorizeRequest:
    """What a caller wants the Runtime to permit."""

    action: str
    attempt_id: str | None = None
    text: str | None = None
    is_proactive: bool = True
    scope: str | None = None
    now: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "action": self.action,
            "attempt_id": self.attempt_id,
            "text": self.text,
            "is_proactive": self.is_proactive,
            "scope": self.scope,
            "now": isoformat(self.now),
        }


def _deny(
    reason: str,
    *,
    constraints: list[str],
    blocking: list[str],
    allow_reply: bool,
) -> AuthorizeResult:
    """Build a denial verdict, consistently."""
    return AuthorizeResult(
        allowed=False,
        reason=reason,
        constraints=list(constraints),
        blocking_boundary_ids=list(blocking),
        allow_reply=allow_reply,
    )


def authorize(
    request: AuthorizeRequest,
    *,
    projections: Projections,
    config: RuntimeConfig,
    state: RuntimeState | None = None,
    now: datetime | None = None,
) -> AuthorizeResult:
    """Decide whether the requested action is permitted.

    Args:
        request: What is being requested.
        projections: Projection bundle.
        config: Runtime configuration.
        state: Pre-read runtime state; read fresh when omitted.
        now: Reference time.

    Returns:
        An :class:`~companion_runtime.typing.AuthorizeResult`. ``allowed`` is the
        final verdict; ``constraints`` are advisory text for the renderer.
    """
    stamp = ensure_aware(now or request.now) or utcnow()
    runtime_state = state or projections.runtime.read()
    active = projections.boundaries.active(stamp)
    verdict = boundary_module.evaluate(
        active,
        now=stamp,
        state=runtime_state,
        is_proactive=request.is_proactive,
        scope=request.scope,
    )
    constraints = list(verdict.constraints)
    blocking = list(verdict.blocking_ids)

    if request.is_proactive and not verdict.allow_proactive:
        return _deny(
            "boundary_blocks_proactive" if blocking else "proactive_disabled",
            constraints=constraints,
            blocking=blocking,
            allow_reply=verdict.allow_reply,
        )

    if not request.is_proactive and not verdict.allow_reply:
        return _deny(
            "reply_not_permitted",
            constraints=constraints,
            blocking=blocking,
            allow_reply=False,
        )

    # --- attempt integrity, read before the budget so the budget can tell a
    # *decision* apart from the *delivery* of a decision already taken.
    attempt: ActionAttempt | None = None
    if request.attempt_id is not None:
        attempt = projections.attempts.get(request.attempt_id)
        if attempt is None:
            return _deny(
                "unknown_attempt",
                constraints=constraints,
                blocking=blocking,
                allow_reply=verdict.allow_reply,
            )

    # --- contact budget.
    # A message that was already committed is in its *delivery* stage: the
    # decision to reach out was made under the old conditions, so the cooldown
    # and the daily budget must not be applied a second time. That exemption
    # cannot be keyed on the action name alone: committing an attempt starts a
    # cooldown, so gating the ``render`` step of that same attempt on the
    # cooldown the commit just set would deadlock every proactive message the
    # Runtime had already decided to send. Any request naming an attempt that has
    # left ``proposed`` is therefore delivery-stage; only boundaries can still
    # stop it.
    #
    # The daily budget itself counts *delivered* messages only (it is charged in
    # ``Reducer.mark_delivered``), which is what makes this check mean "how many
    # times have I actually reached out today".
    delivery_stage = request.action == "send" or (
        attempt is not None and attempt.state != AttemptState.PROPOSED.value
    )
    if request.is_proactive and not delivery_stage:
        if runtime_state.cooldown_until is not None and runtime_state.cooldown_until > stamp:
            return _deny(
                "cooldown_active",
                constraints=constraints,
                blocking=blocking,
                allow_reply=verdict.allow_reply,
            )
        if runtime_state.contact_count_today >= config.drive.max_contacts_per_day:
            return _deny(
                "daily_contact_budget_exhausted",
                constraints=constraints,
                blocking=blocking,
                allow_reply=verdict.allow_reply,
            )

    # --- attempt integrity (continued)
    if attempt is not None:
        attempt_reason = _attempt_blocking_reason(attempt)
        if attempt_reason is not None:
            return _deny(
                attempt_reason,
                constraints=constraints,
                blocking=blocking,
                allow_reply=verdict.allow_reply,
            )
        if attempt.reconcile_action in {"abort", "resolved"}:
            return _deny(
                f"attempt_reconciled:{attempt.reconcile_action}",
                constraints=constraints,
                blocking=blocking,
                allow_reply=verdict.allow_reply,
            )
        if attempt.rendered_text:
            constraints.extend(validate_text(attempt.rendered_text, config=config))

    if request.text is not None:
        constraints.extend(validate_text(request.text, config=config))

    return AuthorizeResult(
        allowed=True,
        reason="permitted",
        constraints=list(dict.fromkeys(constraints)),
        blocking_boundary_ids=list(blocking),
        allow_reply=verdict.allow_reply,
    )


def _attempt_blocking_reason(attempt: ActionAttempt) -> str | None:
    """Return why an attempt may not be sent, or ``None`` when it may."""
    if attempt.state in action_module.TERMINAL_STATES:
        return f"attempt_terminal:{attempt.state}"
    if attempt.state in {AttemptState.PROPOSED.value, AttemptState.COMMITTED.value}:
        return "attempt_not_rendered"
    if attempt.state == AttemptState.RENDERING.value:
        return "attempt_still_rendering"
    if attempt.state == AttemptState.SENT.value:
        return "attempt_already_sent"
    if not (attempt.rendered_text or "").strip():
        return "attempt_has_no_text"
    return None


#: Phrases that must never be sent verbatim: they are internal-state leakage.
LEAK_MARKERS = (
    "valence",
    "arousal",
    "approach_impulse",
    "restraint",
    "pressure =",
    "runtime_version",
    "appraise",
    "候选意图",
    "internal_need",
)


def validate_text(text: str, *, config: RuntimeConfig, max_length: int = 2000) -> list[str]:
    """Return the constraints a piece of outgoing text violates (advisory).

    This is a safety net, not a censor: it reports internal-state leakage and
    excessive length so the caller can re-render rather than silently shipping it.

    Args:
        text: Text about to be sent.
        config: Runtime configuration.
        max_length: Soft maximum message length.

    Returns:
        A list of violated constraint strings (empty when clean).
    """
    violations: list[str] = []
    lowered = (text or "").lower()
    for marker in LEAK_MARKERS:
        if marker in lowered:
            violations.append(f"internal_state_leak:{marker}")
    if len(text or "") > max_length:
        violations.append("text_too_long")
    return violations


def render_constraints(
    *,
    projections: Projections,
    attempt: ActionAttempt | None,
    candidate_constraints: Sequence[str] = (),
    now: datetime | None = None,
) -> list[str]:
    """Assemble the constraint list handed to the renderer.

    Args:
        projections: Projection bundle.
        attempt: Attempt being rendered.
        candidate_constraints: Constraints carried by the candidate.
        now: Reference time.

    Returns:
        A deduplicated list of constraints.
    """
    stamp = ensure_aware(now) or utcnow()
    collected: list[str] = []
    collected.extend(candidate_constraints)
    for boundary in projections.boundaries.active(stamp):
        if boundary.note:
            collected.append(boundary.note)
        if not boundary.allow_proactive:
            collected.append("不要主动联系，除非用户先开口")
    if attempt is not None and attempt.committed_at is not None:
        collected.append("这是内源主动：保持短、轻、容易被忽略，不要假定关系状态")
    return list(dict.fromkeys(collected))


def delivery_window_open(
    *,
    projections: Projections,
    now: datetime,
) -> tuple[bool, str]:
    """Return whether delivery is currently allowed, and why not when it is not.

    Args:
        projections: Projection bundle.
        now: Reference time.

    Returns:
        ``(open, reason)``.
    """
    state = projections.runtime.read()
    if state.foreground_pause_until is not None and now < state.foreground_pause_until:
        return False, "foreground_pause"
    verdict = boundary_module.evaluate(
        projections.boundaries.active(now), now=now, state=state, is_proactive=True
    )
    if not verdict.allow_proactive:
        return False, "boundary_blocks_proactive"
    return True, "open"
