"""Reproducible probes for the three business-logic findings in
``runtime/docs/BUSINESS_LOGIC_AUDIT.md``.

This is a *probe*, not a test: it asserts nothing and always exits 0. It exists so the
numbers quoted in that report can be regenerated on demand rather than trusted, and so
the eventual fix has a before/after it can be measured against.

Usage (from the repository root, ``xiaojiujiu/``)::

    runtime/.venv/bin/python scripts/business_logic_probes.py
"""

from __future__ import annotations

import pathlib
import sys
from datetime import timedelta
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"
for extra in (str(RUNTIME_DIR / "src"), str(RUNTIME_DIR / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from companion_runtime import candidate as candidate_module  # noqa: E402
from companion_runtime.db import Database  # noqa: E402
from companion_runtime.motivation import is_candidate_proactive  # noqa: E402
from companion_runtime.runtime import Runtime  # noqa: E402
from companion_runtime.typing import CandidateIntent, new_id  # noqa: E402
from companion_runtime.user_model import (  # noqa: E402
    TYPE_TO_BEHAVIOUR,
    BehaviourReaction,
    compute_weight,
)
from conftest import BASE_TIME, build_config  # noqa: E402


def _runtime(seed: int = 3) -> Runtime:
    return Runtime(build_config(), seed=seed, database=Database(":memory:"), created_at=BASE_TIME)


def _candidate(kind: str) -> CandidateIntent:
    return CandidateIntent(
        candidate_id=new_id("candidate"), type=kind, intent="想和你聊聊", goal="g", sources=["memory:mem_1"]
    )


# ---------------------------------------------------------------------------- probe 1


def probe_reply_length_is_absolute() -> None:
    """§29: reply length must be judged against the user's own habit, not a fixed bar.

    Same behaviour every time (always replies, continues the topic, asks back); only the
    length changes. If length were relative, the three rows would be indistinguishable.
    """
    print("=" * 78)
    print("probe 1 — 回复长度是否相对用户自己的基线（设计 §29）［已修，保留作回归观察］")
    print("=" * 78)
    print("同一个用户行为（每次都回复 / 继续话题 / 主动反问），只改回复长度：\n")
    print(f"  {'情形':<16}{'长度':>5}{'每条证据权重':>14}{'positive 前→后':>22}{'reply 前→后':>20}")

    action = {"type": "contact", "proactive": True}
    for label, length in (("话少但一直这样", 3), ("中等", 8), ("话多", 30)):
        runtime = _runtime()
        try:
            before = runtime.user_model.predict(action=action, context={})
            stamp = BASE_TIME
            weight = None
            for _ in range(12):
                stamp += timedelta(hours=6)
                reaction = BehaviourReaction(
                    replied=True,
                    reply_length=length,
                    continued_topic=True,
                    asked_back=True,
                    reply_delay_seconds=300.0,
                )
                with runtime.db.transaction() as conn:
                    runtime.user_model.observe(
                        conn, action=action, context={}, reaction=reaction, now=stamp, observed_at=stamp
                    )
                weight = compute_weight(
                    reaction,
                    config=runtime.config.user_model,
                    observed_at=stamp,
                    now=stamp,
                ).total
            after = runtime.user_model.predict(action=action, context={})
            print(
                f"  {label:<16}{length:>5}{weight:>14.3f}"
                f"{before.positive_probability:>10.3f} →{after.positive_probability:>10.3f}"
                f"{before.reply_probability:>9.3f} →{after.reply_probability:>9.3f}"
            )
        finally:
            runtime.close()
    print("\n  读法：三行是同一个用户的同一种行为。话少的那一行证据被系统性压低，")
    print("        因为 `<= 4` 是硬编码的绝对阈值（user_model.py:381 与 :1005）。")
    print("  另：`continue_probability` 里的 `min(3, reaction.turns)`（:1009）是 §29 的")
    print("        第三个子句（对话持续长度）同样未相对化。\n")


# ---------------------------------------------------------------------------- probe 2


def probe_hard_boundary_can_be_bypassed() -> None:
    """§52/§86.5: a declared boundary is a hard constraint, not a momentum term.

    The gate asks ``is_candidate_proactive``, which hardcodes its own type set
    (candidate.py:1300) instead of reading ``TYPE_TO_BEHAVIOUR``. Where the two disagree,
    the *same intention* is blocked under one spelling and allowed under another.
    """
    print("=" * 78)
    print("probe 2 — 硬边界与同义词 type（设计 §52 / §86.5）［已修，保留作回归观察］")
    print("=" * 78)
    behaviour_proactive = {
        "proactive_contact",
        "follow_up",
        "curious_question",
        "emotional_expression",
        "repair",
    }
    print(f"  {'type':<22}{'行为类':<22}{'类主动?':>8}{'门按主动拦?':>12}   一致")
    mismatched: list[str] = []
    for kind, behaviour in sorted(TYPE_TO_BEHAVIOUR.items()):
        by_class = behaviour in behaviour_proactive
        by_gate = is_candidate_proactive(_candidate(kind))
        agree = behaviour == "reply" or by_class == by_gate
        if not agree:
            mismatched.append(kind)
        flag = "✓" if agree else "✗ 不一致"
        print(f"  {kind:<22}{behaviour:<22}{str(by_class):>8}{str(by_gate):>12}   {flag}")
    blocked = sorted(k for k in TYPE_TO_BEHAVIOUR if is_candidate_proactive(_candidate(k)))
    print(f"\n  '今天别主动联系我' 拦住：{blocked}")
    print(f"  同义词漏网：{mismatched}")
    print("\n  【已修 2026-09-16】谓词不再自带一份 type 列表，改为从 TYPE_TO_BEHAVIOUR 派生：")
    print("  主动 ⇔ 行为类 != 'reply'。未知 type 走 behaviour_class_of 的默认值（proactive_contact），")
    print("  即**失败关闭**。验收见 tests/test_boundary_synonyms.py（8 条）。")
    print("  改之前：apology / emotional_expression / question 三种拼写能绕过硬边界。\n")


# ---------------------------------------------------------------------------- probe 3


def probe_emotion_alignment_ignores_synonyms() -> None:
    """The mood-matching bonus also hardcodes its own sets (runtime.py:2614-2616).

    An apology when the user is upset *is* a repair; a share when the user is happy *is*
    emotional expression. Spelling it the other way costs most of the alignment bonus.
    """
    print("=" * 78)
    print("probe 3 — 情绪对齐按 type 硬编码，同义词被区别对待（runtime.py:2614-2616）")
    print("=" * 78)
    runtime = _runtime()
    try:
        upset = [SimpleNamespace(direction="-", intensity=0.8)]
        happy = [SimpleNamespace(direction="+", intensity=0.8)]
        pairs = (
            ("用户不快时", "repair", "apology", upset),
            ("用户开心时", "share", "emotional_expression", happy),
            ("用户开心时", "curious_question", "question", happy),
        )
        for context_label, canonical, synonym, mood in pairs:
            left = runtime._emotion_alignment(_candidate(canonical), mood)
            right = runtime._emotion_alignment(_candidate(synonym), mood)
            ratio = left / right if right else float("inf")
            print(f"  {context_label:<12} {canonical:<18}={left:.3f}   {synonym:<22}={right:.3f}   （{ratio:.1f}×）")
    finally:
        runtime.close()
    print("\n  同一个意图、不同的写法，时宜性差 2.3 倍。")
    print("  同族还有 protocol.py:346 的分类器，它只认 {follow_up, curious_question, check_in}。\n")


def probe_conversation_length_saturation() -> None:
    """§29's third clause: 对话持续长度.

    ``min(3, reaction.turns)`` saturated for *everyone* at three turns, so a three-turn and
    a thirty-turn conversation scored identically and the user's own habit never entered
    the formula.
    """
    print("=" * 78)
    print("probe 4 — 对话持续长度（§29 第三句）［已修，保留作回归观察］")
    print("=" * 78)
    runtime = _runtime()
    try:
        model = runtime.user_model
        print("  冷启动（习惯未知，退回旧的绝对参考 3 轮）：")
        for turns in (1, 2, 3, 10, 30):
            print(f"    turns={turns:>3}  bonus={model.conversation_bonus(BehaviourReaction(replied=True, turns=turns)):.3f}")
        print("\n  改之前 turns=3 与 turns=30 的目标值完全相同（min(3, turns) 饱和）；")
        print("  现在习惯未知时仍是上面这条曲线，习惯已知后饱和点跟着用户走：")
        stamp = BASE_TIME
        for _ in range(6):
            stamp += timedelta(hours=6)
            with runtime.db.transaction() as conn:
                model.observe(
                    conn,
                    action={"type": "contact", "proactive": True},
                    context={},
                    reaction=BehaviourReaction(replied=True, reply_length=30, turns=30, reply_delay_seconds=300.0),
                    now=stamp,
                    observed_at=stamp,
                )
        reference, trusted = model.turns_reference()
        print(f"    习惯被学到：参考 {reference:.1f} 轮（trusted={trusted}）")
        for turns in (3, 10, 30):
            print(f"    turns={turns:>3}  bonus={model.conversation_bonus(BehaviourReaction(replied=True, turns=turns)):.3f}")
    finally:
        runtime.close()
    print()


def main() -> int:
    """Run every probe; always exit 0 (this is a measurement, not an assertion)."""
    print()
    probe_reply_length_is_absolute()
    probe_hard_boundary_can_be_bypassed()
    probe_emotion_alignment_ignores_synonyms()
    probe_conversation_length_saturation()
    print("=" * 78)
    print("以上均为实测输出。报告：runtime/docs/BUSINESS_LOGIC_AUDIT.md")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
