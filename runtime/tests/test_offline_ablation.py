from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from companion_runtime.offline_ablation import AblationFixtureError, audit_ablation, run_ablation

ROOT = Path(__file__).parents[1]
REPO = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures" / "offline_ablation"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_four_groups_are_complete_deterministic_and_offline() -> None:
    fixture = load("synthetic_competition.json")
    first = run_ablation(fixture)
    second = run_ablation(fixture)
    assert first == second
    assert first["offline_only"] is True
    assert first["platform_contacted"] is False
    assert first["claim_scope"] == "selection_mechanics_only_no_user_effect_claim"
    assert [group["group"] for group in first["groups"]] == ["B0", "B1", "B2", "B3"]
    for group in first["groups"]:
        assert {row["candidate_id"] for row in group["candidate_ranks"]} == {"care", "express", "blocked"}
        assert isinstance(group["defer"], bool)
        assert isinstance(group["unknown"], list)
        assert group["decision"]["status"] in {"selected", "defer"}
        assert group["versions"]["candidate_supply"] == "candidates.synthetic.v1"
        blocked = next(row for row in group["candidate_ranks"] if row["candidate_id"] == "blocked")
        assert blocked["blocked"] is True
    assert first["groups"][0]["hazard"].get("fixed_draw") is None
    assert first["groups"][0]["decision"]["reason"] == "hazard_report_only"
    assert "care.unknown" in first["groups"][2]["unknown"]


def test_ablation_definitions_freeze_required_single_factor_changes() -> None:
    result = run_ablation(load("synthetic_competition.json"))
    groups = {item["group"]: item for item in result["groups"]}
    assert groups["B0"]["engine"] == groups["B1"]["engine"] == "runtime_v2"
    assert groups["B0"]["versions"]["tempo"] != groups["B1"]["versions"]["tempo"]
    assert groups["B1"]["versions"]["tempo"] == groups["B2"]["versions"]["tempo"] == groups["B3"]["versions"]["tempo"]
    assert groups["B2"]["engine"] == groups["B3"]["engine"] == "langchao"
    # B2 uses competition_gain=0, no edges and all-one attention. B3 consumes fixture
    # attention/edges, so this synthetic positive control changes the mechanical ranking.
    b2 = [row["candidate_id"] for row in groups["B2"]["candidate_ranks"]]
    b3 = [row["candidate_id"] for row in groups["B3"]["candidate_ranks"]]
    assert b2 != b3


def test_fixed_draw_is_reproducible_and_reported() -> None:
    result = run_ablation(load("synthetic_fixed_draw.json"))
    for group in result["groups"][:2]:
        assert group["hazard"]["fixed_draw"] == 0.25
        assert group["decision"]["status"] == "selected"
        assert group["decision"]["reason"] == "fixed_draw_won"


def test_audit_detects_tamper_and_never_claims_user_effects() -> None:
    fixture = load("synthetic_competition.json")
    result = run_ablation(fixture)
    passed = audit_ablation(fixture, result)
    assert passed == {"audit_version": "offline-ablation-audit.v1", "status": "pass",
                      "fixture_sha256": result["fixture_sha256"], "issues": [],
                      "user_effects_evaluated": False}
    tampered = deepcopy(result)
    tampered["groups"][3]["candidate_ranks"].pop()
    failed = audit_ablation(fixture, tampered)
    assert failed["status"] == "fail"
    assert "B3:candidate_denominator_mismatch" in failed["issues"]
    assert failed["user_effects_evaluated"] is False


def test_fixture_rejects_noncanonical_coverage() -> None:
    fixture = load("synthetic_competition.json")
    del fixture["permissions"]["care"]
    with pytest.raises(AblationFixtureError, match="requires forecasts and permissions"):
        run_ablation(fixture)


def test_runner_and_audit_clis(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    audit_path = tmp_path / "audit.json"
    fixture_path = FIXTURES / "synthetic_competition.json"
    runner = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_offline_ablation.py"),
         str(fixture_path), "--output", str(result_path)],
        check=False, capture_output=True, text=True,
    )
    assert runner.returncode == 0, runner.stderr
    audit = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "audit_offline_ablation.py"),
         str(fixture_path), str(result_path), "--output", str(audit_path)],
        check=False, capture_output=True, text=True,
    )
    assert audit.returncode == 0, audit.stderr
    assert json.loads(audit_path.read_text(encoding="utf-8"))["status"] == "pass"
