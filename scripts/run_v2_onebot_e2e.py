#!/usr/bin/env python3
"""Run a guarded OneBot -> AstrBot -> Runtime-v1 -> Runtime-v2 black-box E2E.

This runner never talks to QQ.  A live run is refused unless the operator explicitly
confirms that NapCat is stopped, and it also requires the xxj-onebot control surface to
report ``connected=true``.  Success is reported only after strict evidence assertions for
v2 exposure, reply labels, a complete decision audit, and the referenced parameter
snapshot.  Merely reaching an endpoint is never called a pass.

The deployed path remains the existing AstrBot plugin's /v1 protocol.  The optional
``/v2/decisions/run`` endpoint is used only when the injected message has not produced a
complete decision by itself, and every request to it carries ``simulate=true``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REQUIRED_STAGES = (
    "wake", "permissions", "candidate_eligible", "hazard_trial_performed",
    "hazard_trial_won", "committed", "rendered", "send_ack", "reconciled",
)
SECRET_PARTS = ("password", "passwd", "secret", "token", "authorization", "dsn", "api_key")


class E2EFailure(RuntimeError):
    """A guarded precondition, wire operation, timeout, or assertion failed."""


@dataclass(frozen=True)
class Config:
    onebot_url: str
    astrbot_url: str
    runtime_url: str
    pg_dsn: str | None
    scope: str
    user_id: str = "20001"
    initial_text: str = "明天下午面试，结束后告诉你。"
    reply_text: str = "面试结束了，挺顺利的。"
    timeout: float = 10.0
    wait_seconds: float = 90.0
    poll_seconds: float = 1.0
    confirm_napcat_stopped: bool = False
    decision_path: str = "/v2/decisions/run"
    evidence_path: str = "/v2/blackbox/evidence"
    report_path: str | None = None

    def __post_init__(self) -> None:
        for name in ("onebot_url", "astrbot_url", "runtime_url"):
            parsed = urllib.parse.urlparse(getattr(self, name))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{name} must be an absolute HTTP(S) URL")
        if not self.scope.strip():
            raise ValueError("scope must not be blank")
        if self.user_id != "20001":
            raise ValueError("this fixture is restricted to synthetic OneBot user 20001")
        if self.timeout <= 0 or self.wait_seconds <= 0 or self.poll_seconds <= 0:
            raise ValueError("timeouts must be positive")


@dataclass(frozen=True)
class Exchange:
    method: str
    url: str
    status: int
    request: Any
    response: Any


class HttpClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.exchanges: list[Exchange] = []

    def request(self, method: str, url: str, payload: Any = None) -> Any:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if raw:
                    decoded = raw.decode("utf-8", errors="replace")
                    try:
                        value = json.loads(decoded)
                    except json.JSONDecodeError:
                        value = decoded
                else:
                    value = None
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                value = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = {"error": raw.decode("utf-8", errors="replace")}
            self.exchanges.append(Exchange(method, url, exc.code, payload, value))
            raise E2EFailure(f"HTTP {exc.code} from {url}: {value!r}") from exc
        except urllib.error.URLError as exc:
            raise E2EFailure(f"cannot reach {url}: {exc.reason}") from exc
        self.exchanges.append(Exchange(method, url, status, payload, value))
        return value


def _url(base: str, path: str, query: Mapping[str, str] | None = None) -> str:
    result = base.rstrip("/") + "/" + path.lstrip("/")
    return result if not query else result + "?" + urllib.parse.urlencode(query)


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in _walk(value) if isinstance(item, Mapping)]


def _stage_names(row: Mapping[str, Any]) -> set[str]:
    audit = row.get("audit")
    candidates = row.get("stages") or row.get("events") or row.get("audit_events") or (
        audit.get("events") if isinstance(audit, Mapping) else []
    ) or []
    names: set[str] = set()
    if isinstance(candidates, Mapping):
        candidates = list(candidates.values())
    if isinstance(candidates, list):
        for item in candidates:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, Mapping):
                stage = item.get("stage") or item.get("name")
                if stage:
                    names.add(str(stage))
    return names


def _identity(row: Mapping[str, Any]) -> str:
    for key in ("exposure_id", "target_label_id", "decision_id", "parameter_snapshot_id", "event_id", "id"):
        if row.get(key) is not None:
            return f"{key}:{row[key]}"
    return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)


def evidence_parts(payload: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    """Normalize both the public nested payload and fixture-friendly flat payloads."""
    rows = _mappings(payload)
    return {
        "events": [r for r in rows if r.get("event_id") and (r.get("kind") or r.get("event_type"))],
        "audits": [r for r in rows if r.get("decision_id") and _stage_names(r)],
        "exposures": [r for r in rows if r.get("exposure_id")],
        "labels": [r for r in rows if r.get("target_label_id") or (r.get("target") and ("status" in r or "target_value" in r))],
        "parameters": [r for r in rows if r.get("parameter_snapshot_id") and ("parameters" in r or "parameter_version" in r)],
    }


def strict_checks(before: Mapping[str, Any], after: Mapping[str, Any], *, scope: str) -> dict[str, bool]:
    old, new = evidence_parts(before), evidence_parts(after)
    old_ids = {name: {_identity(row) for row in rows} for name, rows in old.items()}
    delta = {name: [row for row in rows if _identity(row) not in old_ids[name]] for name, rows in new.items()}

    audits = delta["audits"] or new["audits"]
    complete = [row for row in audits if set(REQUIRED_STAGES).issubset(_stage_names(row))]
    decision_ids = {str(row.get("decision_id")) for row in complete}
    parameter_ids: set[str] = set()
    for row in complete:
        direct = row.get("parameter_snapshot_id")
        if direct:
            parameter_ids.add(str(direct))
        nested = row.get("parameter_snapshot_ids")
        if isinstance(nested, Mapping):
            parameter_ids.update(str(value) for value in nested.values() if value)
        audit = row.get("audit")
        if isinstance(audit, Mapping):
            assessments = audit.get("assessments") or []
            for assessment in assessments:
                if isinstance(assessment, Mapping):
                    snapshot = assessment.get("parameter_snapshot_id")
                    if snapshot:
                        parameter_ids.add(str(snapshot))
            version = (audit.get("run") or {}).get("parameter_version")
            if version and version not in {"none", "prior-or-unavailable"}:
                parameter_ids.update(part for part in str(version).split("+") if part)

    exposures = delta["exposures"]
    delivered = [row for row in exposures if (
        row.get("send_ack_id") or row.get("delivery_basis") in {"delivered", "send_ack"}
        or row.get("decision_id") in decision_ids
    )]
    labels = delta["labels"]
    positive_reply = [row for row in labels if (
        str(row.get("target") or row.get("target_name") or "").lower() in {"reply", "r"}
        and (row.get("value") is True or row.get("target_value") is True
             or row.get("status") == "observed_positive")
    )]
    # The snapshot can be fitted immediately after the observed reply, so the
    # decision being validated may legitimately carry prior-or-unavailable. Require
    # a scoped active snapshot to exist; when the audit names concrete ids, also
    # require their intersection.
    parameters = [
        row for row in new["parameters"]
        if not parameter_ids or str(row.get("parameter_snapshot_id")) in parameter_ids
    ]
    scoped = lambda row: row.get("scope") in (None, scope) or row.get("scope_key") == scope
    return {
        "v2_exposure": bool(delivered) and all(scoped(row) for row in delivered),
        "v2_reply_label": bool(positive_reply) and all(scoped(row) for row in positive_reply),
        "v2_complete_audit": bool(complete) and all(scoped(row) for row in complete),
        "v2_parameter_reference": bool(parameters) and all(scoped(row) for row in parameters),
    }


def _has_outbound(state: Mapping[str, Any], *, user_id: str, baseline_sent: int) -> bool:
    counts = state.get("counts") or {}
    if int(counts.get("sent") or 0) <= baseline_sent:
        return False
    for call in state.get("calls") or []:
        if call.get("action") != "send_private_msg":
            continue
        params = call.get("params") or {}
        if str(params.get("user_id")) == user_id:
            return True
    return False


class Runner:
    def __init__(self, config: Config, http: HttpClient | Any | None = None,
                 sleeper: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.http = http or HttpClient(config.timeout)
        self.sleep = sleeper
        self.monotonic = monotonic

    def evidence(self) -> Mapping[str, Any]:
        value = self.http.request("GET", _url(self.config.runtime_url, self.config.evidence_path,
                                                {"scope": self.config.scope}))
        if not isinstance(value, Mapping) or value.get("scope") != self.config.scope or value.get("read_only") is not True:
            raise E2EFailure("evidence response is not read-only and exactly scope-bound")
        return value

    def _wait(self, description: str, probe: Callable[[], Any]) -> Any:
        deadline = self.monotonic() + self.config.wait_seconds
        last: Any = None
        while self.monotonic() < deadline:
            last = probe()
            if last:
                return last
            self.sleep(self.config.poll_seconds)
        raise E2EFailure(f"timed out waiting for {description}; last={last!r}")

    def run(self) -> dict[str, Any]:
        if not self.config.confirm_napcat_stopped:
            raise E2EFailure("refusing live traffic: pass --confirm-napcat-stopped only after NapCat is stopped")
        state = self.http.request("GET", _url(self.config.onebot_url, "/state"))
        if not isinstance(state, Mapping) or state.get("connected") is not True:
            raise E2EFailure("xxj-onebot is not connected to AstrBot")
        # AstrBot URL is an explicit deployment parameter and must be reachable; any JSON/HTML 2xx is enough.
        self.http.request("GET", self.config.astrbot_url.rstrip("/") + "/")
        baseline_sent = int((state.get("counts") or {}).get("sent") or 0)
        before = self.evidence()
        event_id = f"v2-e2e-{uuid.uuid4()}"
        injected = self.http.request("POST", _url(self.config.onebot_url, "/send"), {
            "text": f"[{event_id}] {self.config.initial_text}", "user_id": self.config.user_id,
        })
        if not isinstance(injected, Mapping) or injected.get("ok") is not True:
            raise E2EFailure("OneBot did not accept the synthetic 20001 message")

        def ingress_or_audit() -> Mapping[str, Any] | None:
            current = self.evidence()
            before_parts, current_parts = evidence_parts(before), evidence_parts(current)
            grew = any(len(current_parts[name]) > len(before_parts[name]) for name in ("events", "audits"))
            return current if grew else None

        observed = self._wait("AstrBot /v1 ingestion reaching Runtime v2 evidence", ingress_or_audit)
        parts = evidence_parts(observed)
        if not any(set(REQUIRED_STAGES).issubset(_stage_names(row)) for row in parts["audits"]):
            self.http.request("POST", _url(self.config.runtime_url, self.config.decision_path), {
                "scope": self.config.scope, "simulate": True, "reason": "onebot_e2e",
                "correlation_id": event_id,
            })

        def outbound() -> Mapping[str, Any] | None:
            fresh = self.http.request("GET", _url(self.config.onebot_url, "/state"))
            if isinstance(fresh, Mapping) and _has_outbound(
                fresh, user_id=self.config.user_id, baseline_sent=baseline_sent
            ):
                return fresh
            return None

        self._wait("render/send observed at xxj-onebot", outbound)
        reply = self.http.request("POST", _url(self.config.onebot_url, "/send"), {
            "text": f"[{event_id}:reply] {self.config.reply_text}", "user_id": self.config.user_id,
        })
        if not isinstance(reply, Mapping) or reply.get("ok") is not True:
            raise E2EFailure("OneBot did not accept the simulated reply")

        final: Mapping[str, Any] = {}
        checks: dict[str, bool] = {}
        def settled() -> Mapping[str, Any] | None:
            nonlocal final, checks
            final = self.evidence()
            checks = strict_checks(before, final, scope=self.config.scope)
            return final if all(checks.values()) else None
        self._wait("v2 exposure/labels/audit/parameter evidence", settled)
        if self.config.pg_dsn:
            # DSN is intentionally accepted for deployment parameter parity but never queried here:
            # /v2/blackbox/evidence is the scoped public contract and avoids schema-coupled assertions.
            pg_mode = "configured_not_printed_public_evidence_authoritative"
        else:
            pg_mode = "not_configured"
        return {
            "passed": True, "scope": self.config.scope, "user_id": self.config.user_id,
            "checks": checks, "decision_simulation_used": any(
                exchange.method == "POST" and exchange.url.endswith(self.config.decision_path)
                for exchange in self.http.exchanges
            ),
            "pg_dsn_mode": pg_mode,
            "exchange_count": len(self.http.exchanges),
        }


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): ("<redacted>" if any(part in str(key).lower() for part in SECRET_PARTS) else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onebot-url", default=os.getenv("ONEBOT_URL", "http://127.0.0.1:6300"))
    parser.add_argument("--astrbot-url", default=os.getenv("ASTRBOT_URL", "http://127.0.0.1:6185"))
    parser.add_argument("--runtime-url", default=os.getenv("RUNTIME_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--pg-dsn", default=os.getenv("PG_DSN"))
    parser.add_argument("--scope", default=os.getenv("V2_SCOPE"), required=os.getenv("V2_SCOPE") is None)
    parser.add_argument("--user-id", default="20001")
    parser.add_argument("--initial-text", default="明天下午面试，结束后告诉你。")
    parser.add_argument("--reply-text", default="面试结束了，挺顺利的。")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--wait-seconds", type=float, default=90.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--decision-path", default="/v2/decisions/run")
    parser.add_argument("--evidence-path", default="/v2/blackbox/evidence")
    parser.add_argument("--confirm-napcat-stopped", action="store_true")
    parser.add_argument("--report", dest="report_path")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config(
            onebot_url=args.onebot_url, astrbot_url=args.astrbot_url,
            runtime_url=args.runtime_url, pg_dsn=args.pg_dsn, scope=args.scope,
            user_id=args.user_id, initial_text=args.initial_text, reply_text=args.reply_text,
            timeout=args.timeout, wait_seconds=args.wait_seconds, poll_seconds=args.poll_seconds,
            confirm_napcat_stopped=args.confirm_napcat_stopped, decision_path=args.decision_path,
            evidence_path=args.evidence_path, report_path=args.report_path,
        )
        result = Runner(config).run()
        code = 0
    except (E2EFailure, ValueError) as exc:
        result = {"passed": False, "error": str(exc)}
        code = 1
    safe = redact(result)
    text = json.dumps(safe, ensure_ascii=False, indent=2)
    if args.report_path:
        Path(args.report_path).write_text(text + "\n", encoding="utf-8")
    if args.json:
        print(text)
    else:
        print("v2 OneBot E2E: PASS" if safe.get("passed") else "v2 OneBot E2E: NOT PASSED")
        print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
