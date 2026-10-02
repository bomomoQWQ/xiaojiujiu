"""Tests for the fail-closed Langchao acceptance preregistration validator."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from companion_runtime.langchao_acceptance_manifest import (
    AcceptanceManifestError,
    EXPECTED_TEST_IDS,
    canonical_json_bytes,
    load_and_validate_manifest,
    main,
    sha256_hex,
    validate_manifest,
)

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
MANIFEST_PATH = RUNTIME_ROOT / "audit" / "langchao_prelaunch_acceptance_20261002.json"
SCHEMA_PATH = RUNTIME_ROOT / "audit" / "langchao_acceptance_manifest_v1.schema.json"


def manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_preregistered_manifest_is_valid_and_covers_t01_through_t32() -> None:
    payload = load_and_validate_manifest(MANIFEST_PATH)
    assert tuple(item["id"] for item in payload["tests"]) == EXPECTED_TEST_IDS
    assert {item["status"] for item in payload["tests"]} <= {"planned", "not_applicable"}
    assert payload["release_decision"] == "hold_not_authorized"
    assert payload["baseline"]["database_schema_version"] == 20


def test_every_registration_freezes_all_required_execution_dimensions() -> None:
    payload = manifest()
    required = {
        "verification_kind", "applicable_stage", "fixture", "oracle", "evidence",
        "positive_control", "status", "blocker", "denominator", "tolerance",
        "seed", "timeout", "privacy",
    }
    for test in payload["tests"]:  # type: ignore[index]
        assert required <= test.keys()
        assert test["positive_control"]["fixture_delta"]
        assert test["denominator"]["planned_count"] > 0
        assert test["denominator"]["zero_exposure_label"] == "no_evaluable_exposure"
        assert test["timeout"]["on_timeout"] == "fail_closed_and_preserve_partial_evidence"
        assert test["privacy"]["direct_identifiers"] == "prohibited"
        assert test["tolerance"]["zero_tolerance_invariants"]


def test_fixture_hashes_are_canonical_and_configuration_hash_is_bound() -> None:
    payload = manifest()
    for test in payload["tests"]:  # type: ignore[index]
        fixture = test["fixture"]
        assert fixture["sha256"] == sha256_hex(canonical_json_bytes(fixture["canonical_input"]))
    config = payload["global_protocol"]["frozen_configuration"]  # type: ignore[index]
    assert payload["hashes"]["configuration"]["value"] == sha256_hex(canonical_json_bytes(config))  # type: ignore[index]


def test_schema_hash_matches_checked_in_schema_bytes() -> None:
    payload = manifest()
    assert payload["hashes"]["acceptance_schema"]["value"] == sha256_hex(SCHEMA_PATH.read_bytes())  # type: ignore[index]
    assert validate_manifest(payload, artifact_root=REPOSITORY_ROOT, check_artifacts=True) == ()


def test_validator_rejects_any_initial_passing_claim() -> None:
    payload = manifest()
    payload["tests"][0]["status"] = "passed_limited"  # type: ignore[index]
    issues = validate_manifest(payload)
    assert any(issue.path == "$.tests[0].status" and "planned or not_applicable" in issue.message for issue in issues)


def test_validator_rejects_missing_duplicate_or_out_of_order_test_ids() -> None:
    payload = manifest()
    payload["tests"][4]["id"] = "T04"  # type: ignore[index]
    issues = validate_manifest(payload)
    assert any(issue.path == "$.tests" and "exactly once in ascending order" in issue.message for issue in issues)


def test_validator_rejects_fixture_and_config_hash_drift() -> None:
    payload = manifest()
    payload["tests"][0]["fixture"]["canonical_input"]["timeline_seconds"].append(999)  # type: ignore[index]
    payload["global_protocol"]["frozen_configuration"]["decision_budget_seconds"] = 301.0  # type: ignore[index]
    issues = validate_manifest(payload)
    assert any("fixture.sha256" in issue.path and "mismatch" in issue.message for issue in issues)
    assert any(issue.path == "$.hashes.configuration.value" for issue in issues)


def test_semantic_and_effect_tests_cannot_omit_required_evidence() -> None:
    payload = manifest()
    semantic = next(item for item in payload["tests"] if item["id"] == "T22")  # type: ignore[index]
    semantic["oracle"]["semantic_review"] = None
    semantic["evidence"]["required_classes"].remove("S")
    effect = next(item for item in payload["tests"] if item["id"] == "T31")  # type: ignore[index]
    effect["evidence"]["required_classes"].remove("R")
    issues = validate_manifest(payload)
    messages = "\n".join(map(str, issues))
    assert "semantic verification requires a review protocol" in messages
    assert "semantic-bearing tests require S evidence" in messages
    assert "effect tests require R evidence" in messages


def test_not_applicable_requires_reason_and_unauthorized_real_data_retains_nothing() -> None:
    payload = manifest()
    effect = next(item for item in payload["tests"] if item["id"] == "T31")  # type: ignore[index]
    effect["blocker"] = None
    effect["privacy"]["retention_days"] = 30
    issues = validate_manifest(payload)
    assert any(issue.path.endswith(".blocker") and "concrete" in issue.message for issue in issues)
    assert any(issue.path.endswith(".privacy.retention_days") and "zero days" in issue.message for issue in issues)


def test_seed_and_timeout_rules_fail_closed() -> None:
    payload = manifest()
    payload["tests"][12]["seed"]["required"] = True  # type: ignore[index]
    payload["tests"][12]["seed"]["values"] = []  # type: ignore[index]
    payload["tests"][12]["timeout"]["per_case_seconds"] = 5000  # type: ignore[index]
    payload["tests"][12]["timeout"]["suite_seconds"] = 100  # type: ignore[index]
    issues = validate_manifest(payload)
    assert any(issue.path.endswith(".seed.values") for issue in issues)
    assert any(issue.path.endswith(".timeout") and "must not exceed" in issue.message for issue in issues)


def test_load_raises_aggregate_error_without_mutating_input(tmp_path: Path) -> None:
    payload = manifest()
    original = copy.deepcopy(payload)
    payload["release_decision"] = "approved"
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AcceptanceManifestError) as exc_info:
        load_and_validate_manifest(invalid)
    assert "hold_not_authorized" in str(exc_info.value)
    assert original["release_decision"] == "hold_not_authorized"


def test_cli_reports_valid_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(MANIFEST_PATH)]) == 0
    assert "32 planned registrations" in capsys.readouterr().out
