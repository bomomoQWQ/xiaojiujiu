"""The new candidate shapes fire through the Runtime, not only in unit tests.

`candidate.py` can produce `share`, `repair` and `reply` from real state, and each of
those producers reads something the *round* has to hand it: what the character holds
about the user, the evidence that something landed badly, a question nothing answered.
That wiring is one call site, and for as long as it was missing every producer was
reachable only by calling `generate()` directly - which is exactly the "declared but
never wired" shape this project keeps finding. These tests drive the Runtime instead.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime import candidate as candidate_module
from companion_runtime.runtime import Runtime

from conftest import BASE_TIME

PREFERENCE = "记住，我平时只喝手冲咖啡，不加糖。"
QUESTION = "对了，你平时喜欢听什么歌？"


def _live_candidates(runtime: Runtime) -> list[dict]:
    """Return the live candidates the operator surface would show."""
    return [
        candidate.to_dict()
        for candidate in runtime.projections.candidates.list_active(limit=50)
    ]


def _refresh(runtime: Runtime, *, now) -> list[dict]:
    """Generate candidates the way a round does, and report the live pool."""
    with runtime.write_session():
        with runtime.db.transaction() as conn:
            state = runtime.projections.runtime.ensure()
            runtime._refresh_candidates(
                now=now, state=state, active=runtime.projections.emotion.list_active()
            )
    return _live_candidates(runtime)


def test_a_preference_the_character_holds_becomes_a_share(runtime: Runtime) -> None:
    """``share`` needs the activation pool, which only the round can hand it."""
    runtime.process_user_message(content=PREFERENCE, timestamp=BASE_TIME)
    runtime.consolidate(now=BASE_TIME + timedelta(hours=2))
    assert runtime.projections.memory.list_activated(limit=10), (
        "the preference must be in the working set for the shape to be about it"
    )

    candidates = _refresh(runtime, now=BASE_TIME + timedelta(hours=3))

    shares = [item for item in candidates if item["type"] == "share"]
    assert shares, f"no share candidate was generated: {[item['type'] for item in candidates]}"
    share = shares[0]
    assert share["sources"], "a shape must name the state it came from"
    assert any(source.startswith("memory:") for source in share["sources"]), share["sources"]


def test_a_question_the_user_asked_becomes_a_reply(runtime: Runtime) -> None:
    """``reply`` needs the event log, which only the round can hand it."""
    runtime.process_user_message(content=QUESTION, timestamp=BASE_TIME)

    candidates = _refresh(runtime, now=BASE_TIME + timedelta(minutes=30))

    replies = [item for item in candidates if item["type"] == "reply"]
    assert replies, f"no reply candidate was generated: {[item['type'] for item in candidates]}"
    reply = replies[0]
    assert any(source.startswith("evt_") for source in reply["sources"]), reply["sources"]


def test_the_round_hands_every_generator_input_it_has(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring itself, pinned: five inputs, none of them empty.

    ``share`` and ``reply`` are asserted above through the pool. This one protects the
    *call site* directly, because the producers' admission thresholds (the pool's
    confidence floor, the target de-duplication) are their own tests' business: what
    was missing in production was that the round never handed them the state at all,
    and a spy is the only thing that can fail when that regresses without any producer
    changing.
    """
    runtime.process_user_message(content=PREFERENCE, timestamp=BASE_TIME)
    runtime.process_user_message(content=QUESTION, timestamp=BASE_TIME + timedelta(minutes=1))
    runtime.process_user_message(
        content="以后别再一直追问我在干嘛。", timestamp=BASE_TIME + timedelta(minutes=2)
    )
    runtime.consolidate(now=BASE_TIME + timedelta(hours=2))

    seen: dict[str, object] = {}
    original = candidate_module.generate

    def spy(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(candidate_module, "generate", spy)
    with runtime.write_session():
        with runtime.db.transaction() as conn:
            state = runtime.projections.runtime.ensure()
            runtime._refresh_candidates(
                now=BASE_TIME + timedelta(hours=3),
                state=state,
                active=runtime.projections.emotion.list_active(),
            )

    for name in ("observations", "emotions", "situations", "boundaries", "recent_events"):
        assert name in seen, f"the round never passed {name} to the generator"
    assert seen["recent_events"], "the event log is never empty after three messages"
    assert seen["boundaries"], "a declared boundary must reach the generator"
    assert seen["situations"], "the working situation must reach the generator"
