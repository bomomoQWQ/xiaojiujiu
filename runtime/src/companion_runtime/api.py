"""HTTP API for the Runtime sidecar.

Every endpoint is thin: it validates input, delegates to the Runtime or the
reducer, and returns JSON. No endpoint writes Runtime state directly — the
single-writer rule holds over HTTP too.

Route map::

    GET  /health
    POST /events                       append a user / assistant / tool event
    GET  /events                       read raw events
    GET  /events/{event_id}            read one event
    GET  /context                      assemble the temporary context bundle
    POST /context/render-block         render the prompt block only
    POST /render                       complete a render (host main LLM result)
    POST /render/fail                  report a failed render
    GET  /outbox                       list queued work
    POST /outbox/claim                 lease work (claim/lease)
    POST /outbox/{id}/ack              acknowledge delivered work
    POST /outbox/{id}/nack             return work to the queue
    POST /authorize                    ask permission for an action
    POST /rendered                     report a rendered message ready to send
    POST /delivery                     report a delivery result
    POST /proposals                    submit a background model result
    POST /reconcile                    re-coordinate in-flight attempts
    POST /tick                         run lazy_tick explicitly
    POST /endogenous                   run one endogenous round
    POST /observations                 record a user reaction
    GET  /state                        inspect the current projection
    GET  /candidates, /memories, /user-model, /unfinished, /boundaries, /attempts
    GET  /schedule                     next endogenous wake-up plan
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from . import candidate as candidate_module
from . import context as context_module
from . import protocol as protocol_module
from . import scheduler as scheduler_module
from .authorize import AuthorizeRequest, authorize
from .config import RuntimeConfig, redact
from .typing import (
    Actor,
    AttemptState,
    EventType,
    OutboxStatus,
    Priority,
    ReconcileAction,
    new_id,
)
from .user_model import BehaviourReaction
from .utility import clamp, ensure_aware, isoformat, parse_datetime, utcnow

LOGGER = logging.getLogger("companion_runtime.api")


# --------------------------------------------------------------------------------------
# Request models (plain dataclass-free dicts keep the surface dependency-light)
# --------------------------------------------------------------------------------------


def _optional_datetime(value: Any) -> datetime | None:
    """Parse an optional ISO-8601 value into an aware datetime."""
    if value in (None, ""):
        return None
    try:
        return parse_datetime(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require(payload: Mapping[str, Any], key: str) -> Any:
    """Return a required payload field or raise a 422."""
    if key not in payload or payload[key] in (None, ""):
        raise HTTPException(status_code=422, detail=f"missing required field: {key}")
    return payload[key]


def create_app(runtime: Any, config: RuntimeConfig | None = None) -> FastAPI:
    """Build the FastAPI application for a Runtime instance.

    Args:
        runtime: A :class:`~companion_runtime.runtime.Runtime`.
        config: Configuration override; defaults to the runtime's own config.

    Returns:
        A configured :class:`fastapi.FastAPI` application.
    """
    settings = config or runtime.config
    app = FastAPI(
        title="Endogenous Companion Runtime",
        version=__version__,
        description=(
            "Sidecar Runtime that owns long-term companion state: emotion, memory, "
            "user model, candidate intents, motivational game and the delivery protocol."
        ),
    )
    app.state.runtime = runtime
    router = APIRouter()

    # ------------------------------------------------------------------ health

    @router.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        """Return liveness plus a compact activity summary."""
        state = runtime.state()
        outbox_stats = runtime.projections.outbox.stats()
        return {
            "status": "ok",
            "runtime_version": __version__,
            "state_version": state.version,
            "runtime_id": settings.runtime_id,
            "now": isoformat(utcnow()),
            "last_tick_at": isoformat(state.last_tick_at),
            "allow_proactive": state.allow_proactive,
            "active_boundaries": len(runtime.projections.boundaries.active(utcnow())),
            "open_unfinished": len(runtime.projections.unfinished.list_open()),
            "active_candidates": len(runtime.projections.candidates.list_active(limit=100)),
            "in_flight_attempts": runtime.projections.attempts.count_in_flight(),
            "outbox": outbox_stats,
            # Operators need to see which appraisal level is actually live, and
            # whether the local model has been answered or bypassed.
            "local_model": runtime.local_model.health(),
            "raw_events": runtime.events.count(),
        }

    # ------------------------------------------------------------------ events

    @router.post("/events", tags=["events"])
    def append_event(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Append an event to the append-only history.

        A ``user_message`` goes through the full foreground path; other event
        types are appended verbatim (they are facts, not triggers).
        """
        event_type = str(_require(payload, "event_type"))
        actor = str(payload.get("actor") or Actor.USER.value)
        content = payload.get("content")
        conversation_id = payload.get("conversation_id") or settings.conversation_id
        timestamp = _optional_datetime(payload.get("timestamp"))
        metadata = payload.get("metadata") or {}
        event_id = payload.get("event_id")

        if event_type == EventType.USER_MESSAGE.value:
            reaction = None
            if payload.get("reaction"):
                reaction = BehaviourReaction(**(payload["reaction"] or {}))
            outcome = runtime.process_user_message(
                content=str(content or ""),
                conversation_id=conversation_id,
                event_id=event_id,
                timestamp=timestamp,
                metadata=metadata,
                reason=reaction,
            )
            return {"kind": "user_message", "outcome": outcome.to_dict()}

        with runtime.db.transaction() as conn:
            state = runtime.state()
            event = runtime.events.append(
                event_type,
                actor=actor,
                content=content,
                conversation_id=conversation_id,
                metadata=metadata,
                source_event_ids=payload.get("source_event_ids") or [],
                timestamp=timestamp,
                runtime_version=state.version,
                event_id=event_id,
                connection=conn,
            )
        return {"kind": "event", "event": event.to_dict()}

    @router.get("/events", tags=["events"])
    def list_events(
        conversation_id: str | None = None,
        event_type: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        newest_first: bool = False,
    ) -> dict[str, Any]:
        """Read raw events."""
        from .eventlog import EventQuery

        events = runtime.events.read(
            EventQuery(
                conversation_id=conversation_id,
                event_types=[event_type] if event_type else None,
                limit=limit,
                newest_first=newest_first,
            )
        )
        return {"count": len(events), "events": [event.to_dict() for event in events]}

    @router.get("/events/{event_id}", tags=["events"])
    def get_event(event_id: str) -> dict[str, Any]:
        """Read one raw event."""
        event = runtime.events.get(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        interpretations = runtime.projections.interpretations.list_for_target("event", event_id)
        return {"event": event.to_dict(), "interpretations": interpretations}

    # ----------------------------------------------------------------- context

    @router.get("/context", tags=["context"])
    def get_context(include_boundaries: bool = True) -> dict[str, Any]:
        """Assemble the temporary Runtime context bundle."""
        runtime.lazy_tick()
        bundle = context_module.build(
            runtime=runtime, now=utcnow(), include_boundaries=include_boundaries
        )
        return bundle.to_dict()

    @router.post("/context/render-block", tags=["context"])
    def render_block(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Render the temporary prompt block the host injects for one turn."""
        runtime.lazy_tick()
        bundle = context_module.build(runtime=runtime, now=utcnow())
        text = context_module.render_block(bundle)
        return {
            "block": text,
            "ephemeral": True,
            "version": bundle.version,
            "note": "inject for one turn only; never persist into conversation history",
        }

    # ------------------------------------------------------------------ render

    @router.post("/render", tags=["render"])
    def complete_render(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Report a rendered message for a claimed render outbox row."""
        outbox_id = str(_require(payload, "outbox_id"))
        text = str(_require(payload, "text"))
        try:
            result = runtime.reducer.complete_render(
                outbox_id=outbox_id, text=text, now=_optional_datetime(payload.get("now"))
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result.to_dict()

    @router.post("/render/fail", tags=["render"])
    def fail_render(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Report that rendering failed for a claimed render row."""
        outbox_id = str(_require(payload, "outbox_id"))
        error = str(payload.get("error") or "render_failed")
        ok = runtime.reducer.fail_render(
            outbox_id=outbox_id, error=error, now=_optional_datetime(payload.get("now"))
        )
        if not ok:
            raise HTTPException(status_code=404, detail="outbox row not found")
        # Report the resulting attempt state alongside the acknowledgement. A caller
        # that has just reported a failure needs to assert on the outcome, and
        # "the call returned 200" says nothing about what the attempt actually
        # became (it may already have been terminal, in which case this is a no-op).
        attempt_state: str | None = None
        item = runtime.projections.outbox.get(outbox_id)
        if item is not None:
            attempt_id = str(item.payload.get("attempt_id") or "")
            attempt = runtime.projections.attempts.get(attempt_id) if attempt_id else None
            if attempt is not None:
                attempt_state = attempt.state
        return {
            "ok": True,
            "outbox_id": outbox_id,
            "error": error,
            "attempt_state": attempt_state,
        }

    # ------------------------------------------------------------------ outbox

    @router.get("/outbox", tags=["outbox"])
    def list_outbox(
        status: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """List outbox rows."""
        items = runtime.projections.outbox.list_items(
            status=status, limit=limit, conversation_id=conversation_id
        )
        return {
            "stats": runtime.projections.outbox.stats(),
            "count": len(items),
            "items": [item.to_dict() for item in items],
        }

    @router.post("/outbox/claim", tags=["outbox"])
    def claim_outbox(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Lease ready outbox rows for a worker (claim/lease)."""
        owner = str(payload.get("owner") or "http-worker")
        limit = int(payload.get("limit") or 1)
        kinds = payload.get("kinds")
        items = runtime.reducer.claim_outbox(
            owner=owner,
            now=_optional_datetime(payload.get("now")),
            limit=clamp(limit, 1, settings.outbox.max_batch),
            kinds=kinds,
        )
        return {
            "owner": owner,
            "lease_seconds": settings.outbox.lease_seconds,
            "count": len(items),
            "items": [item.to_dict() for item in items],
        }

    @router.post("/outbox/{outbox_id}/ack", tags=["outbox"])
    def ack_outbox(outbox_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Acknowledge a leased outbox row."""
        ok = runtime.reducer.ack_outbox(outbox_id, now=_optional_datetime(payload.get("now")))
        if not ok:
            raise HTTPException(status_code=409, detail="row is not leased to this worker")
        return {"ok": True, "outbox_id": outbox_id}

    @router.post("/outbox/{outbox_id}/nack", tags=["outbox"])
    def nack_outbox(outbox_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Return a leased row to the queue, or fail it terminally.

        The row is claimable again immediately unless ``retry_delay_seconds`` is
        given, so a caller that retries at once is not silently given nothing.
        """
        delay = payload.get("retry_delay_seconds")
        ok = runtime.reducer.nack_outbox(
            outbox_id,
            error=str(payload.get("error") or "unspecified"),
            terminal=bool(payload.get("terminal", False)),
            now=_optional_datetime(payload.get("now")),
            retry_delay_seconds=None if delay is None else float(delay),
        )
        if not ok:
            raise HTTPException(status_code=409, detail="row is not leased")
        item = runtime.projections.outbox.get(outbox_id)
        return {
            "ok": True,
            "outbox_id": outbox_id,
            "status": None if item is None else item.status,
            "attempts": None if item is None else item.attempts,
            "available_at": None if item is None else isoformat(item.available_at),
        }

    # --------------------------------------------------------------- authorize

    @router.post("/authorize", tags=["authorize"])
    def authorize_action(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Ask the boundary machine and contact budget for permission."""
        request = AuthorizeRequest(
            action=str(payload.get("action") or "proactive_contact"),
            attempt_id=payload.get("attempt_id"),
            text=payload.get("text"),
            is_proactive=bool(payload.get("is_proactive", True)),
            scope=payload.get("scope"),
            now=_optional_datetime(payload.get("now")),
        )
        runtime.lazy_tick(request.now)
        verdict = authorize(
            request,
            projections=runtime.projections,
            config=settings,
            state=runtime.state(),
            now=request.now,
        )
        # ``allowed`` answers this specific request; ``allow_proactive`` carries the
        # standing permission, which is what a caller needs when deciding whether
        # outreach is permitted at all. Reporting both -- and reusing the name
        # ``GET /boundaries`` already uses -- keeps the two endpoints consistent.
        # A denial caused by the *reply* gate does not deny proactive contact.
        reply_only_denial = verdict.reason == "reply_not_permitted"
        return verdict.to_dict() | {
            "action": request.action,
            "allow_proactive": verdict.allowed or reply_only_denial,
        }

    # ------------------------------------------------- rendered / delivery

    @router.post("/rendered", tags=["delivery"])
    def rendered(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Report a rendered message: attach text and queue it for sending."""
        attempt_id = str(_require(payload, "attempt_id"))
        text = str(_require(payload, "text"))
        now = _optional_datetime(payload.get("now"))
        runtime.lazy_tick(now)
        attempt = runtime.projections.attempts.get(attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="attempt not found")

        verdict = authorize(
            AuthorizeRequest(action="render", attempt_id=attempt_id, text=text, now=now),
            projections=runtime.projections,
            config=settings,
            state=runtime.state(),
            now=now,
        )
        if not verdict.allowed and verdict.reason not in {"attempt_not_rendered", "attempt_still_rendering"}:
            return {
                "accepted": False,
                "reason": verdict.reason,
                "constraints": verdict.constraints,
            }

        outbox_items = runtime.projections.outbox.list_items(status=None, limit=100)
        render_rows = [
            item
            for item in outbox_items
            if item.payload.get("attempt_id") == attempt_id and item.kind == "render"
        ]
        if render_rows:
            result = runtime.reducer.complete_render(
                outbox_id=render_rows[0].outbox_id, text=text, now=now
            )
            return {"accepted": True, "path": "outbox"} | result.to_dict()

        # No render row (the attempt was created outside the outbox): go straight
        # through the state machine and queue the send.
        from . import action as action_module

        with runtime.db.transaction() as conn:
            state = runtime.state()
            if attempt.state == AttemptState.COMMITTED.value:
                action_module.mark_rendering(runtime.projections.attempts, conn, attempt, now=now)
            try:
                action_module.mark_ready(runtime.projections.attempts, conn, attempt, text=text, now=now)
            except (ValueError, action_module.IllegalTransition) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            runtime.projections.runtime.write(state, conn, expect_version=state.version)
        return {"accepted": True, "path": "direct", "attempt": attempt.to_dict()}

    @router.post("/delivery", tags=["delivery"])
    def delivery(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Report a delivery result for a send outbox row."""
        outbox_id = str(_require(payload, "outbox_id"))
        now = _optional_datetime(payload.get("now"))
        reaction = payload.get("reaction")
        behaviour = BehaviourReaction(**(reaction or {})) if reaction else None
        try:
            result = runtime.reducer.mark_delivered(
                outbox_id=outbox_id,
                now=now,
                reaction=behaviour,
                success=bool(payload.get("success", True)),
                error=payload.get("error"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if behaviour is not None and result.get("attempt_id"):
            observation = runtime.observe_reply(
                attempt_id=result["attempt_id"],
                reaction=behaviour,
                now=now,
                event_ids=payload.get("source_event_ids") or [],
            )
            result["observation"] = observation
        return result

    # --------------------------------------------------------------- proposals

    @router.post("/proposals", tags=["protocol"])
    def submit_proposal(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Submit a background model result for APPLY / REBASE / DISCARD."""
        proposal = protocol_module.Proposal(
            task_id=str(payload.get("task_id") or new_id("task")),
            task_type=str(_require(payload, "task_type")),
            based_on_version=int(payload.get("based_on_version") or 0),
            payload=dict(payload.get("payload") or {}),
            source_event_ids=list(payload.get("source_event_ids") or []),
            created_at=_optional_datetime(payload.get("created_at")),
        )
        result = runtime.reducer.process_proposal(proposal)
        return result.to_dict()

    @router.post("/tasks", tags=["protocol"])
    def register_task(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Register a background task snapshot before dispatch."""
        task_id = runtime.reducer.register_task(
            task_id=str(payload.get("task_id") or new_id("task")),
            task_type=str(_require(payload, "task_type")),
            based_on_version=int(payload.get("based_on_version") or runtime.version()),
            source_event_ids=list(payload.get("source_event_ids") or []),
            priority=str(payload.get("priority") or Priority.P1_NEAR_REALTIME.value),
        )
        return {"task_id": task_id}

    @router.post("/reconcile", tags=["protocol"])
    def reconcile(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Re-coordinate in-flight attempts after new user events."""
        now = _optional_datetime(payload.get("now"))
        if payload.get("attempt_id"):
            attempt = runtime.projections.attempts.get(str(payload["attempt_id"]))
            if attempt is None:
                raise HTTPException(status_code=404, detail="attempt not found")
            events = runtime.events.get_many(list(payload.get("event_ids") or []))
            if not events:
                events = runtime.events.recent(3, conversation_id=settings.conversation_id)
            decision = runtime.reducer.reconcile_attempt(
                attempt_id=attempt.attempt_id, new_events=events, now=now
            )
            return {"count": 1, "decisions": [{"attempt_id": attempt.attempt_id} | decision.to_dict()]}
        events = runtime.events.get_many(list(payload.get("event_ids") or []))
        decisions = runtime.reducer.reconcile_pending_attempts(new_events=events, now=now)
        return {"count": len(decisions), "decisions": decisions}

    # ------------------------------------------------------------------ ticks

    @router.post("/tick", tags=["scheduler"])
    def tick(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Run ``lazy_tick`` explicitly (advance the Runtime to a moment)."""
        report = runtime.lazy_tick(_optional_datetime(payload.get("now")))
        return report.to_dict()

    @router.post("/endogenous", tags=["scheduler"])
    def endogenous(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Run one endogenous wake-up round and return the decision."""
        outcome = runtime.endogenous_round(
            now=_optional_datetime(payload.get("now")),
            force=bool(payload.get("force", False)),
            create_attempt=bool(payload.get("create_attempt", True)),
        )
        return outcome.to_dict()

    @router.get("/schedule", tags=["scheduler"])
    def schedule(hazard_wake_at: str | None = None) -> dict[str, Any]:
        """Return the next endogenous wake-up plan."""
        signals = scheduler_module.collect_signals(
            runtime=runtime, now=utcnow(), hazard_wake_at=_optional_datetime(hazard_wake_at)
        )
        plan = scheduler_module.plan(signals, config=settings, rng=runtime.rng)
        allowed, reason = scheduler_module.should_dispatch(
            signals=signals,
            attempt_states=[
                attempt.state
                for attempt in runtime.projections.attempts.list_by_state(
                    ["proposed", "committed", "rendering", "ready_to_send", "sent"], limit=10
                )
            ],
            config=settings,
        )
        return scheduler_module.next_wake_summary(signals, plan) | {
            "dispatch_allowed": allowed,
            "dispatch_reason": reason,
        }

    # ----------------------------------------------------------- observations

    @router.post("/observations", tags=["user-model"])
    def record_observation(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Record a user reaction (with or without an attempt)."""
        reaction = BehaviourReaction(**(payload.get("reaction") or {}))
        now = _optional_datetime(payload.get("now"))
        if payload.get("attempt_id"):
            result = runtime.observe_reply(
                attempt_id=str(payload["attempt_id"]),
                reaction=reaction,
                now=now,
                event_ids=payload.get("source_event_ids") or [],
            )
            return result
        with runtime.db.transaction() as conn:
            state = runtime.state()
            observation = runtime.user_model.observe(
                conn,
                action=payload.get("action") or {"type": "contact", "proactive": True},
                context=payload.get("context") or {},
                reaction=reaction,
                now=now,
                semantic_confidence=float(payload.get("semantic_confidence", 0.6)),
                source_event_ids=payload.get("source_event_ids") or [],
                busy_probability=payload.get("busy_probability"),
                attribution_confidence=payload.get("attribution_confidence"),
            )
            runtime.projections.runtime.write(state, conn, expect_version=state.version)
        return {"observation": observation.to_dict()}

    # --------------------------------------------------------------- read-only

    @router.get("/state", tags=["inspect"])
    def get_state() -> dict[str, Any]:
        """Return the current runtime projection."""
        return runtime.state().to_dict()

    @router.get("/candidates", tags=["inspect"])
    def get_candidates(status: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        """Return the candidate intent pool."""
        if status:
            items = runtime.projections.candidates.list_by_status([status], limit=limit)
        else:
            items = runtime.projections.candidates.list_active(limit=limit)
        return {"count": len(items), "candidates": [item.to_dict() for item in items]}

    @router.post("/candidates/operations", tags=["inspect"])
    def candidate_operations(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Apply ADD/UPDATE/RETIRE/REINTERPRET operations through the pool manager."""
        operations = [
            candidate_module.CandidateOperation.from_mapping(item)
            for item in (payload.get("operations") or [])
        ]
        result = runtime.apply_candidate_operations(
            operations, now=_optional_datetime(payload.get("now")), source="http"
        )
        return result.to_dict()

    @router.get("/memories", tags=["inspect"])
    def get_memories(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """Return long-term memories, the activation pool and pending candidates."""
        memories = runtime.projections.memory.list_memories(limit=limit)
        activated = runtime.projections.memory.list_activated(limit=limit)
        candidates = runtime.projections.memory.list_candidates(limit=limit)
        return {
            "memories": [memory.to_dict() for memory in memories],
            "activated": [item.to_dict() for item in activated],
            "candidates": [item.to_dict() for item in candidates],
        }

    @router.get("/user-model", tags=["inspect"])
    def get_user_model() -> dict[str, Any]:
        """Return both views of the user interaction model."""
        return {
            "numeric": runtime.user_model.numeric_view(),
            "semantic": runtime.user_model.semantic_view(),
        }

    @router.post("/user-model/predict", tags=["inspect"])
    def predict_user(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Predict the user's reaction to a hypothetical behaviour."""
        prediction = runtime.user_model.predict(
            action=payload.get("action") or {"type": "contact", "proactive": True},
            context=payload.get("context") or {},
        )
        conservative = runtime.user_model.conservative_bound(prediction)
        return prediction.to_dict() | {"conservative_reply_probability": conservative}

    @router.get("/unfinished", tags=["inspect"])
    def get_unfinished(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        """Return unfinished matters of every status."""
        matters = runtime.projections.unfinished.list_all(limit=limit)
        return {"count": len(matters), "matters": [matter.to_dict() for matter in matters]}

    @router.post("/unfinished", tags=["inspect"])
    def create_unfinished(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Create an unfinished matter (detected or proposed by a model)."""
        from . import unfinished as unfinished_module

        proposal = unfinished_module.UnfinishedProposal(
            title=str(_require(payload, "title")),
            source_event_ids=list(payload.get("source_event_ids") or []),
            waiting_until=_optional_datetime(payload.get("waiting_until")),
            priority=clamp(float(payload.get("priority", settings.unfinished.default_priority))),
            resolution_conditions=list(payload.get("resolution_conditions") or []),
        )
        with runtime.db.transaction() as conn:
            state = runtime.state()
            matter = unfinished_module.create(
                runtime.projections.unfinished, conn, proposal, config=settings
            )
            runtime.projections.runtime.write(state, conn, expect_version=state.version)
        return {"matter": matter.to_dict()}

    @router.post("/unfinished/{unfinished_id}/resolve", tags=["inspect"])
    def resolve_unfinished(unfinished_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Resolve an unfinished matter."""
        from . import unfinished as unfinished_module

        with runtime.db.transaction() as conn:
            state = runtime.state()
            ok = unfinished_module.resolve(
                runtime.projections.unfinished,
                conn,
                unfinished_id,
                note=payload.get("note"),
            )
            runtime.projections.runtime.write(state, conn, expect_version=state.version)
        if not ok:
            raise HTTPException(status_code=404, detail="matter not found or already resolved")
        return {"ok": True, "unfinished_id": unfinished_id}

    @router.get("/boundaries", tags=["inspect"])
    def get_boundaries(include_revoked: bool = True) -> dict[str, Any]:
        """Return every boundary plus the current permission verdict.

        The verdict is produced for a *prospective proactive contact*, which is
        the question a caller usually has: "may I initiate right now?". Denials
        therefore carry ``reason`` and ``blocking_boundary_ids`` rather than an
        explicit ``allow_proactive`` flag, which only appears on the boundary
        view itself.
        """
        now = utcnow()
        boundaries = runtime.projections.boundaries.list_all(include_revoked=include_revoked)
        verdict = authorize(
            AuthorizeRequest(action="proactive_contact", is_proactive=True, now=now),
            projections=runtime.projections,
            config=settings,
            state=runtime.state(),
            now=now,
        )
        return {
            "boundaries": [boundary.to_dict() for boundary in boundaries],
            "verdict": verdict.to_dict()
            | {
                "allow_proactive": verdict.allowed,
                "active_boundary_ids": [
                    boundary.boundary_id
                    for boundary in boundaries
                    if boundary.is_active(now)
                ],
            },
        }

    @router.get("/attempts", tags=["inspect"])
    def get_attempts(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        """Return recent action attempts with their transition logs."""
        attempts = runtime.projections.attempts.list_all(limit=limit)
        return {
            "count": len(attempts),
            "attempts": [
                attempt.to_dict()
                | {"transitions": runtime.projections.attempts.transitions(attempt.attempt_id)}
                for attempt in attempts
            ],
        }

    @router.get("/observations", tags=["inspect"])
    def get_observations(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """Return recorded interaction observations."""
        items = runtime.projections.user_model.list_observations(limit=limit)
        return {"count": len(items), "observations": items}

    @router.get("/situation", tags=["inspect"])
    def get_situation() -> dict[str, Any]:
        """Return the current working situation."""
        return context_module.build_situation(runtime.projections, now=utcnow())

    @router.get("/config", tags=["system"])
    def get_config() -> dict[str, Any]:
        """Return the effective configuration with secrets redacted."""
        payload = settings.to_dict()
        return {key: redact(key, value) for key, value in payload.items()}

    # -------------------------------------------------------------- durability

    @router.get("/maintenance/verify", tags=["durability"])
    def maintenance_verify() -> dict[str, Any]:
        """Run integrity and structural consistency checks."""
        from .maintenance import verify

        result = verify(runtime.db, expect_wal=settings.storage.wal)
        return result.to_dict()

    @router.post("/maintenance/checkpoint", tags=["durability"])
    def maintenance_checkpoint(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Fold the write-ahead log back into the database file.

        ``TRUNCATE`` (the default) also shrinks the WAL, which is what keeps a
        long-running sidecar from accumulating an unbounded ``-wal`` file.
        """
        from .maintenance import checkpoint

        mode = str(payload.get("mode") or "TRUNCATE")
        try:
            return checkpoint(runtime.db, mode=mode).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/maintenance/backup", tags=["durability"])
    def maintenance_backup(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Write a consistent snapshot of the database.

        The snapshot is produced with ``VACUUM INTO``, so it is transactionally
        consistent and needs no ``-wal`` sidecar to be restorable.
        """
        from .maintenance import backup, snapshot_name

        if runtime.db.path == ":memory:":
            raise HTTPException(
                status_code=409, detail="cannot back up an in-memory database"
            )
        destination = payload.get("destination")
        if not destination:
            destination = str(
                Path(settings.storage.database_path).parent / "backups" / snapshot_name()
            )
        try:
            result = backup(
                runtime.db,
                destination,
                overwrite=bool(payload.get("overwrite", False)),
                checkpoint_first=bool(payload.get("checkpoint_first", True)),
            )
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result.to_dict()

    @router.get("/maintenance/recovery-plan", tags=["durability"])
    def maintenance_recovery_plan(backup_dir: str | None = None) -> dict[str, Any]:
        """Return the actions a recovery drill should take for this data directory."""
        from .maintenance import recovery_plan

        folder = backup_dir or str(Path(settings.storage.database_path).parent / "backups")
        return recovery_plan(settings.storage.database_path, folder)

    @router.post("/maintenance/tick", tags=["durability"])
    def maintenance_run(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Run the routine durability pass: checkpoint, verify, optionally back up."""
        from .maintenance import maintenance_tick

        return maintenance_tick(
            runtime.db,
            backup_dir=payload.get("backup_dir"),
            keep=int(payload.get("keep") or 7),
        )

    @router.post("/explain", tags=["context"])
    def explain(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Return the current first-person psychological explanation."""
        from .emotion import EmotionExplainer

        now = _optional_datetime(payload.get("now")) or utcnow()
        runtime.lazy_tick(now)
        explainer = EmotionExplainer(runtime.projections.emotion, settings)
        explanation = explainer.explain(
            state=runtime.state(),
            active=runtime.projections.emotion.list_active(),
            now=now,
            force=bool(payload.get("force", False)),
            rng=runtime.rng,
        )
        return explanation

    app.include_router(router)

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        """Translate validation errors from the cognitive layer into 422s."""
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
        """Translate missing-record errors into 404s."""
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    return app
