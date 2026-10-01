from __future__ import annotations

import hashlib
import importlib.util
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "apply_mechanical_history.py"
SPEC = importlib.util.spec_from_file_location("apply_mechanical_history", SCRIPT)
assert SPEC and SPEC.loader
operator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)

FLEET_SCRIPT = ROOT / "scripts" / "runtime_fleet.py"
FLEET_SPEC = importlib.util.spec_from_file_location("runtime_fleet_for_schema_test", FLEET_SCRIPT)
assert FLEET_SPEC and FLEET_SPEC.loader
fleet = importlib.util.module_from_spec(FLEET_SPEC)
FLEET_SPEC.loader.exec_module(fleet)


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def transaction(self):
        return self


class Audit:
    table = "raw_events"
    row_count = 1
    planned_count = 1
    quarantined_count = 0
    canonical_rows_sha256 = "c" * 64


class TableResult:
    table = "raw_events"
    inserted = 1
    exact_duplicate = 0
    conflict = 0
    selected_pk_sha256 = "d" * 64
    target_rows_sha256 = "e" * 64


class RowResult:
    table = "raw_events"
    primary_key = "secret-primary-key"
    outcome = "insert"
    differences = ()


def _plan(path: Path, scope: str, *, digest: str = "a" * 64, quarantine=()):
    return SimpleNamespace(
        source_path=str(path), scope_key=scope, plan_sha256=digest,
        tables=(Audit(),), rows=(object(),), quarantine=tuple(quarantine),
    )


def _snapshot(tmp_path: Path, scopes=("default:FriendMessage:100",)) -> tuple[Path, dict[str, Path]]:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir(parents=True)
    paths = {}
    people = []
    for index, scope in enumerate(scopes):
        path = snapshot_dir / f"person-{index}.sqlite3"
        path.write_bytes(f"sqlite-{scope}".encode())
        paths[scope] = path
        people.append({
            "scope_key": scope,
            "slug": f"person-{index}",
            "snapshot_filename": path.name,
            "snapshot": {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            },
        })
    (snapshot_dir / "manifest.json").write_text(
        json.dumps({"manifest_version": 1, "people": people}), encoding="utf-8"
    )
    return snapshot_dir, paths


def _args(snapshot_dir: Path, report_dir: Path, **overrides):
    values = dict(
        snapshot_dir=snapshot_dir, dsn_env="CR_PG_DSN", scope=[], all=True,
        dry_run=False, apply=False, confirm_plan_sha=None,
        require_stopped_marker=None, report_dir=report_dir,
    )
    values.update(overrides)
    return Namespace(**values)


def _wire_success(monkeypatch: pytest.MonkeyPatch, calls: list[object], digest="a" * 64):
    monkeypatch.setenv("CR_PG_DSN", "postgresql://user:secret@db/runtime")
    monkeypatch.setattr(
        operator, "plan_sqlite_mechanical_import",
        lambda path, scope: _plan(Path(path), scope, digest=digest),
    )
    monkeypatch.setattr(
        operator, "migrate", lambda connection, **kwargs: calls.append(("migrate", kwargs))
    )

    def reconcile(plan, connection, **kwargs):
        calls.append(("reconcile", plan.scope_key, kwargs))
        return SimpleNamespace(
            run_id="run-1", status="applied" if not kwargs["dry_run"] else "dry_run",
            dry_run=kwargs["dry_run"], tables=(TableResult(),), rows=(RowResult(),),
        )

    monkeypatch.setattr(operator, "reconcile_mechanical_plan", reconcile)


def test_default_mode_is_dry_run_and_report_has_no_row_body(tmp_path, monkeypatch):
    snapshot_dir, _ = _snapshot(tmp_path)
    calls = []
    _wire_success(monkeypatch, calls)

    code, report = operator.run(
        _args(snapshot_dir, tmp_path / "reports"), connect=lambda _dsn: Connection()
    )

    assert code == 0
    assert report["mode"] == report["status"] == "dry_run"
    reconcile = next(item for item in calls if item[0] == "reconcile")
    assert reconcile[2]["dry_run"] is True
    encoded = json.dumps(report)
    assert "secret-primary-key" not in encoded
    assert "postgresql://" not in encoded
    assert report["scopes"][0]["counts"]["planned_rows"] == 1


def test_apply_requires_marker_and_exact_plan_confirmation(tmp_path, monkeypatch):
    snapshot_dir, _ = _snapshot(tmp_path)
    calls = []
    _wire_success(monkeypatch, calls)
    confirmation = tmp_path / "confirm.txt"
    confirmation.write_text("default:FriendMessage:100=" + "a" * 64 + "\n", encoding="utf-8")

    missing_code, missing = operator.run(
        _args(snapshot_dir, tmp_path / "r1", apply=True, confirm_plan_sha=confirmation),
        connect=lambda _dsn: Connection(),
    )
    assert missing_code == 1
    assert "require-stopped-marker" in missing["error"]
    assert calls == []

    marker = tmp_path / "fleet.stopped"
    marker.write_text("operator attestation", encoding="utf-8")
    confirmation.write_text("default:FriendMessage:100=" + "b" * 64 + "\n", encoding="utf-8")
    mismatch_code, mismatch = operator.run(
        _args(
            snapshot_dir, tmp_path / "r2", apply=True, confirm_plan_sha=confirmation,
            require_stopped_marker=marker,
        ), connect=lambda _dsn: Connection(),
    )
    assert mismatch_code == 1
    assert "confirmation mismatch" in mismatch["error"]
    assert calls == []

    confirmation.write_text("default:FriendMessage:100=" + "a" * 64 + "\n", encoding="utf-8")
    code, report = operator.run(
        _args(
            snapshot_dir, tmp_path / "r3", apply=True, confirm_plan_sha=confirmation,
            require_stopped_marker=marker,
        ), connect=lambda _dsn: Connection(),
    )
    assert code == 0 and report["status"] == "applied"
    assert report["stopped_marker"]["present"] is True
    assert report["stopped_marker"]["sha256"] == hashlib.sha256(marker.read_bytes()).hexdigest()
    reconcile = next(item for item in calls if item[0] == "reconcile")
    assert reconcile[2]["dry_run"] is False


def test_repeated_scope_selection_controls_order_and_connections(tmp_path, monkeypatch):
    scopes = ("default:FriendMessage:100", "default:FriendMessage:200")
    snapshot_dir, _ = _snapshot(tmp_path, scopes)
    calls = []
    _wire_success(monkeypatch, calls)

    code, report = operator.run(
        _args(snapshot_dir, tmp_path / "r", all=False, scope=[scopes[1]]),
        connect=lambda _dsn: Connection(),
    )

    assert code == 0
    assert [item["scope"] for item in report["scopes"]] == [scopes[1]]
    assert [item[1] for item in calls if item[0] == "reconcile"] == [scopes[1]]


def test_schema_derivation_matches_runtime_fleet_including_long_unicode_name(tmp_path):
    scopes = [
        "default:FriendMessage:12345",
        "prefix:" + "甲乙丙丁-very-long-session-" * 8 + ":suffix",
    ]
    for scope in scopes:
        process = fleet.RuntimeProcess(scope, 8787, tmp_path, tmp_path, {})
        assert operator.derive_target_schema(scope) == process.env["CR_STORAGE__SCHEMA"]
        assert len(operator.derive_target_schema(scope).encode("utf-8")) <= 63


def test_manifest_hash_mismatch_exits_before_planning_or_database(tmp_path, monkeypatch):
    snapshot_dir, paths = _snapshot(tmp_path)
    paths["default:FriendMessage:100"].write_bytes(b"tampered")
    monkeypatch.setenv("CR_PG_DSN", "postgresql://unused")
    planned = []
    monkeypatch.setattr(operator, "plan_sqlite_mechanical_import", lambda *args: planned.append(args))

    code, report = operator.run(
        _args(snapshot_dir, tmp_path / "r"),
        connect=lambda _dsn: pytest.fail("database must not be contacted"),
    )

    assert code == 1
    assert "snapshot hash/size mismatch" in report["error"]
    assert planned == []


def test_quarantine_and_conflict_are_nonzero_and_reported(tmp_path, monkeypatch):
    snapshot_dir, paths = _snapshot(tmp_path)
    monkeypatch.setenv("CR_PG_DSN", "postgresql://unused")
    quarantined = SimpleNamespace(reason="invalid_row")
    monkeypatch.setattr(
        operator, "plan_sqlite_mechanical_import",
        lambda path, scope: _plan(Path(path), scope, quarantine=(quarantined,)),
    )
    code, report = operator.run(
        _args(snapshot_dir, tmp_path / "q"), connect=lambda _dsn: pytest.fail("no database")
    )
    assert code == 1 and "quarantined" in report["error"]
    assert report["scopes"][0]["counts"]["quarantined_rows"] == 1

    calls = []
    _wire_success(monkeypatch, calls)
    conflict_table = SimpleNamespace(
        table="raw_events", inserted=0, exact_duplicate=0, conflict=1,
        selected_pk_sha256="1" * 64, target_rows_sha256=None,
    )
    conflict_row = SimpleNamespace(
        table="raw_events", primary_key="private-id", outcome="conflict",
        differences=(SimpleNamespace(column="content", planned_sha256="2" * 64,
                                     existing_sha256="3" * 64),),
    )
    conflict_report = SimpleNamespace(
        run_id="run-c", status="failed", dry_run=True,
        tables=(conflict_table,), rows=(conflict_row,),
    )

    def fail_reconcile(*args, **kwargs):
        raise operator.MechanicalHistoryApplyError("hard conflict secret", report=conflict_report)

    monkeypatch.setattr(operator, "reconcile_mechanical_plan", fail_reconcile)
    code, report = operator.run(
        _args(snapshot_dir, tmp_path / "c"), connect=lambda _dsn: Connection()
    )
    assert code == 1
    assert report["scopes"][0]["reconciliation"]["tables"][0]["conflict"] == 1
    encoded = json.dumps(report)
    assert "private-id" not in encoded and "hard conflict secret" not in encoded
    assert report["scopes"][0]["reconciliation"]["conflict_columns"] == {
        "raw_events": ["content"]
    }


def test_atomic_report_replaces_destination_and_cli_returns_error(tmp_path, monkeypatch, capsys):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    destination = report_dir / operator.REPORT_NAME
    destination.write_text("old", encoding="utf-8")
    result = operator._atomic_report(report_dir, {"status": "dry_run", "scopes": []})
    assert result == destination
    assert json.loads(destination.read_text(encoding="utf-8"))["status"] == "dry_run"
    assert not list(report_dir.glob(f".{operator.REPORT_NAME}.*"))

    snapshot_dir, paths = _snapshot(tmp_path / "cli")
    paths["default:FriendMessage:100"].write_bytes(b"bad")
    monkeypatch.setenv("CR_PG_DSN", "postgresql://unused")
    code = operator.main([
        "--snapshot-dir", str(snapshot_dir), "--all", "--report-dir", str(report_dir)
    ])
    assert code == 1
    persisted = json.loads(destination.read_text(encoding="utf-8"))
    assert persisted["status"] == "failed"
    assert "hash/size mismatch" in persisted["error"]
    assert "operation failed" in capsys.readouterr().err


@pytest.mark.skip(reason="set up a disposable PostgreSQL DSN for manual operator integration")
def test_postgres_integration_placeholder():
    """Reserved for a disposable-server migration/reconciliation smoke test."""
