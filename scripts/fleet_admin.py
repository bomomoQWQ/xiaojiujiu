"""Run the fleet: see who is served, add a person, and find out why nobody is talking.

Three things a deployment needs and did not have:

* **`list`** -- the roster as the supervisor sees it, with the one field that explains most
  "she never messages me" reports (``allow_proactive``), plus whether the scope is in the
  live allowlist at all.
* **`add`** -- provision one person from a session id and *verify* it: the supervisor
  assigns the port and schema, then this waits for health and asserts the schema actually
  migrated to the version this build expects. Provisioning without that assertion is how a
  Runtime ends up healthy while its rows sit in a schema nobody reads.
* **`check`** -- the doctor. Every check names the failure it rules out, so the answer is
  "frozen", "not in the allowlist", "no runtime for that session", "schema is at v24" or
  "nothing is wrong", instead of a guess.

Run it inside the fleet container, which owns the control surface and the credentials:

    docker exec -i xxj-runtime-fleet python /fleet/fleet_admin.py list
    docker exec -i xxj-runtime-fleet python /fleet/fleet_admin.py add default:FriendMessage:10001
    docker exec -i xxj-runtime-fleet python /fleet/fleet_admin.py check default:FriendMessage:10001
    docker exec -i xxj-runtime-fleet python /fleet/fleet_admin.py remove default-friendmessage-10001

Proactive sending is governed by a persisted authority pointer, not by configuration, so
`freeze`/`restore` live in :mod:`dispatch_authority` and rolling updates in
:mod:`rolling_restart`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import fleet_naming
import fleet_roster

#: How long a freshly provisioned Runtime may take to answer its health probe. The child
#: starts, opens a PostgreSQL connection and migrates up to the current version first.
HEALTH_TIMEOUT_SECONDS = 120.0

#: Schema version this build expects every scope to have migrated to. Imported from the
#: package when it is available so there is one definition; the literal is the fallback for
#: running this outside the Runtime image.
EXPECTED_SCHEMA_VERSION_FALLBACK = 26


def _expected_schema_version() -> int:
    """Return the schema version this build migrates scopes to."""
    try:
        from companion_runtime.user_model_v2_schema import USER_MODEL_SCHEMA_VERSION
    except ImportError:
        return EXPECTED_SCHEMA_VERSION_FALLBACK
    return int(USER_MODEL_SCHEMA_VERSION)


def _get(path: str, control_url: str) -> tuple[dict, str]:
    """Return ``(payload, error)`` for one control-surface GET."""
    try:
        with urllib.request.urlopen(f"{control_url}{path}", timeout=15) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as error:
        return {}, f"HTTP {error.code}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as error:
        return {}, str(error)[:160]


def _post(path: str, payload: dict, control_url: str) -> tuple[dict, str]:
    """Return ``(payload, error)`` for one control-surface POST."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{control_url}{path}", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "ignore")[:160]
        return {}, f"HTTP {error.code}: {detail}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as error:
        return {}, str(error)[:160]


def _live_allowlist() -> tuple[str, ...]:
    """Return the scopes 「浪潮」 is allowed to send to, per this deployment's environment."""
    raw = os.environ.get("CR_LANGCHAO__LIVE_SCOPE_ALLOWLIST") or ""
    if not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()


def _live_enabled() -> bool:
    """Return whether the live engine is switched on for this deployment."""
    return (os.environ.get("CR_LANGCHAO__LIVE_ENABLED") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _connect(dsn: str | None):
    """Return a read-only connection, or ``None`` with a reason printed by the caller."""
    if not dsn:
        return None
    try:
        import psycopg
    except ImportError:
        return None
    try:
        return psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row)
    except Exception:  # noqa: BLE001 - the doctor must keep working without a database
        return None


def _schema_facts(connection, schema: str) -> dict[str, object]:
    """Return what the database says about one scope's schema."""
    exists = connection.execute(
        "SELECT 1 AS present FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    ).fetchone()
    if not exists:
        return {"exists": False, "version": None, "queued": None, "unsettled": None}
    facts: dict[str, object] = {"exists": True, "version": None, "queued": None, "unsettled": None}
    try:
        connection.execute(f'SET search_path TO "{schema}", public')
        row = connection.execute(
            "SELECT max(version) AS version FROM schema_migrations_v2"
        ).fetchone()
        facts["version"] = None if row is None else row["version"]
    except Exception:  # noqa: BLE001 - a pre-migration schema has no such table
        pass
    for key, statement in (
        ("queued", "SELECT count(*) AS n FROM outbox WHERE status IN ('pending','leased')"),
        ("unsettled", "SELECT count(*) AS n FROM langchao_live_commits"
                      " WHERE terminal_ack_kind IS NULL"),
    ):
        try:
            row = connection.execute(statement).fetchone()
            facts[key] = None if row is None else int(row["n"])
        except Exception:  # noqa: BLE001 - table absent on an older schema
            pass
    try:
        connection.execute(f'SET search_path TO "{schema}", public')
        row = connection.execute(
            """SELECT r.engine_key, r.mode FROM langchao_authority_active a
                 JOIN langchao_authority_revisions r
                   ON r.scope_key = a.scope_key AND r.authority_id = a.authority_id
                  AND r.revision = a.revision"""
        ).fetchone()
        facts["authority"] = "unknown" if row is None else f"{row['engine_key']}/{row['mode']}"
    except Exception:  # noqa: BLE001
        facts["authority"] = None
    return facts


def cmd_list(args: argparse.Namespace) -> int:
    """Print the roster with the fields that explain proactive behaviour."""
    payload, error = _get("/fleet/status", args.control_url)
    if error:
        print(f"control surface unreachable: {error}", file=sys.stderr)
        return 1
    people = payload.get("people") or []
    allowlist = _live_allowlist()
    live_on = _live_enabled()
    print(f"fleet: {payload.get('count')} runtime(s); live engine "
          f"{'ENABLED' if live_on else 'DISABLED'}; allowlist {len(allowlist)} scope(s)")
    print()
    header = (f"{'session':<34}{'port':>6}{'health':>10}{'may send':>10}"
              f"{'in allowlist':>14}  schema")
    print(header)
    for item in people:
        session = str(item.get("session"))
        allow = "yes" if session in allowlist else "NO"
        allowed = "yes" if item.get("allow_proactive") else "no"
        print(f"{session:<34}{str(item.get('port')):>6}{str(item.get('health')):>10}"
              f"{allowed:>10}{allow:>14}  {fleet_naming.schema_for_session(session)}")
    if not people:
        print("(no runtimes: nobody is being served yet -- add one with `add <session>`)")
    return 0


def cmd_routes(args: argparse.Namespace) -> int:
    """Print the session-to-Runtime map the plugin's registry uses."""
    payload, error = _get("/fleet/routes", args.control_url)
    if error:
        print(f"control surface unreachable: {error}", file=sys.stderr)
        return 1
    routes = payload.get("routes") or {}
    if not routes:
        print("(no routes: the plugin's registry has nothing to resolve)")
        return 0
    for session, url in sorted(routes.items()):
        print(f"{session:<40} -> {url}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Provision one session and verify it became a working scope."""
    session = args.session.strip()
    if not session:
        print("a session is required, e.g. default:FriendMessage:10001", file=sys.stderr)
        return 1
    if ":" not in session:
        print(f"'{session}' does not look like a session origin; AstrBot sessions look like "
              "'default:FriendMessage:<id>'. A wrong id here is the 'messages go to the "
              "wrong session' failure.", file=sys.stderr)
        return 1

    schema = fleet_naming.schema_for_session(session)
    slug = fleet_naming.slugify(session)
    print(f"provisioning {session}\n  slug   {slug}\n  schema {schema}")
    payload, error = _post("/fleet/provision", {"session": session}, args.control_url)
    if error:
        print(f"provision failed: {error}", file=sys.stderr)
        return 1
    print(f"  port   {payload.get('port')}\n  url    {payload.get('url')}")

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        status, error = _get("/fleet/status", args.control_url)
        item = next(
            (p for p in (status.get("people") or []) if p.get("session") == session), None
        )
        if item is not None and item.get("health") == "ok":
            print("  health ok")
            break
        last = str(item.get("health")) if item else "not listed yet"
        time.sleep(3)
    else:
        print(f"  health did not come up within {HEALTH_TIMEOUT_SECONDS:.0f}s (last: {last})",
              file=sys.stderr)
        print(f"  the child's log is /data/logs/{slug}.log inside the fleet container",
              file=sys.stderr)
        return 1

    connection = _connect(args.dsn)
    if connection is None:
        print("  schema version not asserted (no usable CR_STORAGE__DSN here)")
    else:
        facts = _schema_facts(connection, schema)
        expected = _expected_schema_version()
        if not facts["exists"]:
            print(f"  schema {schema} MISSING after a healthy start -- the Runtime is "
                  "answering from nowhere", file=sys.stderr)
            return 1
        if facts["version"] != expected:
            print(f"  schema is at v{facts['version']}, this build expects v{expected}",
                  file=sys.stderr)
            return 1
        print(f"  schema migrated to v{facts['version']}")

    allowlist = _live_allowlist()
    if _live_enabled() and session not in allowlist:
        print(f"  note: {session} is not in CR_LANGCHAO__LIVE_SCOPE_ALLOWLIST, so 「浪潮」 "
              "live will never send for it. Add it and recreate the fleet container.")
    if not _live_enabled():
        print("  note: CR_LANGCHAO__LIVE_ENABLED is not on, so nothing sends proactively yet.")
    print("done")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Explain, for one scope or all of them, why she is or is not talking."""
    status, error = _get("/fleet/status", args.control_url)
    if error:
        print(f"control surface unreachable: {error}", file=sys.stderr)
        return 1
    people = {str(p.get("session")): p for p in (status.get("people") or [])}

    if args.session:
        scopes = (args.session,)
    else:
        scopes = fleet_roster.resolve_scopes(None, allow_empty=True) or tuple(people)
    if not scopes:
        print("no scopes: pass a session, or provision one with `add <session>`")
        return 1

    allowlist = _live_allowlist()
    live_on = _live_enabled()
    expected = _expected_schema_version()
    connection = _connect(args.dsn)
    problems = 0
    for session in scopes:
        print(f"=== {session}")
        schema = fleet_naming.schema_for_session(session)
        item = people.get(session)
        if item is None:
            problems += 1
            print("  [x] no Runtime is serving this session: the plugin has nowhere to "
                  "dispatch to, so nothing can be sent. Fix: `add` it.")
            continue
        healthy = item.get("health") == "ok"
        print(f"  [{'v' if healthy else 'x'}] runtime health: {item.get('health')} "
              f"(port {item.get('port')}, pid {item.get('pid')}, restarts {item.get('restarts')})")
        if not healthy:
            problems += 1
        allowed = bool(item.get("allow_proactive"))
        print(f"  [{'v' if allowed else 'x'}] runtime says it may send proactively: {allowed}")
        if not allowed:
            problems += 1
        if live_on:
            listed = session in allowlist
            print(f"  [{'v' if listed else 'x'}] in CR_LANGCHAO__LIVE_SCOPE_ALLOWLIST: {listed}")
            if not listed:
                problems += 1
        else:
            print("  [-] CR_LANGCHAO__LIVE_ENABLED is off: nothing sends proactively")
        print(f"  [i] schema {schema}")

        if connection is None:
            print("  [-] database checks skipped (no usable CR_STORAGE__DSN here)")
            continue
        facts = _schema_facts(connection, schema)
        if not facts["exists"]:
            problems += 1
            print("  [x] schema missing: the Runtime's rows are not where they should be")
            continue
        version = facts["version"]
        if version != expected:
            problems += 1
            print(f"  [x] schema version v{version}, expected v{expected}")
        else:
            print(f"  [v] schema version v{version}")
        authority = facts.get("authority")
        if authority is None:
            print("  [i] no authority row yet (a round has not run since provision)")
        else:
            frozen = not str(authority).endswith("/live")
            print(f"  [{'v' if not frozen else 'x'}] authority: {authority}"
                  + ("  (dispatch authority is not live: freeze/shadow, so nothing sends)"
                     if frozen else ""))
            if frozen:
                problems += 1
        queued = facts.get("queued")
        unsettled = facts.get("unsettled")
        if queued:
            problems += 1
            print(f"  [x] {queued} outbox row(s) pending/leased: work is stuck")
        if unsettled:
            problems += 1
            print(f"  [x] {unsettled} commit(s) without a terminal acknowledgement")
        if not queued and not unsettled:
            print("  [v] no stuck outbox rows and no unsettled commits")

    print()
    print("verdict:", "nothing looks wrong" if not problems
          else f"{problems} thing(s) would stop or have stopped proactive messages")
    return 0 if not problems else 2


def cmd_remove(args: argparse.Namespace) -> int:
    """Deprovision one person, optionally dropping their stored data.

    Removal deliberately does **not** destroy data by default. Two owners share this
    machine's history: one typo in a slug should not erase somebody's memories, and the
    schema is the only copy. Purging is therefore explicit, and each purge step says what
    it removed so an operator cleaning up after a manual roster edit can see it worked.
    """
    target = args.person.strip()
    slug = target if ":" not in target else fleet_naming.slugify(target)
    session = target if ":" in target else None
    status, error = _get("/fleet/status", args.control_url)
    if error and session is None:
        print(f"control surface unreachable: {error}", file=sys.stderr)
        return 1
    people = (status.get("people") or []) if not error else []
    if session is None:
        session = next(
            (str(p.get("session")) for p in people if p.get("person") == slug), None
        )
    listed = session is not None and any(p.get("session") == session for p in people)

    planned = []
    if listed:
        planned.append(f"deprovision {session} from the fleet")
    if args.purge_schema and session:
        planned.append(f"DROP SCHEMA {fleet_naming.schema_for_session(session)} CASCADE")
    if args.purge_data:
        planned.append(f"delete {args.data_root}/{slug} and {args.log_dir}/{slug}.log")
    if not planned:
        print(f"{session or slug} is not being served"
              + ("" if session is None else
                 f", and no --purge flag was given, so its stored data is left alone")
              + "\nusage: remove <slug|session> [--purge-schema] [--purge-data] --confirm")
        return 0
    if not args.confirm:
        print("would:\n  " + "\n  ".join(planned) + "\nre-run with --confirm")
        return 0

    if listed:
        payload, error = _post(f"/fleet/deprovision/{slug}", {}, args.control_url)
        if error:
            print(f"deprovision failed: {error}", file=sys.stderr)
            return 1
        print(f"deprovisioned {payload.get('deprovisioned') or session}")
    elif session:
        print(f"{session} was not being served; nothing to deprovision")

    failures = 0
    if args.purge_schema and session:
        schema = fleet_naming.schema_for_session(session)
        connection = _connect(args.dsn)
        if connection is None:
            print(f"schema {schema} NOT dropped (no usable CR_STORAGE__DSN here)",
                  file=sys.stderr)
            failures += 1
        else:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            print(f"dropped schema {schema}")
    if args.purge_data:
        import shutil
        for path in (Path(args.data_root) / slug, Path(args.log_dir) / f"{slug}.log"):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                    print(f"deleted {path}")
                elif path.exists():
                    path.unlink()
                    print(f"deleted {path}")
            except OSError as error:
                print(f"could not delete {path}: {error}", file=sys.stderr)
                failures += 1
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the operator CLI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--control-url", default=fleet_roster.DEFAULT_CONTROL_URL,
        help="fleet control surface (default: %(default)s)",
    )
    parser.add_argument(
        "--dsn", default=os.environ.get("CR_STORAGE__DSN") or "",
        help="PostgreSQL DSN for the database checks (default: CR_STORAGE__DSN)",
    )
    parser.add_argument("--data-root", default="/data",
                        help="where the supervisor keeps each child's directory")
    parser.add_argument("--log-dir", default="/data/logs",
                        help="where the supervisor writes each child's log")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="roster, health, and whether each scope may send")
    sub.add_parser("routes", help="session -> Runtime URL map")

    add = sub.add_parser("add", help="provision one session and verify it")
    add.add_argument("session")

    check = sub.add_parser("check", help="diagnose why a scope is or is not talking")
    check.add_argument("session", nargs="?")

    remove = sub.add_parser("remove", help="deprovision one person")
    remove.add_argument("person", help="slug or session")
    remove.add_argument("--purge-schema", action="store_true",
                        help="also drop the scope's PostgreSQL schema (destructive)")
    remove.add_argument("--purge-data", action="store_true",
                        help="also delete the child's data directory and log (destructive)")
    remove.add_argument("--confirm", action="store_true", help="actually do it")
    return parser


def main() -> int:
    """Dispatch one operator command."""
    args = build_parser().parse_args()
    handlers = {
        "list": cmd_list,
        "routes": cmd_routes,
        "add": cmd_add,
        "check": cmd_check,
        "remove": cmd_remove,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
