"""The one definition of how a session becomes a slug and a schema.

Both names are identity: the supervisor gives a Runtime its schema from here, and every
operator tool derives the same name when it goes looking for that scope's rows. A second
copy of these rules anywhere would drift, and the symptom of holding two different answers
is the worst kind -- a Runtime that looks healthy while its rows live somewhere nobody
reads, or an operator inspecting an empty schema and concluding the character never acts.

Kept dependency-free on purpose: importing the supervisor for a naming rule would drag a
subprocess fleet, an HTTP server and thread bookkeeping into every operator tool.
"""

from __future__ import annotations

import hashlib

__all__ = ["slugify", "schema_for_session"]

#: The slug is a filesystem name (the child's data directory and log file) and a DNS label
#: (the fleet advertises ``<slug>`` to the plugin registry), so it stays short and safe.
SLUG_MAX_CHARS = 40

#: PostgreSQL truncates identifiers at 63 bytes, silently.
SCHEMA_MAX_BYTES = 63


def slugify(session: str) -> str:
    """Turn a session into a filesystem- and DNS-safe name.

    Args:
        session: The unified session origin, e.g. ``default:FriendMessage:10001``.

    Returns:
        The slug the supervisor uses for this person.
    """
    cleaned = "".join(char if char.isalnum() else "-" for char in session)
    return cleaned.strip("-").lower()[-SLUG_MAX_CHARS:] or "person"


def schema_for_session(session: str) -> str:
    """Return the PostgreSQL schema that belongs to one session.

    One schema per person is the isolation boundary: the retained mechanical projections
    (memory, candidates, outbox) are not tenant-keyed, so two people in one schema would
    blend even though the v2 tables carry ``scope_key``.

    Args:
        session: The unified session origin.

    Returns:
        The schema name holding this scope's rows.
    """
    raw = f"cr_{slugify(session).replace('-', '_')}"
    if len(raw.encode("utf-8")) > SCHEMA_MAX_BYTES:
        # Retain the recognizable session suffix instead of a bare truncation.
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        raw = (
            raw.encode("utf-8")[: SCHEMA_MAX_BYTES - 13].decode("utf-8", "ignore")
            + "_"
            + suffix
        )
    return raw
