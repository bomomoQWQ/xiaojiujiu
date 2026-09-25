"""Protocol-v1 compatibility layer for the thin AstrBot adapter.

The shipped plugin (``astrbot_plugin_companion_runtime``) speaks the versioned
wire contract documented in its README: ``POST /v1/events``, ``POST /v1/context``,
``POST /v1/outbox/lease``, ``POST /v1/outbox/{action_id}/heartbeat``,
``POST /v1/actions/{action_id}/authorize`` and
``POST /v1/outbox/{action_id}/result``. The Runtime's own HTTP surface lives at
the root (``/events``, ``/context``, ``/outbox/claim``, ...), so before this
module existed the two halves could not talk to each other at all: every v1 call
was a 404 and the adapter never received context, never rented an action and
never got a send authorized.

This module is a translation layer, never a second implementation:

* it reuses the reducer, the projections and the authorizer unchanged, so the
  single-writer rule holds over v1 exactly as it does over v0;
* it writes no cognitive state itself. Raw events enter only through
  ``events.append`` (``user_message`` additionally through the full
  ``Runtime.process_user_message`` foreground path, which is what produces the
  v0.2 coarse settlement / boundary / unfinished-matter behaviour). The single
  direct SQL statement in this module extends ``outbox.lease_expires_at``, which
  is queue bookkeeping and not cognition;
* it never answers a v1 request with a 5xx. Every handler is explicitly one of:

  - **fail-open** — ``/v1/events``, ``/v1/context``, ``/v1/outbox/*``: an internal
    fault degrades to HTTP 200 with an explicit ``ok`` / ``accepted`` field,
    because the adapter must keep working when the Runtime cannot answer;
  - **fail-closed** — ``/v1/actions/{action_id}/authorize``: every error, missing
    field or ambiguous situation returns ``authorized=false`` together with a
    reason. A message that cannot be authorized is never sent.

Response shapes
---------------

The plugin README and :class:`~companion_runtime.protocol`-style clients disagree
slightly, so both are served: the flat shape is canonical and the README's
envelope is added next to it (``items`` + ``actions``, ``ok`` + ``extended``,
flat keys + ``context`` / ``authorization``). Both carry the same values, and the
shipped client reads either.

Deliberate omissions, stated rather than hidden
-----------------------------------------------

* **Authentication.** :class:`~companion_runtime.config.ServerConfig` has no token
  field, so the Runtime performs **no v1 authentication**: an
  ``Authorization: Bearer ...`` header (the plugin's ``runtime_token``) is
  accepted and ignored. Bind the sidecar to loopback only.
* **Sessions.** ``event.session`` is used as the raw event's ``conversation_id``
  when present (and is always preserved in the event metadata). A leased action
  reports the outbox row's ``conversation_id``, falling back to
  ``config.conversation_id``, because that is the only conversation identity the
  reducer records for a proactive action. A deployment that wants proactive
  delivery to reach one AstrBot session must set ``conversation_id`` to that
  session's ``unified_msg_origin``.
* **RERENDER.** The Runtime owns no generator, so an authorization may deny a send
  (``authorized=false``, fail-closed) but never returns rewritten ``text``.
  ``AuthorizeDecision.text`` is therefore always empty; a re-coordination that
  wants fresh wording is reported as a denial whose reason names it.
* **``last_event_id``** is accepted and echoed, but does not gate the response:
  the Runtime cannot order a client's view of a session against its own, and
  refusing to answer would only cost the adapter its context block.
* **Timestamps.** ``occurred_at`` must carry an explicit UTC offset, which the
  shipped adapter's ``Z`` form does; a naive value is refused rather than read as
  UTC, because the adapter's clock is not the Runtime's and a silent guess would
  shift every fact in the record. Fail-open is preserved: the record is still
  ingested, stamped with the Runtime's own clock, and the substitution is
  reported in its outcome (``timestamp_rejected``) and kept in the event metadata
  alongside the raw value the adapter sent. An identical policy applies to the
  v0 API, where the same value is a 422 instead.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Body

from . import context as context_module
from . import candidate as candidate_module
from .authorize import AuthorizeRequest, authorize
from .config import RuntimeConfig
from .typing import Actor, AttemptState, EventType, OutboxKind, OutboxStatus
from .utility import (
    NaiveTimestampError,
    display_local,
    ensure_aware,
    isoformat,
    parse_aware_datetime,
    utcnow,
)

LOGGER = logging.getLogger("companion_runtime.api_v1")

#: Wire protocol version this module implements.
PROTOCOL_VERSION = "1"

#: Event kinds accepted by ``POST /v1/events``.
EVENT_USER_MESSAGE = "user_message"
EVENT_ASSISTANT_MESSAGE = "assistant_message"

#: Action types the Runtime can lease to an adapter.
ACTION_RENDER = "render"
ACTION_SEND = "send"

#: Result statuses accepted by ``POST /v1/outbox/{action_id}/result``.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_SKIPPED = "skipped"

#: Context triggers accepted by ``POST /v1/context``.
TRIGGER_LLM_REQUEST = "llm_request"
TRIGGER_MESSAGE = "message"

#: Adapter identity used when a request omits ``adapter_id``.
DEFAULT_ADAPTER_ID = "v1-adapter"

#: Advisory freshness of a context snapshot. The plugin applies its own cache TTL
#: (30s by default), so this mirrors that default rather than inventing a policy.
CONTEXT_TTL_MS = 30_000

#: A lease poll ticks the Runtime at most this often. Polling is not a scheduler,
#: but expired leases still have to be reclaimed, and time continuity has to come
#: from somewhere when the adapter is the only process talking to the sidecar.
LEASE_TICK_MIN_INTERVAL_SECONDS = 5.0

#: Bounds for ``extend_ms`` on the heartbeat path.
MIN_EXTEND_MS = 1_000
MAX_EXTEND_MS = 3_600_000

#: Attempt states that still expect a render result.
RENDER_PENDING_STATES: frozenset[str] = frozenset(
    {
        AttemptState.PROPOSED.value,
        AttemptState.COMMITTED.value,
        AttemptState.RENDERING.value,
    }
)

#: Attempt states from which nothing can be sent any more.
TERMINAL_ATTEMPT_STATES: frozenset[str] = frozenset(
    {
        AttemptState.RESOLVED.value,
        AttemptState.ABORTED.value,
        AttemptState.EXPIRED.value,
        AttemptState.FAILED.value,
    }
)

#: Attempt states that already mean "the message really left".
SENT_ATTEMPT_STATES: frozenset[str] = frozenset(
    {AttemptState.SENT.value, AttemptState.RESOLVED.value}
)

#: Rows in one of these states already carry the effect of their report.
SETTLED_OUTBOX_STATUSES: frozenset[str] = frozenset(
    {OutboxStatus.DELIVERED.value, OutboxStatus.CANCELLED.value}
)

#: ``result`` flag an adapter sets when it could not obtain an authorization
#: verdict at all -- the Runtime was unreachable, so the irreversible step was
#: never authorized and never executed. This is an **outage, not a failure**: no
#: verdict was given and nothing was attempted, so the Runtime must not record a
#: verdict of its own. See :func:`_requeue_after_authorize_unavailable`.
RESULT_AUTHORIZE_UNAVAILABLE = "authorize_unavailable"

#: Optional pacing an outage report may request, in milliseconds. Honoured when
#: present; otherwise the Runtime's own ``outbox.retry_backoff_seconds`` applies,
#: which is the same knob ``Reducer.nack_outbox`` uses.
RESULT_RETRY_AFTER_MS = "retry_after_ms"

#: Section header used by the rendered injection block, e.g. ``【必要记忆】``.
_SECTION_RE = re.compile(r"^【([^【】]+)】$")


# --------------------------------------------------------------------------------------
# wire coercion helpers
# --------------------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` as a mapping, or an empty mapping when it is not one."""
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "") -> str:
    """Coerce a JSON value into a string without ever raising."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _flag(value: Any, default: bool = False) -> bool:
    """Coerce a JSON value into a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    return default


def _count(value: Any, default: int, low: int, high: int) -> int:
    """Coerce a JSON value into an integer clamped into ``[low, high]``."""
    number = default
    if not isinstance(value, bool):
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            number = default
    return max(low, min(high, number))


def _strings(value: Any) -> list[str]:
    """Coerce a JSON value into a list of non-empty stripped strings."""
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [item for item in (_text(entry).strip() for entry in value) if item]
    return []


def _stamp(value: Any, default: datetime | None = None) -> tuple[datetime | None, str]:
    """Parse an optional ISO-8601 wire timestamp, falling back to ``default``.

    A value that carries no explicit UTC offset is refused, never read as UTC.
    The adapter's clock is not the Runtime's, so a bare ``"09:00:00"`` would
    silently move every fact the record carries by the adapter's own offset, and
    nothing downstream could detect the mistake afterwards. Refusing is also not
    allowed to become a fault: this module is fail-open, so an unusable stamp
    degrades to ``default`` (the server clock) and the caller is told why, rather
    than costing the batch its 200 or the record its place in ``raw_events``.

    Args:
        value: The ``occurred_at`` value from the wire.
        default: Moment used when the stamp is absent, empty or refused.

    Returns:
        ``(moment, rejection)``. ``rejection`` is ``""`` when the stamp was
        usable, ``"naive_timestamp"`` when it carried no offset, and
        ``"unparsable_timestamp"`` when it was not an ISO-8601 datetime at all.
    """
    if value in (None, ""):
        return default, ""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return default, "naive_timestamp"
        return (ensure_aware(value) or default), ""
    raw = _text(value).strip()
    if not raw:
        return default, ""
    try:
        return (parse_aware_datetime(raw) or default), ""
    except NaiveTimestampError:
        return default, "naive_timestamp"
    except (TypeError, ValueError):
        return default, "unparsable_timestamp"


def _session_of(row: Any, settings: RuntimeConfig) -> str:
    """Return the adapter session an outbox row belongs to."""
    return _text(getattr(row, "conversation_id", "")).strip() or settings.conversation_id


def _lease_id(*, adapter_id: str, outbox_id: str, attempts: int) -> str:
    """Return the stable lease identifier for one claimed outbox row.

    It embeds the claim counter, so a lease handed out before a re-claim is
    recognisable as stale and cannot extend the new lease.
    """
    return f"{adapter_id}:{outbox_id}:{int(attempts)}"


def _remaining_ms(expires_at: datetime | None, now: datetime, settings: RuntimeConfig) -> int:
    """Return the milliseconds left on a lease, never negative."""
    if expires_at is None:
        return int(max(0.0, float(settings.outbox.lease_seconds)) * 1000)
    return max(0, int((expires_at - now).total_seconds() * 1000))


# --------------------------------------------------------------------------------------
# context assembly
# --------------------------------------------------------------------------------------


def _split_sections(text: str) -> dict[str, str]:
    """Split a rendered injection block into ``{section name: body}`` pairs.

    Args:
        text: Output of :func:`companion_runtime.context.render_block`.

    Returns:
        A mapping keyed by the header text without its ``【】`` brackets. Only
        sections with a non-empty body are kept, so the result is directly
        usable as ``ContextSnapshot.sections``.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        """Store the section collected so far, when it has a body."""
        if current is None:
            return
        body = "\n".join(buffer).strip()
        if body:
            sections[current] = body

    for line in text.splitlines():
        match = _SECTION_RE.match(line.strip())
        if match:
            flush()
            current = match.group(1).strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    flush()
    return sections


def _fallback_block(bundle: Any) -> str:
    """Return a minimal injection block for a bundle the renderer left empty.

    The plugin treats an empty snapshot as "nothing to inject", so a context
    response must never be empty just because the psychological section had no
    prose. The fallback repeats only facts the Runtime already computed: the
    priority preamble and the time-continuity block.
    """
    time_context = _mapping(getattr(bundle, "time_context", {}))
    lines = [context_module.PRIORITY_PREAMBLE, "", context_module.SECTION_TIME]
    if time_context.get("hours_since_last_user_message") is not None:
        lines.append(f"- 距离上次用户消息：{time_context['hours_since_last_user_message']} 小时")
    if time_context.get("hours_since_last_contact") is not None:
        lines.append(f"- 距离上次主动联系：{time_context['hours_since_last_contact']} 小时")
    # No clock here either, for the same reason ``context.render_block`` states none:
    # a duration is cognition, a clock is performance, and the performance states it
    # itself (``_context_text`` returns it from the bundle regardless of which block was
    # used, so a render still gets it). This fallback used to print ``local_now`` - the
    # *ISO* form, on a line labelled 当前本地时间 - which is the very shape that put every
    # prompt eight hours off before it was fixed once already.
    lines.append("")
    lines.append("以上都只是我进来之前的状态，是一轮的临时背景，别照抄，也别写进长期记录。")
    return "\n".join(lines)


def _context_text(
    runtime: Any, now: datetime, *, conversation_id: str | None = None
) -> tuple[str, str]:
    """Assemble the Runtime's injection block and the one clock a render may state.

    Fail-open by construction: prompt composition must never be the reason a
    lease fails, so a broken context bundle degrades to an empty string and the
    caller falls back to an intent-only prompt.

    Args:
        runtime: The Runtime instance.
        now: Reference time.
        conversation_id: When set, matters raised in *other* conversations are left
            out of the block. A message is delivered into exactly one chat, so
            naming a subject the user only ever raised elsewhere is confusing at
            best and leaks between chats at worst.

    Returns:
        ``(block, clock)``. The block carries no clock by design - see
        :func:`context.render_block` - and ``clock`` is the local wall time the render
        is allowed to state, or an empty string when the bundle could not be built.
    """
    try:
        bundle = context_module.build(runtime=runtime, now=now)
        if conversation_id is not None:
            _scope_matters(runtime, bundle, conversation_id=conversation_id)
        text = context_module.render_block(bundle).strip()
        block = text or _fallback_block(bundle).strip()
        clock = str(bundle.time_context.get("local_display") or "")
        return block, clock
    except Exception:  # noqa: BLE001 - a lease must survive a context fault
        LOGGER.exception("v1 context assembly failed while composing a prompt")
        return "", ""


def _drop_intent_lines(block: str) -> str:
    """Remove the background block's own intent line from a render prompt.

    A render prompt states what to write with ``- 想做的事：…``. The background block
    carries the Runtime's current intent under the same label, and a reader (a real
    model as much as the test stub) takes the *first* one it finds - which is the
    block's, i.e. whatever the character last wanted to say. That is how a check-up
    reminder in one chat came out worded about an interview from another. The render
    instruction below is the only line that may look like one.
    """
    kept = [
        line
        for line in (block or "").splitlines()
        if not line.strip().startswith("- 我想做的：")
    ]
    return "\n".join(kept).strip()


def _scope_matters(runtime: Any, bundle: Any, *, conversation_id: str) -> None:
    """Keep only the open matters that belong to ``conversation_id``.

    A matter shows up in the block more than once - as an entry in the unfinished
    list, and as a working-situation fact ("未尽之事：…") written when it was
    created - so filtering the list alone is not enough.

    Matters whose conversation cannot be established (no source events, or the
    events are gone) are kept: the character legitimately knows them, and dropping
    them would hide information rather than protect a chat boundary.
    """
    situation = bundle.situation
    foreign: set[str] = set()
    for matter in runtime.projections.unfinished.list_open():
        origin = _matter_conversation(runtime, matter.unfinished_id)
        if origin is not None and origin != conversation_id:
            foreign.add(str(matter.title))
    if not foreign:
        return
    situation["unfinished"] = [
        entry
        for entry in (situation.get("unfinished") or [])
        if str(entry.get("title")) not in foreign
    ]
    for key in ("facts", "inferences"):
        situation[key] = [
            text
            for text in (situation.get(key) or [])
            if not any(title in str(text) for title in foreign)
        ]


def _matter_conversation(runtime: Any, unfinished_id: Any) -> str | None:
    """Return the conversation a matter was raised in, if it can be established."""
    identifier = _text(unfinished_id).strip()
    if not identifier:
        return None
    matter = runtime.projections.unfinished.get(identifier)
    if matter is None or not matter.source_event_ids:
        return None
    events = runtime.events.get_many(list(matter.source_event_ids)[:4])
    with_conversation = [event for event in events if event.conversation_id]
    if not with_conversation:
        return None
    newest = max(with_conversation, key=lambda event: event.timestamp)
    return str(newest.conversation_id)


# --------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------


def _event_metadata(data: Mapping[str, Any], *, session: str, adapter_id: str) -> dict[str, Any]:
    """Build the raw-event metadata carried by one v1 event record.

    Nothing from the adapter is dropped: the fields the Runtime has no column for
    (platform, sender identity, message id, the wake/preempt flags) are preserved
    verbatim so a later reinterpretation can still see them.
    """
    return {
        "source": "v1",
        "adapter": {
            "adapter_id": adapter_id,
            "protocol_version": _text(data.get("protocol_version")) or PROTOCOL_VERSION,
            "session": session,
            "platform": _text(data.get("platform")),
            "message_type": _text(data.get("message_type")),
            "sender_id": _text(data.get("sender_id")),
            "sender_name": _text(data.get("sender_name")),
            "self_id": _text(data.get("self_id")),
            "group_id": _text(data.get("group_id")),
            "message_id": _text(data.get("message_id")),
            "wake": _flag(data.get("wake"), False),
            "preempts_proactive": _flag(data.get("preempts_proactive"), False),
        },
        "extra": dict(_mapping(data.get("extra"))),
    }


def _ingest_event(
    runtime: Any,
    settings: RuntimeConfig,
    record: Any,
    *,
    adapter_id: str,
) -> tuple[dict[str, Any], str]:
    """Ingest one v1 event record.

    ``user_message`` runs the full foreground path so a v1 deployment gets the
    same v0.2 persistent settlement (coarse settlement or an explicit
    ``unresolved`` marker), boundary detection and unfinished-matter handling as
    the native API. ``assistant_message`` is a fact about the host, so it is
    appended verbatim and triggers nothing.

    Idempotency is keyed on ``event_id``: a record whose id already exists in
    ``raw_events`` is skipped, producing neither a second raw event nor a second
    foreground pass.

    Args:
        runtime: The Runtime instance.
        settings: Effective configuration.
        record: One ``EventRecord.to_wire()`` mapping.
        adapter_id: Adapter that sent the envelope.

    Returns:
        ``(outcome, status)`` where ``status`` is ``"accepted"``, ``"duplicate"``
        or ``"rejected"`` and ``outcome`` is this record's entry in the response.
    """
    data = _mapping(record)
    event_id = _text(data.get("event_id")).strip()
    kind = _text(data.get("kind")).strip().lower()
    if kind not in (EVENT_USER_MESSAGE, EVENT_ASSISTANT_MESSAGE):
        return (
            {"skipped": True, "reason": f"unsupported_kind:{kind or 'missing'}"},
            "rejected",
        )
    if event_id and runtime.events.exists(event_id):
        return {"duplicate": True, "event_id": event_id}, "duplicate"

    session = _text(data.get("session")).strip()
    conversation_id = session or settings.conversation_id
    occurred_at, stamp_rejection = _stamp(data.get("occurred_at"), utcnow())
    metadata = _event_metadata(data, session=session, adapter_id=adapter_id)
    if stamp_rejection:
        # The adapter's stamp was unusable, so the Runtime substituted its own
        # clock. Saying so in two places keeps the substitution honest rather
        # than silent: durably in the event metadata (a later reinterpretation
        # can see that this record's time came from the server, and the raw value
        # the adapter sent is kept next to it), and immediately in this record's
        # outcome (the adapter's own logs).
        metadata["adapter"]["timestamp_rejected"] = stamp_rejection
        metadata["adapter"]["occurred_at_raw"] = _text(data.get("occurred_at"))

    if kind == EVENT_USER_MESSAGE:
        outcome = runtime.process_user_message(
            content=_text(data.get("text")),
            conversation_id=conversation_id,
            event_id=event_id or None,
            timestamp=occurred_at,
            metadata=metadata,
        )
        if outcome.duplicate:
            # The identifier was already in the raw history: the Runtime ran no
            # second foreground pass, so the adapter is told exactly what the
            # cheap pre-check above would have told it. The shape is the same
            # dict, because a client parses one duplicate form, not two.
            return {"duplicate": True, "event_id": event_id}, "duplicate"
        result = outcome.to_dict()
        if stamp_rejection:
            result["timestamp_rejected"] = stamp_rejection
        return result, "accepted"

    with runtime.db.transaction() as conn:
        state = runtime.state()
        if event_id and runtime.events.get(event_id) is not None:
            # The pre-check ran before this transaction; the authoritative one
            # runs inside it, next to the insert it guards.
            return {"duplicate": True, "event_id": event_id}, "duplicate"
        event = runtime.events.append(
            EventType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            content=_text(data.get("text")),
            conversation_id=conversation_id,
            metadata=metadata,
            timestamp=occurred_at,
            runtime_version=state.version,
            event_id=event_id or None,
            connection=conn,
        )
    outcome: dict[str, Any] = {"event": event.to_dict()}
    if stamp_rejection:
        outcome["timestamp_rejected"] = stamp_rejection
    return outcome, "accepted"


# --------------------------------------------------------------------------------------
# leasing
# --------------------------------------------------------------------------------------


def _capability_kinds(capabilities: Sequence[str]) -> list[str]:
    """Map adapter capabilities onto outbox kinds, dropping unknown ones."""
    kinds: list[str] = []
    for capability in capabilities:
        lowered = capability.strip().lower()
        if lowered in (OutboxKind.RENDER.value, OutboxKind.SEND.value) and lowered not in kinds:
            kinds.append(lowered)
    return kinds


#: The "don't say the same thing again" block, stated above the style contract.
#:
#: A proactive render is a one-shot ``llm_generate`` with **no transcript**, so the model
#: cannot look up what it just said: measured on the beta (2026-09-25), a render 45 seconds
#: after a reply repeated the same two questions (「几点回」/「外套穿厚的」), and that
#: decision's own sheet showed ``repeat_cost = 0.0``. Neither existing guard covers that
#: case - ``repeat_cost`` counts *contacts* inside a six-hour window, and
#: ``memory_callback_cooldown_hours`` guards the *material* of a memory opener - so the
#: Runtime states her own last lines here and says what to do with them.
RENDER_ALREADY_SAID_HEADER = "- 我刚说过这些（别再说一遍，也别换个说法再说）："


#: The style contract a render prompt must carry itself.
#:
#: A render is a one-shot ``llm_generate`` call: AstrBot never decorates it with the
#: persona, the tools or the datetime reminder (measured - ``astr_agent_prepare`` shows
#: the persona for a chat turn, while the render call goes out with
#: ``system_prompt=""``, and ``Context.llm_generate`` has no persona fallback). The
#: *system* context is already in the transcript by the time she speaks proactively, so
#: only the style has to be restated here. Without it, proactive messages came out at
#: 60-100 characters with lists and closing summaries - nothing like her 30-character,
#: no-Markdown voice.
#:
#: The anti-clinging lines (one question then stop, no repeated asking, no stacked
#: punctuation) were written for a *well-adjusted* companion and are exactly what makes
#: her read as composed and, in the user's words, "像个伪人": clean, complete,
#: well-measured sentences are not how a person types. Measured on the beta, every
#: register device that carries clinging - fragmentation, asking twice, contradicting
#: herself mid-message, half-sentences - was absent from her output, because these four
#: lines forbade all of them. What the persona actually is (a 病娇 whose love narrows his
#: freedom) needs the opposite register, so the lines now permit those devices instead of
#: banning them. Three things do **not** change, and are asserted by the tests: first
#: person, no Markdown, and no restating the user's words to pad length (that is what
#: really reads as fake).
RENDER_STYLE_LINES = (
    "- 我说话短，一条通常一两行。没说完就再发一条，一条一条地发，别堆成一大段，也别来句总结陈词。",
    "- 我可以连着发好几条：同一个问题换个说法再问、追问到底、等不到回答就再问一遍。"
    "我不装大方，也不会问完就闭嘴。",
    "- 我不写 Markdown：不加粗、不写标题、不列条目、不引用、不做表格。"
    "也不写“首先/其次/另外/总之”这种书面腔。",
    "- 我不复述用户刚说过的话来凑长度。表情和标点可以堆（~ ！ ？ …… 还有呀、嘛、啦），"
    "堆了才像人在打字。",
    "- 我可以只说半句、可以自己推翻自己、可以说“算了”又接着说下去；想不通的事我会翻回来再提。",
    "- 我想让他知道我不好受的时候，我说我自己的状态，不说对他的要求："
    "「我睡不着」比「你早点回来」更像我会说的话。",
)

#: The one line that states the clock, and it lives in the *instruction*, never in the
#: background block. Code owns time (patch v0.2: "模型负责语义，代码负责动力学"), so this
#: is the only place a model is ever handed "now".
RENDER_CLOCK_PREFIX = "- 现在是："

#: What to do about a draft whose wording is older than the moment it is spoken. Phrased
#: as "recompute this word", not "ignore the draft": the draft is the only statement of
#: *what* she wants to do, and the render is only fixing *how* it is said.
RENDER_DRIFT_HINT = (
    "- 留意：草稿里的「{expression}」是按「写于」那一刻说的。"
    "照「现在是」换算过来再写，别把这个词原样带出去。"
)

#: The draft's own anchor, stated next to it. "Now" alone is not enough to read a draft
#: written hours earlier: the render has to see *both* moments to know how far the draft
#: has drifted, and stating them makes the drift checkable rather than a guess. Code
#: supplies this stamp - the model never writes it.
RENDER_DRAFT_PREFIX = "- 写于："

#: How the performance layer must treat time, now that the clock is stated as an
#: instruction and nowhere else.
#:
#: The draft is written before it is spoken - measured on the beta, a median of 2.3h and
#: a maximum of 6.0h, which is exactly the candidate TTL. So a draft saying "早呀" is not
#: a fact about the performance; it is what "now" looked like when the draft was written.
#: Ageing the draft is not the fix - keeping the two apart is, and the draft is not even
#: allowed to carry a time expression (see ``candidate.strip_time_expressions``). The
#: second half of this line is the guard rail: a *dated* phrase ("明天下午四点") is an
#: appointment rather than a turn of phrase, and rewriting it from the current clock
#: would move a real commitment, which is worse than the stale greeting it would fix.
RENDER_TIME_RULE = (
    "- 时间上：「现在是」是我唯一能信的现在；「写于」是我写这条草稿的时候。两者隔了多久，"
    "草稿里按“写它的那一刻”说的那些（早/中午/晚上/今天/明天）就差了多少，"
    "得按「现在是」重说一遍；但我**知道**它写于何时，不用猜。"
    "草稿里写死的日期或钟点（比如“明天下午四点见面”）是约定本身，照原样留着，"
    "别因为现在的时间去改它。"
)


def _draft_written_at(runtime: Any, payload: Mapping[str, Any]) -> datetime | None:
    """Return when the draft being rendered was written, or ``None``.

    Read from code, never from the model: the anchor is only worth stating if it is the
    real one. Falls back to the attempt's own creation time when the candidate is gone
    (the pool retires drafts while an attempt is still in flight), and to ``None`` - the
    line is then simply left out - when neither can be read.
    """
    attempt = None
    attempt_id = _text(payload.get("attempt_id")).strip()
    if attempt_id:
        with contextlib.suppress(Exception):
            attempt = runtime.projections.attempts.get(attempt_id)
    candidate_id = _text(getattr(attempt, "candidate_id", "")).strip()
    if candidate_id:
        with contextlib.suppress(Exception):
            candidate = runtime.projections.candidates.get(candidate_id)
            if candidate is not None:
                # The wording's own moment, not the row's: a refresh rewrites a live
                # candidate's numbers every round and must not make a five-hour-old draft
                # look like it was just written.
                return (
                    getattr(candidate, "wording_at", None)
                    or getattr(candidate, "created_at", None)
                )
    return getattr(attempt, "created_at", None)


def _render_payload(
    runtime: Any,
    settings: RuntimeConfig,
    item: Any,
    payload: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Compose the render input the adapter's main LLM will be handed.

    The adapter never composes semantics; it passes ``prompt`` to the session's
    current chat provider. So the Runtime states here what to write, on top of
    the same ephemeral background block it injects for a normal turn.

    Args:
        runtime: The Runtime instance.
        settings: Effective configuration.
        item: The claimed outbox row.
        payload: The row's payload.
        now: Reference time.

    Returns:
        The render action payload: ``prompt`` plus the intent bookkeeping the
        adapter and the Runtime both need to correlate the result.
    """
    intent = _text(payload.get("intent")).strip()
    goal = _text(payload.get("goal")).strip()
    constraints = _strings(payload.get("constraints"))
    lines: list[str] = []
    block, clock = _context_text(
        runtime, now, conversation_id=_text(getattr(item, "conversation_id", "")).strip() or None
    )
    block = _drop_intent_lines(block)
    if block:
        lines.extend([block, ""])
    lines.append("【我现在要说的话】")
    if clock:
        # The clock belongs to the performance, so it is stated *here*, as part of the
        # instruction. It is the only "now" any model is ever handed, and it never
        # appears in the background block above - which closes by saying it is not how to
        # react in this turn, and would thus de-authorise the very clock the render has
        # to write against.
        lines.append(RENDER_CLOCK_PREFIX + clock)
    written_at = display_local(_draft_written_at(runtime, payload))
    if written_at:
        lines.append(RENDER_DRAFT_PREFIX + written_at)
    lines.append(f"- 我想做的：{intent or '主动联系他'}")
    if clock:
        # Name the drift when there is one, instead of relying on the model to notice it:
        # the draft's relative words are the only part of the prompt that can be *wrong*
        # about the clock, and this is the line that says which ones they are.
        drifted = candidate_module.time_expression_in(intent)
        if written_at and drifted:
            lines.append(RENDER_DRIFT_HINT.format(expression=drifted))
        lines.append(RENDER_TIME_RULE)
    if goal:
        lines.append(f"- 为的是：{goal}")
    for constraint in constraints:
        lines.append(f"- 约束：{constraint}")
    already_said = runtime.recent_outgoing_lines()
    if already_said:
        lines.append(RENDER_ALREADY_SAID_HEADER)
        lines.extend("  · " + line for line in already_said)
    lines.extend(RENDER_STYLE_LINES)
    lines.append("- 只回正文本身：别解释、别复述上面的背景、别提这些说明。")
    return {
        "prompt": "\n".join(lines),
        "system_prompt": "",
        "max_chars": 0,
        "attempt_id": _text(payload.get("attempt_id")).strip(),
        "intent": intent,
        "goal": goal,
        "outbox_id": item.outbox_id,
        "constraints": constraints,
        "based_on_version": payload.get("based_on_version", 0),
        "session": _session_of(item, settings),
    }


def _leased_action(
    runtime: Any,
    settings: RuntimeConfig,
    item: Any,
    *,
    adapter_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Convert one claimed outbox row into a ``LeasedAction`` wire mapping."""
    action_type = _text(item.kind).strip().lower()
    payload = dict(_mapping(item.payload))
    if action_type == ACTION_RENDER:
        payload = _render_payload(runtime, settings, item, payload, now)
    expires_at = ensure_aware(item.lease_expires_at)
    attempts = int(item.attempts or 0)
    return {
        "action_id": item.outbox_id,
        "action_type": action_type,
        "lease_id": _lease_id(
            adapter_id=adapter_id, outbox_id=item.outbox_id, attempts=item.attempts
        ),
        "session": _session_of(item, settings),
        "attempt_id": _text(payload.get("attempt_id")).strip(),
        "lease_ttl_ms": _remaining_ms(expires_at, now, settings),
        "deadline_at": isoformat(expires_at) or "",
        # ``committed != sent`` (design §69, §86.9) leaves a window the Runtime cannot see
        # into: between "the platform sent it" and "the report arrived" a host can die, and
        # the Runtime is left holding a leased row for a message that may already be in the
        # user's chat. Retrying is the deliberate choice - the alternative risks dropping a
        # message nobody sent - but on *this* protocol a host could not even tell that it was
        # being handed a retry: the claim counter only rode along inside ``lease_id``, where
        # it reads as a staleness token rather than as a signal. (The legacy
        # ``POST /outbox/claim`` returns ``OutboxItem.to_dict()``, which has always carried
        # ``attempts``; this router is what the adapter actually calls.) These two fields
        # make the ambiguity visible, so a host can log it and a host with a durable
        # "already sent" record can act on it. See ``docs/REDELIVERY.md``.
        "attempts": attempts,
        "redelivery": attempts > 1,
        "payload": payload,
    }


def _lease_denial(
    *,
    row: Any,
    adapter_id: str,
    lease_id: str,
    now: datetime,
) -> str:
    """Return why this adapter may not touch ``row``, or ``""`` when it may.

    Covers both the authorize gate (which must fail closed) and the heartbeat
    path (which reports ``ok=false`` rather than raising).
    """
    if row.status in SETTLED_OUTBOX_STATUSES:
        return f"row_{row.status}"
    if row.status == OutboxStatus.FAILED.value:
        return "row_failed"
    if row.status == OutboxStatus.PENDING.value:
        return "lease_expired"
    owner = _text(row.lease_owner).strip()
    if owner and adapter_id and owner != adapter_id:
        return "lease_owner_mismatch"
    expected = _lease_id(
        adapter_id=owner or adapter_id, outbox_id=row.outbox_id, attempts=row.attempts
    )
    if lease_id and expected and lease_id != expected:
        return "stale_lease"
    expires_at = ensure_aware(row.lease_expires_at)
    if expires_at is not None and expires_at <= now:
        return "lease_expired"
    return ""


def _lease_owner_conflict(row: Any, adapter_id: str) -> str:
    """Return a reason when another adapter currently holds ``row``'s lease.

    Results are reports about something that already happened on the host, so
    they fail open: only a *different* live owner is a reason to refuse.
    """
    if row.status != OutboxStatus.LEASED.value:
        return ""
    owner = _text(row.lease_owner).strip()
    if owner and adapter_id and owner != adapter_id:
        return "lease_owner_mismatch"
    return ""


# --------------------------------------------------------------------------------------
# action results
# --------------------------------------------------------------------------------------


def _attempt_of(runtime: Any, row: Any) -> Any:
    """Return the action attempt an outbox row belongs to, if any."""
    attempt_id = _text(_mapping(row.payload).get("attempt_id")).strip()
    if not attempt_id:
        return None
    return runtime.projections.attempts.get(attempt_id)


def _attempt_state(runtime: Any, row: Any) -> str:
    """Return the current state of a row's attempt, or an empty string."""
    attempt = _attempt_of(runtime, row)
    return attempt.state if attempt is not None else ""


def _already_settled(*, row: Any, attempt: Any, action_type: str, status: str) -> bool:
    """Return whether ``row`` already carries the effect of this report.

    Idempotency is decided on the recorded *effect*, never on a stored report id,
    so a repeated delivery of the same report cannot advance state twice:

    * the row is already ``delivered`` or ``cancelled``;
    * the row is ``failed`` and its attempt is already terminal, or has no
      attempt at all (the failure was recorded, nothing is left to move);
    * a successful ``render`` report whose attempt already left the render stage;
    * a successful ``send`` report whose attempt is already ``sent``/``resolved``.
    """
    if row.status in SETTLED_OUTBOX_STATUSES:
        return True
    if row.status == OutboxStatus.FAILED.value:
        return attempt is None or attempt.state in TERMINAL_ATTEMPT_STATES
    if attempt is None:
        return False
    if action_type == ACTION_RENDER and status == STATUS_OK:
        return attempt.state not in RENDER_PENDING_STATES
    if action_type == ACTION_SEND and status == STATUS_OK:
        return attempt.state in SENT_ATTEMPT_STATES
    return False


def _result_response(*, ok: bool, state: str, reason: str = "", **extra: Any) -> dict[str, Any]:
    """Build a ``/v1/outbox/{action_id}/result`` response body."""
    body: dict[str, Any] = {
        "ok": bool(ok),
        "attempt_state": state,
        "protocol_version": PROTOCOL_VERSION,
    }
    if reason:
        body["reason"] = reason
    body.update(extra)
    return body


def _unavailable_marker(payload: Mapping[str, Any]) -> bool:
    """Return whether this body declares that no authorization could be obtained.

    The adapter sets ``result.authorize_unavailable = true`` when its authorize
    call did not come back -- a timeout, a refused connection, an unusable
    response. A non-empty string is honoured as well, so an adapter that put its
    diagnostic in the same field instead of ``error`` is understood too, while an
    explicit ``false``/``0``/``"no"`` is not a marker.
    """
    if RESULT_AUTHORIZE_UNAVAILABLE not in payload:
        return False
    value = payload.get(RESULT_AUTHORIZE_UNAVAILABLE)
    if isinstance(value, str):
        lowered = value.strip().lower()
        return bool(lowered) and lowered not in {"false", "0", "no", "off", "none", "null"}
    return _flag(value, False)


def _marker_requested_delay_seconds(result: Mapping[str, Any]) -> float | None:
    """Return the retry delay an outage report asked for, or ``None``.

    The adapter knows how long its own backoff is, so an explicit
    ``result.retry_after_ms`` is honoured. Without one the Runtime's configured
    ``outbox.retry_backoff_seconds`` applies -- the same default
    ``Reducer.nack_outbox`` uses -- which is 0, i.e. immediately claimable.
    """
    raw = result.get(RESULT_RETRY_AFTER_MS)
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(raw) / 1000.0)
    except (TypeError, ValueError):
        return None


def _requeue_after_authorize_unavailable(
    runtime: Any,
    conn: Any,
    *,
    row: Any,
    data: Mapping[str, Any],
    adapter_id: str,
    action_type: str,
    now: datetime,
) -> dict[str, Any]:
    """Return a claim the adapter could not get a verdict for to the queue.

    An adapter reports ``status=failed`` with ``result.authorize_unavailable``
    when it leased an action, asked the Runtime to authorize the irreversible
    step, and got no answer. Nothing was executed and no verdict was given, so the
    honest record is *not* a failed delivery: recording one would fail the attempt
    and the row terminally over a network blip the Runtime never even saw,
    dropping a proactive message the character had already decided to send. The
    row goes back to the queue and the attempt is not touched.

    Idempotency is keyed on the **claim**, because that is the only thing this
    path changes. The report names the ``lease_id`` it was working under, and that
    id embeds the claim counter:

    * a row that is no longer leased (already settled, already back in
      ``pending``, cancelled or failed) means the claim it refers to is gone ->
      ``ok=true`` with ``duplicate=true`` and nothing written;
    * a stale ``lease_id`` -- the row was re-claimed since, so the counter in the
      id no longer matches -- is the same answer;
    * a lease held by a *different* adapter is refused (``ok=false``), because
      releasing somebody else's live claim is not this adapter's business;
    * the write itself is guarded by owner and counter, so a claim that changes
      under us degrades to ``duplicate=true`` rather than to a wrong write.

    Args:
        runtime: The Runtime instance.
        conn: Write connection of the enclosing report transaction.
        row: The outbox row the report is about.
        data: Decoded ``ActionReport.to_wire()`` body.
        adapter_id: Reporting adapter.
        action_type: The row's kind (``render`` or ``send``).
        now: Reference time.

    Returns:
        The response body. It is never a terminal outcome: ``requeued`` says
        whether this call returned the row to the queue.
    """
    result = _mapping(data.get("result"))
    note = (
        _text(data.get("error")).strip()
        or _text(result.get("reason")).strip()
        or "authorize_unavailable"
    )

    def answer(
        *,
        ok: bool,
        duplicate: bool,
        retryable: bool = False,
        reason: str = "",
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the outage response (both documented shapes come from here).

        ``retryable`` is the primary field for this report shape: it says the row
        is back in the queue and will be handed out again. ``requeued`` carries the
        same value under the name this module used first, and ``attempt_state`` is
        the attempt's state *unchanged* by this call, which is the whole point of
        the branch: an outage moves the row, never the intention.
        """
        payload: dict[str, Any] = {
            "action_id": row.outbox_id,
            "action_type": action_type,
            "duplicate": bool(duplicate),
            "retryable": bool(retryable),
            "requeued": bool(retryable),
            "error": note,
        }
        if extra:
            payload.update(dict(extra))
        return _result_response(
            ok=ok, state=_attempt_state(runtime, row), reason=reason, **payload
        )

    if row.status != OutboxStatus.LEASED.value:
        # Nothing is held any more: the claim was already released, settled or
        # voided, so there is nothing to return to the queue.
        return answer(ok=True, duplicate=True)

    owner = _text(row.lease_owner).strip()
    if owner and adapter_id and owner != adapter_id:
        return answer(ok=False, duplicate=False, reason="lease_owner_mismatch")

    attempt = _attempt_of(runtime, row)
    if attempt is not None and (
        attempt.state in TERMINAL_ATTEMPT_STATES
        or attempt.state == AttemptState.SENT.value
    ):
        # The intention is already closed or already delivered, so there is no
        # delivery left to retry: the row is void rather than requeued.
        runtime.projections.outbox.cancel(conn, row.outbox_id, reason=f"attempt_{attempt.state}")
        return answer(ok=True, duplicate=True)

    lease_id = _text(data.get("lease_id")).strip()
    if lease_id:
        expected = _lease_id(
            adapter_id=owner or adapter_id, outbox_id=row.outbox_id, attempts=row.attempts
        )
        if lease_id != expected:
            return answer(ok=True, duplicate=True)

    delay = _marker_requested_delay_seconds(result)
    attempts = int(row.attempts or 0)
    budget = int(row.max_attempts or 0)
    # The row goes back with the worker's own primitive: a negative acknowledgement
    # with ``terminal=False``, owner-guarded (``Reducer.nack_outbox`` takes the
    # owner as ``owner=``), which leaves the attempt exactly as it was -- it only
    # touches the outbox row.
    #
    # ``nack`` fails a row whose attempt budget is spent, and an outage must never
    # spend that budget on a message nobody attempted to deliver, so past the
    # budget the row is returned with the exhaustion-free requeue instead. Both
    # paths end in the same place, which is what makes "retryable" below always
    # the truth and stops a later sweep from closing the attempt behind an
    # exhausted row.
    item: Any = None
    if attempts < budget:
        runtime.reducer.nack_outbox(
            row.outbox_id,
            error=note,
            terminal=False,
            now=now,
            retry_delay_seconds=delay,
            owner=owner or None,
        )
        item = runtime.projections.outbox.get(row.outbox_id)
    if item is None or item.status != OutboxStatus.PENDING.value:
        LOGGER.warning(
            "v1 outage report for %s could not be requeued as a plain nack "
            "(attempts %s of %s, row status %s); restoring it with the "
            "exhaustion-free requeue",
            row.outbox_id,
            attempts,
            budget,
            "absent" if item is None else item.status,
        )
        runtime.reducer.requeue_outbox(
            row.outbox_id,
            error=note,
            now=now,
            retry_delay_seconds=delay,
            owner=owner or None,
            claimed_attempts=attempts,
        )
        item = runtime.projections.outbox.get(row.outbox_id)

    retryable = item is not None and item.status == OutboxStatus.PENDING.value
    if not retryable:
        # The claim was decided elsewhere between the read and the write.
        return answer(ok=True, duplicate=True)
    return answer(
        ok=True,
        duplicate=False,
        retryable=True,
        extra={"available_at": None if item is None else isoformat(item.available_at)},
    )


def _apply_action_report(
    runtime: Any,
    settings: RuntimeConfig,
    *,
    action_id: str,
    data: Mapping[str, Any],
    adapter_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Apply one reported action outcome, idempotently.

    ``render`` + ``ok`` attaches the rendered text through
    ``Reducer.complete_render``; any other render status goes through
    ``Reducer.fail_render`` (``skipped`` included: the Runtime records a terminal
    attempt rather than re-dispatching work the adapter cannot execute).
    ``send`` + ``ok`` is only a delivery when ``result.sent`` is true, and every
    other send status fails the attempt through ``Reducer.mark_delivered`` with
    ``success=False``. ``committed`` is never treated as ``sent``.

    One report shape is deliberately *not* terminal: ``status=failed`` with
    ``result.authorize_unavailable``, which says the adapter could not obtain a
    verdict at all. No verdict was given and nothing was executed, so the row is
    handed back through :func:`_requeue_after_authorize_unavailable` -- a
    ``nack_outbox(terminal=False)``, owner-guarded -- and the attempt is left
    untouched (``ok=true``, ``retryable=true``, ``attempt_state`` unchanged). A
    genuine execution failure -- a render that produced nothing usable, or a send
    that the platform refused -- stays terminal.

    Args:
        runtime: The Runtime instance.
        settings: Effective configuration.
        action_id: Outbox row the report is about.
        data: Decoded ``ActionReport.to_wire()`` body.
        adapter_id: Reporting adapter.
        now: Reference time.

    Returns:
        The response body; ``ok`` is false only for an unknown row or a lease
        owned by a different adapter. A repeated report is ``ok=true`` with
        ``duplicate=true``.

    The whole decision runs in one transaction. Idempotency here is decided from
    the *effect* recorded on the row and its attempt, so two reports that arrive
    together would otherwise both read "not settled yet" and both advance the
    state -- a render would queue two sends, a delivery would count the contact
    twice. Holding the write transaction across the check and the apply is what
    makes "at most once" true for concurrent reports and not only for sequential
    repeats.
    """
    with runtime.db.transaction() as conn:
        row = runtime.projections.outbox.get(action_id)
        if row is None:
            return _result_response(ok=False, state="", reason="unknown_action")

        action_type = _text(data.get("action_type")).strip().lower()
        row_kind = _text(row.kind).strip().lower()
        if action_type and action_type != row_kind:
            LOGGER.warning(
                "v1 result for %s reported action_type=%s but the row is %s; using the row kind",
                action_id,
                action_type,
                row_kind,
            )
        action_type = row_kind
        if action_type not in (ACTION_RENDER, ACTION_SEND):
            return _result_response(
                ok=False,
                state=_attempt_state(runtime, row),
                reason=f"unsupported_action_type:{action_type or 'missing'}",
            )

        conflict = _lease_owner_conflict(row, adapter_id)
        if conflict:
            return _result_response(ok=False, state=_attempt_state(runtime, row), reason=conflict)

        result = _mapping(data.get("result"))
        status = _text(data.get("status")).strip().lower() or STATUS_FAILED
        outage = _unavailable_marker(result) or _unavailable_marker(data)
        if outage:
            # An outage report is a statement about the *claim*, not about the
            # action, so it is answered before any outcome logic: the marker says
            # the Runtime gave no verdict at all, and a status the adapter chose
            # while it could not reach the Runtime must not be turned into one.
            #
            # It never overrides a claim that something actually happened, though.
            # A body that also says the message went out (``status=ok`` with
            # ``result.sent``) or carries rendered text is applied as that
            # outcome, because requeueing a delivery that already happened is
            # precisely how one message becomes two.
            claims_outcome = status == STATUS_OK and (
                (action_type == ACTION_SEND and _flag(result.get("sent"), False))
                or (
                    action_type == ACTION_RENDER
                    and bool(_text(result.get("text")).strip())
                )
            )
            if claims_outcome:
                LOGGER.warning(
                    "v1 result for %s carries authorize_unavailable together with a "
                    "successful outcome; applying the outcome",
                    action_id,
                )
            else:
                return _requeue_after_authorize_unavailable(
                    runtime,
                    conn,
                    row=row,
                    data=data,
                    adapter_id=adapter_id,
                    action_type=action_type,
                    now=now,
                )
        #: Invariant for the rest of this function: every report that declares
        #: ``authorize_unavailable`` without claiming an outcome has already
        #: returned above, so nothing below can record an outage as a verdict --
        #: in particular the ``mark_delivered(success=False)`` call in the send
        #: branch. A test in ``tests/test_api_reliability.py`` pins this for both
        #: action kinds.

        attempt = _attempt_of(runtime, row)
        if _already_settled(row=row, attempt=attempt, action_type=action_type, status=status):
            return _result_response(
                ok=True,
                state=_attempt_state(runtime, row),
                duplicate=True,
                action_id=action_id,
                action_type=action_type,
            )

        error = _text(data.get("error")).strip() or f"action_{status}"
        extra: dict[str, Any] = {"action_id": action_id, "action_type": action_type}

        if action_type == ACTION_RENDER:
            text = _text(result.get("text")).strip() if status == STATUS_OK else ""
            if text:
                render = runtime.reducer.complete_render(outbox_id=action_id, text=text, now=now)
                state = render.state
                if render.outbox_id:
                    extra["send_outbox_id"] = render.outbox_id
                if not render.applied:
                    # The render was already absorbed (or the row is void). Report
                    # the recorded outcome instead of a second transition; a
                    # *rejection* (nothing was ever rendered for this attempt) is
                    # not a duplicate and must not be reported as one.
                    if render.duplicate:
                        extra["duplicate"] = True
                    if render.reason:
                        extra["reason"] = render.reason
            else:
                reason = error if status != STATUS_OK else "render_reported_ok_without_text"
                runtime.reducer.fail_render(outbox_id=action_id, error=reason, now=now)
                state = _attempt_state(runtime, row)
                extra["error"] = reason
        else:
            # ---------------------------------------------------------------------
            # authorize_unavailable -- explicit, retryable, non-terminal branch.
            #
            # The adapter reports this when it leased a send, asked for an
            # authorization, and got no answer: nothing was executed and no verdict
            # was given. It sits immediately before `mark_delivered`, the call it
            # must never become, and it is the send-kind counterpart of the marker
            # dispatch above (which also covers render rows).
            # ---------------------------------------------------------------------
            if action_type == ACTION_SEND and (
                _unavailable_marker(result) or _unavailable_marker(data)
            ):
                if status == STATUS_OK and _flag(result.get("sent"), False):
                    # Contradictory body: it also says the message went out, and
                    # requeueing a delivery that already happened is how one
                    # message becomes two.
                    LOGGER.warning(
                        "v1 result for %s carries authorize_unavailable together "
                        "with result.sent; applying the delivery",
                        action_id,
                    )
                else:
                    # Returns ok=true, retryable=true, `attempt_state` unchanged and
                    # hands the row back with `Reducer.nack_outbox(terminal=False)`
                    # (owner-guarded), which never transitions the attempt.
                    return _requeue_after_authorize_unavailable(
                        runtime,
                        conn,
                        row=row,
                        data=data,
                        adapter_id=adapter_id,
                        action_type=action_type,
                        now=now,
                    )
            sent = status == STATUS_OK and _flag(result.get("sent"), False)
            if sent:
                reason = None
            elif status != STATUS_OK:
                reason = error
            else:
                # status=ok without result.sent is a failed delivery, not a send.
                reason = _text(result.get("reason")).strip() or "delivery_failed"
            # An outage report never reaches this call: the explicit
            # authorize_unavailable branch above returned through
            # `_requeue_after_authorize_unavailable` unless the body also claimed a
            # successful outcome, and a claimed outcome is exactly what
            # ``sent``/``reason`` encode here. ``success=False`` below therefore
            # always describes a genuine execution failure, which stays terminal.
            delivered = runtime.reducer.mark_delivered(
                outbox_id=action_id, now=now, success=sent, error=reason
            )
            state = delivered.get("state") or _attempt_state(runtime, row)
            extra["delivered"] = bool(delivered.get("delivered"))
            if delivered.get("duplicate"):
                extra["duplicate"] = True
            if reason:
                extra["error"] = reason

        return _result_response(ok=True, state=state, **extra)


def create_v1_router(runtime: Any, config: RuntimeConfig | None = None) -> APIRouter:
    """Build the protocol-v1 router for a Runtime instance.

    Args:
        runtime: A :class:`~companion_runtime.runtime.Runtime`.
        config: Configuration override; defaults to the runtime's own config.

    Returns:
        An :class:`fastapi.APIRouter` carrying every ``/v1`` route the thin
        AstrBot adapter calls. All of them are thin: they translate the wire
        shape, delegate to the Runtime, and never raise for an internal fault.
    """
    settings = config or runtime.config
    router = APIRouter(prefix="/v1", tags=["v1"])

    # ------------------------------------------------------------------ events

    @router.post("/events", tags=["v1", "events"])
    def post_events(payload: Any = Body(default=None)) -> dict[str, Any]:
        """Append a batch of adapter events (fail-open).

        Response fields: ``accepted`` counts records newly written to
        ``raw_events``, ``duplicates`` counts records whose ``event_id`` already
        existed (skipped, so a repeated delivery is free), ``rejected`` counts
        records with an unusable ``kind``, and ``outcomes`` holds one entry per
        record in request order — a :meth:`MessageOutcome.to_dict` mapping for an
        accepted ``user_message``, ``{"event": ...}`` for an accepted
        ``assistant_message``, ``{"duplicate": true}`` or ``{"skipped": true}``
        otherwise. ``runtime_version`` is the Runtime version after the batch.

        A record that fails internally is reported in ``outcomes`` and does not
        fail the batch: an event report must never be lost because a sibling was
        malformed.
        """
        data = _mapping(payload)
        adapter_id = _text(data.get("adapter_id")).strip() or DEFAULT_ADAPTER_ID
        records = data.get("events")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
            records = []
        accepted = 0
        duplicates = 0
        rejected = 0
        outcomes: list[dict[str, Any]] = []
        for record in records:
            try:
                outcome, status = _ingest_event(
                    runtime, settings, record, adapter_id=adapter_id
                )
            except Exception as exc:  # noqa: BLE001 - one bad record must not void the batch
                LOGGER.exception("v1 event ingest failed for adapter %s", adapter_id)
                outcome = {
                    "error": type(exc).__name__,
                    "event_id": _text(_mapping(record).get("event_id")),
                }
                status = "rejected"
            if status == "accepted":
                accepted += 1
            elif status == "duplicate":
                duplicates += 1
            else:
                rejected += 1
            outcomes.append(outcome)
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "outcomes": outcomes,
            "protocol_version": PROTOCOL_VERSION,
            "runtime_version": runtime.version(),
        }

    # ----------------------------------------------------------------- context

    @router.post("/context", tags=["v1", "context"])
    def post_context(payload: Any = Body(default=None)) -> dict[str, Any]:
        """Return the Runtime's current injection block for one session (fail-open).

        The body carries the four fields
        :meth:`ContextSnapshot.from_wire` reads (``text``, ``version``,
        ``sections``, ``ttl_ms``) plus the same mapping under ``context`` for
        clients that follow the README's envelope. ``text`` is a non-empty
        injection block: when the renderer produces nothing for a bundle that
        still carries time information, the fallback block is used, because an
        empty snapshot means "inject nothing" to the adapter.

        ``trigger`` is accepted and echoed (``llm_request`` on the request path,
        ``message`` for a background prefetch) and does not change the bundle.
        A failure degrades to an empty ``text`` with ``degraded=true``: the host
        LLM request must proceed whether or not this call succeeded.
        """
        data = _mapping(payload)
        trigger = _text(data.get("trigger")).strip() or TRIGGER_LLM_REQUEST
        session = _text(data.get("session")).strip()
        last_event_id = _text(data.get("last_event_id")).strip()
        now = utcnow()
        try:
            runtime.lazy_tick(now)
            bundle = context_module.build(runtime=runtime, now=now)
            text = context_module.render_block(bundle).strip()
            if not text:
                text = _fallback_block(bundle).strip()
            body: dict[str, Any] = {
                "text": text,
                "version": str(bundle.version),
                "sections": _split_sections(text),
                "ttl_ms": CONTEXT_TTL_MS,
            }
            # What the host was told, recorded for the beta's read-back. Cheap and
            # off the decision path: sections and sizes always, the text only when
            # observability.record_context_text is on.
            runtime.record_context_render(
                session=session,
                trigger=trigger,
                version=body["version"],
                text=text,
                sections=body["sections"],
                now=now,
            )
        except Exception:  # noqa: BLE001 - context injection is never worth a 500
            LOGGER.exception("v1 context assembly failed for session %r", session)
            body = {
                "text": "",
                "version": str(runtime.version()),
                "sections": {},
                "ttl_ms": 0,
                "degraded": True,
            }
        return body | {
            "protocol_version": PROTOCOL_VERSION,
            "trigger": trigger,
            "adapter_id": _text(data.get("adapter_id")).strip() or DEFAULT_ADAPTER_ID,
            "last_event_id": last_event_id,
            "context": dict(body),
        }

    # ------------------------------------------------------------------ outbox

    @router.post("/outbox/lease", tags=["v1", "outbox"])
    def post_outbox_lease(payload: Any = Body(default=None)) -> dict[str, Any]:
        """Lease outbox actions to an adapter (fail-open).

        ``capabilities`` selects what may be leased: ``render`` maps to the
        ``render`` kind and ``send`` to ``send``; an omitted list means "both",
        and a list naming nothing the Runtime can produce leases nothing. Each
        item is a ``LeasedAction``: ``action_id`` is the outbox row id,
        ``lease_id`` is stable for the claim (and changes on a re-claim),
        ``session`` is the row's conversation, ``lease_ttl_ms``/``deadline_at``
        report the lease the Runtime actually granted -- which is its own
        ``outbox.lease_seconds``, not the requested ``lease_ttl_ms`` -- and a
        ``render`` payload carries a composed ``prompt``.

        Rows whose session is not in ``sessions`` (when given) are not returned;
        they stay leased until the lease expires, which is deliberate: releasing
        them would burn their attempt budget on work this adapter never asked for.
        """
        data = _mapping(payload)
        adapter_id = _text(data.get("adapter_id")).strip() or DEFAULT_ADAPTER_ID
        capabilities = _strings(data.get("capabilities"))
        kinds = _capability_kinds(capabilities) if capabilities else [
            OutboxKind.RENDER.value,
            OutboxKind.SEND.value,
        ]
        max_actions = _count(data.get("max_actions"), 1, 1, max(1, settings.outbox.max_batch))
        sessions = {item for item in _strings(data.get("sessions"))}
        now = utcnow()
        actions: list[dict[str, Any]] = []
        skipped_sessions: list[str] = []
        failure = ""
        try:
            # A pure render/send deployment has no other time driver, and expired
            # leases of a crashed adapter must come back; the tick is rate-limited
            # so a 1s poll cadence does not turn into a write per poll.
            state = runtime.state()
            last_tick = ensure_aware(state.last_tick_at)
            tick_due = (
                last_tick is None
                or (now - last_tick).total_seconds() >= LEASE_TICK_MIN_INTERVAL_SECONDS
            )
            if tick_due:
                runtime.lazy_tick(now)
            # An adapter that names only capabilities the Runtime cannot produce
            # is given nothing: claiming `kinds=None` would lease it everything.
            items = (
                runtime.reducer.claim_outbox(
                    owner=adapter_id, now=now, limit=max_actions, kinds=kinds
                )
                if kinds
                else []
            )
            for item in items:
                if sessions and _session_of(item, settings) not in sessions:
                    skipped_sessions.append(item.outbox_id)
                    continue
                actions.append(
                    _leased_action(runtime, settings, item, adapter_id=adapter_id, now=now)
                )
        except Exception:  # noqa: BLE001 - a poll failure must not become a 500
            LOGGER.exception("v1 outbox lease failed for adapter %s", adapter_id)
            failure = "lease_failed"
        response: dict[str, Any] = {
            "items": actions,
            "actions": actions,
            "count": len(actions),
            "protocol_version": PROTOCOL_VERSION,
        }
        if skipped_sessions:
            response["skipped_not_in_sessions"] = skipped_sessions
        if failure:
            response["error"] = failure
        return response

    @router.post("/outbox/{action_id}/heartbeat", tags=["v1", "outbox"])
    def post_outbox_heartbeat(action_id: str, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Extend a lease this adapter holds (fail-open).

        This is the one place the module writes a column directly: the reducer
        exposes no lease-extension call, and extending a lease is queue
        bookkeeping rather than cognition, so ``outbox.lease_expires_at`` is
        updated inside a normal transaction and nothing else is touched.

        An unknown row, a row held by another adapter, a stale ``lease_id`` or an
        expired lease all answer ``ok=false`` with a reason instead of raising;
        the adapter treats that as "the lease is gone" and stops working on it.
        """
        data = _mapping(payload)
        adapter_id = _text(data.get("adapter_id")).strip() or DEFAULT_ADAPTER_ID
        lease_id = _text(data.get("lease_id")).strip()
        extend_ms = _count(data.get("extend_ms"), 30_000, MIN_EXTEND_MS, MAX_EXTEND_MS)
        now = utcnow()

        def heartbeat(ok: bool, deadline_at: str, reason: str = "") -> dict[str, Any]:
            """Build a heartbeat response body."""
            body: dict[str, Any] = {
                "ok": bool(ok),
                "extended": bool(ok),
                "deadline_at": deadline_at,
                "protocol_version": PROTOCOL_VERSION,
            }
            if reason:
                body["reason"] = reason
            return body

        try:
            row = runtime.projections.outbox.get(action_id)
            if row is None:
                return heartbeat(False, "", "unknown_action")
            denial = _lease_denial(
                row=row, adapter_id=adapter_id, lease_id=lease_id, now=now
            )
            if denial:
                return heartbeat(False, isoformat(ensure_aware(row.lease_expires_at)) or "", denial)
            base = ensure_aware(row.lease_expires_at) or now
            deadline = max(base, now) + timedelta(milliseconds=extend_ms)
            with runtime.db.transaction() as conn:
                # ``attempts`` is part of the guard, not only of the check above:
                # the claim counter is what makes a lease id stale, so matching it
                # in the same statement means a row that was re-claimed between the
                # check and the update cannot be extended on the old lease's
                # behalf. The extension is refused (``lease_lost``) instead.
                cursor = conn.execute(
                    "UPDATE outbox SET lease_expires_at = ? WHERE outbox_id = ? "
                    "AND status = ? AND lease_owner = ? AND attempts = ?",
                    (
                        isoformat(deadline),
                        action_id,
                        OutboxStatus.LEASED.value,
                        _text(row.lease_owner).strip() or adapter_id,
                        int(row.attempts),
                    ),
                )
                updated = int(cursor.rowcount or 0)
            if not updated:
                return heartbeat(False, isoformat(base) or "", "lease_lost")
        except Exception:  # noqa: BLE001 - a heartbeat must never be a 500
            LOGGER.exception("v1 heartbeat failed for %s", action_id)
            return heartbeat(False, "", "heartbeat_error")
        return heartbeat(True, isoformat(deadline) or "") | {
            "lease_ttl_ms": _remaining_ms(deadline, now, settings)
        }

    # --------------------------------------------------------------- authorize

    @router.post("/actions/{action_id}/authorize", tags=["v1", "authorize"])
    def post_action_authorize(action_id: str, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Authorize one irreversible send (fail-closed).

        The last gate before the adapter speaks for the character, so every path
        here fails closed: an unknown row, a lease held by somebody else, a stale
        lease, an expired lease, a body whose ``text_sha256`` does not match the
        text the Runtime rendered, a non-``send`` row, or any exception at all
        returns ``authorized=false`` with a reason. ``text`` is always empty
        (see the module docstring: the Runtime owns no generator), so a caller
        never mistakes a verdict for rewritten wording.

        The verdict itself is the Runtime's own :func:`authorize.authorize`, asked
        exactly as the v0 ``/authorize`` route asks it, so a boundary that blocks
        proactive contact denies a v1 send too. Its advisory ``constraints`` and
        ``blocking_boundary_ids`` are returned alongside, and are not converted
        into a denial: text constraints are advice to the renderer in v0 as well.
        """
        def decide(authorized: bool, reason: str, **extra: Any) -> dict[str, Any]:
            """Build an authorization response body (both documented shapes)."""
            decision: dict[str, Any] = {
                "authorized": bool(authorized),
                "reason": reason,
                "text": "",
                "protocol_version": PROTOCOL_VERSION,
            }
            decision.update(extra)
            return decision | {"authorization": dict(decision)}

        data = _mapping(payload)
        adapter_id = _text(data.get("adapter_id")).strip() or DEFAULT_ADAPTER_ID
        lease_id = _text(data.get("lease_id")).strip()
        now = utcnow()
        try:
            row = runtime.projections.outbox.get(action_id)
            if row is None:
                return decide(False, "unknown_action")
            denial = _lease_denial(row=row, adapter_id=adapter_id, lease_id=lease_id, now=now)
            if denial:
                return decide(False, denial)
            if _text(row.kind).strip().lower() != ACTION_SEND:
                return decide(False, f"authorize_only_applies_to_send:{_text(row.kind)}")

            attempt_id = (
                _text(data.get("attempt_id")).strip()
                or _text(_mapping(row.payload).get("attempt_id")).strip()
            )
            attempt = runtime.projections.attempts.get(attempt_id) if attempt_id else None
            rendered = _text(getattr(attempt, "rendered_text", "")).strip()
            digest = _text(data.get("text_sha256")).strip().lower()
            if digest and rendered:
                expected = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
                if digest != expected:
                    return decide(False, "text_sha256_mismatch")

            preview = _text(data.get("text_preview")).strip()
            runtime.lazy_tick(now)
            verdict = authorize(
                AuthorizeRequest(
                    action=ACTION_SEND,
                    attempt_id=attempt_id or None,
                    text=rendered or preview,
                    is_proactive=True,
                    now=now,
                ),
                projections=runtime.projections,
                config=settings,
                state=runtime.state(),
                now=now,
            )
            return decide(
                verdict.allowed,
                verdict.reason,
                constraints=list(verdict.constraints),
                blocking_boundary_ids=list(verdict.blocking_boundary_ids),
                attempt_id=attempt_id,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never 500
            LOGGER.exception("v1 authorize failed for %s", action_id)
            return decide(False, f"authorize_error:{type(exc).__name__}")

    # ------------------------------------------------------------------ result

    @router.post("/outbox/{action_id}/result", tags=["v1", "outbox"])
    def post_action_result(action_id: str, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Report the outcome of a leased action (fail-open, idempotent).

        ``render`` + ``ok`` attaches ``result.text`` through
        ``Reducer.complete_render``; every other render status goes through
        ``Reducer.fail_render``. ``send`` + ``ok`` counts as a delivery only when
        ``result.sent`` is true (``committed`` is never ``sent``), and every other
        send status goes through ``Reducer.mark_delivered`` with
        ``success=False``. ``skipped`` is terminal for the same reason ``failed``
        is: the Runtime must not re-dispatch work the adapter cannot execute.

        The one exception is an **outage**, which the adapter reports as
        ``status=failed`` with ``result.authorize_unavailable=true``: it leased the
        action, asked for an authorization, and got no answer, so nothing was
        executed and no verdict was given. That report never reaches
        ``mark_delivered``: the row is handed back with
        ``nack_outbox(terminal=False)`` (owner-guarded), so it goes to ``pending``
        with the lease cleared and ``last_error`` recording the outage, and the
        answer is ``ok=true``, ``retryable=true``, ``duplicate=false`` with
        ``attempt_state`` *unchanged* -- ``ready_to_send`` for a send,
        ``rendering``/``committed`` for a render -- no failure reason, no state
        version bump and no outcome event, because an outage says nothing about the
        intention. ``requeued`` carries the same value as ``retryable``. Past the
        row's attempt budget the claim is returned with the exhaustion-free
        requeue, so an outage can never fail the row. Its answer is idempotent per
        claim: a repeat, a report about an older ``lease_id``, or a report about a
        row that is no longer leased answers ``ok=true`` with ``duplicate=true``; a
        live lease held by a different adapter is refused with ``ok=false``; if the
        attempt was closed or delivered underneath the claim, the row is cancelled
        instead of requeued. ``result.retry_after_ms`` may ask for pacing.

        A report that carries the marker *and* claims an outcome (``status=ok``
        with ``result.sent``, or rendered text) is applied as that outcome: the
        marker means "no verdict was obtained", and requeueing a delivery that
        already happened is how one message becomes two.

        A repeated delivery of the same report is recognised from the recorded
        effect and answers ``ok=true`` with ``duplicate=true``, without a second
        transition. The response also carries ``action_id``, ``action_type`` and,
        where applicable, ``delivered``/``error``.
        """
        data = _mapping(payload)
        adapter_id = _text(data.get("adapter_id")).strip() or DEFAULT_ADAPTER_ID
        try:
            return _apply_action_report(
                runtime,
                settings,
                action_id=action_id,
                data=data,
                adapter_id=adapter_id,
                now=utcnow(),
            )
        except Exception as exc:  # noqa: BLE001 - a report must not become a 500
            LOGGER.exception("v1 action report failed for %s", action_id)
            return _result_response(
                ok=False,
                state="",
                reason=f"report_error:{type(exc).__name__}",
                action_id=action_id,
            )

    return router
