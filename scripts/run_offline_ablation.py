#!/usr/bin/env python3
"""Run the deterministic B0--B3 selection ablation from canonical JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "runtime" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from companion_runtime.offline_ablation import AblationFixtureError, run_ablation  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline-only B0-B3 deterministic selection runner; never sends"
    )
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", "-o", type=Path, help="result JSON (default: stdout)")
    args = parser.parse_args(argv)
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        result = run_ablation(fixture)
    except (OSError, UnicodeError, json.JSONDecodeError, AblationFixtureError) as exc:
        parser.exit(2, f"offline ablation failed: {exc}\n")
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(payload)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
