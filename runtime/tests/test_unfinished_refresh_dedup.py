"""A deep refresh must not re-open an obligation it has already recorded.

The refresh re-reads the *same* unresolved events on every run, so an event it has
already understood gets interpreted again and the model re-derives the same
obligation with fresh wording. Nothing on the deep-refresh path consulted the
subject-reservation machinery the ingest path uses, so each run appended another
copy.

Measured on the live test deployment before the guard existed: ten open matters,
all tracing to two events ("🫡，当个事办" and "同 qq 昵称"), five copies each,
created over twelve hours -- and not one of them was ever resolved, so the open set
only grew. Every matter is injected into the context block, so this is a slow leak
straight into the character's prompt.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from companion_runtime.api import create_app
from companion_runtime.providers import DeepRefreshSuggestions
from companion_runtime.runtime import Runtime
from companion_runtime.typing import EventType

from conftest import BASE_TIME, build_config


def _provider(*suggestions: dict) -> object:
    """Return a stub provider answering with ``unfinished_matter_suggestions``."""

    class _Provider:
        name = "stub"

        def available(self) -> bool:
            return True

        def deep_refresh(self, request, *, timeout_s=None):  # noqa: ANN001
            return DeepRefreshSuggestions(
                provider="stub",
                degraded=False,
                unfinished_matter_suggestions=list(suggestions),
            )

    return _Provider()


@pytest.fixture()
def runtime() -> Runtime:
    """A Runtime whose refresh is allowed to run whenever it is asked."""
    config = build_config()
    config.semantic.deep_refresh_min_interval_seconds = 0.0
    instance = Runtime(config=config)
    try:
        yield instance
    finally:
        instance.close()


def _ingest(client: TestClient, text: str) -> str:
    """Ingest one ambiguous message and return its event id."""
    body = client.post(
        "/events",
        json={
            "event_type": EventType.USER_MESSAGE.value,
            "content": text,
            "timestamp": BASE_TIME.isoformat(),
        },
    ).json()
    return body["outcome"]["event"]["event_id"]


def _open_titles(runtime: Runtime) -> list[str]:
    """Return the titles of every open matter."""
    return [matter.title for matter in runtime.projections.unfinished.list_open()]


def test_the_same_event_cannot_open_a_second_matter(runtime: Runtime) -> None:
    """The case the live deployment hit: same event, reworded proposal.

    The wording differs between runs on purpose here, because that is what the model
    does -- a title comparison alone would not have caught it.
    """
    client = TestClient(create_app(runtime, runtime.config))
    event_id = _ingest(client, "🫡，当个事办")

    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "用户答应“当个事办”的具体事项尚未明确，可能需要后续确认。", "sources": [event_id]}
    )
    first = runtime.deep_refresh(force=True)
    assert first.applied.get("unfinished_matter") == 1
    assert len(_open_titles(runtime)) == 1

    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "“当个事办”具体指什么尚未明确，可在自然语境下轻量确认", "sources": [event_id]}
    )
    second = runtime.deep_refresh(force=True)
    assert _open_titles(runtime) == _open_titles(runtime)[:1], "no second copy"
    assert len(_open_titles(runtime)) == 1, "the reworded proposal must not create one"
    assert "unfinished_matter" not in (second.applied or {}), "and must not be counted as applied"


def test_the_event_is_still_marked_understood_when_the_matter_is_a_restatement(
    runtime: Runtime,
) -> None:
    """Skipping the duplicate must not leave the event unresolved forever.

    The source events of a restated obligation were understood -- that is *why* a
    matter already exists -- so they still count as settled. Otherwise the backlog
    would keep triggering refreshes about something already dealt with.
    """
    client = TestClient(create_app(runtime, runtime.config))
    event_id = _ingest(client, "🫡，当个事办")
    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "当个事办还没说清", "sources": [event_id]}
    )
    runtime.deep_refresh(force=True)
    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "当个事办到底指什么", "sources": [event_id]}
    )
    runtime.deep_refresh(force=True)
    assert runtime.projections.semantics.unresolved_count() == 0


def test_the_same_subject_from_another_event_is_refused(runtime: Runtime) -> None:
    """The wording-independent half of the guard: the subject is already taken."""
    client = TestClient(create_app(runtime, runtime.config))
    first_event = _ingest(client, "🫡，当个事办")
    second_event = _ingest(client, "对了，那件事我记着呢")

    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "等待面试结果", "sources": [first_event]}
    )
    runtime.deep_refresh(force=True)
    assert len(_open_titles(runtime)) == 1

    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "等待面试结果", "sources": [second_event]}
    )
    runtime.deep_refresh(force=True)
    assert len(_open_titles(runtime)) == 1, "the same subject cannot be held twice"


def test_a_genuinely_new_obligation_is_still_created(runtime: Runtime) -> None:
    """The guard must not become a blanket refusal.

    A different event with a different subject is a new obligation and has to be
    recorded, or the fix would trade a leak for amnesia. The second message is
    deliberately one the *rule* detector does not turn into a matter ("告诉你结果"
    would have), so this exercises the refresh path on its own.
    """
    client = TestClient(create_app(runtime, runtime.config))
    first_event = _ingest(client, "🫡，当个事办")
    second_event = _ingest(client, "在想一件事")

    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "当个事办具体指什么", "sources": [first_event]}
    )
    runtime.deep_refresh(force=True)

    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "等待体检结果", "sources": [second_event]}
    )
    outcome = runtime.deep_refresh(force=True)
    assert outcome.applied.get("unfinished_matter") == 1
    titles = _open_titles(runtime)
    assert len(titles) == 2
    assert any("体检" in title for title in titles)


def test_the_rule_path_and_the_refresh_do_not_both_create_one(runtime: Runtime) -> None:
    """A promise the ingest detector already recorded is not the refresh's to open.

    Found while writing the test above: "面试完告诉你结果" is turned into
    "等待面试结果" by the rule detector the moment it arrives. The refresh then
    re-reads that same event and proposes the same obligation, which is how the live
    deployment ended up with five copies of one matter -- the two paths were never
    aware of each other.
    """
    client = TestClient(create_app(runtime, runtime.config))
    event_id = _ingest(client, "面试完告诉你结果")
    from_rule = _open_titles(runtime)
    assert from_rule, "the rule detector should have recorded this promise"
    assert any("面试" in title for title in from_rule)

    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "等待面试结果", "sources": [event_id]}
    )
    outcome = runtime.deep_refresh(force=True)
    assert len(_open_titles(runtime)) == len(from_rule), "the refresh must not add a copy"
    assert "unfinished_matter" not in (outcome.applied or {})


def test_two_matters_from_one_batch_do_not_duplicate_each_other(runtime: Runtime) -> None:
    """Within one refresh, the second copy of a just-created matter is refused too.

    The guard reads the stored matters, so it must also see the one this same batch
    just wrote -- otherwise a model that returns the same item twice reopens the
    original bug in miniature.
    """
    client = TestClient(create_app(runtime, runtime.config))
    event_id = _ingest(client, "🫡，当个事办")
    runtime.semantic_provider = _provider(  # type: ignore[assignment]
        {"title": "当个事办的具体事项", "sources": [event_id]},
        {"title": "当个事办的具体事项", "sources": [event_id]},
    )
    runtime.deep_refresh(force=True)
    assert len(_open_titles(runtime)) == 1
