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
_CANONICAL_REQUIREMENT_RE = re.compile(r"^LC-(?:MAT|PRA|CON|HIS|PUR|VER|AGY)-[0-9]{2}$")
_LEGACY_TEST_RE = re.compile(r"^T(?:0[1-9]|[12][0-9]|3[0-2])$")


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


def _validate_legacy_aliases(value: Any, path: str, issues: list[ManifestIssue]) -> list[tuple[str, str]]:
    aliases = _sequence(value, path, issues)
    parsed: list[tuple[str, str]] = []
    if aliases is None:
        return parsed
    if not aliases:
        issues.append(ManifestIssue(path, "must contain at least one namespaced alias"))
    for index, raw in enumerate(aliases):
        alias_path = f"{path}[{index}]"
        alias = _mapping(raw, alias_path, issues)
        if alias is None:
            continue
        _required(alias, ("namespace", "id"), alias_path, issues)
        namespace, legacy_id = alias.get("namespace"), alias.get("id")
        if not isinstance(namespace, str) or not namespace.strip():
            issues.append(ManifestIssue(f"{alias_path}.namespace", "must be non-empty; bare Txx aliases are forbidden"))
        if not isinstance(legacy_id, str) or not _LEGACY_TEST_RE.fullmatch(legacy_id):
            issues.append(ManifestIssue(f"{alias_path}.id", "must be T01 through T32"))
        if isinstance(namespace, str) and namespace.strip() and isinstance(legacy_id, str):
            parsed.append((namespace, legacy_id))
    if len(parsed) != len(set(parsed)):
        issues.append(ManifestIssue(path, "must not contain duplicate namespaced aliases"))
    return parsed


def _load_requirement_registry(
    root: Mapping[str, Any], artifact_root: Path | None, issues: list[ManifestIssue]
) -> Mapping[str, Any] | None:
    record = _mapping(root.get("requirement_registry"), "$.requirement_registry", issues)
    if record is None:
        return None
    _required(record, ("path", "sha256"), "$.requirement_registry", issues)
    path, expected = record.get("path"), record.get("sha256")
    if not _safe_relative_path(path):
        issues.append(ManifestIssue("$.requirement_registry.path", "must be a safe repository-relative path"))
        return None
    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
        issues.append(ManifestIssue("$.requirement_registry.sha256", "must be a lowercase SHA-256 digest"))
        return None
    if artifact_root is None:
        return None
    candidate = artifact_root / str(path)
    try:
        raw = candidate.read_bytes()
        registry = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issues.append(ManifestIssue("$.requirement_registry.path", f"cannot load registry: {exc}"))
        return None
    if sha256_hex(raw) != expected:
        issues.append(ManifestIssue("$.requirement_registry.sha256", "requirement registry hash mismatch"))
    return registry if isinstance(registry, Mapping) else None


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
        "id", "canonical_requirement_id", "legacy_aliases", "title", "requirement_families", "verification_kind", "applicable_stage",
        "fixture", "oracle", "evidence", "positive_control", "status", "blocker",
        "denominator", "tolerance", "seed", "timeout", "privacy",
    )
    _required(test, required, path, issues)
    test_id = test.get("id")
    if test_id not in EXPECTED_TEST_IDS:
        issues.append(ManifestIssue(f"{path}.id", "must be one of T01 through T32"))
        test_id = None
    canonical_id = test.get("canonical_requirement_id")
    if not isinstance(canonical_id, str) or not _CANONICAL_REQUIREMENT_RE.fullmatch(canonical_id):
        issues.append(ManifestIssue(f"{path}.canonical_requirement_id", "must be a canonical LC-<family>-NN id"))
    aliases = _validate_legacy_aliases(test.get("legacy_aliases"), f"{path}.legacy_aliases", issues)
    if test_id is not None and ("acceptance-manifest-20261002", test_id) not in aliases:
        issues.append(ManifestIssue(f"{path}.legacy_aliases", "must preserve the acceptance manifest id as a namespaced legacy alias"))
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
        "baseline", "hashes", "requirement_registry", "stages", "evidence_classes", "status_policy",
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

    registry = _load_requirement_registry(root, artifact_root, issues)
    registry_by_id: dict[str, Mapping[str, Any]] = {}
    alias_targets: dict[tuple[str, str], str] = {}
    if registry is not None:
        entries = _sequence(registry.get("entries"), "$.requirement_registry.entries", issues)
        if entries is not None:
            for index, raw in enumerate(entries):
                entry_path = f"$.requirement_registry.entries[{index}]"
                entry = _mapping(raw, entry_path, issues)
                if entry is None:
                    continue
                canonical_id = entry.get("canonical_requirement_id")
                if not isinstance(canonical_id, str) or not _CANONICAL_REQUIREMENT_RE.fullmatch(canonical_id):
                    issues.append(ManifestIssue(f"{entry_path}.canonical_requirement_id", "is invalid"))
                    continue
                if canonical_id in registry_by_id:
                    issues.append(ManifestIssue(f"{entry_path}.canonical_requirement_id", "is duplicated"))
                registry_by_id[canonical_id] = entry
                definition = entry.get("definition")
                expected_definition_hash = entry.get("definition_sha256")
                if isinstance(definition, Mapping):
                    actual_definition_hash = sha256_hex(canonical_json_bytes(definition))
                    if expected_definition_hash != actual_definition_hash:
                        issues.append(ManifestIssue(f"{entry_path}.definition_sha256", "canonical definition hash mismatch"))
                else:
                    issues.append(ManifestIssue(f"{entry_path}.definition", "must bind title, oracle and fixture hashes"))
                for alias in _validate_legacy_aliases(entry.get("legacy_aliases"), f"{entry_path}.legacy_aliases", issues):
                    previous = alias_targets.get(alias)
                    if previous is not None and previous != canonical_id:
                        issues.append(ManifestIssue(f"{entry_path}.legacy_aliases", f"semantic conflict: alias {alias[0]}:{alias[1]} maps to both {previous} and {canonical_id}"))
                    alias_targets[alias] = canonical_id

    tests = _sequence(root.get("tests"), "$.tests", issues)
    ids: list[str] = []
    canonical_ids: list[str] = []
    if tests is not None:
        for index, item in enumerate(tests):
            test_id = _validate_test(item, index, issues)
            if test_id is not None:
                ids.append(test_id)
            if isinstance(item, Mapping) and isinstance(item.get("canonical_requirement_id"), str):
                canonical_id = item["canonical_requirement_id"]
                canonical_ids.append(canonical_id)
                entry = registry_by_id.get(canonical_id)
                if registry is not None and entry is None:
                    issues.append(ManifestIssue(f"$.tests[{index}].canonical_requirement_id", "is absent from requirement registry"))
                elif entry is not None:
                    expected_definition = {
                        "title": item.get("title"),
                        "oracle_sha256": sha256_hex(canonical_json_bytes(item.get("oracle"))),
                        "fixture_sha256": item.get("fixture", {}).get("sha256") if isinstance(item.get("fixture"), Mapping) else None,
                    }
                    if entry.get("definition") != expected_definition:
                        issues.append(ManifestIssue(f"$.tests[{index}].canonical_requirement_id", "semantic conflict with registered title/oracle/fixture hash"))
        if len(tests) != 32:
            issues.append(ManifestIssue("$.tests", "must contain exactly 32 test registrations"))
        if tuple(ids) != EXPECTED_TEST_IDS:
            issues.append(ManifestIssue("$.tests", "must contain T01 through T32 exactly once in ascending order"))
        if len(canonical_ids) != len(set(canonical_ids)):
            issues.append(ManifestIssue("$.tests", "canonical requirement ids must be unique"))
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
    root_path = Path(artifact_root) if artifact_root is not None else manifest_path.resolve().parents[2]
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
