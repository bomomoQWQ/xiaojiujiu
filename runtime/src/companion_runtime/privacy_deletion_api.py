"""Authorized HTTP control surface for privacy deletion v1."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Body, Header, HTTPException

from .privacy_deletion_repository import DeletionRequest, DeletionStrategy, PrivacyDeletionRepository
from .privacy_deletion_service import PrivacyDeletionCoordinator

AuthorizationCheck = Callable[[str, str], bool]


def create_privacy_deletion_router(*, scope_key: str, repository: PrivacyDeletionRepository,
                                   coordinator: PrivacyDeletionCoordinator,
                                   authorize: AuthorizationCheck) -> APIRouter:
    """Create an explicit fail-closed API; no default/implicit authorization exists."""
    if repository.scope_key != scope_key or coordinator.scope_key != scope_key:
        raise ValueError("privacy deletion components must use the configured scope")
    if not callable(authorize):
        raise TypeError("authorize callback is required")
    router = APIRouter(prefix="/v1/privacy/deletions", tags=["privacy-deletion"])

    def require(token: str | None) -> None:
        if not token or not authorize(scope_key, token):
            raise HTTPException(status_code=403, detail="privacy deletion authorization required")

    @router.post("", status_code=202)
    def request_deletion(payload: dict[str, Any] = Body(...),
                         authorization: str | None = Header(default=None)) -> Mapping[str, Any]:
        require(authorization)
        if payload.get("scope") != scope_key:
            raise HTTPException(status_code=403, detail="scope is not allowed")
        try:
            request = DeletionRequest(
                scope_key=scope_key,
                request_id=str(payload["request_id"]),
                requested_by=str(payload.get("requested_by", "user")),
                selector_kind=str(payload["selector_kind"]),
                selector=dict(payload.get("selector", {})),
                strategy=DeletionStrategy(str(payload["strategy"])),
                requested_at=datetime.now(timezone.utc),
            )
            created = coordinator.request(request)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"request_id": request.request_id, "created": created, "status": "pending"}

    @router.post("/{request_id}/run")
    def run_deletion(request_id: str,
                     authorization: str | None = Header(default=None)) -> Mapping[str, Any]:
        require(authorization)
        if repository.status(request_id=request_id) is None:
            raise HTTPException(status_code=404, detail="deletion request not found")
        return coordinator.run(request_id=request_id)

    @router.get("/{request_id}")
    def deletion_status(request_id: str,
                        authorization: str | None = Header(default=None)) -> Mapping[str, Any]:
        require(authorization)
        status = repository.status(request_id=request_id)
        if status is None:
            raise HTTPException(status_code=404, detail="deletion request not found")
        return status

    return router


__all__ = ["AuthorizationCheck", "create_privacy_deletion_router"]
