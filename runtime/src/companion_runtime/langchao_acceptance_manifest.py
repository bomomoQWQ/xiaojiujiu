"""Fail-closed validation for preregistered Langchao acceptance manifests.

The JSON Schema is the portable shape contract.  This module adds invariants that
JSON Schema cannot express succinctly: exact T01--T32 coverage, canonical fixture
hashes, coherent evidence/stage/status rules, and optional verification of artifacts
that are available in the local checkout.  Validation never executes a test and never
promotes a status to a passing state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

MANIFEST_VERSION = "langchao.acceptance-manifest.v1"
INITIAL_STATUSES = frozenset({"planned", "not_applicable"})
EVIDENCE_CLASSES = frozenset({"D", "C", "T", "R", "S", "I"})
VERIFICATION_KINDS = frozenset({"mechanical", "numerical", "semantic", "mixed", "effect"})
STAGES = frozenset(
    {
        "design_review",
        "isolated_unit",
        "isolated_postgres",
        "shadow",
        "authorized_canary",
        "authorized_effect_study",
    }
)
EXPECTED_TEST_IDS = tuple(f"T{number:02d}" for number in range(1, 33))
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True, slots=True, order=True)
class ManifestIssue:
    """One deterministic validation failure."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class AcceptanceManifestError(ValueError):
    """Raised when a manifest fails validation."""

    def __init__(self, issues: Sequence[ManifestIssue]) -> None:
        self.issues = tuple(sorted(issues))
        super().__init__("invalid Langchao acceptance manifest:\n" + "\n".join(map(str, self.issues)))


def canonical_json_bytes(value: Any) -> bytes:
    """Return the repository's documented deterministic JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mapping(value: Any, path: str, issues: list[ManifestIssue]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        issues.append(ManifestIssue(path, "must be an object"))
        return None
    return value


def _sequence(value: Any, path: str, issues: list[ManifestIssue]) -> list[Any] | None:
    if not isinstance(value, list):
        issues.append(ManifestIssue(path, "must be an array"))
        return None
    return value


def _required(obj: Mapping[str, Any], names: Sequence[str], path: str, issues: list[ManifestIssue]) -> None:
    for name in names:
        if name not in obj:
            issues.append(ManifestIssue(f"{path}.{name}", "is required"))


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ":" not in path.parts[0] and ".." not in path.parts


def _validate_hash_record(
    record: Any,
    path: str,
    issues: list[ManifestIssue],
    *,
    artifact_root: Path | None,
    check_artifacts: bool,
) -> None:
    item = _mapping(record, path, issues)
    if item is None:
        return
    _required(item, ("algorithm", "canonicalization", "artifact", "value"), path, issues)
    if item.get("algorithm") != "sha256":
        issues.append(ManifestIssue(f"{path}.algorithm", "must equal sha256"))
    value = item.get("value")
    canonicalization = item.get("canonicalization")
    if canonicalization == "git_object_id":
        if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
            issues.append(ManifestIssue(f"{path}.value", "must be a 40- or 64-hex object id"))
    elif not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        issues.append(ManifestIssue(f"{path}.value", "must be a lowercase SHA-256 digest"))
    if canonicalization not in {"raw_bytes", "canonical_json_rfc8785_subset", "git_object_id"}:
        issues.append(ManifestIssue(f"{path}.canonicalization", "is unsupported"))

    artifact = item.get("artifact")
    if not check_artifacts or artifact_root is None or not isinstance(artifact, str):
        return
    if canonicalization != "raw_bytes" or artifact.startswith("ordered-concatenation:"):
        return
    if not _safe_relative_path(artifact):
        issues.append(ManifestIssue(f"{path}.artifact", "must be a safe repository-relative path"))
        return
    candidate = artifact_root / artifact
    if not candidate.is_file():
        # Attachments named in an external review packet need not be copied into the repo.
        if not artifact.startswith("attachment/"):
            issues.append(ManifestIssue(f"{path}.artifact", "artifact is not present"))
        return
    actual = sha256_hex(candidate.read_bytes())
    if actual != value:
        issues.append(ManifestIssue(f"{path}.value", f"does not match artifact bytes ({actual})"))


def _validate_fixture(item: Mapping[str, Any], path: str, issues: list[ManifestIssue]) -> None:
    fixture = _mapping(item.get("fixture"), f"{path}.fixture", issues)
    if fixture is None:
        return
    _required(fixture, ("id", "kind", "canonical_input", "sha256"), f"{path}.fixture", issues)
    digest = fixture.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        issues.append(ManifestIssue(f"{path}.fixture.sha256", "must be a lowercase SHA-256 digest"))
        return
    if "canonical_input" in fixture:
        try:
            actual = sha256_hex(canonical_json_bytes(fixture["canonical_input"]))
        except (TypeError, ValueError) as exc:
            issues.append(ManifestIssue(f"{path}.fixture.canonical_input", f"is not canonical JSON: {exc}"))
        else:
            if actual != digest:
                issues.append(ManifestIssue(f"{path}.fixture.sha256", f"canonical fixture hash mismatch ({actual})"))


def _validate_test(item: Any, index: int, issues: list[ManifestIssue]) -> str | None:
    path = f"$.tests[{index}]"
    test = _mapping(item, path, issues)
    if test is None:
        return None
    required = (
        "id", "title", "requirement_families", "verification_kind", "applicable_stage",
        "fixture", "oracle", "evidence", "positive_control", "status", "blocker",
        "denominator", "tolerance", "seed", "timeout", "privacy",
    )
    _required(test, required, path, issues)
    test_id = test.get("id")
    if test_id not in EXPECTED_TEST_IDS:
        issues.append(ManifestIssue(f"{path}.id", "must be one of T01 through T32"))
        test_id = None
    status = test.get("status")
    if status not in INITIAL_STATUSES:
        issues.append(ManifestIssue(f"{path}.status", "initial manifest status must be planned or not_applicable"))
    blocker = test.get("blocker")
    if status == "not_applicable" and (not isinstance(blocker, str) or not blocker.strip()):
        issues.append(ManifestIssue(f"{path}.blocker", "not_applicable requires a concrete blocker/reason"))
    if blocker is not None and (not isinstance(blocker, str) or not blocker.strip()):
        issues.append(ManifestIssue(f"{path}.blocker", "must be null or a non-empty string"))

    kind = test.get("verification_kind")
    if kind not in VERIFICATION_KINDS:
        issues.append(ManifestIssue(f"{path}.verification_kind", "is unsupported"))
    stages = _sequence(test.get("applicable_stage"), f"{path}.applicable_stage", issues)
    if stages is not None:
        if not stages:
            issues.append(ManifestIssue(f"{path}.applicable_stage", "must not be empty"))
        if len(stages) != len(set(map(str, stages))):
            issues.append(ManifestIssue(f"{path}.applicable_stage", "must not contain duplicates"))
        for stage in stages:
            if stage not in STAGES:
                issues.append(ManifestIssue(f"{path}.applicable_stage", f"unsupported stage {stage!r}"))
        if kind == "effect" and "authorized_effect_study" not in stages:
            issues.append(ManifestIssue(f"{path}.applicable_stage", "effect tests require authorized_effect_study"))

    _validate_fixture(test, path, issues)
    oracle = _mapping(test.get("oracle"), f"{path}.oracle", issues)
    semantic_review: Any = None
    if oracle is not None:
        _required(oracle, ("mechanical_assertions", "allowed_outcomes", "semantic_review"), f"{path}.oracle", issues)
        assertions = _sequence(oracle.get("mechanical_assertions"), f"{path}.oracle.mechanical_assertions", issues)
        outcomes = _sequence(oracle.get("allowed_outcomes"), f"{path}.oracle.allowed_outcomes", issues)
        semantic_review = oracle.get("semantic_review")
        if assertions is not None and not assertions:
            issues.append(ManifestIssue(f"{path}.oracle.mechanical_assertions", "must not be empty"))
        if outcomes is not None and not outcomes:
            issues.append(ManifestIssue(f"{path}.oracle.allowed_outcomes", "must not be empty"))
    if kind in {"semantic", "mixed", "effect"} and not isinstance(semantic_review, Mapping):
        issues.append(ManifestIssue(f"{path}.oracle.semantic_review", f"{kind} verification requires a review protocol"))
    if kind in {"mechanical", "numerical"} and semantic_review is not None:
        issues.append(ManifestIssue(f"{path}.oracle.semantic_review", f"{kind} verification must use null"))

    evidence = _mapping(test.get("evidence"), f"{path}.evidence", issues)
    if evidence is not None:
        classes = _sequence(evidence.get("required_classes"), f"{path}.evidence.required_classes", issues)
        if classes is not None:
            invalid = [value for value in classes if value not in EVIDENCE_CLASSES]
            if invalid:
                issues.append(ManifestIssue(f"{path}.evidence.required_classes", f"contains unsupported classes {invalid!r}"))
            if kind in {"semantic", "mixed", "effect"} and "S" not in classes:
                issues.append(ManifestIssue(f"{path}.evidence.required_classes", "semantic-bearing tests require S evidence"))
            if kind == "effect" and "R" not in classes:
                issues.append(ManifestIssue(f"{path}.evidence.required_classes", "effect tests require R evidence"))
        artifacts = _sequence(evidence.get("artifacts"), f"{path}.evidence.artifacts", issues)
        if artifacts is not None:
            for artifact in artifacts:
                if not _safe_relative_path(artifact):
                    issues.append(ManifestIssue(f"{path}.evidence.artifacts", f"unsafe artifact path {artifact!r}"))

    control = _mapping(test.get("positive_control"), f"{path}.positive_control", issues)
    if control is not None:
        delta = control.get("fixture_delta")
        if not isinstance(delta, Mapping) or not delta:
            issues.append(ManifestIssue(f"{path}.positive_control.fixture_delta", "must be a non-empty object"))
        capability = control.get("expected_capability")
        if not isinstance(capability, str) or not capability.strip():
            issues.append(ManifestIssue(f"{path}.positive_control.expected_capability", "must be non-empty"))

    denominator = _mapping(test.get("denominator"), f"{path}.denominator", issues)
    if denominator is not None:
        count = denominator.get("planned_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            issues.append(ManifestIssue(f"{path}.denominator.planned_count", "must be a positive integer"))
        if denominator.get("zero_exposure_label") != "no_evaluable_exposure":
            issues.append(ManifestIssue(f"{path}.denominator.zero_exposure_label", "must prevent zero-exposure from being reported as 0%"))

    tolerance = _mapping(test.get("tolerance"), f"{path}.tolerance", issues)
    if tolerance is not None:
        zero = _sequence(tolerance.get("zero_tolerance_invariants"), f"{path}.tolerance.zero_tolerance_invariants", issues)
        if zero is not None and not zero:
            issues.append(ManifestIssue(f"{path}.tolerance.zero_tolerance_invariants", "must not be empty"))
        if kind == "numerical" and tolerance.get("absolute") is None and tolerance.get("relative") is None:
            issues.append(ManifestIssue(f"{path}.tolerance", "numerical tests require an absolute or relative tolerance"))

    seed = _mapping(test.get("seed"), f"{path}.seed", issues)
    if seed is not None:
        values = _sequence(seed.get("values"), f"{path}.seed.values", issues)
        required = seed.get("required")
        if not isinstance(required, bool):
            issues.append(ManifestIssue(f"{path}.seed.required", "must be boolean"))
        elif values is not None and required != bool(values):
            issues.append(ManifestIssue(f"{path}.seed.values", "must be non-empty exactly when seed.required is true"))

    timeout = _mapping(test.get("timeout"), f"{path}.timeout", issues)
    if timeout is not None:
        per_case, suite = timeout.get("per_case_seconds"), timeout.get("suite_seconds")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (per_case, suite)):
            issues.append(ManifestIssue(f"{path}.timeout", "per-case and suite timeouts must be positive integers"))
        elif per_case > suite:
            issues.append(ManifestIssue(f"{path}.timeout", "per-case timeout must not exceed suite timeout"))
        if timeout.get("on_timeout") != "fail_closed_and_preserve_partial_evidence":
            issues.append(ManifestIssue(f"{path}.timeout.on_timeout", "must fail closed"))

    privacy = _mapping(test.get("privacy"), f"{path}.privacy", issues)
    if privacy is not None:
        if privacy.get("direct_identifiers") != "prohibited":
            issues.append(ManifestIssue(f"{path}.privacy.direct_identifiers", "must be prohibited"))
        retention = privacy.get("retention_days")
        if isinstance(retention, bool) or not isinstance(retention, int) or not 0 <= retention <= 365:
            issues.append(ManifestIssue(f"{path}.privacy.retention_days", "must be an integer from 0 to 365"))
        if status == "not_applicable" and test_id == "T31" and retention != 0:
            issues.append(ManifestIssue(f"{path}.privacy.retention_days", "an unauthorized real-data fixture must retain zero days"))
    return test_id


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
    check_artifacts: bool = False,
) -> tuple[ManifestIssue, ...]:
    """Return every detected issue without mutating the supplied manifest."""

    issues: list[ManifestIssue] = []
    root = _mapping(manifest, "$", issues)
    if root is None:
        return tuple(issues)
    required = (
        "manifest_version", "manifest_id", "generated_at", "purpose", "release_decision",
        "baseline", "hashes", "stages", "evidence_classes", "status_policy",
        "global_protocol", "tests",
    )
    _required(root, required, "$", issues)
    if root.get("manifest_version") != MANIFEST_VERSION:
        issues.append(ManifestIssue("$.manifest_version", f"must equal {MANIFEST_VERSION}"))
    if root.get("release_decision") != "hold_not_authorized":
        issues.append(ManifestIssue("$.release_decision", "a preregistration must remain hold_not_authorized"))

    baseline = _mapping(root.get("baseline"), "$.baseline", issues)
    if baseline is not None:
        if baseline.get("database_schema_version") != 20:
            issues.append(ManifestIssue("$.baseline.database_schema_version", "must equal current v20"))
        commit = baseline.get("commit")
        if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
            issues.append(ManifestIssue("$.baseline.commit", "must be a full 40- or 64-hex commit id"))

    stages = _sequence(root.get("stages"), "$.stages", issues)
    if stages is not None and set(stages) != STAGES:
        issues.append(ManifestIssue("$.stages", "must enumerate every supported stage exactly once"))

    policy = _mapping(root.get("status_policy"), "$.status_policy", issues)
    if policy is not None:
        if policy.get("initial_allowed") != ["planned", "not_applicable"]:
            issues.append(ManifestIssue("$.status_policy.initial_allowed", "must freeze initial statuses to planned/not_applicable"))
        if policy.get("passing_status_requires_separate_result_artifact") is not True:
            issues.append(ManifestIssue("$.status_policy.passing_status_requires_separate_result_artifact", "must be true"))

    hashes = _mapping(root.get("hashes"), "$.hashes", issues)
    if hashes is not None:
        required_hashes = ("commit", "acceptance_schema", "database_schema", "configuration", "review_standard")
        _required(hashes, required_hashes, "$.hashes", issues)
        for name in required_hashes:
            if name in hashes:
                _validate_hash_record(
                    hashes[name], f"$.hashes.{name}", issues,
                    artifact_root=artifact_root, check_artifacts=check_artifacts,
                )
        if baseline is not None:
            commit_hash = hashes.get("commit")
            if isinstance(commit_hash, Mapping) and commit_hash.get("value") != baseline.get("commit"):
                issues.append(ManifestIssue("$.hashes.commit.value", "must equal baseline.commit"))
        configuration = hashes.get("configuration")
        protocol = root.get("global_protocol")
        if isinstance(configuration, Mapping) and isinstance(protocol, Mapping):
            frozen = protocol.get("frozen_configuration")
            try:
                actual = sha256_hex(canonical_json_bytes(frozen))
            except (TypeError, ValueError) as exc:
                issues.append(ManifestIssue("$.global_protocol.frozen_configuration", f"is not canonical JSON: {exc}"))
            else:
                if configuration.get("value") != actual:
                    issues.append(ManifestIssue("$.hashes.configuration.value", f"does not match frozen configuration ({actual})"))

    tests = _sequence(root.get("tests"), "$.tests", issues)
    ids: list[str] = []
    if tests is not None:
        for index, item in enumerate(tests):
            test_id = _validate_test(item, index, issues)
            if test_id is not None:
                ids.append(test_id)
        if len(tests) != 32:
            issues.append(ManifestIssue("$.tests", "must contain exactly 32 test registrations"))
        if tuple(ids) != EXPECTED_TEST_IDS:
            issues.append(ManifestIssue("$.tests", "must contain T01 through T32 exactly once in ascending order"))
    return tuple(sorted(issues))


def load_and_validate_manifest(
    path: str | Path,
    *,
    artifact_root: str | Path | None = None,
    check_artifacts: bool = False,
) -> dict[str, Any]:
    """Load JSON, validate it, and raise :class:`AcceptanceManifestError` on failure."""

    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceManifestError((ManifestIssue("$", f"cannot load JSON: {exc}"),)) from exc
    if not isinstance(payload, dict):
        raise AcceptanceManifestError((ManifestIssue("$", "must be an object"),))
    root_path = Path(artifact_root) if artifact_root is not None else None
    issues = validate_manifest(payload, artifact_root=root_path, check_artifacts=check_artifacts)
    if issues:
        raise AcceptanceManifestError(issues)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Langchao acceptance preregistration")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--check-artifacts", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = load_and_validate_manifest(
            args.manifest,
            artifact_root=args.artifact_root,
            check_artifacts=args.check_artifacts,
        )
    except AcceptanceManifestError as exc:
        parser.exit(1, f"{exc}\n")
    print(f"valid {payload['manifest_id']} ({len(payload['tests'])} planned registrations)")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main tests
    raise SystemExit(main())
