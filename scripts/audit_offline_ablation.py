#!/usr/bin/env python3
"""Audit an offline B0--B3 result against its exact canonical fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "runtime" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from companion_runtime.offline_ablation import audit_ablation  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit offline ablation completeness and isolation")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--output", "-o", type=Path)
    args = parser.parse_args(argv)
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        result = json.loads(args.result.read_text(encoding="utf-8"))
        audit = audit_ablation(fixture, result)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        parser.exit(2, f"offline ablation audit failed: {exc}\n")
    payload = json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(args.output)
    else:
        sys.stdout.write(payload)
    return 0 if audit["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
