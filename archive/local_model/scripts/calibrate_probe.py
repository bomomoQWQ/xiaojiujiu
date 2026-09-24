"""Dump the endogenous decision payload for calibration."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(ROOT / "src"))

from companion_runtime.config import RuntimeConfig  # noqa: E402
from companion_runtime.db import Database  # noqa: E402
from companion_runtime.runtime import Runtime  # noqa: E402

BASE = datetime.fromisoformat("2026-03-01T09:00:00+00:00")


def make_config() -> RuntimeConfig:
    """Return an in-memory configuration."""
    config = RuntimeConfig()
    config.storage.mirror_raw_events = False
    config.storage.database_path = ":memory:"
    return config


def dump(label: str, config: RuntimeConfig) -> None:
    """Print the decision payload for one configuration."""
    runtime = Runtime(config, seed=7, database=Database(":memory:"), created_at=BASE)
    try:
        runtime.lazy_tick(BASE)
        outcome = runtime.endogenous_round(now=BASE + timedelta(hours=49), force=True)
        state = runtime.state()
        print(f"--- {label} ---")
        print(f"I={state.approach_impulse:.3f} R={state.restraint:.3f} P={state.pressure:.3f}")
        print(json.dumps(outcome.decision, ensure_ascii=False)[:1800])
    finally:
        runtime.close()


def main() -> int:
    """Dump both calibration scenarios."""
    dump("2b restrained", make_config())
    config = make_config()
    config.values.boundary_respect = 0.25
    config.values.stability_commitment = 0.30
    config.values.autonomy = 0.55
    config.values.relationship_maintenance = 0.95
    config.values.user_care = 0.95
    config.candidate.contact_baseline_prior = 0.10
    dump("2d unrestrained", config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
