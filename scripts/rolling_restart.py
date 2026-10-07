"""Roll one Runtime scope at a time without interrupting the whole service.

Each scope is frozen, restarted and verified on its own: freeze its dispatch authority,
assert its dispatch state is drained, restart only that child through the fleet control
surface, wait for a new healthy pid, assert the schema migrated and that nothing was queued
while frozen, then restore authority. A failure leaves that scope fail-closed and stops the
rollout, so a later scope is never touched by a half-verified change.

AstrBot, NapCat and the fleet supervisor are never restarted; ordinary replies keep
working throughout, and only the scope being restarted is briefly unreachable (the plugin
fails open).

**This performs a real rollout and changes what every scope may dispatch**, so it refuses
to run without ``--confirm``. Without it you get a dry-run of exactly what it would do.

Run it inside the fleet container, which owns the Runtime package and the credentials:

    docker exec -i xxj-runtime-fleet python /fleet/rolling_restart.py            # dry run
    docker exec -i xxj-runtime-fleet python /fleet/rolling_restart.py --confirm  # roll

Use ``--leave shadow`` (or ``freeze``) to finish the rollout without handing dispatch
authority back, and :mod:`scripts.dispatch_authority` when you only need to stop or start
sending.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import psycopg

from companion_runtime.langchao_authority import AuthorityEngine, AuthorityMode
from companion_runtime.langchao_authority_repository import LangchaoAuthorityRepository
import fleet_roster
from dispatch_authority import schema_for, slugify

#: Routing and control surface of the fleet supervisor.
CONTROL_URL = "http://127.0.0.1:8800"

#: How long a restarted child may take to answer its health probe.
HEALTH_TIMEOUT_SECONDS = 120.0

#: The schema version the new code is expected to have migrated each scope to. Set to
#: ``None`` to skip the assertion when rolling out a change that carries no migration.
EXPECTED_SCHEMA_VERSION: int | None = 26


def _get(path: str) -> dict:
    """Return one decoded JSON document from the fleet control surface.

    Args:
        path: Absolute control path, e.g. ``/fleet/status``.

    Returns:
        The decoded body.
    """
    with urllib.request.urlopen(CONTROL_URL + path, timeout=15) as response:
        return json.load(response)


def _post(path: str) -> dict:
    """POST an empty JSON body to the fleet control surface.

    Args:
        path: Absolute control path, e.g. ``/fleet/restart/<slug>``.

    Returns:
        The decoded body.
    """
    request = urllib.request.Request(
        CONTROL_URL + path, data=b"{}", headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _people() -> dict[str, dict]:
    """Return the fleet's per-person status keyed by session."""
    return {item["session"]: item for item in _get("/fleet/status")["people"]}


def _drained(connection, schema: str) -> dict[str, int]:
    """Return the queued and in-flight counts that must be zero before a restart.

    Args:
        connection: Open PostgreSQL connection.
        schema: The scope's schema name.

    Returns:
        ``{"outbox": n, "attempts": n}``.
    """
    connection.execute(f'SET search_path TO "{schema}", public')
    row = connection.execute(
        """SELECT
             (SELECT count(*) FROM outbox WHERE status IN ('pending','leased')) AS outbox,
             (SELECT count(*) FROM action_attempts
               WHERE state IN ('proposed','committed','rendering','ready_to_send')) AS attempts"""
    ).fetchone()
    return {key: int(value) for key, value in row.items()}


def _schema_version(connection, schema: str) -> int:
    """Return the highest applied migration version for one scope."""
    connection.execute(f'SET search_path TO "{schema}", public')
    row = connection.execute("SELECT max(version) AS v FROM schema_migrations_v2").fetchone()
    return int(row["v"])


def _wait_healthy(session: str, old_pid: int) -> dict:
    """Block until ``session`` serves health from a new pid.

    Args:
        session: The unified session origin.
        old_pid: The pid observed before the restart, which must change.

    Returns:
        The person's status entry once healthy.

    Raises:
        RuntimeError: If the child did not come back healthy in time.
    """
    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        match = _people().get(session)
        if (
            match
            and match.get("running")
            and match.get("health") == "ok"
            and int(match.get("pid", 0)) != old_pid
        ):
            return match
        time.sleep(1.0)
    raise RuntimeError(
        f"{session} did not become healthy with a new pid within "
        f"{HEALTH_TIMEOUT_SECONDS:.0f}s"
    )


#: What each scope is left as once it has rolled successfully.
FINAL_AUTHORITY: dict[str, tuple[AuthorityEngine, AuthorityMode]] = {
    "live": (AuthorityEngine.LANGCHAO, AuthorityMode.LIVE),
    "shadow": (AuthorityEngine.LANGCHAO, AuthorityMode.SHADOW),
    "freeze": (AuthorityEngine.NONE, AuthorityMode.DISABLED),
}


def main() -> int:
    """Roll every scope (with ``--confirm``) or describe what that would do.

    Returns:
        ``0`` on a completed rollout or a dry run, ``1`` when the rollout stopped early.
    """
    parser = argparse.ArgumentParser(description="Roll one Runtime scope at a time.")
    parser.add_argument(
        "--confirm", action="store_true",
        help="actually perform the rollout; without it this only prints the plan",
    )
    parser.add_argument(
        "--leave", choices=sorted(FINAL_AUTHORITY), default="live",
        help="authority each scope is left with after a successful restart (default: live)",
    )
    parser.add_argument(
        "--scopes", default="",
        help="comma-separated scopes; defaults to this deployment's roster",
    )
    args = parser.parse_args()

    # The scopes come from the roster, not from a tuple in the code: a deployment's people
    # are operator data, and a rollout must never be aimed at another deployment's list.
    scopes = fleet_roster.resolve_scopes(args.scopes)

    if not args.confirm:
        print(f"dry run: would roll {len(scopes)} scopes, leaving each as "
              f"{args.leave!r} (dispatch "
              f"{'enabled' if args.leave == 'live' else 'disabled'})")
        print("per scope: freeze -> assert drained -> restart -> health -> schema check "
              "-> assert drained -> set " + args.leave)
        print("\npass --confirm to perform it")
        return 0

    print(f"rolling {len(scopes)} scopes"
          + ("" if EXPECTED_SCHEMA_VERSION is None
             else f" to schema v{EXPECTED_SCHEMA_VERSION}")
          + f", leaving each as {args.leave!r}")
    connection = psycopg.connect(
        os.environ["CR_STORAGE__DSN"], autocommit=True, row_factory=psycopg.rows.dict_row
    )
    try:
        for index, session in enumerate(scopes, start=1):
            people = _people()
            current = people.get(session)
            if current is None:
                print(f"[{index}/{len(scopes)}] {session}: not in the fleet; stopping")
                return 1
            schema = schema_for(session)
            state = _drained(connection, schema)
            if state["outbox"] or state["attempts"]:
                print(f"[{index}/{len(scopes)}] {session}: dispatch state is not drained "
                      f"{state}; stopping the rollout")
                return 1
            repository = LangchaoAuthorityRepository(connection, scope_key=session)
            active = repository.get_active()
            if active is None:
                print(f"[{index}/{len(scopes)}] {session}: no active authority; stopping")
                return 1
            repository.switch_authority(
                engine_key=AuthorityEngine.NONE, mode=AuthorityMode.DISABLED,
                expected_pointer_version=int(active["pointer_version"]),
                in_flight_count=0, reason="rolling restart: freeze",
                payload={"operation": "rolling_restart", "phase": "freeze"},
            )
            frozen_pointer = int(repository.get_active()["pointer_version"])
            print(f"[{index}/{len(scopes)}] {session}: frozen at pointer {frozen_pointer} "
                  f"(old pid {current['pid']}); restarting", flush=True)

            _post("/fleet/restart/" + slugify(session))
            healthy = _wait_healthy(session, int(current["pid"]))

            version = _schema_version(connection, schema)
            if EXPECTED_SCHEMA_VERSION is not None and version != EXPECTED_SCHEMA_VERSION:
                print(f"[{index}/{len(scopes)}] {session}: schema is v{version}, expected "
                      f"v{EXPECTED_SCHEMA_VERSION}; leaving it disabled")
                return 1
            state = _drained(connection, schema)
            if state["outbox"] or state["attempts"]:
                print(f"[{index}/{len(scopes)}] {session}: acquired work while frozen "
                      f"{state}; leaving it disabled")
                return 1
            repository.switch_authority(
                engine_key=FINAL_AUTHORITY[args.leave][0],
                mode=FINAL_AUTHORITY[args.leave][1],
                expected_pointer_version=frozen_pointer, in_flight_count=0,
                reason=f"rolling restart: leave as {args.leave}",
                payload={"operation": "rolling_restart", "phase": args.leave},
            )
            published = repository.get_active()
            print(f"[{index}/{len(scopes)}] {session}: new pid {healthy['pid']}, "
                  f"schema v{version}, authority "
                  f"{published['engine_key']}/{published['mode']} "
                  f"pointer {published['pointer_version']}", flush=True)
    finally:
        connection.close()
    print(f"\nROLLING_RESTART_COMPLETE {len(scopes)}/{len(scopes)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.URLError, RuntimeError, psycopg.Error) as error:
        print(f"ROLLING_RESTART_FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
