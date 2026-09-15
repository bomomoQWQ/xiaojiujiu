"""Endogenous proactive long-term companion Runtime sidecar.

The Runtime is a sidecar process that owns the *cognitive* state of a long-lived
companion agent: time continuity, emotion, memory, knowledge about the user,
unfinished matters, proactive drive and the decision of whether to speak at all.

Design principles (see the architecture document in the workspace root):

* The main LLM is only responsible for the final wording, never for internal facts.
* Every state change goes through a single writer (the reducer) with a version.
* All model output arrives as a *proposal* and is classified APPLY / REBASE / DISCARD.
* Numbers carry the dynamics; language carries the semantics.
* Raw events are append-only and are never rewritten.
"""

from __future__ import annotations

__all__ = ["__version__", "RUNTIME_API_VERSION"]

__version__ = "0.3.1"

#: Version of the HTTP/proposal contract exposed to the host framework.
RUNTIME_API_VERSION = "1"
