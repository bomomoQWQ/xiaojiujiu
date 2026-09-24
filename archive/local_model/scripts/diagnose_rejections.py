"""Diagnose why synthetic emotion-explanation samples are rejected.

This is a development aid: it calls the teacher model for a few seed scenarios
and prints the validator violations that cause rejection, so the generation
prompt can be tightened without guessing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "training" / "src"))

from qboss_training.config import GenerationConfig  # noqa: E402
from qboss_training.data.client import DeepSeekClient  # noqa: E402
from qboss_training.data.generator import generate_one, _parse_generation_reply  # noqa: E402
from qboss_training.data.prompts import build_generation_messages  # noqa: E402
from qboss_training.data.seeds import sample_scenarios  # noqa: E402
from qboss_training.validators import validate_output  # noqa: E402


async def main() -> int:
    """Run the diagnostic and print the violation histogram."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("DEEPSEEK_API_KEY is required", file=sys.stderr)
        return 2

    config = GenerationConfig()
    config.task = "emotion_explain"
    config.max_sample_attempts = 1
    client = DeepSeekClient(config.client, config.retry)

    scenarios = sample_scenarios("emotion_explain", 4, seed=99)
    codes: Counter[str] = Counter()
    async with client:
        for scenario in scenarios:
            record, meta, problems = await generate_one(
                client, "emotion_explain", scenario, config=config
            )
            status = "accepted" if record else "rejected"
            print(f"\n=== {scenario.scenario_id} ({scenario.event_kind}) -> {status} ===")
            for problem in problems:
                codes[problem.split(":")[0]] += 1
                print(f"  {problem}")
    print("\nviolation histogram:", dict(codes))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
