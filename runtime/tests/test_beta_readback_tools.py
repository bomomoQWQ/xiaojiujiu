"""The beta read-back tools read the deep-refresh ledger correctly.

The three scripts under ``scripts/`` are how a week is read after the fact, and they
are not imported by the Runtime at all -- which is exactly why they need their own
tests: a report that silently omits a column is a report nobody notices is wrong.
The case under test is the one that started this: a person with a backlog, refresh
attempts, and settled events, where the flat-mood question is decided by numbers the
report has to print.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    """Import one tool from ``scripts/`` by path."""
    spec = importlib.util.spec_from_file_location(f"beta_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report() -> ModuleType:
    """The daily-report tool."""
    return _load("beta_daily_report")


@pytest.fixture(scope="module")
def replay() -> ModuleType:
    """The replay tool."""
    return _load("replay_session")


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    """Write rows as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


@pytest.fixture()
def export(tmp_path: Path) -> Path:
    """One person's export directory with a refresh ledger in it."""
    person = tmp_path / "run" / "people" / "default-friendmessage-20001"
    day = "2026-09-17"
    _write(
        person / "events.jsonl",
        [
            {"event_type": "user_message", "content": "在吗", "created_at": f"{day}T10:00:00+00:00"},
            {"event_type": "assistant_message", "content": "在", "created_at": f"{day}T10:00:05+00:00"},
            {"event_type": "system", "content": "context_rendered", "created_at": f"{day}T10:00:01+00:00",
             "metadata_json": {"sections": {"a": 3}, "chars": 3, "version": "9", "trigger": "llm_request"}},
        ],
    )
    _write(
        person / "decisions.jsonl",
        [
            {"decided_at": f"{day}T10:01:00+00:00", "acted": 0, "reason": "no_candidate_beats_silence",
             "hazard": 0.0, "action_probability": 0.1, "payload_json": {"outcome": {"utilities": [], "silence_utility": 0.7}}},
        ],
    )
    _write(
        person / "state_samples.jsonl",
        [
            {"sampled_at": f"{day}T09:00:00+00:00", "mood_valence": 0.0, "approach_impulse": 0.11,
             "restraint": 0.55},
            {"sampled_at": f"{day}T10:01:00+00:00", "mood_valence": 0.08, "approach_impulse": 0.14,
             "restraint": 0.61},
        ],
    )
    _write(
        person / "refresh_runs.jsonl",
        [
            {
                "ran_at": f"{day}T10:05:00+00:00",
                "ran": 1,
                "reason": "applied",
                "trigger": "unresolved_backlog",
                "operations": 3,
                "settled_events": 2,
                "degraded": 0,
                "latency_ms": 4200,
                "payload_json": {"applied": {"reinterpretation": 2, "memory": 1}, "violations": []},
            },
            {
                "ran_at": f"{day}T10:35:00+00:00",
                "ran": 0,
                "reason": "empty_suggestions",
                "trigger": "unresolved_backlog",
                "operations": 0,
                "settled_events": 0,
                "degraded": 0,
                "latency_ms": 900,
                "payload_json": {"applied": {}, "violations": []},
            },
            {
                "ran_at": f"{day}T11:05:00+00:00",
                "ran": 0,
                "reason": "invalid_json",
                "trigger": "unresolved_backlog",
                "operations": 0,
                "settled_events": 0,
                "degraded": 1,
                "latency_ms": 800,
                "payload_json": {"applied": {}, "violations": [{"kind": "reinterpretation", "reason": "missing_sources"}]},
            },
        ],
    )
    _write(
        person / "tables" / "event_semantics.jsonl",
        [{"semantic_status": "unresolved", "created_at": f"{day}T10:00:00+00:00"}],
    )
    _write(
        person / "tables" / "reappraisals.jsonl",
        [{"content": "那句是在试探", "created_at": f"{day}T10:05:00+00:00"}],
    )
    _write(person / "tables" / "memory_candidates.jsonl", [{"created_at": f"{day}T10:00:00+00:00"}])
    (person / "summary.json").write_text(
        json.dumps({"person": person.name, "user_turns": 1, "decisions": 1, "context_renders": 1}),
        encoding="utf-8",
    )
    return tmp_path / "run"


def test_report_counts_attempts_settlements_and_degradations(report: ModuleType, export: Path) -> None:
    """The ledger has to be summarised, not merely present in the files."""
    lines = report.person_report(export / "people", "2026-09-17", {})
    text = "\n".join(lines)
    assert "深刷新：3 次尝试，结算 2 条，降级 1 次" in text
    assert "'applied': 1" in text and "'empty_suggestions': 1" in text


def test_report_says_so_when_a_backlog_was_never_revisited(report: ModuleType, export: Path) -> None:
    """Zero attempts with a backlog is the flat-mood signature and must be visible."""
    for name in ("refresh_runs.jsonl",):
        (export / "people" / "default-friendmessage-20001" / name).unlink()
    lines = report.person_report(export / "people", "2026-09-17", {})
    text = "\n".join(lines)
    assert "0 次尝试" in text
    assert "没人回头读" in text


def test_replay_prints_the_refresh_ledger_and_reappraisals(replay: ModuleType, export: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Replay is where "why is the mood flat" gets answered, so both blocks print."""
    assert replay.main.__module__
    old_argv = sys.argv
    sys.argv = ["replay_session.py", "--export", str(export / "people" / "default-friendmessage-20001")]
    try:
        assert replay.main() == 0
    finally:
        sys.argv = old_argv
    out = capsys.readouterr().out
    assert "# deep refreshes: 3 · settled 2 events · 1 still unresolved" in out
    assert "ran=1 applied" in out
    assert "empty_suggestions" in out
    assert "violations=1 first=reinterpretation/missing_sources" in out
    assert "# reappraisals: 1" in out
    assert "那句是在试探" in out
