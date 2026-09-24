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
