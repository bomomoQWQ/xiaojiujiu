"""Command line entry point for the Runtime sidecar.

Subcommands::

    serve        run the HTTP sidecar
    tick         advance the Runtime to a moment (lazy_tick)
    endogenous   run one endogenous round and print the decision
    state        print a read-only view of the Runtime
    verify       integrity and consistency check
    checkpoint   fold the WAL back into the database file
    backup       write a consistent snapshot
    restore      install a snapshot over the database file
    recover      print (and optionally run) the recovery plan for a data directory
    config       print the effective configuration (redacted)
    health       open the database and print a health summary

Every command accepts ``--config`` and honours the ``CR_`` environment overrides.
No command reads, prints or stores an API key: secrets belong to the host
framework, and configuration output is redacted by the configuration layer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import configure_logging, load_config, resolve_paths
from .db import Database
from .maintenance import (
    backup,
    checkpoint,
    maintenance_tick,
    prune_backups,
    recovery_plan,
    restore,
    snapshot_name,
    sqlite_error_is_corruption,
    verify,
)
from .runtime import Runtime
from .utility import parse_datetime, utcnow

LOGGER = logging.getLogger("companion_runtime.cli")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VERIFY_FAILED = 3
EXIT_ERROR = 4


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="companion-runtime",
        description=(
            "Endogenous proactive long-term companion Runtime sidecar: persistent "
            "emotion, memory, user model, candidate intents and motivational decisions."
        ),
    )
    parser.add_argument("--version", action="version", version=f"companion-runtime {__version__}")
    parser.add_argument("--config", help="path to a TOML or JSON configuration file")
    parser.add_argument("--log-level", default=None, help="override the log level (e.g. DEBUG)")
    parser.add_argument(
        "--base-dir",
        default=None,
        help="base directory for relative storage paths (default: current directory)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the HTTP sidecar")
    serve.add_argument("--host", default=None, help="bind host")
    serve.add_argument("--port", type=int, default=None, help="bind port")
    serve.add_argument(
        "--seed", type=int, default=None, help="seed the internal RNG (useful for staging)"
    )
    serve.add_argument(
        "--maintenance-interval",
        type=float,
        default=0.0,
        help="seconds between automatic checkpoint/backup passes; 0 disables",
    )
    serve.add_argument(
        "--backup-dir",
        default=None,
        help="when set, write a snapshot on every automatic maintenance pass",
    )

    tick = subparsers.add_parser("tick", help="advance the Runtime with lazy_tick")
    tick.add_argument("--now", default=None, help="ISO-8601 reference time (default: now)")

    endogenous = subparsers.add_parser("endogenous", help="run one endogenous round")
    endogenous.add_argument("--now", default=None, help="ISO-8601 reference time (default: now)")
    endogenous.add_argument(
        "--force", action="store_true", help="ignore the foreground pause (never the boundaries)"
    )
    endogenous.add_argument(
        "--dry-run", action="store_true", help="decide but do not create an action attempt"
    )

    state = subparsers.add_parser("state", help="print a read-only view of the Runtime")
    state.add_argument(
        "--include",
        default="state",
        choices=[
            "state",
            "candidates",
            "memories",
            "unfinished",
            "boundaries",
            "attempts",
            "semantics",
            "all",
        ],
        help="which section to print",
    )

    refresh = subparsers.add_parser(
        "refresh",
        help="run one deep cognition refresh (patch v0.2; optional semantic provider)",
    )
    refresh.add_argument("--now", default=None, help="ISO-8601 reference time (default: now)")
    refresh.add_argument(
        "--force", action="store_true", help="ignore the trigger check (diagnostics)"
    )

    backlog = subparsers.add_parser(
        "backlog", help="list events the Runtime has deliberately left uninterpreted"
    )
    backlog.add_argument("--limit", type=int, default=50, help="maximum items to list")

    check = subparsers.add_parser("verify", help="integrity and consistency check")
    check.add_argument("--json", action="store_true", help="print compact JSON")

    checkpoint_cmd = subparsers.add_parser("checkpoint", help="fold the WAL into the database file")
    checkpoint_cmd.add_argument(
        "--mode",
        default="TRUNCATE",
        choices=["PASSIVE", "FULL", "RESTART", "TRUNCATE"],
        help="SQLite checkpoint mode",
    )

    backup_cmd = subparsers.add_parser("backup", help="write a consistent snapshot")
    backup_cmd.add_argument(
        "destination",
        nargs="?",
        default=None,
        help="target file or directory (default: <data dir>/backups/<timestamp>.sqlite3)",
    )
    backup_cmd.add_argument("--keep", type=int, default=0, help="prune to this many snapshots")
    backup_cmd.add_argument(
        "--no-checkpoint", action="store_true", help="skip the pre-backup checkpoint"
    )

    restore_cmd = subparsers.add_parser("restore", help="install a snapshot over the database")
    restore_cmd.add_argument("snapshot", help="snapshot produced by 'backup'")
    restore_cmd.add_argument(
        "--no-verify", action="store_true", help="skip verifying the snapshot first"
    )
    restore_cmd.add_argument(
        "--no-keep-previous",
        action="store_true",
        help="delete the previous database instead of keeping a .replaced copy",
    )

    recover_cmd = subparsers.add_parser(
        "recover", help="print the recovery plan for this data directory"
    )
    recover_cmd.add_argument("--backup-dir", default=None, help="snapshot directory override")
    recover_cmd.add_argument(
        "--run",
        action="store_true",
        help="also run a maintenance pass (checkpoint + verify + backup)",
    )

    subparsers.add_parser("config", help="print the effective configuration (redacted)")
    subparsers.add_parser("health", help="open the database and print a health summary")
    return parser


def _resolve_config(args: argparse.Namespace):
    """Load and resolve the configuration described by ``args``."""
    config = load_config(args.config)
    if args.log_level:
        config.server.log_level = args.log_level
    resolve_paths(config, args.base_dir)
    return config


def _open_database(config) -> Database:
    """Open and migrate a database handle from the configuration."""
    database = Database(
        config.storage.database_path,
        busy_timeout_ms=config.storage.busy_timeout_ms,
        wal=config.storage.wal,
    )
    database.migrate()
    return database


def _emit(payload: Any, *, as_json: bool = False) -> None:
    """Print a payload as compact JSON or as indented JSON for humans."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _parse_now(value: str | None) -> datetime | None:
    """Parse an optional ``--now`` argument."""
    return parse_datetime(value) if value else None


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the HTTP sidecar with uvicorn."""
    import uvicorn

    from .api import create_app

    config = _resolve_config(args)
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    configure_logging(config.server.log_level)

    runtime = Runtime(config, seed=args.seed)
    app = create_app(runtime, config)

    async def maintenance_loop() -> None:
        """Periodically checkpoint (and optionally back up) while serving."""
        while True:
            await asyncio.sleep(max(60.0, args.maintenance_interval))
            try:
                # Blocking SQLite work runs in a thread so the event loop keeps
                # serving HTTP requests.
                await asyncio.to_thread(
                    maintenance_tick, runtime.db, backup_dir=args.backup_dir
                )
            except Exception:  # noqa: BLE001 - maintenance must never kill the server
                LOGGER.exception("Maintenance pass failed")

    async def run() -> None:
        """Start background tasks, serve, and shut down cleanly."""
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.server.host,
                port=config.server.port,
                log_level=config.server.log_level.lower(),
            )
        )
        maintenance: asyncio.Task[Any] | None = None
        if args.maintenance_interval and args.maintenance_interval > 0:
            maintenance = asyncio.create_task(maintenance_loop(), name="runtime-maintenance")
        try:
            await server.serve()
        finally:
            if maintenance is not None:
                maintenance.cancel()
            # A clean shutdown folds the WAL back so the next start is fast and
            # the on-disk layout is a single self-contained file.
            try:
                checkpoint(runtime.db, mode="TRUNCATE")
            except Exception:  # noqa: BLE001 - shutdown path
                LOGGER.exception("Final checkpoint failed")
            runtime.close()

    LOGGER.info(
        "Starting companion Runtime sidecar on http://%s:%d (database=%s)",
        config.server.host,
        config.server.port,
        config.storage.database_path,
    )
    asyncio.run(run())
    return EXIT_OK


def cmd_tick(args: argparse.Namespace) -> int:
    """Advance the Runtime and print the tick report."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    runtime = Runtime(config)
    try:
        report = runtime.lazy_tick(_parse_now(args.now))
        _emit(report.to_dict())
    finally:
        runtime.close()
    return EXIT_OK


def cmd_endogenous(args: argparse.Namespace) -> int:
    """Run one endogenous round and print the decision."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    runtime = Runtime(config)
    try:
        outcome = runtime.endogenous_round(
            now=_parse_now(args.now), force=args.force, create_attempt=not args.dry_run
        )
        _emit(outcome.to_dict(), as_json=True)
    finally:
        runtime.close()
    return EXIT_OK


def cmd_refresh(args: argparse.Namespace) -> int:
    """Run one deep cognition refresh and print what happened.

    A refresh that declines is a normal result, not a failure, so this exits 0
    either way and reports the reason. Only a database problem is an error.
    """
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    runtime = Runtime(config)
    try:
        outcome = runtime.deep_refresh(now=_parse_now(args.now), force=args.force)
        _emit(outcome.to_dict(), as_json=True)
    finally:
        runtime.close()
    return EXIT_OK


def cmd_backlog(args: argparse.Namespace) -> int:
    """Print the unresolved-event backlog and its relevance breakdown."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    runtime = Runtime(config)
    try:
        _emit(
            {
                "stats": runtime.projections.semantics.stats(),
                "items": runtime.projections.semantics.list_unresolved(limit=args.limit),
            },
            as_json=True,
        )
    finally:
        runtime.close()
    return EXIT_OK


def cmd_state(args: argparse.Namespace) -> int:
    """Print a read-only view of the Runtime."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    runtime = Runtime(config)
    try:
        runtime.lazy_tick()
        projections = runtime.projections
        payload: dict[str, Any] = {"state": runtime.state().to_dict()}
        whether = {
            "candidates": lambda: [
                item.to_dict() for item in projections.candidates.list_active(limit=50)
            ],
            "memories": lambda: [
                item.to_dict() for item in projections.memory.list_memories(limit=50)
            ],
            "unfinished": lambda: [
                item.to_dict() for item in projections.unfinished.list_all(limit=50)
            ],
            "boundaries": lambda: [item.to_dict() for item in projections.boundaries.list_all()],
            "attempts": lambda: [
                item.to_dict() for item in projections.attempts.list_all(limit=20)
            ],
        }
        if args.include == "all":
            for name, supplier in whether.items():
                payload[name] = supplier()
            payload["activated_memories"] = [
                item.to_dict() for item in projections.memory.list_activated(limit=20)
            ]
        elif args.include == "state":
            payload = {"state": payload["state"]}
        else:
            payload = {args.include: whether[args.include]()}
        _emit(payload)
    finally:
        runtime.close()
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    """Run integrity and consistency checks."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    database = _open_database(config)
    try:
        result = verify(database, expect_wal=config.storage.wal)
        _emit(result.to_dict(), as_json=args.json)
        return EXIT_OK if result.ok else EXIT_VERIFY_FAILED
    finally:
        database.close()


def cmd_checkpoint(args: argparse.Namespace) -> int:
    """Fold the WAL back into the database file."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    database = _open_database(config)
    try:
        _emit(checkpoint(database, mode=args.mode).to_dict())
    finally:
        database.close()
    return EXIT_OK


def cmd_backup(args: argparse.Namespace) -> int:
    """Write a consistent snapshot."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    database = _open_database(config)
    try:
        destination = args.destination
        if destination is None:
            destination = Path(config.storage.database_path).parent / "backups" / snapshot_name()
        elif Path(destination).is_dir():
            destination = Path(destination) / snapshot_name()
        result = backup(
            database, destination, overwrite=True, checkpoint_first=not args.no_checkpoint
        )
        payload = result.to_dict()
        if args.keep:
            payload["pruned"] = prune_backups(Path(destination).parent, keep=args.keep)
        _emit(payload)
    finally:
        database.close()
    return EXIT_OK


def cmd_restore(args: argparse.Namespace) -> int:
    """Install a snapshot over the database file."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    try:
        result = restore(
            args.snapshot,
            config.storage.database_path,
            verify_first=not args.no_verify,
            keep_previous=not args.no_keep_previous,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    _emit(result.to_dict())
    return EXIT_OK


def cmd_recover(args: argparse.Namespace) -> int:
    """Print (and optionally run) the recovery plan for the data directory."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    backup_dir = args.backup_dir or str(Path(config.storage.database_path).parent / "backups")
    plan = recovery_plan(config.storage.database_path, backup_dir)
    if args.run:
        database = _open_database(config)
        try:
            plan["maintenance"] = maintenance_tick(database, backup_dir=backup_dir)
        finally:
            database.close()
    _emit(plan)
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """Print the effective configuration with secrets redacted."""
    _emit(_resolve_config(args).to_dict())
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    """Open the database and print a health summary."""
    config = _resolve_config(args)
    configure_logging(config.server.log_level)
    runtime = Runtime(config)
    try:
        state = runtime.state()
        _emit(
            {
                "status": "ok",
                "version": __version__,
                "state_version": state.version,
                "database_path": config.storage.database_path,
                "raw_events": runtime.events.count(),
                "outbox": runtime.projections.outbox.stats(),
                "in_flight_attempts": runtime.projections.attempts.count_in_flight(),
                "open_unfinished": len(runtime.projections.unfinished.list_open()),
                "active_candidates": len(runtime.projections.candidates.list_active(limit=100)),
                "total_memories": len(runtime.projections.memory.list_memories(limit=1000)),
                "now": utcnow().isoformat(),
            }
        )
    finally:
        runtime.close()
    return EXIT_OK


COMMANDS = {
    "serve": cmd_serve,
    "tick": cmd_tick,
    "endogenous": cmd_endogenous,
    "refresh": cmd_refresh,
    "backlog": cmd_backlog,
    "state": cmd_state,
    "verify": cmd_verify,
    "checkpoint": cmd_checkpoint,
    "backup": cmd_backup,
    "restore": cmd_restore,
    "recover": cmd_recover,
    "config": cmd_config,
    "health": cmd_health,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list (defaults to :data:`sys.argv[1:]`).

    Returns:
        A process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]
    try:
        return handler(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except sqlite3.DatabaseError as exc:
        # A malformed database is an operational condition, not a crash: report it
        # in a way a shell script or an operator can act on.
        if sqlite_error_is_corruption(exc):
            print(
                f"error: the database is unreadable or corrupt: {exc}\n"
                "       run 'recover' to see the recovery plan, then "
                "'restore <snapshot>' from the newest backup.",
                file=sys.stderr,
            )
            return EXIT_VERIFY_FAILED
        print(f"error: sqlite error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
