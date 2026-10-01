"""The v2 scheduler round must also run the one unattended memory writer.

Production ``serve`` replaced ``Runtime.endogenous_round`` with the v2 coordinator.
Consolidation's only other callers are the protocol ``memory_summary`` path (needs a
semantic provider) and the operator CLI, so measured live: every user had
``memories = 0`` while rule-based candidates sat ``pending`` forever.
"""
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "companion_runtime"


def test_v2_round_calls_consolidation_after_the_decision():
    cli = (SRC / "cli.py").read_text(encoding="utf-8")
    round_body = cli.split("def run_v2_round", 1)[1].split("def simulate_v2_decision", 1)[0]
    assert "runtime.consolidate_memories_if_due(now=now)" in round_body
    assert round_body.index("decide_endogenous") < round_body.index(
        "consolidate_memories_if_due"
    )
    # It must follow the maintenance pass too, so the decision it cannot change is
    # already committed.
    assert round_body.index("v2_maintenance.run_due") < round_body.index(
        "consolidate_memories_if_due"
    )


def test_runtime_exposes_consolidation_independently_of_the_legacy_round():
    runtime = (SRC / "runtime.py").read_text(encoding="utf-8")
    body = runtime.split("def consolidate_memories_if_due", 1)[1].split(
        "def _consolidate_if_due", 1
    )[0]
    assert "_consolidate_if_due(now=stamp)" in body
    # The public wrapper must not re-introduce a legacy-only gate: the whole point is
    # that consolidation runs while the legacy endogenous round is refused.
    assert "legacy_endogenous_enabled" not in body
