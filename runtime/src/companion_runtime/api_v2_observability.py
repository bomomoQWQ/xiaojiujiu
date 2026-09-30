"""Public, read-only Runtime-v2 observability endpoints.

The router owns no database connection and never reaches into legacy projections.  All
returned evidence comes from explicitly injected repositories, is constrained to one
configured scope, and passes through a final defensive credential scrub before FastAPI
serializes it.  A decision-run route is deliberately absent unless a simulation runner is
both injected and explicitly enabled by the composition root.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping, Protocol

from fastapi import APIRouter, Body, HTTPException, Query

from .config import redact_tree

_REDACTED = "***redacted***"
_URI_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", re.I)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")


class V2EvidenceRepository(Protocol):
    """Read capability implemented by a v2 runtime repository."""

    def read_blackbox_evidence(self, *, scope_key: str) -> Any: ...


class V2AuditRepository(Protocol):
    """Read capability for persisted decision-v2 audit records."""

    def list_decision_audits(self, *, scope_key: str) -> Any: ...


DecisionSimulationRunner = Callable[[Mapping[str, Any]], Any]


def _jsonable(value: Any) -> Any:
    """Detach repository results without exposing arbitrary object internals."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    raise TypeError(f"v2 evidence contains a non-JSON value: {type(value).__name__}")


def _scrub_strings(value: Any) -> Any:
    """Mask common inline credentials in addition to secret-shaped mapping keys."""

    value = redact_tree(value)
    if isinstance(value, Mapping):
        return {str(key): _scrub_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_strings(item) for item in value]
    if isinstance(value, str):
        value = _URI_CREDENTIALS.sub(lambda match: match.group("scheme") + _REDACTED + "@", value)
        return _BEARER.sub("Bearer " + _REDACTED, value)
    return value


def _safe(value: Any) -> Any:
    return _scrub_strings(_jsonable(value))


def _health_payload(health: Any) -> dict[str, Any]:
    payload = health.to_dict() if callable(getattr(health, "to_dict", None)) else health
    if not isinstance(payload, Mapping):
        raise TypeError("v2 health must be a mapping or expose to_dict()")
    result = dict(_safe(payload))
    # Jev is not a public capability in v2 yet.  Do not expose provider/config details.
    result["jev"] = {"status": "disabled"}
    result.setdefault("status", "ok")
    return result


def _read(repository: Any, names: tuple[str, ...], *, scope_key: str) -> Any:
    for name in names:
        reader = getattr(repository, name, None)
        if callable(reader):
            return reader(scope_key=scope_key)
    expected = " or ".join(names)
    raise RuntimeError(f"injected repository is missing read capability: {expected}")


def create_v2_observability_router(
    *,
    scope_key: str,
    runtime_repository: V2EvidenceRepository | Any,
    audit_repository: V2AuditRepository | Any,
    health: Any,
    enable_decision_run: bool = False,
    decision_simulation_runner: DecisionSimulationRunner | None = None,
) -> APIRouter:
    """Create the isolated ``/v2`` public router.

    ``scope_key`` is an allow-list, not a default: callers must supply ``scope`` and it
    must match exactly.  The optional POST route is registered only for an explicitly
    enabled simulation composition; production callers therefore receive 404 rather than
    discovering a dormant mutation endpoint.
    """

    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key is required")
    if enable_decision_run and decision_simulation_runner is None:
        raise ValueError("an enabled decision run requires a simulation runner")

    router = APIRouter(prefix="/v2", tags=["v2-observability"])

    @router.get("/health")
    def v2_health() -> dict[str, Any]:
        return _health_payload(health)

    @router.get("/blackbox/evidence")
    def blackbox_evidence(scope: str = Query(..., min_length=1)) -> dict[str, Any]:
        if scope != scope_key:
            # Avoid confirming whether any other tenant/scope exists.
            raise HTTPException(status_code=403, detail="scope is not allowed")
        runtime_evidence = _read(
            runtime_repository,
            ("read_blackbox_evidence", "blackbox_evidence", "list_blackbox_evidence"),
            scope_key=scope_key,
        )
        audits = _read(
            audit_repository,
            ("list_decision_audits", "read_decision_audits", "blackbox_decision_audits"),
            scope_key=scope_key,
        )
        return _safe(
            {
                "scope": scope_key,
                "read_only": True,
                "evidence": runtime_evidence,
                "decision_audits": audits,
            }
        )

    if enable_decision_run:
        assert decision_simulation_runner is not None

        @router.post("/decisions/run")
        def simulate_decision(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
            if payload.get("simulate") is not True:
                raise HTTPException(status_code=422, detail="simulate=true is required")
            requested_scope = payload.get("scope")
            if requested_scope != scope_key:
                raise HTTPException(status_code=403, detail="scope is not allowed")
            result = decision_simulation_runner(dict(payload))
            return _safe({"scope": scope_key, "simulated": True, "result": result})

    return router


__all__ = [
    "DecisionSimulationRunner",
    "V2AuditRepository",
    "V2EvidenceRepository",
    "create_v2_observability_router",
]
