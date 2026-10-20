"""Shared pytest fixtures for the Runtime test suite."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from companion_runtime.config import RuntimeConfig  # noqa: E402
from companion_runtime.db import Database  # noqa: E402
from companion_runtime.delivery import (  # noqa: E402
    DeliveryService,
    EchoRenderer,
    NullTransport,
    Transport,
)
from companion_runtime.runtime import Runtime  # noqa: E402
from companion_runtime.utility import utcnow  # noqa: E402

#: A fixed instant used as "now" so tests are deterministic.
BASE_TIME = datetime.fromisoformat("2026-03-01T09:00:00+00:00")


@dataclass(slots=True)
class Harness:
    """A Runtime plus its delivery stack, wired for tests."""

    runtime: Runtime
    config: RuntimeConfig
    service: DeliveryService
    transport: NullTransport
    start: datetime

    def advance(self, **kwargs) -> datetime:
        """Return a time offset from the harness start.

        Args:
            **kwargs: ``hours``, ``minutes`` or ``seconds`` offsets.

        Returns:
            The offset timestamp.
        """
        return self.start + timedelta(**kwargs)


def build_config(**overrides) -> RuntimeConfig:
    """Return a test configuration with the raw-event mirror disabled.

    Args:
        **overrides: Attribute overrides applied to the top-level config.

    Returns:
        A :class:`RuntimeConfig` suitable for ephemeral tests.
    """
    config = RuntimeConfig()
    config.storage.mirror_raw_events = False
    config.storage.database_path = ":memory:"
    # Existing tests exercise legacy text-only renderers deliberately. Production
    # defaults fail closed; strict-contract tests turn this explicit test switch off.
    config.allow_legacy_render_completion_inference = True
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


@pytest.fixture()
def config() -> RuntimeConfig:
    """A default test configuration."""
    return build_config()


@pytest.fixture()
def runtime(config: RuntimeConfig) -> Iterator[Runtime]:
    """A Runtime over an in-memory database, closed after the test.

    The creation epoch is pinned to :data:`BASE_TIME` so the simulated timeline
    used throughout the suite starts from a known instant.
    """
    instance = Runtime(config, seed=1234, database=Database(":memory:"), created_at=BASE_TIME)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def harness(config: RuntimeConfig) -> Iterator[Harness]:
    """A Runtime with a delivery service attached."""
    runtime_instance = Runtime(
        config, seed=1234, database=Database(":memory:"), created_at=BASE_TIME
    )
    transport = NullTransport()
    service = DeliveryService(
        reducer=runtime_instance.reducer,
        config=config,
        runtime=runtime_instance,
        renderer=EchoRenderer(),
        transport=transport,
    )
    try:
        yield Harness(
            runtime=runtime_instance,
            config=config,
            service=service,
            transport=transport,
            start=BASE_TIME,
        )
    finally:
        runtime_instance.close()


@pytest.fixture(scope="session", autouse=True)
def _offline_database_seam():
    """Let ``Runtime(...)`` run offline instead of demanding a PostgreSQL DSN.

    Production is PostgreSQL-only on purpose: ``open_database`` refuses to start without
    ``CR_STORAGE__DSN``, and that refusal is a feature, not something to soften for tests.
    But the suite is built to run offline -- the shared fixtures here always injected their
    own database -- while three dozen files construct ``Runtime`` on their own without
    passing one. On a machine with no database those errored during construction, so
    ``pytest`` reported 230 red for a tree that was fine, while the repo's own README
    promises "全部通过". A suite that fails because the *developer's machine* lacks a
    database is a suite defect, not a finding.

    The stand-in is a **file** database, not ``:memory:``, and that detail is the whole
    point: the durability tests exercise WAL, checkpoint and backup, and an in-memory
    database has none of those ("cannot back up an in-memory database"). A seam that turns
    32 errors into 32 failures has not helped anyone.

    With ``CR_STORAGE__DSN`` set, nothing is patched and the real PostgreSQL path is
    exercised exactly as shipped.
    """
    import itertools
    import os
    import tempfile

    from companion_runtime import runtime as runtime_module

    if os.environ.get("CR_STORAGE__DSN"):
        yield
        return

    session_dir = tempfile.mkdtemp(prefix="cr-test-db-")
    counter = itertools.count()

    def _file_database(storage) -> Database:
        # Honour an explicit path first. The durability tests point database_path at a
        # path they then inspect ("...runtime.sqlite3-wal" must exist next to it), so
        # handing them a generated file would break the very contract they assert - the
        # seam is supposed to replace a *missing* database, not override a chosen one.
        chosen = str(getattr(storage, "database_path", "") or "").strip()
        if chosen and chosen != ":memory:":
            return Database(chosen)
        # One generated file per construction: state must not leak between tests, and a
        # shared file would let one test's WAL sidecar decide another test's recovery.
        return Database(os.path.join(session_dir, f"db-{next(counter):04d}.sqlite3"))

    original = runtime_module.open_database
    runtime_module.open_database = _file_database
    try:
        yield
    finally:
        runtime_module.open_database = original
