"""The fleet's children are wired for PostgreSQL, and their schema name is bounded.

These checks started as literal source assertions in ``runtime_fleet.py``. The schema rule
has since moved to :mod:`fleet_naming` -- so the supervisor and the operator tools derive a
scope's schema from the same function instead of two copies that could disagree -- and an
assertion pinned to the old line only tested where the code happened to live. The
invariants are kept, and the PostgreSQL identifier limit is now checked against the
function that owns the rule rather than against its text.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[2] / "scripts"


def test_fleet_children_inherit_postgres_and_do_not_create_sqlite_paths():
    source = (SCRIPTS / "runtime_fleet.py").read_text(encoding="utf-8")
    # v2 is PostgreSQL-only: a child must never be handed a SQLite path.
    assert 'self.env.pop("CR_STORAGE__DATABASE_PATH", None)' in source
    assert 'self.env["CR_STORAGE__DATABASE_PATH"] =' not in source
    # Identity: the session *is* the conversation, and each person gets their own runtime id.
    assert 'self.env["CR_CONVERSATION_ID"] = session' in source
    assert 'self.env["CR_RUNTIME_ID"] = f"companion-{self.slug}"' in source
    # The schema comes from the shared definition, not from a copy kept here.
    assert 'self.env["CR_STORAGE__SCHEMA"] = schema_for_session(session)' in source


def test_the_schema_name_stays_within_postgresqls_identifier_limit():
    """PostgreSQL truncates identifiers at 63 bytes, silently.

    Silent truncation would put a person's rows in a schema nobody derives, which is the
    "the Runtime is healthy but she never acts" failure. Checked on the function that owns
    the rule, so it keeps holding wherever the rule is called from.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        import fleet_naming
    finally:
        sys.path.pop(0)

    for session in (
        "default:FriendMessage:10001",
        "default:FriendMessage:" + "9" * 120,
        "webchat:" + "x" * 200,
    ):
        schema = fleet_naming.schema_for_session(session)
        assert schema.startswith("cr_"), schema
        assert len(schema.encode("utf-8")) <= 63, schema
