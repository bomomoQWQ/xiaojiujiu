"""Nothing may be declared and never produced (the audit's first and largest theme).

The recurring defect this project keeps finding is not a wrong number: it is a name that
reads as "this happens" while nothing happens. ``EventType`` is the tightest place to
enforce the opposite, because an enum member is uniquely named, has no dynamic callers, and
"is it emitted" is a question the source can answer exactly.

Four members were removed on this basis (``TICK``, ``USER_MODEL_SUMMARY``,
``EMOTION_EVENT_EVAL``, ``MEMORY_CONSOLIDATED``) and one was given a producer
(``REAPPRAISAL``, design §67). This test keeps the set honest: adding a member without
emitting it fails here.
"""

from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src/companion_runtime"

#: Members an *external* caller emits into the append-only log rather than the Runtime.
#: ``POST /events`` accepts any ``event_type`` string, so these are reachable by the host
#: even though nothing in this package appends them. They are listed explicitly rather than
#: inferred, so adding another one is a decision instead of an accident.
HOST_WRITTEN: frozenset[str] = frozenset({"TOOL_RESULT"})


def _event_type_members() -> list[str]:
    tree = ast.parse((SRC / "typing.py").read_text(encoding="utf-8"), filename="typing.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "EventType":
            return [
                stmt.targets[0].id
                for stmt in node.body
                if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)
            ]
    raise AssertionError("EventType not found")


def _producers() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SRC.glob("*.py"))
        if path.name != "typing.py"
    )


def test_every_event_type_has_a_producer() -> None:
    """Each member is either appended by this package, or explicitly host-written."""
    producers = _producers()
    members = _event_type_members()
    assert members, "the enum must not be empty"

    unproduced = [
        name
        for name in members
        if name not in HOST_WRITTEN and not re.search(rf"EventType\.{name}\b", producers)
    ]
    assert not unproduced, (
        "these EventType members are never emitted by the package; either emit them, "
        f"list them in HOST_WRITTEN with a reason, or delete them: {unproduced}"
    )


def test_the_allowlist_does_not_outlive_its_reason() -> None:
    """An allowlist entry that no longer names a member is a stale exemption."""
    members = set(_event_type_members())
    stale = sorted(HOST_WRITTEN - members)
    assert not stale, f"HOST_WRITTEN names members that no longer exist: {stale}"


def test_the_reappraisal_event_type_is_the_one_the_reducer_appends() -> None:
    """The design asks for a *reappraisal event* by name (design §67).

    Pinned separately from the generic check above because deleting the member would also
    satisfy "no orphans" - and that would hide the gap instead of closing it.
    """
    assert "REAPPRAISAL" in _event_type_members()
    assert re.search(r"EventType\.REAPPRAISAL\b", _producers())
