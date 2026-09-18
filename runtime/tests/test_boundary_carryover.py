"""A wipe must not drop what the user explicitly forbade.

The first beta ended with a data wipe: every instance directory was archived and rebuilt
empty, because two and a half months of misclassified memories were worse than none. One
category had to be carried over, and it is not a memory - it is a *hard constraint*. A
boundary is the user saying "do not contact me" or "do not bring that up again". If the
record of it is wiped, the character keeps acting under a rule the user no longer
remembers setting and has no way to correct, because saying it again is the only
correction there is and the message that said it is gone.

So these tests are about two opposite failures:

* a boundary that is *lost* in the wipe (the feature would be worse than no wipe at all);
* a boundary that is *resurrected* by the import - one the user revoked, or one whose
  window has passed, coming back to life because the file was read as a list of rules
  rather than as a state with a timeline.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path

import pytest

from companion_runtime import cli
from companion_runtime import boundaries as boundary_module
from companion_runtime.authorize import AuthorizeRequest, authorize
from companion_runtime.boundaries import (
    BOUNDARY_EXPORT_FORMAT,
    BOUNDARY_EXPORT_VERSION,
    BoundaryImportError,
    export_boundaries,
    import_boundaries,
    parse_boundary_export,
)
from companion_runtime.db import Database
from companion_runtime.projections import Projections
from companion_runtime.runtime import Runtime
from companion_runtime.typing import BoundaryType
from companion_runtime.utility import utcnow

from conftest import BASE_TIME, build_config

#: A permanent no-proactive declaration, used throughout the boundary suite as well.
PERMANENT = "永远别联系我"
#: A declaration with a window, which is what makes expiry testable.
TEMPORAL = "今天不要主动联系我。"


def _declare(config, *, content: str = PERMANENT, database: Database | None = None) -> Runtime:
    """Start a Runtime on ``database`` and have the user declare a boundary."""
    runtime = Runtime(config, seed=1234, database=database or Database(":memory:"), created_at=BASE_TIME)
    runtime.process_user_message(content=content, timestamp=BASE_TIME)
    return runtime


def _denies_proactive(runtime: Runtime, now=None) -> bool:
    """Return whether the motivational layer is denied proactive contact right now."""
    verdict = authorize(
        AuthorizeRequest(action="proactive_contact", is_proactive=True, now=now or BASE_TIME),
        projections=runtime.projections,
        config=runtime.config,
        state=runtime.state(),
        now=now or BASE_TIME,
    )
    return verdict.allowed is False


# --------------------------------------------------------------------------------------
# the round trip
# --------------------------------------------------------------------------------------


def test_a_boundary_survives_the_wipe(tmp_path: Path) -> None:
    """The product promise: a limit declared before the wipe is still in force after it."""
    config = build_config()
    config.storage.database_path = str(tmp_path / "companion.sqlite3")
    config.storage.mirror_raw_events = False
    before = Runtime(config, seed=1234, created_at=BASE_TIME)
    try:
        before.process_user_message(content=PERMANENT, timestamp=BASE_TIME)
        assert _denies_proactive(before), "the boundary must be in force before the wipe"
        payload = export_boundaries(before.projections.boundaries)
    finally:
        before.close()

    # The wipe: the whole data directory goes, and a fresh one takes its place.
    for path in tmp_path.iterdir():
        path.unlink()
    assert not list(tmp_path.iterdir()), "the wipe removed nothing to prove a point"

    after = Runtime(config, seed=1234, created_at=BASE_TIME)
    try:
        assert not _denies_proactive(after), "an empty data directory must permit contact"
        with after.db.transaction() as connection:
            result = import_boundaries(after.projections.boundaries, connection, payload)
        assert result["installed"] >= 1
        assert result["blocking_proactive"] >= 1
        assert _denies_proactive(after), "the carried-over boundary must still deny contact"
    finally:
        after.close()


def test_a_revoked_boundary_does_not_come_back_to_life(tmp_path: Path) -> None:
    """The opposite failure: an import must not re-arm a limit the user took back.

    A file that carries only "what was forbidden" and not "what was withdrawn" turns a
    restore into a new restriction. The export therefore copies the table, and the import
    preserves ``revoked_at`` - so a boundary the user revoked stays revoked, and one whose
    window has passed stays expired.
    """
    config = build_config()
    runtime = Runtime(config, seed=1234, database=Database(":memory:"), created_at=BASE_TIME)
    try:
        runtime.process_user_message(content=TEMPORAL, timestamp=BASE_TIME)
        projection = runtime.projections.boundaries
        declared = projection.list_all()
        assert declared, "the declaration produced no boundary to test with"
        with runtime.db.transaction() as conn:
            boundary_module.revoke(projection, conn, [declared[0].boundary_id], now=BASE_TIME)
        payload = export_boundaries(projection)
    finally:
        runtime.close()

    record = payload["boundaries"][0]
    assert record["revoked_at"], "the export dropped the revocation"
    restored = parse_boundary_export(payload)
    assert restored[0].is_active(BASE_TIME) is False

    # And an expired window is equally dead after a round trip.
    expired = dict(record, revoked_at=None, expires_at=(BASE_TIME - timedelta(hours=1)).isoformat())
    assert parse_boundary_export(
        {"format": BOUNDARY_EXPORT_FORMAT, "version": BOUNDARY_EXPORT_VERSION, "boundaries": [expired]}
    )[0].is_active(BASE_TIME) is False


def test_import_is_idempotent(tmp_path: Path) -> None:
    """A wipe script may be re-run after a failure; that must not duplicate or double-apply."""
    database = Database(":memory:")
    database.migrate()
    projection = Projections(database).boundaries
    runtime = _declare(build_config(), database=database)
    try:
        payload = export_boundaries(runtime.projections.boundaries)
        for _ in range(2):
            with database.transaction() as connection:
                import_boundaries(projection, connection, payload)
        ids = [boundary.boundary_id for boundary in projection.list_all(include_revoked=True)]
        assert len(ids) == len(set(ids)) == payload["count"]
    finally:
        runtime.close()


# --------------------------------------------------------------------------------------
# what the file is allowed to say
# --------------------------------------------------------------------------------------


def _envelope(*rows: dict) -> dict:
    """Build a well-formed envelope around the given rows."""
    return {
        "format": BOUNDARY_EXPORT_FORMAT,
        "version": BOUNDARY_EXPORT_VERSION,
        "boundaries": list(rows),
    }


def _row(**overrides) -> dict:
    """Build one well-formed boundary row."""
    row = {
        "boundary_id": "bnd_test",
        "type": BoundaryType.PERMANENT.value,
        "scope": "all_topics",
        "allow_reply": True,
        "allow_proactive": False,
        "starts_at": BASE_TIME.isoformat(),
        "expires_at": None,
        "revocable_by": "explicit_user_revoke",
        "source_event_id": "evt_1",
        "revoked_at": None,
        "note": "user asked not to be contacted proactively",
        "subject": None,
    }
    row.update(overrides)
    return row


#: Payloads that must be refused, one per way a file can lie about being an export.
UNTRUSTWORTHY = (
    pytest.param("not an object", id="not-json-object"),
    pytest.param({"boundaries": []}, id="no-format"),
    pytest.param(_envelope() | {"format": "something.else"}, id="wrong-format"),
    pytest.param(_envelope() | {"version": BOUNDARY_EXPORT_VERSION + 1}, id="newer-version"),
    pytest.param(_envelope() | {"version": "1"}, id="version-not-a-number"),
    pytest.param({"format": BOUNDARY_EXPORT_FORMAT, "version": 1}, id="no-rows"),
    pytest.param(_envelope("a string"), id="row-not-an-object"),
    pytest.param(_envelope(_row(boundary_id="")), id="empty-id"),
    pytest.param(_envelope(_row(type="whatever")), id="unknown-type"),
    pytest.param(_envelope(_row(allow_proactive="false")), id="string-boolean"),
)


@pytest.mark.parametrize("payload", UNTRUSTWORTHY)
def test_an_untrustworthy_export_is_refused_whole(payload: object) -> None:
    """A boundary that silently fails to install is a limit the character walks through.

    So a payload is validated *before* the first row is written, and a bad one installs
    nothing rather than the part that happened to parse.
    """
    database = Database(":memory:")
    database.migrate()
    projection = Projections(database).boundaries
    with pytest.raises(BoundaryImportError):
        parse_boundary_export(payload)
    with database.transaction() as connection:
        with pytest.raises(BoundaryImportError):
            import_boundaries(projection, connection, payload)
    assert projection.list_all(include_revoked=True) == [], "a refused import wrote rows anyway"


def test_the_export_carries_the_whole_table() -> None:
    """Including the rows that are not in force: "never declared" and "withdrawn" differ."""
    runtime = _declare(build_config())
    try:
        projection = runtime.projections.boundaries
        declared = projection.list_all()[0]
        with runtime.db.transaction() as conn:
            boundary_module.revoke(projection, conn, [declared.boundary_id], now=BASE_TIME)
        payload = export_boundaries(projection, now=BASE_TIME)
        assert payload["count"] == 1
        assert payload["active"] == 0, "a revoked boundary is not active"
        assert payload["boundaries"][0]["revoked_at"], "the revoked row was dropped from the file"
        assert payload["format"] == BOUNDARY_EXPORT_FORMAT
    finally:
        runtime.close()


def test_an_unknown_type_is_refused_rather_than_skipped() -> None:
    """Forward compatibility is not worth an unenforced limit."""
    payload = _envelope(_row(type="future_type"))
    with pytest.raises(BoundaryImportError) as error:
        parse_boundary_export(payload)
    assert "future_type" in str(error.value)


# --------------------------------------------------------------------------------------
# the operator's two commands
# --------------------------------------------------------------------------------------


def test_the_cli_round_trip(tmp_path: Path) -> None:
    """The commands an operator actually types, including the quoting-free file path.

    Driven through ``cli.main`` rather than by calling the functions, because the wiring
    is the part that goes missing: the export is useless if ``--base-dir`` resolves to a
    data directory nobody wiped.
    """
    base = tmp_path / "person"
    base.mkdir()
    database_path = base / "data" / "runtime.sqlite3"
    database_path.parent.mkdir(parents=True)
    config = build_config()
    config.storage.database_path = str(database_path)
    runtime = _declare(config, database=Database(str(database_path)))
    runtime.close()

    export_path = tmp_path / "backup" / "boundaries.json"
    assert (
        cli.main(
            ["--base-dir", str(base), "boundaries", "export", str(export_path)]
        )
        == cli.EXIT_OK
    )
    assert export_path.exists(), "the export wrote nothing"
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["count"] == 1

    # The wipe, then the import into the rebuilt (but already migrated) directory.
    database_path.unlink()
    fresh = _declare(config, content="在吗", database=Database(str(database_path)))
    assert not _denies_proactive(fresh), "a fresh directory starts with no boundaries"
    fresh.close()
    assert (
        cli.main(["--base-dir", str(base), "boundaries", "import", str(export_path)])
        == cli.EXIT_OK
    )

    reopened = Runtime(config, seed=1234, database=Database(str(database_path)), created_at=BASE_TIME)
    try:
        assert _denies_proactive(reopened), "the CLI import did not land"
    finally:
        reopened.close()


def test_the_cli_refuses_a_bad_file_without_writing(tmp_path: Path) -> None:
    """An operator gets an exit code and a reason, not a half-imported state."""
    base = tmp_path / "person"
    base.mkdir()
    source = tmp_path / "not-an-export.json"
    source.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert (
        cli.main(["--base-dir", str(base), "boundaries", "import", str(source)])
        == cli.EXIT_ERROR
    )
    assert (
        cli.main(["--base-dir", str(base), "boundaries", "import", str(source), "--dry-run"])
        == cli.EXIT_ERROR
    )
    assert (
        cli.main(["--base-dir", str(base), "boundaries", "import", str(tmp_path / "missing.json")])
        == cli.EXIT_ERROR
    )


def test_the_cli_imports_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wipe script pipes the file in, because the command runs inside a container.

    The file lives on the host next to the snapshot; the command runs in a container built
    from the same image. ``docker cp`` into a running fleet has been unreliable here, so
    the payload travels on stdin - which is also the only form that works when the fleet
    is stopped and the CLI is run from a one-off container.
    """
    base = tmp_path / "person"
    base.mkdir()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_envelope(_row()))))
    assert cli.main(["--base-dir", str(base), "boundaries", "import", "-"]) == cli.EXIT_OK
    database = Database(str(base / "data" / "runtime.sqlite3"))
    database.migrate()
    try:
        rows = Projections(database).boundaries.list_all(include_revoked=True)
    finally:
        database.close()
    assert [row.boundary_id for row in rows] == ["bnd_test"]
    assert rows[0].type == BoundaryType.PERMANENT.value
    assert rows[0].allow_proactive is False
    assert rows[0].is_active(BASE_TIME) is True


def test_the_cli_dry_run_reports_without_writing(tmp_path: Path) -> None:
    """The dry run exists so the wipe script can report what it would re-arm."""
    base = tmp_path / "person"
    base.mkdir()
    export_path = tmp_path / "boundaries.json"
    export_path.write_text(
        json.dumps(_envelope(_row(), _row(boundary_id="bnd_two"))), encoding="utf-8"
    )
    assert (
        cli.main(["--base-dir", str(base), "boundaries", "import", str(export_path), "--dry-run"])
        == cli.EXIT_OK
    )
    database = Database(str(base / "data" / "runtime.sqlite3"))
    database.migrate()
    try:
        assert Projections(database).boundaries.list_all(include_revoked=True) == []
    finally:
        database.close()


def test_an_export_of_an_empty_directory_is_a_valid_file(tmp_path: Path) -> None:
    """Most instances have nothing to carry over, and that must not be an error.

    A wipe script runs this once per person and cannot stop on the ones with no
    boundaries - and it must not be able to tell "the file is empty" from "the export
    failed", so an empty export is a documented, valid envelope with ``count: 0``.
    """
    directory = Database(":memory:")
    directory.migrate()
    try:
        payload = export_boundaries(Projections(directory).boundaries, now=utcnow())
    finally:
        directory.close()
    assert payload["count"] == 0
    assert payload["boundaries"] == []
    assert parse_boundary_export(payload) == []
