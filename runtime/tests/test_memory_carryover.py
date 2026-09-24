"""A hand-picked memory may outlive a wipe - and must not pretend the wipe did not happen.

The first beta ended with a data wipe: two and a half months of misclassified memories were
worse than none, so nothing was restored wholesale. But one person's own words are not a
misclassification. She gave her name; she was in the middle of a real decision about
someone in her life; and her last messages before the wipe were about the update itself -
"你明天就更新了 / 会失忆", then "没什么了 / 谢谢你 / 晚安".

So a *few* facts may be carried into the rebuilt directory, and these tests cover the three
properties that make that safe:

* they land where they can be used (working set, prompt), not merely in a table;
* they say where they came from - a row that reads as something the Runtime heard *here*
  would be a lie the consistency checks cannot see;
* a file that cannot be trusted installs nothing at all, because a memory that installs
  wrongly is something the character will assert about the user's life.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from companion_runtime import cli
from companion_runtime import context as context_module
from companion_runtime import memory as memory_module
from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.projections import Projections
from companion_runtime.typing import MemoryKind, MemoryStatus

from conftest import BASE_TIME, build_config

#: One carried fact, written the way the operator writes it: her own words, a kind, a key
#: so a re-run replaces instead of duplicating, and a note saying where it came from.
AMAN = {
    "key": "qq04-name",
    "kind": MemoryKind.STABLE_KNOWLEDGE.value,
    "summary": "用户希望被称呼为「Aneam」或「阿曼」。",
    "topics": ["称呼", "名字"],
    "importance": 0.7,
    "confidence": 0.6,
    "activation": 0.9,
    "note": "她 2026-09-17 自己说的；原话：你可以称呼我Aneam或者阿曼",
    "source_event_ids": ["evt_1b2d76cbddde466c837bd8e1ab16fbef"],
}


def _envelope(*rows: object) -> dict:
    """Build a well-formed carryover around the given rows (they need not be valid)."""
    return {
        "format": memory_module.MEMORY_CARRYOVER_FORMAT,
        "version": memory_module.MEMORY_CARRYOVER_VERSION,
        "memories": [dict(row) if isinstance(row, dict) else row for row in rows],
    }


def _fresh() -> tuple[Database, Projections]:
    """Return a migrated in-memory database and its projections."""
    database = Database(":memory:")
    database.migrate()
    return database, Projections(database)


def test_a_carried_memory_reaches_the_prompt() -> None:
    """The product promise: she can still be called by the name she gave.

    Asserted through ``select_memories`` rather than by reading the table back, because a
    memory that exists and never reaches a prompt has not been carried over at all.
    """
    database, projections = _fresh()
    try:
        with database.transaction() as connection:
            result = memory_module.import_memories(
                projections.memory, connection, _envelope(AMAN), config=RuntimeConfig()
            )
        assert result["installed"] == 1
        assert result["kinds"] == {MemoryKind.STABLE_KNOWLEDGE.value: 1}

        stored = projections.memory.list_memories(limit=10)
        assert [memory.memory_id for memory in stored] == result["ids"]
        assert stored[0].kind == MemoryKind.STABLE_KNOWLEDGE.value
        assert stored[0].summary == AMAN["summary"]
        assert stored[0].importance == pytest.approx(0.7)

        selected = context_module.select_memories(projections, limit=4)
        assert [item["memory_id"] for item in selected] == result["ids"], (
            "a carried memory must be in the working set"
        )
    finally:
        database.close()


def test_a_carried_memory_says_it_came_from_outside_this_directory() -> None:
    """Provenance, not wording: nothing here may read as something the user said *here*.

    The events live in the archived directory, so ``source_event_ids`` has to stay empty -
    a memory claiming an event this directory never received is a false record the checks
    cannot detect - and the identifiers are kept under ``structured["carryover"]`` instead.
    """
    database, projections = _fresh()
    try:
        with database.transaction() as connection:
            memory_module.import_memories(
                projections.memory, connection, _envelope(AMAN), config=RuntimeConfig()
            )
        memory = projections.memory.list_memories(limit=10)[0]
        assert memory.structured["proposed_by"] == memory_module.PROVENANCE_OPERATOR_CARRYOVER
        assert memory.source_event_ids == []
        carried = memory.structured[memory_module.CARRYOVER_KEY]
        assert carried["key"] == AMAN["key"]
        assert carried["note"] == AMAN["note"]
        assert carried["source_event_ids"] == AMAN["source_event_ids"]
        # And it is in the activation pool, with the provenance as the reason.
        activation = projections.memory.list_activated(limit=10)
        assert [item.memory_id for item in activation] == [memory.memory_id]
        assert activation[0].reason == memory_module.PROVENANCE_OPERATOR_CARRYOVER
    finally:
        database.close()


def test_the_same_key_makes_the_import_idempotent() -> None:
    """A wipe script may be re-run after a failure; that must not duplicate a memory."""
    database, projections = _fresh()
    try:
        with database.transaction() as connection:
            first = memory_module.import_memories(
                projections.memory, connection, _envelope(AMAN), config=RuntimeConfig()
            )
        with database.transaction() as connection:
            second = memory_module.import_memories(
                projections.memory, connection, _envelope(AMAN), config=RuntimeConfig()
            )
        assert first["replaced"] == 0
        assert second["replaced"] == 1
        assert second["ids"] == first["ids"]
        assert len(projections.memory.list_memories(limit=10)) == 1
        assert len(projections.memory.list_activated(limit=10)) == 1
    finally:
        database.close()


def test_without_a_key_every_run_adds_a_memory() -> None:
    """The other half of the contract: the key is what makes a file re-runnable."""
    database, projections = _fresh()
    row = {key: value for key, value in AMAN.items() if key != "key"}
    try:
        for _ in range(2):
            with database.transaction() as connection:
                memory_module.import_memories(
                    projections.memory, connection, _envelope(row), config=RuntimeConfig()
                )
        assert len(projections.memory.list_memories(limit=10)) == 2
    finally:
        database.close()


def test_defaults_come_from_the_kind() -> None:
    """An author may leave importance and activation out; the Runtime's own rules fill in.

    The activation default is deliberately the consolidation seed, so a carried fact is
    exactly as present in the working set as one the Runtime formed itself.
    """
    database, projections = _fresh()
    config = RuntimeConfig()
    row = {
        "kind": MemoryKind.RELATIONSHIP.value,
        "summary": "她正在纠结要不要继续对现实里的那个人付出感情，这个选择她还没有答案。",
    }
    try:
        with database.transaction() as connection:
            memory_module.import_memories(
                projections.memory, connection, _envelope(row), config=config
            )
        memory = projections.memory.list_memories(limit=10)[0]
        expected = memory_module.kind_importance(MemoryKind.RELATIONSHIP.value, config)
        assert memory.importance == pytest.approx(expected)
        assert memory.confidence == pytest.approx(0.55)
        activation = projections.memory.list_activated(limit=10)[0]
        assert activation.activation == pytest.approx(0.35 + 0.5 * expected)
    finally:
        database.close()


def _row(**overrides) -> dict:
    """Build one well-formed carryover row."""
    row = dict(AMAN)
    row.update(overrides)
    return row


#: Payloads that must be refused, one per way a file can lie about being a carryover.
UNTRUSTWORTHY = (
    pytest.param("not an object", id="not-json-object"),
    pytest.param({"memories": []}, id="no-format"),
    pytest.param(_envelope() | {"format": "something.else"}, id="wrong-format"),
    pytest.param(
        _envelope() | {"version": memory_module.MEMORY_CARRYOVER_VERSION + 1}, id="newer-version"
    ),
    pytest.param({"format": memory_module.MEMORY_CARRYOVER_FORMAT, "version": 1}, id="no-rows"),
    pytest.param(_envelope("a string"), id="row-not-an-object"),
    pytest.param(_envelope(_row(summary="")), id="empty-summary"),
    pytest.param(_envelope(_row(summary=None)), id="missing-summary"),
    pytest.param(_envelope(_row(kind="vibes")), id="unknown-kind"),
    pytest.param(_envelope(_row(key="")), id="empty-key"),
    pytest.param(_envelope(_row(importance=1.4)), id="importance-out-of-range"),
    pytest.param(_envelope(_row(confidence="high")), id="confidence-not-a-number"),
    pytest.param(_envelope(_row(activation=2)), id="activation-out-of-range"),
    pytest.param(_envelope(_row(topics="称呼")), id="topics-not-a-list"),
)


@pytest.mark.parametrize("payload", UNTRUSTWORTHY)
def test_an_untrustworthy_carryover_is_refused_whole(payload: object) -> None:
    """Refused *whole*: the good rows of a bad file are not installed either.

    The rows are validated before the first write, because half a carryover is worse than
    none - the operator would have to diff the file against the database to find out what
    is missing, and the missing memory is one she silently does not have.
    """
    database, projections = _fresh()
    try:
        with pytest.raises(memory_module.MemoryCarryoverError):
            memory_module.parse_memory_carryover(payload)
        with database.transaction() as connection:
            with pytest.raises(memory_module.MemoryCarryoverError):
                memory_module.import_memories(
                    projections.memory, connection, payload, config=RuntimeConfig()
                )
        assert projections.memory.list_memories(limit=10) == []
        assert projections.memory.list_activated(limit=10) == []
    finally:
        database.close()


def test_an_optional_field_may_be_left_out_or_null() -> None:
    """The file is written by hand: what has a default may be omitted, and null means it.

    ``activation: null`` is not the same as omitting it only in intent - both mean "let the
    Runtime's own seed decide", which is the point of allowing null at all.
    """
    database, projections = _fresh()
    config = RuntimeConfig()
    minimal = {
        "kind": MemoryKind.USER_PREFERENCE.value,
        "summary": "她拿图来考过识别：炎拳、银镜伊织。",
        "importance": None,
        "activation": None,
    }
    try:
        with database.transaction() as connection:
            result = memory_module.import_memories(
                projections.memory, connection, _envelope(minimal), config=config
            )
        assert result["installed"] == 1
        memory = projections.memory.list_memories(limit=10)[0]
        assert memory.importance == pytest.approx(
            memory_module.kind_importance(MemoryKind.USER_PREFERENCE.value, config)
        )
        assert memory.topics == []
        assert memory.structured[memory_module.CARRYOVER_KEY]["key"] is None
    finally:
        database.close()


# --------------------------------------------------------------------------------------
# the command an operator types
# --------------------------------------------------------------------------------------


def test_the_cli_imports_from_stdin_and_leaves_an_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A memory that arrived from outside the conversation is a fact about the directory.

    The event log is append-only and is where such facts belong, so the import writes one
    ``system`` event naming the command and the identifiers - never a fabricated
    ``user_message``, which would rewrite what the person said.
    """
    base = tmp_path / "person"
    base.mkdir()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_envelope(AMAN))))
    assert cli.main(["--base-dir", str(base), "memories", "import", "-"]) == cli.EXIT_OK

    database = Database(str(base / "data" / "runtime.sqlite3"))
    database.migrate()
    try:
        projections = Projections(database)
        memories = projections.memory.list_memories(limit=10)
        assert [memory.summary for memory in memories] == [AMAN["summary"]]
        rows = [dict(row) for row in database.query(
            "SELECT * FROM raw_events WHERE event_type = 'system' ORDER BY timestamp"
        )]
    finally:
        database.close()
    assert len(rows) == 1, "the import left no audit trail"
    assert rows[0]["actor"] == "runtime"
    assert rows[0]["content"] == "operator_carryover:1"
    metadata = json.loads(rows[0]["metadata_json"])
    assert metadata["command"] == "memories import"
    assert metadata["memory_ids"] == [memories[0].memory_id]
    assert rows[0]["source_event_ids"] == "[]"


def test_the_cli_dry_run_reports_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry run exists so an operator can read the list before it becomes her memory."""
    base = tmp_path / "person"
    base.mkdir()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_envelope(AMAN))))
    assert cli.main(["--base-dir", str(base), "memories", "import", "-", "--dry-run"]) == cli.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["would_install"] == 1
    assert printed["summaries"] == [AMAN["summary"]]
    database = Database(str(base / "data" / "runtime.sqlite3"))
    database.migrate()
    try:
        projections = Projections(database)
        assert projections.memory.list_memories(limit=10) == []
        assert projections.memory.list_activated(limit=10) == []
        count = database.query_one("SELECT count(*) AS n FROM raw_events")["n"]
    finally:
        database.close()
    assert count == 0, "a dry run wrote history"


def test_the_cli_refuses_a_bad_file(tmp_path: Path) -> None:
    """An operator gets an exit code and a reason, not a half-installed carryover."""
    base = tmp_path / "person"
    base.mkdir()
    source = tmp_path / "not-a-carryover.json"
    source.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert cli.main(["--base-dir", str(base), "memories", "import", str(source)]) == cli.EXIT_ERROR
    assert (
        cli.main(["--base-dir", str(base), "memories", "import", str(source), "--dry-run"])
        == cli.EXIT_ERROR
    )
    assert (
        cli.main(["--base-dir", str(base), "memories", "import", str(tmp_path / "gone.json")])
        == cli.EXIT_ERROR
    )
    database = Database(str(base / "data" / "runtime.sqlite3"))
    database.migrate()
    try:
        assert Projections(database).memory.list_memories(limit=10) == []
    finally:
        database.close()


def test_a_carryover_file_touches_only_the_rows_it_names() -> None:
    """It is a carryover list, not a restore: everything else the Runtime decided stands.

    The risk this pins down is a future "import memories" that grows into "put the archive
    back" - which would also put back the reason for the wipe. A row the file does not name
    keeps whatever status the Runtime gave it, archived included.
    """
    database, projections = _fresh()
    other = {
        "kind": MemoryKind.STABLE_KNOWLEDGE.value,
        "summary": "她随口提过的一句闲话。",
    }
    try:
        with database.transaction() as connection:
            memory_module.import_memories(
                projections.memory, connection, _envelope(AMAN), config=RuntimeConfig()
            )
            aman_id = projections.memory.list_memories(limit=10)[0].memory_id
            projections.memory.set_memory_status(
                connection, aman_id, MemoryStatus.ARCHIVED.value
            )
        assert context_module.select_memories(projections, limit=4) == []

        with database.transaction() as connection:
            memory_module.import_memories(
                projections.memory, connection, _envelope(other), config=RuntimeConfig()
            )
        assert projections.memory.get_memory(aman_id).status == MemoryStatus.ARCHIVED.value
        selected = context_module.select_memories(projections, limit=4)
        assert len(selected) == 1, selected
        assert "闲话" in selected[0]["summary"], "the file's own row is the one that landed"

        # Re-running the file *does* re-install the row it owns, content and presence
        # alike - the operator asked for it a second time, and the archive is not what is
        # being restored. Anything the file never named is untouched above.
        with database.transaction() as connection:
            memory_module.import_memories(
                projections.memory, connection, _envelope(AMAN), config=RuntimeConfig()
            )
        assert projections.memory.get_memory(aman_id).status == MemoryStatus.ACTIVE.value
    finally:
        database.close()
