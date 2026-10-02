"""PostgreSQL v25: pre-witness rows stay reconcilable but never become dispatchable.

These checks need a real database because the behaviour under test lives in a
``BEFORE INSERT`` trigger, which also fires for ``INSERT ... ON CONFLICT DO UPDATE``.
That is exactly how production lost the ability to reconcile historical attempts.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from companion_runtime.user_model_v2_migrations import migrate  # noqa: E402


_DSN = os.environ.get("CR_TEST_PG_DSN") or os.environ.get("LANGCHAO_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="no PostgreSQL test DSN configured")

NOW = datetime(2027, 1, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def connection():
    schema = "v25test_" + uuid.uuid4().hex[:12]
    handle = psycopg.connect(_DSN, autocommit=False)
    handle.row_factory = psycopg.rows.dict_row
    try:
        with handle.transaction():
            migrate(handle, schema=schema)
        handle.execute(f'SET search_path TO "{schema}"')
        handle.commit()
        yield handle
    finally:
        handle.rollback()
        handle.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        handle.close()


def _attempt(connection, *, attempt_id: str, state: str = "sent") -> None:
    connection.execute(
        """INSERT INTO action_attempts
           (attempt_id,candidate_id,state,intent,created_at,updated_at,outbox_id)
           VALUES (%s,'candidate:legacy',%s,'pre-witness',%s,%s,'obx:legacy')""",
        (attempt_id, state, NOW, NOW),
    )


def _seed_pre_witness_row(connection, *, attempt_id: str, state: str = "sent") -> None:
    """Create a row exactly as history did: before the witness trigger existed.

    Production rows carry ``dispatch_claim_id IS NULL`` because they were written
    before v18 installed the trigger.  The only faithful way to reproduce that state is
    to disable the trigger for the seed; every later write goes through it again.
    """
    connection.execute("ALTER TABLE action_attempts DISABLE TRIGGER action_attempts_require_live_dispatch_claim")
    try:
        with connection.transaction():
            _attempt(connection, attempt_id=attempt_id, state=state)
    finally:
        connection.execute("ALTER TABLE action_attempts ENABLE TRIGGER action_attempts_require_live_dispatch_claim")
    connection.commit()


def _seed_pre_witness_outbox(
    connection, *, outbox_id: str, attempt_id: str, status: str = "delivered"
) -> None:
    connection.execute("ALTER TABLE outbox DISABLE TRIGGER outbox_require_live_dispatch_claim")
    try:
        with connection.transaction():
            _outbox(connection, outbox_id=outbox_id, status=status, attempt_id=attempt_id)
    finally:
        connection.execute("ALTER TABLE outbox ENABLE TRIGGER outbox_require_live_dispatch_claim")
    connection.commit()


def _outbox(connection, *, outbox_id: str, status: str, attempt_id: str) -> None:
    connection.execute(
        """INSERT INTO outbox (outbox_id,kind,payload_json,status,priority,created_at)
           VALUES (%s,'send',%s::jsonb,%s,100,%s)""",
        (outbox_id, '{"attempt_id":"%s"}' % attempt_id, status, NOW),
    )


def test_new_attempt_without_a_claim_is_still_rejected(connection) -> None:
    import psycopg.errors

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with connection.transaction():
            _attempt(connection, attempt_id="att:brand-new")


def test_legacy_attempt_rewrite_is_allowed_and_never_claims_identity(connection) -> None:
    attempt_id = "att:legacy-rewrite"
    _seed_pre_witness_row(connection, attempt_id=attempt_id)
    assert connection.execute(
        "SELECT dispatch_claim_id, dispatch_scope_key FROM action_attempts WHERE attempt_id=%s",
        (attempt_id,),
    ).fetchone() == {"dispatch_claim_id": None, "dispatch_scope_key": None}

    # The v1 ingest reconciliation path re-upserts the same historical row.
    with connection.transaction():
        connection.execute(
            "UPDATE action_attempts SET state='sent', updated_at=%s WHERE attempt_id=%s",
            (NOW, attempt_id),
        )
        _attempt(connection, attempt_id=attempt_id)
    row = connection.execute(
        "SELECT state, dispatch_claim_id, dispatch_scope_key FROM action_attempts WHERE attempt_id=%s",
        (attempt_id,),
    ).fetchone()
    assert row == {"state": "sent", "dispatch_claim_id": None, "dispatch_scope_key": None}


def test_legacy_attempt_cannot_become_dispatchable(connection) -> None:
    import psycopg.errors

    attempt_id = "att:legacy-escalate"
    _seed_pre_witness_row(connection, attempt_id=attempt_id)
    for state in ("committed", "rendering", "ready_to_send"):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with connection.transaction():
                _attempt(connection, attempt_id=attempt_id, state=state)


def test_legacy_outbox_can_be_recorded_terminal_but_never_requeued(connection) -> None:
    import psycopg.errors

    attempt_id = "att:legacy-outbox"
    outbox_id = "obx:legacy-send"
    _seed_pre_witness_row(connection, attempt_id=attempt_id)
    _seed_pre_witness_outbox(connection, outbox_id=outbox_id, attempt_id=attempt_id)

    with connection.transaction():
        _outbox(connection, outbox_id=outbox_id, status="delivered", attempt_id=attempt_id)

    for status in ("pending", "leased"):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with connection.transaction():
                _outbox(connection, outbox_id=outbox_id, status=status, attempt_id=attempt_id)

    assert connection.execute(
        "SELECT dispatch_claim_id FROM outbox WHERE outbox_id=%s", (outbox_id,)
    ).fetchone() == {"dispatch_claim_id": None}
