"""Where the list of scopes comes from, and why it is not a constant in the code.

A deployment's scopes are operator data -- one per person being served -- so they belong
in the roster, not in a tuple inside an operations script that everyone has to edit. That
tuple was the single biggest self-deployment obstacle: adding a user meant editing
committed code, and a fresh deployment inherited somebody else's QQ numbers.

Resolution order, most explicit first:

1. ``--scopes a,b`` / ``CR_LANGCHAO_SCOPES`` -- an operator who knows exactly what to act on;
2. the fleet's people file -- the supervisor's own record of who it serves. A plain file,
   which matters: the emergency stop must not depend on the component it may need to stop,
   so this is preferred over asking the supervisor;
3. the control surface -- authoritative while the fleet is up, useless while it is wedged;
4. nothing resolved: raise, with instructions, instead of acting on a guess.

Dependency-free beyond the standard library: this is read on the fail-closed path.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

__all__ = [
    "DEFAULT_PEOPLE_FILE",
    "DEFAULT_CONTROL_URL",
    "resolve_scopes",
    "people_file_scopes",
    "control_surface_scopes",
    "parse_scope_list",
]

#: Where the fleet writes its roster. ``fleet-v2.yml`` passes this to the supervisor.
DEFAULT_PEOPLE_FILE = Path(os.environ.get("CR_FLEET_PEOPLE_FILE") or "/fleet-data/people.json")

#: The supervisor's control surface, reachable from inside the fleet container.
DEFAULT_CONTROL_URL = os.environ.get("CR_FLEET_CONTROL_URL") or "http://127.0.0.1:8800"


def parse_scope_list(raw: str) -> tuple[str, ...]:
    """Split a comma- or newline-separated scope list, dropping blanks.

    Args:
        raw: e.g. ``"default:FriendMessage:1, default:FriendMessage:2"``.

    Returns:
        The scopes, in the order given.
    """
    parts = [chunk.strip() for chunk in raw.replace("\n", ",").split(",")]
    return tuple(part for part in parts if part)


def people_file_scopes(path: Path | str = DEFAULT_PEOPLE_FILE) -> tuple[str, ...]:
    """Return the sessions in the supervisor's people file.

    Args:
        path: Location of the people file.

    Returns:
        The sessions it lists; empty when the file is missing or unreadable, because a
        caller that cannot read the roster must decide for itself what to do about it.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if isinstance(data, dict):
        data = data.get("people", [])
    if not isinstance(data, list):
        return ()
    sessions: list[str] = []
    for item in data:
        if isinstance(item, str):
            sessions.append(item)
        elif isinstance(item, dict) and item.get("session"):
            sessions.append(str(item["session"]))
    return tuple(dict.fromkeys(sessions))


def control_surface_scopes(control_url: str = DEFAULT_CONTROL_URL) -> tuple[str, ...]:
    """Return the sessions the running supervisor reports.

    Args:
        control_url: Base URL of the fleet control surface.

    Returns:
        The sessions, or empty when the control surface cannot be reached.
    """
    try:
        with urllib.request.urlopen(f"{control_url}/fleet/status", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return ()
    people = payload.get("people") if isinstance(payload, dict) else None
    if not isinstance(people, list):
        return ()
    sessions = [
        str(item["session"]) for item in people
        if isinstance(item, dict) and item.get("session")
    ]
    return tuple(dict.fromkeys(sessions))


def resolve_scopes(
    explicit: str | tuple[str, ...] | None = None,
    *,
    people_file: Path | str = DEFAULT_PEOPLE_FILE,
    control_url: str = DEFAULT_CONTROL_URL,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Return the scopes an operator tool should act on.

    Args:
        explicit: ``--scopes`` value, or an already-split tuple.
        people_file: Where the supervisor records its roster.
        control_url: The control surface to fall back to.
        allow_empty: Return an empty tuple instead of raising when nothing is known.

    Returns:
        The resolved scopes.

    Raises:
        RuntimeError: When no source yields a scope and ``allow_empty`` is false. The
            message names every source tried, because "acted on nothing" and "acted on the
            wrong deployment's scopes" are both worse than stopping.
    """
    if isinstance(explicit, str):
        explicit = parse_scope_list(explicit)
    if explicit:
        return tuple(explicit)

    from_env = parse_scope_list(os.environ.get("CR_LANGCHAO_SCOPES") or "")
    if from_env:
        return from_env

    from_file = people_file_scopes(people_file)
    if from_file:
        return from_file

    from_control = control_surface_scopes(control_url)
    if from_control:
        return from_control

    if allow_empty:
        return ()
    raise RuntimeError(
        "no scopes resolved. Pass --scopes, or set CR_LANGCHAO_SCOPES, or make the roster "
        f"readable (people file: {people_file}; control surface: {control_url}). Acting on a "
        "guessed list would freeze or roll the wrong people's scopes."
    )
