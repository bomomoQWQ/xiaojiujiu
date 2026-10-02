"""Run registered prelaunch structural scenarios and preserve fail-closed evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
DEFAULT_PLAN = Path(__file__).with_name("prelaunch_structural_scenarios_v1.json")
DEFAULT_SCHEMA = Path(__file__).with_name("prelaunch_structural_result_v1.schema.json")
DEFAULT_OUTPUT = Path(__file__).with_name("evidence") / "prelaunch_structural_latest.json"
RUNNER_PATH = Path(__file__).resolve()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=REPOSITORY_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _artifact(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {"path": _relative(path), "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def run(*, plan_path: Path, output_path: Path, timeout_seconds: int, scenario_ids: set[str] | None = None) -> dict[str, Any]:
    plan = _read_json(plan_path)
    registered = plan.get("scenarios")
    if not isinstance(registered, list):
        raise ValueError("plan.scenarios must be a list")
    expected_ids = tuple(plan.get("scenario_ids", ()))
    actual_ids = tuple(item.get("canonical_requirement_id") for item in registered)
    if actual_ids != expected_ids or len(set(actual_ids)) != len(actual_ids):
        raise ValueError("plan scenarios must occur exactly once in canonical scenario_ids order")

    selected = [item for item in registered if scenario_ids is None or item["canonical_requirement_id"] in scenario_ids]
    unknown = set() if scenario_ids is None else scenario_ids - set(actual_ids)
    if unknown:
        raise ValueError(f"unknown scenario ids: {sorted(unknown)}")

    results: list[dict[str, Any]] = []
    for scenario in selected:
        nodes = scenario["pytest_nodes"]
        blocker = scenario["blocked_reason"]
        if bool(nodes) == bool(blocker):
            raise ValueError(f"{scenario['id']} must be executable xor blocked")
        started = time.monotonic()
        if blocker:
            result = {
                "id": scenario["id"], "canonical_requirement_id": scenario["canonical_requirement_id"],
                "legacy_alias": scenario["legacy_alias"], "title": scenario["title"], "status": "blocked",
                "component": scenario["component"], "fault_injection": scenario["fault_injection"],
                "pytest_nodes": [], "returncode": None, "duration_seconds": 0.0,
                "stdout": "", "stderr": "", "artifacts": [_artifact(plan_path)],
                "blocker": blocker, "claim_limit": scenario["claim_limit"],
            }
        else:
            command = [sys.executable, "-m", "pytest", "-q", *nodes]
            env = dict(os.environ)
            env["PYTHONPATH"] = str(RUNTIME_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
            try:
                completed = subprocess.run(
                    command, cwd=RUNTIME_ROOT, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout_seconds,
                    env=env, check=False,
                )
                returncode: int | None = completed.returncode
                stdout, stderr = completed.stdout, completed.stderr
                status = "passed_limited" if completed.returncode == 0 else "failed"
                run_blocker = None
            except subprocess.TimeoutExpired as exc:
                returncode = None
                stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
                stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
                stderr += f"\nscenario timed out after {timeout_seconds}s"
                status, run_blocker = "failed", None
            result = {
                "id": scenario["id"], "canonical_requirement_id": scenario["canonical_requirement_id"],
                "legacy_alias": scenario["legacy_alias"], "title": scenario["title"], "status": status,
                "component": scenario["component"], "fault_injection": scenario["fault_injection"],
                "pytest_nodes": nodes, "returncode": returncode,
                "duration_seconds": round(time.monotonic() - started, 6),
                "stdout": stdout, "stderr": stderr,
                "artifacts": [_artifact(plan_path), _artifact(RUNTIME_ROOT / "tests" / "test_prelaunch_structural_scenarios.py")],
                "blocker": run_blocker, "claim_limit": scenario["claim_limit"],
            }
        results.append(result)

    passed = sum(item["status"] == "passed_limited" for item in results)
    failed = sum(item["status"] == "failed" for item in results)
    blocked = sum(item["status"] == "blocked" for item in results)
    overall = "failed" if failed else "blocked" if blocked and passed == 0 else "passed_limited"
    commit = _git("rev-parse", "HEAD")
    dirty_text = _git("status", "--porcelain")
    command = [sys.executable, _relative(RUNNER_PATH), "--plan", _relative(plan_path), "--output", _relative(output_path)]
    payload = {
        "schema_version": "langchao.prelaunch-structural-result.v1",
        "run_id": f"prelaunch-structural-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository_root": ".",
        "baseline": {"commit": commit, "dirty": None if dirty_text is None else bool(dirty_text)},
        "runner": {
            "path": _relative(RUNNER_PATH), "python": sys.version.split()[0],
            "command": command, "timeout_seconds": timeout_seconds,
        },
        "summary": {
            "total": len(results), "passed": passed, "failed": failed,
            "blocked": blocked, "status": overall,
        },
        "results": results,
        "claim_limits": [
            "T evidence is isolated structural execution only; it is not authorized real-runtime evidence (R).",
            "Blocked cases remain blocked and are not counted as passes.",
            "No semantic correctness or production-launch authorization is inferred.",
        ],
    }
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    if jsonschema is not None:
        jsonschema.Draft202012Validator(_read_json(DEFAULT_SCHEMA)).validate(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--scenario", action="append", dest="scenarios")
    args = parser.parse_args(argv)
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    plan_path = args.plan if args.plan.is_absolute() else REPOSITORY_ROOT / args.plan
    output_path = args.output if args.output.is_absolute() else REPOSITORY_ROOT / args.output
    result = run(
        plan_path=plan_path.resolve(), output_path=output_path.resolve(),
        timeout_seconds=args.timeout_seconds,
        scenario_ids=None if args.scenarios is None else set(args.scenarios),
    )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
