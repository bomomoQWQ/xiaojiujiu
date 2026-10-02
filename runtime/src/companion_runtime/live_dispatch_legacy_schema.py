"""PostgreSQL v25: keep the dispatch witness from freezing pre-witness history.

The v18 witness made ``action_attempts``/``outbox`` inserts require a live dispatch
claim.  The trigger fires for ``INSERT ... ON CONFLICT DO UPDATE`` too, so a Runtime
that reconciles a row created *before* the witness existed could no longer write at all:
every historical attempt/outbox row has ``dispatch_claim_id IS NULL``, and PostgreSQL
aborts the statement before the conflict is resolved.  In production that surfaced as

    ConflictError: action attempt att_... has no live dispatch claim

on every v1 event ingest, which permanently blocked legacy reconciliation.

This migration keeps the real invariant — *nothing dispatchable may exist without a
claim* — while allowing bookkeeping rewrites of rows that predate the witness:

* a brand-new attempt/outbox row still requires a claim;
* a claim-less row may never be moved into a dispatchable state
  (attempt: committed/rendering/ready_to_send; outbox: pending/leased);
* the witness columns of a claim-less row stay ``NULL``, so such a row can never be
  presented as claimed evidence;
* a row that *has* a claim is still stamped from its claim, and a candidate mismatch is
  still rejected.
"""

from __future__ import annotations

from typing import Final


LIVE_DISPATCH_LEGACY_SCHEMA_V25_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE OR REPLACE FUNCTION witness_action_attempt_dispatch()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE claim live_dispatch_claims%ROWTYPE;
    DECLARE legacy_state TEXT;
    BEGIN
        SELECT * INTO claim FROM live_dispatch_claims WHERE attempt_id = NEW.attempt_id;
        IF FOUND THEN
            IF claim.candidate_id IS DISTINCT FROM NEW.candidate_id THEN
                RAISE EXCEPTION 'action attempt % candidate differs from dispatch claim', NEW.attempt_id
                    USING ERRCODE = '23514';
            END IF;
            NEW.dispatch_scope_key := claim.scope_key;
            NEW.dispatch_claim_id := claim.claim_id;
            RETURN NEW;
        END IF;

        SELECT a.state INTO legacy_state FROM action_attempts AS a
         WHERE a.attempt_id = NEW.attempt_id AND a.dispatch_claim_id IS NULL;
        IF legacy_state IS NULL THEN
            RAISE EXCEPTION 'action attempt % has no live dispatch claim', NEW.attempt_id
                USING ERRCODE = '23503';
        END IF;
        -- A pre-witness row stays rewritable for bookkeeping, but never becomes a
        -- dispatchable attempt and never acquires witness columns.
        IF NEW.state IN ('committed', 'rendering', 'ready_to_send') THEN
            RAISE EXCEPTION 'action attempt % cannot become dispatchable without a live dispatch claim',
                NEW.attempt_id USING ERRCODE = '23503';
        END IF;
        NEW.dispatch_scope_key := NULL;
        NEW.dispatch_claim_id := NULL;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION witness_outbox_dispatch()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE attempt_identity TEXT;
    DECLARE round_identity TEXT;
    DECLARE claim live_dispatch_claims%ROWTYPE;
    DECLARE legacy_status TEXT;
    BEGIN
        IF NEW.kind NOT IN ('render', 'send') THEN
            RETURN NEW;
        END IF;
        attempt_identity := NEW.payload_json ->> 'attempt_id';
        IF attempt_identity IS NULL OR btrim(attempt_identity) = '' THEN
            RAISE EXCEPTION '% outbox % has no attempt identity', NEW.kind, NEW.outbox_id
                USING ERRCODE = '23514';
        END IF;
        SELECT * INTO claim FROM live_dispatch_claims WHERE attempt_id = attempt_identity;
        IF FOUND THEN
            IF NEW.kind = 'render' AND claim.render_outbox_id <> NEW.outbox_id THEN
                RAISE EXCEPTION 'render outbox % differs from dispatch claim', NEW.outbox_id
                    USING ERRCODE = '23514';
            END IF;
            round_identity := NEW.payload_json ->> 'decision_id';
            IF round_identity IS NOT NULL AND round_identity <> claim.round_id THEN
                RAISE EXCEPTION 'outbox % round differs from dispatch claim', NEW.outbox_id
                    USING ERRCODE = '23514';
            END IF;
            NEW.dispatch_scope_key := claim.scope_key;
            NEW.dispatch_claim_id := claim.claim_id;
            RETURN NEW;
        END IF;

        SELECT o.status INTO legacy_status FROM outbox AS o
         WHERE o.outbox_id = NEW.outbox_id AND o.dispatch_claim_id IS NULL;
        IF legacy_status IS NULL THEN
            RAISE EXCEPTION 'outbox % has no live dispatch claim', NEW.outbox_id
                USING ERRCODE = '23503';
        END IF;
        -- A pre-witness row may be recorded as terminal, but may never be (re)queued:
        -- ``pending``/``leased`` is exactly the state the delivery lease dispatches.
        IF NEW.status IN ('pending', 'leased') THEN
            RAISE EXCEPTION 'outbox % cannot become dispatchable without a live dispatch claim',
                NEW.outbox_id USING ERRCODE = '23503';
        END IF;
        NEW.dispatch_scope_key := NULL;
        NEW.dispatch_claim_id := NULL;
        RETURN NEW;
    END
    $$
    """,
)


__all__ = ["LIVE_DISPATCH_LEGACY_SCHEMA_V25_STATEMENTS"]
