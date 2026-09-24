"""Tests for utilities, configuration and storage fundamentals."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.config import (
    RuntimeConfig,
    configure_logging,
    load_config,
    redact,
    resolve_paths,
)
from companion_runtime.db import Database, SCHEMA_VERSION, dumps, loads, row_to_dict
from companion_runtime.eventlog import EventLog, EventQuery
from companion_runtime.typing import Actor, EventType, RawEvent, ValueProfile, new_id
from companion_runtime.utility import (
    approach,
    clamp,
    delta_seconds,
    exponential_decay,
    from_epoch,
    isoformat,
    local_now,
    logit,
    min_datetime,
    parse_datetime,
    sigmoid,
    softmax,
    softplus,
    summarize_text,
    to_epoch,
    tokenize,
    utcnow,
)


# --------------------------------------------------------------------------------------
# utility
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "low", "high", "expected"),
    [(0.5, 0.0, 1.0, 0.5), (-1.0, 0.0, 1.0, 0.0), (2.0, 0.0, 1.0, 1.0), (5, 1, 3, 3)],
)
def test_clamp(value, low, high, expected) -> None:
    """Clamping stays inside the requested bounds."""
    assert clamp(value, low, high) == expected


def test_sigmoid_and_logit_are_inverses() -> None:
    """Sigmoid and logit round-trip within floating point tolerance."""
    for value in (-3.0, -0.5, 0.0, 0.5, 3.0):
        assert logit(sigmoid(value)) == pytest.approx(value, abs=1e-9)


def test_sigmoid_is_stable_at_extremes() -> None:
    """Sigmoid saturates without overflowing."""
    assert sigmoid(1000.0) == pytest.approx(1.0)
    assert sigmoid(-1000.0) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("beta", [0.5, 1.0, 4.0])
def test_softplus_properties(beta: float) -> None:
    """Softplus is non-negative and linear for large inputs."""
    assert softplus(0.0, beta=beta) == pytest.approx(math.log(2.0) / beta)
    assert softplus(-50.0, beta=beta) >= 0.0
    assert softplus(50.0, beta=beta) == pytest.approx(50.0)


def test_softplus_rejects_non_positive_beta() -> None:
    """A non-positive beta is a programming error."""
    with pytest.raises(ValueError):
        softplus(1.0, beta=0.0)


def test_softmax_sums_to_one_and_respects_temperature() -> None:
    """Softmax output is a distribution that sharpens as temperature drops."""
    scores = [1.0, 2.0, 3.0]
    warm = softmax(scores, temperature=1.0)
    cold = softmax(scores, temperature=0.1)
    assert sum(warm) == pytest.approx(1.0)
    assert sum(cold) == pytest.approx(1.0)
    assert cold[-1] > warm[-1]


def test_softmax_handles_degenerate_input() -> None:
    """Empty and infinite inputs do not crash the decision layer."""
    assert softmax([]) == []
    uniform = softmax([0.0, 0.0, 0.0])
    assert uniform == pytest.approx([1 / 3, 1 / 3, 1 / 3])
    huge = softmax([1e9, 1e9])
    assert sum(huge) == pytest.approx(1.0)


def test_exponential_decay_half_life() -> None:
    """Half-life decay halves the value after one half-life."""
    assert exponential_decay(0.0, 100.0, half_life=100.0) == pytest.approx(0.5)
    assert exponential_decay(0.0, 0.0) == 1.0
    assert exponential_decay(1.0, -5.0) == 1.0


def test_approach_is_analytic_over_long_steps() -> None:
    """A single large step converges to the target without oscillating."""
    tau = 100.0
    assert approach(0.0, 1.0, tau, tau) == pytest.approx(1 - math.exp(-1.0))
    assert approach(0.0, 1.0, tau, 10 * tau) == pytest.approx(1.0, abs=1e-4)
    assert approach(0.3, 0.7, 0.0, 5.0) == 0.7


def test_datetime_helpers_round_trip() -> None:
    """Parsing, formatting and epoch conversion agree."""
    moment = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    assert parse_datetime(moment) == moment
    assert parse_datetime("2026-03-01T09:00:00Z") == moment
    assert parse_datetime("2026-03-01T09:00:00+00:00") == moment
    assert from_epoch(to_epoch(moment)) == moment
    assert isoformat(None) is None
    assert to_epoch(None) == 0.0


def test_parse_datetime_treats_naive_as_utc() -> None:
    """A naive timestamp is interpreted as UTC rather than local time."""
    parsed = parse_datetime("2026-03-01T09:00:00")
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_parse_datetime_rejects_garbage() -> None:
    """Unsupported formats raise instead of silently returning None."""
    with pytest.raises(ValueError):
        parse_datetime("not a date")


def test_delta_seconds_clamps_negative() -> None:
    """Time deltas never go backwards."""
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert delta_seconds(now, now + timedelta(hours=2)) == 0.0
    assert delta_seconds(now + timedelta(hours=2), now) == 7200.0
    assert delta_seconds(None, now) == 0.0


def test_min_datetime_ignores_none() -> None:
    """Only present timestamps participate in the minimum."""
    early = datetime(2026, 1, 1, tzinfo=timezone.utc)
    late = datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert min_datetime(None, late, early) == early
    assert min_datetime(None, None) is None


def test_local_now_and_utcnow_are_aware() -> None:
    """Both clocks return timezone-aware values."""
    assert utcnow().tzinfo is not None
    assert local_now(utcnow()).tzinfo is not None


def test_text_helpers() -> None:
    """Summaries collapse whitespace and tokenising splits CJK and Latin."""
    assert summarize_text("  hello   world  ") == "hello world"
    assert summarize_text("x" * 200, limit=10).endswith("…")
    tokens = tokenize("Hello 面试 world")
    assert "hello" in tokens and "world" in tokens and "面" in tokens


def test_new_id_uses_documented_prefix() -> None:
    """Identifiers carry a short, stable kind prefix."""
    assert new_id("event").startswith("evt_")
    assert new_id("attempt").startswith("att_")
    assert new_id("unknown-kind").startswith("unk_")
    assert new_id("event") != new_id("event")


# --------------------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------------------


def test_redact_masks_secret_like_keys() -> None:
    """Anything that looks like a credential is masked."""
    assert redact("api_key", "sk-secret") == "***redacted***"
    assert redact("OPENAI_API_KEY", "sk-secret") == "***redacted***"
    assert redact("access_token", "abc") == "***redacted***"
    assert redact("server", {"port": 1}) == {"port": 1}


def test_config_never_exposes_secrets_in_dump() -> None:
    """``to_dict`` output cannot leak a credential-shaped key."""
    config = RuntimeConfig()
    config.extras["api_key"] = "sk-should-not-appear"
    dumped = config.dumps()
    assert "sk-should-not-appear" not in dumped
    assert json.loads(dumped)["extras"]["api_key"] == "***redacted***"


def test_load_config_from_toml(tmp_path) -> None:
    """A TOML file overrides nested dataclass fields."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[server]\nport = 9999\n\n[drive]\ncooldown_seconds = 60.0\n", encoding="utf-8"
    )
    config = load_config(path)
    assert config.server.port == 9999
    assert config.drive.cooldown_seconds == 60.0


def test_load_config_from_json(tmp_path) -> None:
    """A JSON file is accepted as well, and unknown keys are ignored."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"runtime_id": "abc", "nonsense": 1}), encoding="utf-8")
    config = load_config(path)
    assert config.runtime_id == "abc"


def test_load_config_environment_override() -> None:
    """``CR_`` environment variables override the file and the defaults."""
    config = load_config(env={"CR_SERVER__PORT": "9111", "CR_DRIVE__COOLDOWN_SECONDS": "42"})
    assert config.server.port == 9111
    assert config.drive.cooldown_seconds == 42.0


def test_load_config_rejects_unknown_extension(tmp_path) -> None:
    """An unsupported config format fails loudly."""
    path = tmp_path / "config.yaml"
    path.write_text("a: 1", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_missing_file(tmp_path) -> None:
    """A missing config file is an error, not a silent default."""
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_resolve_paths_makes_relative_paths_absolute(tmp_path) -> None:
    """Storage paths are anchored to the requested base directory."""
    config = RuntimeConfig()
    config.storage.database_path = "data/x.sqlite3"
    resolve_paths(config, tmp_path)
    assert config.storage.database_path.startswith(str(tmp_path))


def test_value_profile_defaults_and_partial_mapping() -> None:
    """A partial value mapping keeps defaults for absent axes."""
    profile = ValueProfile.from_mapping({"autonomy": 0.1, "unknown": 9})
    assert profile.autonomy == 0.1
    assert profile.user_care == ValueProfile().user_care
    assert ValueProfile.from_mapping(None).autonomy == ValueProfile().autonomy


def test_configure_logging_is_idempotent() -> None:
    """Logging can be configured repeatedly without raising."""
    configure_logging("WARNING")
    configure_logging("INFO")


# --------------------------------------------------------------------------------------
# database + event log
# --------------------------------------------------------------------------------------


def test_migrate_creates_runtime_row_and_schema() -> None:
    """Migration is idempotent and records its version."""
    db = Database(":memory:")
    try:
        assert db.migrate() == SCHEMA_VERSION
        db.migrate()
        row = db.query_one("SELECT value FROM schema_meta WHERE key = 'schema_version'")
        assert row is not None and int(row["value"]) == SCHEMA_VERSION
    finally:
        db.close()


def test_transaction_rolls_back_on_error() -> None:
    """A raised exception inside a transaction leaves no partial write."""
    db = Database(":memory:")
    db.migrate()
    try:
        with pytest.raises(RuntimeError):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO raw_events(event_id, event_type, timestamp, actor, created_at) "
                    "VALUES('x', 't', 'now', 'user', 'now')"
                )
                raise RuntimeError("boom")
        assert db.query_one("SELECT 1 FROM raw_events WHERE event_id = 'x'") is None
    finally:
        db.close()


def test_nested_transaction_uses_savepoint() -> None:
    """An inner failure does not discard the outer commit."""
    db = Database(":memory:")
    db.migrate()
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO raw_events(event_id, event_type, timestamp, actor, created_at) "
                "VALUES('a', 't', 'now', 'user', 'now')"
            )
            with pytest.raises(RuntimeError):
                with db.transaction() as inner:
                    inner.execute(
                        "INSERT INTO raw_events(event_id, event_type, timestamp, actor, created_at) "
                        "VALUES('b', 't', 'now', 'user', 'now')"
                    )
                    raise RuntimeError("inner boom")
        assert db.query_one("SELECT 1 FROM raw_events WHERE event_id = 'a'") is not None
        assert db.query_one("SELECT 1 FROM raw_events WHERE event_id = 'b'") is None
    finally:
        db.close()


def test_post_commit_hooks_follow_the_commit_boundary() -> None:
    """A post-commit hook runs only when its transaction actually commits."""
    db = Database(":memory:")
    db.migrate()
    try:
        # Autocommit: there is nothing to wait for.
        ran: list[str] = []
        db.post_commit(lambda: ran.append("immediate"))
        assert ran == ["immediate"]

        # A rolled-back transaction drops its hooks.
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.post_commit(lambda: ran.append("rollback"))
                raise RuntimeError("boom")
        assert ran == ["immediate"]

        # Nested levels commit together, in registration order, exactly once.
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO raw_events(event_id, event_type, timestamp, actor, created_at) "
                "VALUES('a', 't', 'now', 'user', 'now')"
            )
            db.post_commit(lambda: ran.append("outer"))
            assert ran == ["immediate"], "hooks must not run before COMMIT"
            with db.transaction():
                db.post_commit(lambda: ran.append("inner"))
                assert ran == ["immediate"]
            with pytest.raises(RuntimeError):
                with db.transaction():
                    db.post_commit(lambda: ran.append("discarded"))
                    raise RuntimeError("savepoint boom")
            db.post_commit(lambda: ran.append("outer-last"))
        assert ran == ["immediate", "outer", "inner", "outer-last"]
    finally:
        db.close()


def test_nested_commit_then_sibling_rollback_keeps_hook_order() -> None:
    """A savepoint that committed keeps its hooks when a sibling rolls back.

    Each level is buffered on its own, so rolling one savepoint back discards
    only the hooks registered inside it. Hooks that already survived their own
    savepoint stay queued, in registration order, for the outermost commit.
    """
    db = Database(":memory:")
    db.migrate()
    ran: list[str] = []
    try:
        with db.transaction():
            db.post_commit(lambda: ran.append("outer"))
            with db.transaction():
                db.post_commit(lambda: ran.append("inner"))
                with db.transaction():
                    db.post_commit(lambda: ran.append("grand-inner"))
                db.post_commit(lambda: ran.append("inner-last"))
            with pytest.raises(RuntimeError):
                with db.transaction():
                    db.post_commit(lambda: ran.append("sibling"))
                    raise RuntimeError("sibling boom")
            assert ran == [], "nothing may run before the outermost COMMIT"
            db.post_commit(lambda: ran.append("outer-last"))
        assert ran == ["outer", "inner", "grand-inner", "inner-last", "outer-last"]
    finally:
        db.close()


def test_rollback_hooks_only_fire_for_a_real_rollback() -> None:
    """Release is a success: discard hooks are dropped, not fired."""
    db = Database(":memory:")
    db.migrate()
    ran: list[str] = []
    try:
        # A released savepoint hands its work to the parent, so its discard hook
        # must not run - the rows are still there.
        with db.transaction():
            with db.transaction():
                db.on_rollback(lambda: ran.append("released-level"))
                db.on_release(lambda: ran.append("released"))
            assert ran == ["released"]
        assert ran == ["released"]

        # A discarded savepoint fires only its own discard hook.
        with db.transaction():
            with db.transaction():
                db.on_rollback(lambda: ran.append("survivor"))
                db.on_release(lambda: ran.append("survivor-release"))
            assert ran == ["released", "survivor-release"]
            with pytest.raises(RuntimeError):
                with db.transaction():
                    db.on_rollback(lambda: ran.append("discarded-level"))
                    raise RuntimeError("boom")
            assert ran == ["released", "survivor-release", "discarded-level"]
        assert ran == ["released", "survivor-release", "discarded-level"]

        # An outer rollback discards every level inside it.
        with pytest.raises(RuntimeError):
            with db.transaction():
                with db.transaction():
                    db.on_rollback(lambda: ran.append("inner-of-rollback"))
                    db.on_release(lambda: ran.append("inner-release"))
                raise RuntimeError("outer boom")
        assert ran[-1] == "inner-of-rollback"
        assert "inner-release" in ran
    finally:
        db.close()


def test_post_commit_hook_failure_does_not_break_the_commit(caplog) -> None:
    """A failing hook is logged; the committed write stays committed."""

    def explode() -> None:
        raise OSError("hook failed")

    db = Database(":memory:")
    db.migrate()
    try:
        with caplog.at_level(logging.WARNING, logger="companion_runtime.db"):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO raw_events(event_id, event_type, timestamp, actor, created_at) "
                    "VALUES('kept', 't', 'now', 'user', 'now')"
                )
                db.post_commit(explode)
        assert db.query_one("SELECT 1 FROM raw_events WHERE event_id = 'kept'") is not None
        assert any("Post-commit hook failed" in record.getMessage() for record in caplog.records)
        # The connection is still usable afterwards.
        with db.transaction():
            pass
    finally:
        db.close()


def test_json_helpers_tolerate_bad_input() -> None:
    """Malformed JSON degrades to the default instead of raising."""
    assert loads(dumps({"a": 1})) == {"a": 1}
    assert loads(None, []) == []
    assert loads("", {}) == {}
    assert loads("{not json", []) == []
    assert loads(["already", "parsed"]) == ["already", "parsed"]


def test_row_to_dict_decodes_json_columns() -> None:
    """JSON columns are decoded according to the table's declaration."""
    db = Database(":memory:")
    db.migrate()
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO raw_events(event_id, event_type, timestamp, actor, metadata_json, "
                "source_event_ids, created_at) VALUES('e1', 'user_message', 't', 'user', "
                "'{\"a\":1}', '[\"x\"]', 't')"
            )
        row = db.query_one("SELECT * FROM raw_events WHERE event_id = 'e1'")
        data = row_to_dict(row, "raw_events")
        assert data is not None
        assert data["metadata_json"] == {"a": 1}
        assert data["source_event_ids"] == ["x"]
    finally:
        db.close()


def test_event_log_appends_and_reads_back() -> None:
    """Appended events round-trip with their structured payload intact."""
    db = Database(":memory:")
    db.migrate()
    log = EventLog(db)
    try:
        event = log.append(
            EventType.USER_MESSAGE,
            actor=Actor.USER,
            content="hello",
            conversation_id="c1",
            metadata={"k": "v"},
            source_event_ids=["parent"],
            timestamp=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
            runtime_version=7,
        )
        assert isinstance(event, RawEvent)
        loaded = log.get(event.event_id)
        assert loaded is not None
        assert loaded.content == "hello"
        assert loaded.metadata == {"k": "v"}
        assert loaded.source_event_ids == ["parent"]
        assert loaded.runtime_version == 7
        assert log.exists(event.event_id)
        assert not log.exists("nope")
        assert log.count() == 1
        assert log.count(EventType.USER_MESSAGE.value) == 1
    finally:
        db.close()


def test_event_log_is_append_only() -> None:
    """The event log exposes no update or delete path at all."""
    forbidden = {"update", "delete", "replace", "purge", "truncate", "edit"}
    public = {name.lower() for name in dir(EventLog) if not name.startswith("_")}
    assert not (public & forbidden)


def test_event_log_filters_and_ordering() -> None:
    """Filters, time windows and ordering behave as documented."""
    db = Database(":memory:")
    db.migrate()
    log = EventLog(db)
    base = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    try:
        for index in range(5):
            log.append(
                EventType.USER_MESSAGE if index % 2 == 0 else EventType.ASSISTANT_MESSAGE,
                actor=Actor.USER,
                content=f"m{index}",
                conversation_id="c1" if index < 4 else "c2",
                timestamp=base + timedelta(minutes=index),
            )
        conversations = log.read(EventQuery(conversation_id="c1"))
        assert len(conversations) == 4
        typed = log.read(EventQuery(event_types=[EventType.ASSISTANT_MESSAGE.value]))
        assert len(typed) == 2
        windowed = log.read(
            EventQuery(since=base + timedelta(minutes=3), until=base + timedelta(minutes=4))
        )
        assert [event.content for event in windowed] == ["m3"]
        newest = log.read(EventQuery(limit=2, newest_first=True))
        assert [event.content for event in newest] == ["m4", "m3"]
        assert [event.content for event in log.recent(2)] == ["m3", "m4"]
    finally:
        db.close()


def test_event_log_lookup_helpers() -> None:
    """The convenience readers return the newest matching events."""
    db = Database(":memory:")
    db.migrate()
    log = EventLog(db)
    base = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    try:
        log.append(EventType.USER_MESSAGE, actor=Actor.USER, content="first", timestamp=base)
        log.append(
            EventType.USER_MESSAGE,
            actor=Actor.USER,
            content="second",
            timestamp=base + timedelta(minutes=5),
        )
        last = log.last_user_message()
        assert last is not None and last.content == "second"
        assert log.last_of_types(["nope"]) is None
        many = log.get_many(["missing", last.event_id])
        assert [event.content for event in many] == ["second"]
    finally:
        db.close()


def test_event_log_jsonl_mirror(tmp_path) -> None:
    """The optional JSONL mirror receives one line per appended event."""
    db = Database(":memory:")
    db.migrate()
    mirror = tmp_path / "raw_events.jsonl"
    log = EventLog(db, mirror)
    try:
        log.append(EventType.USER_MESSAGE, actor=Actor.USER, content="mirrored")
        lines = mirror.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["content"] == "mirrored"
        assert payload["event_type"] == "user_message"
    finally:
        db.close()


def test_append_many_is_atomic() -> None:
    """Batch appends commit together."""
    db = Database(":memory:")
    db.migrate()
    log = EventLog(db)
    try:
        written = log.append_many(
            [
                {"event_type": EventType.USER_MESSAGE, "actor": Actor.USER, "content": "a"},
                {"event_type": EventType.USER_MESSAGE, "actor": Actor.USER, "content": "b"},
            ]
        )
        assert len(written) == 2
        assert log.count() == 2
    finally:
        db.close()
