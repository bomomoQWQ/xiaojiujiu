"""HTTP-level tests for the two cognition endpoints (patch v0.2).

The refresh endpoint is the operational entry point for the only automated path
that can change long-term state, and the backlog endpoint is how an operator
sees what the Runtime declined to guess about. Both must be honest about
declining rather than silently succeeding.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from companion_runtime.api import create_app
from companion_runtime.providers import DeepRefreshSuggestions
from companion_runtime.runtime import Runtime
from companion_runtime.typing import EventType

from conftest import BASE_TIME, build_config


@pytest.fixture()
def client() -> TestClient:
    """A Runtime with no provider, plus its HTTP client."""
    config = build_config()
    config.semantic.deep_refresh_min_interval_seconds = 0.0
    runtime = Runtime(config=config)
    try:
        yield TestClient(create_app(runtime, config))
    finally:
        runtime.close()


def _post_ambiguous(client: TestClient, text: str = "算了，也没什么。") -> dict:
    """Ingest one message and return the ``MessageOutcome`` payload."""
    body = client.post(
        "/events",
        json={
            "event_type": EventType.USER_MESSAGE.value,
            "content": text,
            "timestamp": BASE_TIME.isoformat(),
        },
    ).json()
    return body["outcome"]


class TestBacklogEndpoint:
    """What the Runtime has left uninterpreted must be inspectable."""

    def test_backlog_starts_empty(self, client: TestClient) -> None:
        body = client.get("/cognition/backlog").json()
        assert body["stats"]["unresolved"] == 0
        assert body["items"] == []

    def test_an_ambiguous_event_lands_in_the_backlog(self, client: TestClient) -> None:
        outcome = _post_ambiguous(client)
        assert outcome["semantic_status"] == "unresolved"
        body = client.get("/cognition/backlog").json()
        assert body["stats"]["unresolved"] == 1
        item = body["items"][0]
        assert item["event_id"] == outcome["event"]["event_id"]
        assert item["semantic_status"] == "unresolved"
        assert item["content"] == "算了，也没什么。"

    def test_an_explicit_event_does_not_land_in_the_backlog(self, client: TestClient) -> None:
        outcome = _post_ambiguous(client, "谢谢你，我今天好多了。")
        assert outcome["semantic_status"] == "resolved"
        assert client.get("/cognition/backlog").json()["stats"]["unresolved"] == 0

    def test_backlog_reports_relevance_bands(self, client: TestClient) -> None:
        _post_ambiguous(client, "你觉得我们以后会一直这样吗")
        stats = client.get("/cognition/backlog").json()["stats"]
        assert stats["by_relevance"].get("high") == 1


class TestRefreshEndpoint:
    """The refresh endpoint declines loudly and applies visibly."""

    def test_refresh_declines_without_a_provider(self, client: TestClient) -> None:
        body = client.post("/cognition/refresh", json={"force": True}).json()
        assert body["ran"] is False
        assert body["reason"] == "provider_unavailable"

    def test_health_exposes_the_provider_and_the_backlog(self, client: TestClient) -> None:
        _post_ambiguous(client)
        health = client.get("/health").json()
        assert health["semantic_provider"]["provider"] == "disabled"
        assert health["semantic_provider"]["available"] is False
        assert health["semantics"]["unresolved"] == 1

    def test_a_disabled_runtime_still_serves_every_endpoint(self, client: TestClient) -> None:
        """Patch v0.2: nothing about the Runtime may depend on a model."""
        for path in ("/health", "/cognition/backlog", "/context", "/boundaries"):
            assert client.get(path).status_code == 200
        assert client.post("/context/render-block", json={}).status_code == 200

    def test_refresh_applies_a_grounded_bundle_over_http(self) -> None:
        config = build_config()
        config.semantic.deep_refresh_min_interval_seconds = 0.0
        runtime = Runtime(config=config)
        try:
            client = TestClient(create_app(runtime, config))
            first = _post_ambiguous(client)

            class _Provider:
                name = "stub"

                def available(self) -> bool:
                    return True

                def deep_refresh(self, request, *, timeout_s=None):  # noqa: ANN001
                    return DeepRefreshSuggestions(
                        provider="stub",
                        degraded=False,
                        reinterpretations=[
                            {
                                "content": "那时他大概是失望的。",
                                "sources": [first["event"]["event_id"]],
                            }
                        ],
                    )

                def explain_state(self, payload, *, state_key=""):  # noqa: ANN001
                    return None

                def health(self) -> dict:
                    return {"name": self.name, "available": True}

            runtime.semantic_provider = _Provider()
            body = client.post("/cognition/refresh", json={"force": True}).json()
            assert body["ran"] is True
            assert body["applied"].get("reinterpretation") == 1
            assert body["settled_events"] == 1
            assert client.get("/cognition/backlog").json()["stats"]["unresolved"] == 0
        finally:
            runtime.close()

    def test_an_ungrounded_bundle_changes_nothing(self) -> None:
        config = build_config()
        runtime = Runtime(config=config)
        try:
            client = TestClient(create_app(runtime, config))
            _post_ambiguous(client)

            class _Provider:
                name = "stub"

                def available(self) -> bool:
                    return True

                def deep_refresh(self, request, *, timeout_s=None):  # noqa: ANN001
                    return DeepRefreshSuggestions(
                        degraded=False,
                        memory_suggestions=[{"summary": "编造的事", "sources": ["evt_ghost"]}],
                    )

                def explain_state(self, payload, *, state_key=""):  # noqa: ANN001
                    return None

                def health(self) -> dict:
                    return {"name": self.name, "available": True}

            runtime.semantic_provider = _Provider()
            body = client.post("/cognition/refresh", json={"force": True}).json()
            assert body["ran"] is False
            assert body["reason"] == "all_suggestions_ungrounded"
            # The backlog must stay open: nothing was actually understood.
            assert client.get("/cognition/backlog").json()["stats"]["unresolved"] == 1
            assert runtime.projections.memory.pending_candidates() == []
        finally:
            runtime.close()
