#!/usr/bin/env python3
"""Black-box v2 acceptance harness (safe skeleton; no Runtime internals).

The harness deliberately knows only three boundaries:

* public HTTP endpoints exposed by the Runtime;
* a OneBot-compatible HTTP frontend used to inject/observe chat traffic; and
* optional, direct PostgreSQL reads made in a read-only transaction.

The current Runtime does not yet expose the proposed v2 black-box evidence route.
Consequently the default is ``--dry-run`` and this module also provides a result
validator that can be exercised with synthetic HTTP fixtures.  A dry-run proving
that request construction is valid is *not* a claim that a deployed Runtime has
passed the contract.

No credentials are embedded.  A DSN is accepted only as an argument/environment
value, is never included in reports, and PostgreSQL sessions execute
``SET TRANSACTION READ ONLY`` before querying.

Proposed public fixture contract
--------------------------------
``POST {runtime}/v2/events`` accepts an event envelope.
``POST {runtime}/v2/decisions/run`` asks the public decision funnel to run.
``POST {onebot}/v2/test/events`` injects a OneBot-style user event.
``GET  {runtime}/v2/blackbox/evidence?scope=...`` returns normalized evidence.

Every path is parameterizable so wiring can change without changing assertions.
The evidence shape is documented by :func:`synthetic_evidence` and checked by
:class:`ContractValidator`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence

CONTRACT_VERSION = "v2-blackbox-evidence-1"
DEFAULT_SCOPE = "onebot:private:blackbox-user"
REQUIRED_DECISION_STAGES = (
    "wake",
    "permissions",
    "candidate_eligible",
    "hazard_trial_performed",
    "hazard_trial_won",
    "committed",
    "rendered",
    "send_ack",
    "reconciled",
)
SECRET_KEYS = ("password", "passwd", "secret", "token", "api_key", "authorization", "dsn")


def utc_iso(value: datetime) -> str:
    """Serialize an aware timestamp in a stable UTC spelling."""
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Parse an ISO timestamp and reject naive values."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp: {value!r}")
    return parsed.astimezone(timezone.utc)


def redact(value: Any) -> Any:
    """Recursively redact common credential fields before report output."""
    if isinstance(value, Mapping):
        return {
            str(key): ("<redacted>" if any(part in str(key).lower() for part in SECRET_KEYS) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """All deployment-specific addresses and paths; none are secret."""

    runtime_base_url: str = "http://127.0.0.1:8090"
    onebot_base_url: str = "http://127.0.0.1:5700"
    runtime_event_path: str = "/v2/events"
    decision_path: str = "/v2/decisions/run"
    evidence_path: str = "/v2/blackbox/evidence"
    onebot_event_path: str = "/v2/test/events"
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in ("runtime_base_url", "onebot_base_url"):
            value = getattr(self, name)
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{name} must be an absolute HTTP(S) URL")
        for name in ("runtime_event_path", "decision_path", "evidence_path", "onebot_event_path"):
            if not getattr(self, name).startswith("/"):
                raise ValueError(f"{name} must start with '/'")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def runtime_url(self, path: str) -> str:
        return self.runtime_base_url.rstrip("/") + path

    def onebot_url(self, path: str) -> str:
        return self.onebot_base_url.rstrip("/") + path


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    """Scenario identity and safety switches."""

    scope: str = DEFAULT_SCOPE
    seed_time: datetime = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    reply_horizon_seconds: int = 6 * 60 * 60
    dry_run: bool = True

    def __post_init__(self) -> None:
        if not self.scope.strip():
            raise ValueError("scope must not be blank")
        if self.seed_time.tzinfo is None:
            raise ValueError("seed_time must be timezone-aware")
        if self.reply_horizon_seconds <= 0:
            raise ValueError("reply_horizon_seconds must be positive")


@dataclass(frozen=True, slots=True)
class HttpExchange:
    method: str
    url: str
    status: int
    request_json: Mapping[str, Any] | None
    response_json: Any


class JsonTransport(Protocol):
    """Small injectable public-HTTP boundary used by live and fixture runs."""

    exchanges: list[HttpExchange]

    def request(
        self, method: str, url: str, payload: Mapping[str, Any] | None = None
    ) -> Any: ...


class UrlLibJsonTransport:
    """Standard-library JSON-over-HTTP transport."""

    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.exchanges: list[HttpExchange] = []

    def request(self, method: str, url: str, payload: Mapping[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method.upper(),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                decoded = json.loads(raw.decode("utf-8")) if raw else None
                exchange = HttpExchange(method.upper(), url, response.status, payload, decoded)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {"error": raw.decode("utf-8", errors="replace")}
            exchange = HttpExchange(method.upper(), url, exc.code, payload, decoded)
            self.exchanges.append(exchange)
            raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
        self.exchanges.append(exchange)
        return decoded


class DryRunTransport:
    """Records intended traffic without opening sockets."""

    def __init__(self) -> None:
        self.exchanges: list[HttpExchange] = []

    def request(self, method: str, url: str, payload: Mapping[str, Any] | None = None) -> Any:
        response = {"dry_run": True, "not_executed": True}
        self.exchanges.append(HttpExchange(method.upper(), url, 0, payload, response))
        return response


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class ValidationReport:
    checks: list[Check] = field(default_factory=list)
    executed: bool = False
    source: str = "synthetic"

    @property
    def passed(self) -> bool:
        return self.executed and bool(self.checks) and all(check.passed for check in self.checks)

    def add(self, name: str, condition: bool, detail: str) -> None:
        self.checks.append(Check(name, bool(condition), detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "executed": self.executed,
            "source": self.source,
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
        }


class ContractValidator:
    """Validate normalized evidence without importing Runtime implementation code."""

    def validate(
        self, evidence: Mapping[str, Any], *, expected_scope: str, executed: bool = True, source: str = "http"
    ) -> ValidationReport:
        report = ValidationReport(executed=executed, source=source)
        report.add(
            "evidence_contract",
            evidence.get("contract_version") == CONTRACT_VERSION,
            "normalized evidence declares the expected version",
        )
        report.add("scope", evidence.get("scope") == expected_scope, "all evidence is scope-bound")

        events = list(evidence.get("events") or [])
        event_ids = [row.get("event_id") for row in events]
        report.add(
            "message_entered",
            any(row.get("kind") == "user_message" and row.get("scope") == expected_scope for row in events),
            "a public user-message event is present in the requested scope",
        )
        report.add(
            "idempotent_replay",
            len(event_ids) == len(set(event_ids)) and int(evidence.get("duplicate_events", 0)) >= 1,
            "replay is reported but does not create a second event identity",
        )

        decisions = list(evidence.get("decisions") or [])
        complete = []
        for decision in decisions:
            stages = [row.get("stage") if isinstance(row, Mapping) else row for row in decision.get("stages", [])]
            complete.append(all(stage in stages for stage in REQUIRED_DECISION_STAGES))
        report.add(
            "active_decision_funnel",
            bool(decisions) and any(complete),
            "a proactive decision exposes wake through reconciliation stages",
        )
        report.add(
            "send_ack_exposure",
            any(
                row.get("delivery_basis") == "delivered" and row.get("send_ack_id")
                for row in evidence.get("exposures", [])
            ),
            "only acknowledged delivery creates a delivered exposure",
        )

        labels = list(evidence.get("labels") or [])
        statuses = {(row.get("case"), row.get("target"), row.get("status"), row.get("value")) for row in labels}
        report.add(
            "reply_label",
            ("in_window", "reply", "observed_positive", True) in statuses,
            "an attributable in-window reply is positive",
        )
        report.add(
            "late_and_no_reply",
            ("late", "reply", "observed_negative", False) in statuses
            and ("no_reply", "reply", "observed_negative", False) in statuses,
            "late and absent replies remain fixed-window negatives",
        )

        boundaries = list(evidence.get("boundaries") or [])
        report.add(
            "boundary",
            any(row.get("scope") == expected_scope and row.get("blocked") is True for row in boundaries)
            and not any(row.get("sent") is True for row in decisions if row.get("boundary_blocked") is True),
            "a hard boundary blocks sending before utility can override it",
        )

        snapshots = list(evidence.get("parameter_snapshots") or [])
        active = [row for row in snapshots if row.get("active") is True]
        used_ids = {row.get("parameter_snapshot_id") for row in decisions}
        report.add(
            "parameter_snapshot",
            len(active) == 1
            and active[0].get("scope") == expected_scope
            and active[0].get("parameter_snapshot_id") in used_ids
            and isinstance(active[0].get("parameters"), Mapping),
            "one scoped immutable parameter snapshot is active and referenced by a decision",
        )
        return report


class PostgresEvidenceReader:
    """Optional read-only PostgreSQL evidence collector.

    This is intentionally a narrow query adapter.  It does not run migrations and
    never writes.  Missing tables mean the v2 wiring is incomplete and are surfaced
    as an ordinary failure by the caller rather than silently treated as success.
    """

    def __init__(self, dsn: str, *, scope: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn must not be blank")
        if not scope.strip():
            raise ValueError("scope must not be blank")
        self._dsn = dsn
        self.scope = scope

    def collect(self) -> dict[str, Any]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on optional live environment
            raise RuntimeError("psycopg is required only for --dsn evidence reads") from exc

        evidence: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "scope": self.scope,
            "events": [],
            "decisions": [],
            "exposures": [],
            "labels": [],
            "boundaries": [],
            "parameter_snapshots": [],
            "duplicate_events": 0,
        }
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                # Tables below are public persistence contracts, not Python internals.
                evidence["exposures"] = list(
                    connection.execute(
                        """SELECT exposure_id::text, scope_key AS scope, occurred_at,
                                  action, context, idempotency_key,
                                  context->>'send_ack_id' AS send_ack_id,
                                  COALESCE(context->>'delivery_basis', 'delivered') AS delivery_basis
                           FROM interaction_exposures_v2 WHERE scope_key = %s
                           ORDER BY occurred_at, exposure_id""",
                        (self.scope,),
                    ).fetchall()
                )
                evidence["labels"] = list(
                    connection.execute(
                        """SELECT target_label_id::text, scope_key AS scope, target_name AS target,
                                  target_value, evidence, label_version
                           FROM interaction_target_labels_v2 WHERE scope_key = %s
                           ORDER BY labelled_at, label_version""",
                        (self.scope,),
                    ).fetchall()
                )
                evidence["parameter_snapshots"] = list(
                    connection.execute(
                        """SELECT p.parameter_snapshot_id::text, p.scope_key AS scope,
                                  p.parameters, p.parameter_version,
                                  (a.deactivated_at IS NULL) AS active
                           FROM user_model_parameter_snapshots_v2 p
                           LEFT JOIN user_model_active_parameters_v2 a
                             ON a.scope_key = p.scope_key
                            AND a.parameter_snapshot_id = p.parameter_snapshot_id
                           WHERE p.scope_key = %s
                           ORDER BY p.parameter_version""",
                        (self.scope,),
                    ).fetchall()
                )
        return evidence


class BlackBoxScenario:
    """Construct and execute the minimum public-wire scenario."""

    def __init__(
        self,
        endpoints: EndpointConfig,
        scenario: ScenarioConfig,
        transport: JsonTransport | None = None,
    ) -> None:
        self.endpoints = endpoints
        self.scenario = scenario
        self.transport = transport or (
            DryRunTransport() if scenario.dry_run else UrlLibJsonTransport(endpoints.timeout_seconds)
        )

    def _event(self, event_id: str, text: str, occurred_at: datetime) -> dict[str, Any]:
        return {
            "protocol_version": "2",
            "event_id": event_id,
            "kind": "user_message",
            "scope": self.scenario.scope,
            "occurred_at": utc_iso(occurred_at),
            "message": {"message_type": "private", "user_id": "blackbox-user", "text": text},
        }

    def run(self) -> dict[str, Any]:
        start = self.scenario.seed_time
        event = self._event("bb-event-1", "明天下午面试，结束后告诉你。", start)
        onebot = {
            "post_type": "message",
            "message_type": "private",
            "time": int(start.timestamp()),
            "self_id": 90001,
            "user_id": 10001,
            "message_id": "bb-message-1",
            "raw_message": event["message"]["text"],
            "scope": self.scenario.scope,
            "event_id": event["event_id"],
        }
        self.transport.request(
            "POST", self.endpoints.onebot_url(self.endpoints.onebot_event_path), onebot
        )
        self.transport.request(
            "POST",
            self.endpoints.runtime_url(self.endpoints.runtime_event_path),
            {"protocol_version": "2", "scope": self.scenario.scope, "events": [event]},
        )
        # Exact replay: same event id and same body.
        self.transport.request(
            "POST",
            self.endpoints.runtime_url(self.endpoints.runtime_event_path),
            {"protocol_version": "2", "scope": self.scenario.scope, "events": [event]},
        )
        self.transport.request(
            "POST",
            self.endpoints.runtime_url(self.endpoints.decision_path),
            {
                "protocol_version": "2",
                "scope": self.scenario.scope,
                "as_of": utc_iso(start + timedelta(hours=24)),
                "reason": "blackbox_e2e",
            },
        )
        query = urllib.parse.urlencode({"scope": self.scenario.scope})
        evidence = self.transport.request(
            "GET", f"{self.endpoints.runtime_url(self.endpoints.evidence_path)}?{query}"
        )
        return {
            "executed": not self.scenario.dry_run,
            "evidence": evidence,
            "exchanges": [asdict(item) for item in self.transport.exchanges],
        }


def synthetic_evidence(scope: str = DEFAULT_SCOPE) -> dict[str, Any]:
    """Return a complete synthetic fixture illustrating the proposed evidence wire shape."""
    parameter_id = str(uuid.UUID("00000000-0000-0000-0000-000000000101"))
    return {
        "contract_version": CONTRACT_VERSION,
        "scope": scope,
        "duplicate_events": 1,
        "events": [{"event_id": "bb-event-1", "kind": "user_message", "scope": scope}],
        "decisions": [
            {
                "decision_id": "bb-decision-1",
                "scope": scope,
                "parameter_snapshot_id": parameter_id,
                "stages": [{"stage": stage} for stage in REQUIRED_DECISION_STAGES],
                "sent": True,
                "boundary_blocked": False,
            },
            {
                "decision_id": "bb-boundary-decision",
                "scope": scope,
                "parameter_snapshot_id": parameter_id,
                "stages": [{"stage": "wake"}, {"stage": "permissions"}, {"stage": "reconciled"}],
                "sent": False,
                "boundary_blocked": True,
            },
        ],
        "exposures": [
            {
                "exposure_id": "bb-exposure-1",
                "scope": scope,
                "delivery_basis": "delivered",
                "send_ack_id": "onebot-message-9001",
            }
        ],
        "labels": [
            {"case": "in_window", "target": "reply", "status": "observed_positive", "value": True},
            {"case": "late", "target": "reply", "status": "observed_negative", "value": False},
            {"case": "no_reply", "target": "reply", "status": "observed_negative", "value": False},
        ],
        "boundaries": [{"scope": scope, "kind": "no_proactive_contact", "blocked": True}],
        "parameter_snapshots": [
            {
                "parameter_snapshot_id": parameter_id,
                "scope": scope,
                "parameter_version": 1,
                "parameters": {"reply_horizon_seconds": 21600},
                "active": True,
            }
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-base-url", default=os.getenv("V2_RUNTIME_BASE_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--onebot-base-url", default=os.getenv("V2_ONEBOT_BASE_URL", "http://127.0.0.1:5700"))
    parser.add_argument("--runtime-event-path", default="/v2/events")
    parser.add_argument("--decision-path", default="/v2/decisions/run")
    parser.add_argument("--evidence-path", default="/v2/blackbox/evidence")
    parser.add_argument("--onebot-event-path", default="/v2/test/events")
    parser.add_argument("--scope", default=os.getenv("V2_BLACKBOX_SCOPE", DEFAULT_SCOPE))
    parser.add_argument("--dsn", default=os.getenv("V2_BLACKBOX_PG_DSN"), help="optional PostgreSQL DSN; never printed")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--execute", action="store_true", help="perform HTTP traffic (default is dry-run)")
    parser.add_argument("--synthetic", action="store_true", help="validate the bundled fixture; not a live pass")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    endpoints = EndpointConfig(
        runtime_base_url=args.runtime_base_url,
        onebot_base_url=args.onebot_base_url,
        runtime_event_path=args.runtime_event_path,
        decision_path=args.decision_path,
        evidence_path=args.evidence_path,
        onebot_event_path=args.onebot_event_path,
        timeout_seconds=args.timeout,
    )
    scenario_config = ScenarioConfig(scope=args.scope, dry_run=not args.execute)

    if args.synthetic:
        evidence = synthetic_evidence(args.scope)
        report = ContractValidator().validate(evidence, expected_scope=args.scope, executed=True, source="synthetic_fixture")
        # Synthetic success tests the validator, never the deployment.
        output = {"live_contract_passed": False, "synthetic_fixture_valid": report.passed, "validation": report.to_dict()}
        code = 0 if report.passed else 1
    else:
        run = BlackBoxScenario(endpoints, scenario_config).run()
        evidence = run["evidence"]
        if args.dsn and args.execute:
            # PG evidence is supplemental until its normalized decision/event views are wired.
            run["postgres_read_only"] = PostgresEvidenceReader(args.dsn, scope=args.scope).collect()
        if scenario_config.dry_run:
            output = {
                "live_contract_passed": False,
                "status": "dry_run_not_executed",
                "scope": args.scope,
                "requests": run["exchanges"],
            }
            code = 0
        else:
            report = ContractValidator().validate(evidence, expected_scope=args.scope, executed=True, source="public_http")
            output = {"live_contract_passed": report.passed, "validation": report.to_dict(), "requests": run["exchanges"]}
            code = 0 if report.passed else 1

    safe = redact(output)
    if args.json:
        print(json.dumps(safe, ensure_ascii=False, indent=2, default=str))
    else:
        status = "PASS" if safe.get("live_contract_passed") else "NOT A LIVE PASS"
        print(f"v2 black-box: {status}")
        if safe.get("status"):
            print(safe["status"])
        validation = safe.get("validation") or {}
        for check in validation.get("checks", []):
            mark = "PASS" if check["passed"] else "FAIL"
            print(f"[{mark}] {check['name']}: {check['detail']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
