#!/usr/bin/env python3
"""Replay persisted 浪潮 shadow audits in an explicitly isolated PostgreSQL copy.

The command never constructs any platform delivery client and never runs the committing
Runtime-v2 path.  It accepts DSN by environment-variable *name* so credentials do not
appear in argv or reports.  Both the PostgreSQL hostname allowlist and a byte-for-byte
marker SHA-256 are mandatory fail-closed gates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT / "runtime" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from companion_runtime.langchao_shadow_replay import (  # noqa: E402
    ReplaySafetyError,
    dsn_from_env,
    parse_as_of,
    run_replay,
)


def parser() -> argparse.ArgumentParser:
    item = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    item.add_argument("--dsn-env", required=True, help="environment variable containing PostgreSQL DSN")
    item.add_argument("--scope", action="append", required=True, help="isolated scope; repeat exactly 11 times for B2")
    item.add_argument("--as-of", required=True, help="timezone-aware ISO-8601 audit cutoff")
    item.add_argument("--report-dir", required=True)
    item.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="allowed PostgreSQL hostname; repeat as needed (or set LANGCHAO_REPLAY_ALLOWED_HOSTS)",
    )
    item.add_argument(
        "--require-isolated-marker",
        required=True,
        metavar="PATH",
        help="required isolation marker file (only existence/readability and SHA-256 are checked)",
    )
    item.add_argument(
        "--isolated-marker-sha256",
        required=True,
        help="expected lowercase SHA-256 of --require-isolated-marker bytes",
    )
    item.add_argument(
        "--expected-scope-count",
        type=int,
        default=11,
        help="hard expected scope count (default: 11 for B2 production-copy replay)",
    )
    return item


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.expected_scope_count < 1:
        raise SystemExit("--expected-scope-count must be positive")
    if len(args.scope) != args.expected_scope_count:
        raise SystemExit(
            f"refusing partial replay: expected {args.expected_scope_count} --scope values, got {len(args.scope)}"
        )
    env_hosts = [part.strip() for part in os.environ.get("LANGCHAO_REPLAY_ALLOWED_HOSTS", "").split(",")]
    allowed_hosts = [*args.allow_host, *[item for item in env_hosts if item]]
    try:
        summary = run_replay(
            dsn=dsn_from_env(args.dsn_env),
            scopes=args.scope,
            as_of=parse_as_of(args.as_of),
            report_dir=args.report_dir,
            allowed_hosts=allowed_hosts,
            marker_path=args.require_isolated_marker,
            marker_sha256=args.isolated_marker_sha256,
        )
    except (ReplaySafetyError, ValueError, RuntimeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
