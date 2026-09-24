#!/usr/bin/env python3
"""抓下 Runtime 交给主 LLM 的两份提示词原文，写进 ``docs/prompt_samples/``。

改过提示词之后跑一次，`runtime/docs/PROMPTS_TO_THE_MAIN_LLM.md` 里贴的样例才不会过期——
那份文档里的"逐字"就是这两份文件，手抄一遍必然走样。

用法：

    runtime/.venv/bin/python scripts/dump_runtime_prompt_samples.py

抓的是两份：

* ``runtime_block_A.txt``    注入块 —— 回话那一轮，插件注入给主 LLM 的整段；
* ``runtime_render_B.txt``   渲染提示词 —— 主动消息那一次单独调用，块 + 【我现在要说的话】。

场景固定（用户 11:00 说"我明天下午三点面试，结束了告诉你"，写一条带相对时刻的草稿，
16:00 才发出去），这样两次跑出来的差异只可能来自提示词本身。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "framework"))

from cf.clock import parse_duration  # noqa: E402
from cf.harness import Harness, HarnessConfig  # noqa: E402

PROGRAM_SRC = ROOT / "runtime" / "src"
SAMPLES = ROOT / "docs" / "prompt_samples"

#: 11:00 CST，草稿写好的时刻。
START = "2026-09-15T03:00:00Z"
USER_LINE = "我明天下午三点面试，结束了告诉你。"
#: 带着写它那一刻的相对时刻——渲染提示词里那句「留意：」就是为它出现的。
DRAFT = "问他明天中午还去不去那家手冲店"


def main() -> int:
    """Render both prompts once and write them out."""
    run_dir = Path(tempfile.mkdtemp(prefix="prompt-samples-"))
    config = run_dir / "cfg.toml"
    # Pin the candidate draw so "which draft got rendered" is not a coin flip.
    config.write_text("[utility]\ntemperature = 0.01\n", encoding="utf-8")
    harness = Harness(
        HarnessConfig(
            run_dir=run_dir,
            program_src=PROGRAM_SRC,
            start_time=START,
            time_scale=0.0,
            heartbeat_interval_s=0,
            config_path=str(config),
            # Absolute: the framework's default is relative to *its* directory, and this
            # script runs from the repository root.
            plugin_root=ROOT / "astrbot_plugin_companion_runtime",
            echo_logs=False,
        )
    )
    harness.start()
    try:
        harness.user_turn(USER_LINE)
        events = harness.program.get("/events", event_type="user_message", limit=5)
        source = events["events"][-1]["event_id"]
        harness.program.post(
            "/candidates/operations",
            {
                "operations": [
                    {
                        "op": "add",
                        "candidate": {
                            "type": "share",
                            "intent": DRAFT,
                            "goal": "约见面",
                            "sources": [source],
                            "confidence": 1.0,
                            "internal_need": 1.0,
                            "unfinished_relevance": 1.0,
                            "emotion_relevance": 1.0,
                        },
                    }
                ]
            },
        )
        harness.clock.advance(parse_duration("5h"))
        for _ in range(8):
            outcome = (harness.endogenous({"force": True}).get("decision") or {}).get("outcome") or {}
            if outcome.get("acted"):
                break

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if [m for m in harness.platform.messages if m.kind == "proactive"]:
                break
            time.sleep(0.3)

        block = harness.program.post("/context/render-block", {}).get("block", "")
        renders = [call for call in harness.llm.calls if call.kind == "render"]
        if not renders:
            print("no render happened; nothing to dump", file=sys.stderr)
            return 1
        prompt = renders[-1].prompt
    finally:
        harness.stop()

    SAMPLES.mkdir(parents=True, exist_ok=True)
    (SAMPLES / "runtime_block_A.txt").write_text(block + "\n", encoding="utf-8")
    (SAMPLES / "runtime_render_B.txt").write_text(prompt + "\n", encoding="utf-8")
    print(f"===== 注入块 → {SAMPLES / 'runtime_block_A.txt'} =====")
    print(block)
    print(f"===== 渲染提示词 → {SAMPLES / 'runtime_render_B.txt'} =====")
    print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
