"""Tests for the rolling log, the JSONL trace and the program-log bridge."""

from __future__ import annotations

import json
import logging

import pytest

from cf.logbook import Logbook, LogBridge


class TestLogbook:
    """The trace is the framework's primary evidence, so its shape is asserted."""

    def test_writes_both_sinks(self, tmp_path) -> None:
        """One event lands in the rolling log and in the trace."""
        book = Logbook(tmp_path, echo=False)
        book.event("heartbeat", {"virtual_now": "2026-09-15T09:00:00Z", "pressure": 0.5})
        book.close()
        assert (tmp_path / "framework.log").exists()
        records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [record["kind"] for record in records] == ["run", "heartbeat"]
        heartbeat = records[-1]
        assert heartbeat["pressure"] == 0.5
        assert heartbeat["virtual_now"] == "2026-09-15T09:00:00Z"
        assert heartbeat["seq"] == 2

    def test_payload_cannot_rename_the_record(self, tmp_path) -> None:
        """A payload key called ``kind`` must not overwrite the routing field.

        This is a regression test. The mock endpoint's payload carries the prompt
        kind, and an earlier version spread it *after* the record's own fields,
        so those records appeared as ``kind="deep_refresh"`` and vanished from
        ``cf tail --kind mock_openai_call``.
        """
        book = Logbook(tmp_path, echo=False)
        book.event("mock_openai_call", {"kind": "deep_refresh", "seq": 999})
        book.close()
        record = book.read_trace()[-1]
        assert record["kind"] == "mock_openai_call"
        assert record["seq"] == 2
        assert record["shadowed_keys"] == ["kind", "seq"]

    def test_identical_virtual_now_is_not_a_collision(self, tmp_path) -> None:
        """``event`` lifts ``virtual_now`` from the payload, so the same value is not shadowing."""
        book = Logbook(tmp_path, echo=False)
        book.event("heartbeat", {"virtual_now": "2026-09-15T09:00:00Z"})
        book.close()
        record = book.read_trace()[-1]
        assert record["virtual_now"] == "2026-09-15T09:00:00Z"
        assert "shadowed_keys" not in record

    def test_round_trip_preserves_unicode(self, tmp_path) -> None:
        """Chinese text survives the trip through JSON."""
        book = Logbook(tmp_path, echo=False)
        book.event("say", {"text": "最近有点累，也不知道该怎么说。"})
        book.close()
        assert book.read_trace()[-1]["text"] == "最近有点累，也不知道该怎么说。"

    def test_read_trace_filters_and_limits(self, tmp_path) -> None:
        """``kinds`` and ``limit`` select what a test wants to look at."""
        book = Logbook(tmp_path, echo=False)
        for index in range(5):
            book.event("heartbeat", {"index": index})
            book.event("other", {"index": index})
        book.close()
        assert len(book.read_trace(kinds=("heartbeat",))) == 5
        assert [record["index"] for record in book.read_trace(kinds=("heartbeat",), limit=2)] == [3, 4]

    def test_malformed_line_is_skipped(self, tmp_path) -> None:
        """A half-written final line is normal after a kill and must not raise."""
        book = Logbook(tmp_path, echo=False)
        book.event("heartbeat", {"index": 0})
        book.close()
        with (tmp_path / "trace.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "truncated", "seq": ')
        assert len(book.read_trace()) == 2

    def test_rotation_bounds_the_log(self, tmp_path) -> None:
        """The rolling log rotates instead of growing without bound."""
        book = Logbook(tmp_path, echo=False, max_bytes=2048, backups=2)
        for index in range(500):
            book.event("heartbeat", {"index": index, "padding": "x" * 100})
        book.close()
        rotated = sorted(path.name for path in tmp_path.glob("framework.log*"))
        assert len(rotated) <= 3, rotated
        assert (tmp_path / "framework.log").stat().st_size <= 2048 * 2

    def test_close_is_idempotent(self, tmp_path) -> None:
        """Closing twice is allowed."""
        book = Logbook(tmp_path, echo=False)
        book.close()
        book.close()

    def test_unserialisable_payload_still_produces_a_line(self, tmp_path) -> None:
        """Objects that JSON cannot encode are stringified rather than crashing.

        A trace that dies mid-run is worse than one carrying ``str(obj)``: the
        point of the file is to still be there after something went wrong.
        """
        book = Logbook(tmp_path, echo=False)
        book.event("odd", {"when": object()})
        book.close()
        assert "object object at" in json.dumps(book.read_trace()[-1])


class TestLogBridge:
    """The program's own log lines belong in the run's log."""

    def test_forwards_program_records(self, tmp_path) -> None:
        """A record written to the program's logger appears in the trace."""
        book = Logbook(tmp_path, echo=False)
        logger = logging.getLogger("cf_bridge_probe")
        logger.propagate = False
        bridge = LogBridge(book, logger_names=("cf_bridge_probe",), minimum_level=logging.INFO)
        try:
            logger.warning("lazy_tick called with a time earlier than the creation epoch")
        finally:
            bridge.detach()
            book.close()
        records = book.read_trace(kinds=("program_log",))
        assert len(records) == 1
        assert records[0]["level"] == "WARNING"
        assert "earlier than the creation epoch" in records[0]["program_message"]

    def test_detach_stops_forwarding(self, tmp_path) -> None:
        """After detaching, nothing more is captured."""
        book = Logbook(tmp_path, echo=False)
        logger = logging.getLogger("cf_bridge_probe_two")
        logger.propagate = False
        bridge = LogBridge(book, logger_names=("cf_bridge_probe_two",), minimum_level=logging.INFO)
        bridge.detach()
        logger.warning("after detach")
        book.close()
        assert book.read_trace(kinds=("program_log",)) == []

    def test_below_minimum_level_is_ignored(self, tmp_path) -> None:
        """DEBUG chatter does not fill the log."""
        book = Logbook(tmp_path, echo=False)
        logger = logging.getLogger("cf_bridge_probe_three")
        logger.propagate = False
        bridge = LogBridge(book, logger_names=("cf_bridge_probe_three",), minimum_level=logging.INFO)
        try:
            logger.debug("noise")
        finally:
            bridge.detach()
            book.close()
        assert book.read_trace(kinds=("program_log",)) == []
