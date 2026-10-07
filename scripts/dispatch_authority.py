"""Freeze, shadow, or restore 「浪潮」 dispatch authority for every scope.

Dispatch authority is a persisted CAS pointer, not a configuration switch, so this is the
operator's stop/start control for proactive messages. None of the three actions touches
AstrBot, NapCat or the fleet supervisor, and none of them restarts a Runtime: the change
is visible to the very next round.

    freeze   stop proactive sends (engine none/disabled); ordinary replies are unaffected
    shadow   keep evaluating and rebuilding contracts, but dispatch nothing
    restore  hand dispatch authority back to 「浪潮」 live
    show     print each scope's current authority and pointer version

Run it inside the fleet container, which owns the Runtime package and the database
credentials:

    docker exec -i xxj-runtime-fleet python /fleet/dispatch_authority.py show
    docker exec -i xxj-runtime-fleet python /fleet/dispatch_authority.py freeze "operator: stop"

Why a script rather than a config change: authority lives in the database and freezes
in-flight disposition, so the switch has to assert that in-flight work is drained rather
than silently drop somebody's unfinished action.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

import fleet_naming
import fleet_roster
import psycopg

from companion_runtime.langchao_authority import AuthorityEngine, AuthorityMode
from companion_runtime.langchao_authority_repository import LangchaoAuthorityRepository

#: Which engine and mode each action publishes. The scope list is *not* here: a deployment's
#: scopes are operator data, resolved from the roster at run time (:mod:`fleet_roster`) so
#: adding a person never means editing committed code.
ACTIONS: dict[str, tuple[AuthorityEngine, AuthorityMode]] = {
    # No engine may dispatch; the round still assesses purely and records a router no-send.
    "freeze": (AuthorityEngine.NONE, AuthorityMode.DISABLED),
    # 「浪潮」 keeps rebuilding contracts and state (so a later switch to live is already
    # consistent) but ``may_dispatch`` is false, so it can never send.
    "shadow": (AuthorityEngine.LANGCHAO, AuthorityMode.SHADOW),
    "restore": (AuthorityEngine.LANGCHAO, AuthorityMode.LIVE),
}


def slugify(session: str) -> str:
    """Return the fleet supervisor's slug for one session.

    Args:
        session: The unified session origin, e.g. ``default:FriendMessage:1``.

    Returns:
        The filesystem-safe slug the supervisor uses for this person.
    """
    return fleet_naming.slugify(session)


def schema_for(session: str) -> str:
    """Return the schema the supervisor gives this session.

    Delegates to the supervisor's own definition rather than repeating it: two copies of
    this rule drift, and two different answers to "where are this scope's rows" is how an
    operator ends up inspecting an empty schema and concluding the character never acts.

    Args:
        session: The unified session origin.

    Returns:
        The PostgreSQL schema name holding this scope's rows.
    """
    return fleet_naming.schema_for_session(session)


def main() -> int:
    """Apply one action to every scope and report each result.

    Returns:
        ``0`` when every scope succeeded, ``1`` when any scope failed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=[*ACTIONS, "show"], nargs="?", default="show")
    parser.add_argument("reason", nargs="?", default="operator action")
    parser.add_argument(
        "--scopes", default="",
        help="comma-separated scopes; defaults to this deployment's roster",
    )
    args = parser.parse_args()

    scopes = fleet_roster.resolve_scopes(args.scopes)

    connection = psycopg.connect(
        os.environ["CR_STORAGE__DSN"], autocommit=True, row_factory=psycopg.rows.dict_row
    )
    failures = 0
    try:
        for session in scopes:
            connection.execute(f'SET search_path TO "{schema_for(session)}", public')
            repository = LangchaoAuthorityRepository(connection, scope_key=session)
            active = repository.get_active()
            if active is None:
                print(f"{session}: no active authority")
                failures += 1
                continue
            if args.action == "show":
                print(
                    f"{session:<38} {active['engine_key']}/{active['mode']} "
                    f"may_dispatch={active['may_dispatch']} pointer={active['pointer_version']}"
                )
                continue
            engine, mode = ACTIONS[args.action]
            try:
                repository.switch_authority(
                    engine_key=engine,
                    mode=mode,
                    expected_pointer_version=int(active["pointer_version"]),
                    # The repository re-derives this from durable pending/leased work and
                    # rejects a caller that under-reports it.
                    in_flight_count=0,
                    reason=args.reason,
                    payload={"operation": args.action, "reason": args.reason},
                )
            except Exception as error:  # noqa: BLE001 - report per scope and continue
                print(f"{session}: FAILED {type(error).__name__}: {error}")
                failures += 1
                continue
            published = repository.get_active()
            print(
                f"{session:<38} -> {published['engine_key']}/{published['mode']} "
                f"may_dispatch={published['may_dispatch']} pointer={published['pointer_version']}"
            )
    finally:
        connection.close()
    print(f"\n{args.action}: {len(scopes) - failures}/{len(scopes)} scopes ok")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
