"""时间：钟只有一个来源，而每个时刻都有锚，所以出入是可查的。

历史：意图写于上午、几小时后才说出口（封测实测中位 2.3 小时、最长 6.0 小时），于是
17:53 的渲染开口是"早呀"、11:44 生出的意图在 13:30 说成"中午人少"。当时唯一的钟印在
**背景块**里，而背景块以「不是这一句该怎么回」收尾——等于把时间来源预先贬成了背景。

现在的做法不是禁止草稿里有时刻（草稿的素材就是用户自己的话，抹掉它就抹掉了内容），而是
**把两个时刻都摆出来，让出入可查**：

* 「现在是」——演出的钟，代码给；
* 「写于」——这条草稿动笔的时刻，代码从候选上读，不是模型写的；
* 草稿里含相对时刻时，另外**点名**是哪个词要按现在换算。

于是"11:44 写的'中午'在 13:30 说出口"不再是一句悄悄变假的话，而是一个明写的、可以核对的
偏差。代价要说清楚：**换算最终是渲染模型做的**，所以这一条只保证"信息齐全、偏差被点名"，
不保证"模型一定照办"——下面那条 ``xfail`` 钉的就是这个残差。

场景用真插件 + 真 Runtime + 框架替身模型跑。替身按 ``- 我想做的：`` 造句
（``main_llm._render_from_prompt``），所以它**不会**换算，正好演示那个残差。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from cf.clock import parse_duration
from cf.harness import Harness, HarnessConfig
from conftest import PROGRAM_SRC, program_available

pytestmark = pytest.mark.skipif(
    not program_available(), reason="the program checkout or uvicorn is not available"
)

#: 场景依赖 CST(+08:00)：11:00 写的草稿、16:00 才说出口。
#: 换个时区这个场景本身就不成立，所以宁可跳掉，也不要让它在那里以另一种意义通过。
if datetime.now().astimezone().utcoffset() != timedelta(hours=8):
    pytest.skip("this scenario is written for a UTC+08:00 clock", allow_module_level=True)

START = "2026-09-15T03:00:00Z"  # 11:00 CST
LATER = "5h"  # → 16:00 CST；候选 TTL 是 6 小时，所以草稿还活着
USER_LINE = "我明天下午三点面试，结束了告诉你。"
#: 草稿带着**写它那一刻**的相对时刻。它允许存在（内容不能被抹掉），但必须被点名为"要换算"。
DRAFT = "问他明天中午还去不去那家手冲店"
FINGERPRINT = "手冲"
#: 草稿里那个相对时刻——提示词应当点名它。
DRIFTED = "明天"

CLOCK_LINE = re.compile(r"^- 现在是：(?P<stamp>.+)$", re.MULTILINE)
WRITTEN_LINE = re.compile(r"^- 写于：(?P<stamp>.+)$", re.MULTILINE)
TASK_HEADER = "【我现在要说的话】"
TASK_LINE = re.compile(r"^- 我想做的：(?P<intent>.+)$", re.MULTILINE)
BACKGROUND_RULING = "不是这一句该怎么回"
BLOCK_CLOCK_PREFIXES = ("- 当前本地时间：", "- 现在是：", "- 写于：")
BACKGROUND_KEEPS = "- 距离上次用户消息："
STAMP = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S"


@dataclass
class Timeline:
    """一次跑下来值得断言的东西。"""

    drafted_at: str
    render_clock: str
    written_stamp: str
    render_at: str
    prompt: str
    sent: list[str]


@pytest.fixture(scope="module")
def timeline(tmp_path_factory) -> Timeline:
    """写一条带时刻的草稿 → 等 5 小时 → 逼她开口 → 收提示词与发出的正文。"""
    run_dir = tmp_path_factory.mktemp("proactive-timing")
    config = run_dir / "cfg.toml"
    config.write_text("[utility]\ntemperature = 0.01\n", encoding="utf-8")
    harness = Harness(
        HarnessConfig(
            run_dir=run_dir,
            program_src=PROGRAM_SRC,
            start_time=START,
            time_scale=0.0,
            heartbeat_interval_s=0,
            config_path=str(config),
            echo_logs=False,
        )
    )
    harness.start()
    try:
        harness.user_turn(USER_LINE)
        events = harness.program.get("/events", event_type="user_message", limit=5)
        source = events["events"][-1]["event_id"]

        applied = harness.program.post(
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
        assert not applied["rejected"], applied
        rows = harness.program.get("/candidates").get("candidates") or []
        mine = next((row for row in rows if row["intent"] == DRAFT), None)
        assert mine is not None, rows
        drafted_at = mine["created_at"]

        harness.clock.advance(parse_duration(LATER))

        acted = None
        for _ in range(8):
            outcome = (harness.endogenous({"force": True}).get("decision") or {}).get("outcome") or {}
            if outcome.get("acted"):
                acted = outcome
                break
        assert acted is not None, "她始终没有决定开口"
        assert acted["chosen_candidate_id"] == mine["candidate_id"], acted

        deadline = time.monotonic() + 60
        sent: list[str] = []
        while time.monotonic() < deadline:
            sent = [m.text for m in harness.platform.messages if m.kind == "proactive"]
            if sent:
                break
            time.sleep(0.5)
        assert sent, "主动消息没有投递到平台"

        renders = [call for call in harness.llm.calls if call.kind == "render"]
        assert renders, "没有发生渲染调用"
        prompt = renders[-1].prompt
        clock = CLOCK_LINE.search(prompt)
        written = WRITTEN_LINE.search(prompt)
        assert clock is not None, "演出那一段里没有当前时间"
        assert written is not None, "演出那一段里没有草稿的写就时刻"
        return Timeline(
            drafted_at=drafted_at,
            render_clock=clock.group("stamp").strip(),
            written_stamp=written.group("stamp").strip(),
            render_at=harness.clock.now().isoformat(),
            prompt=prompt,
            sent=sent,
        )
    finally:
        harness.stop()


class TestTheTwoAnchors:
    """起作用的不是"禁止时刻"，是"两个时刻都在，偏差被点名"。"""

    def test_the_draft_was_authored_hours_before_it_was_sent(self, timeline: Timeline) -> None:
        """草稿生于 11:00，发出在 16:00 —— 5 小时的滞后依然存在。"""
        drafted = datetime.fromisoformat(timeline.drafted_at).astimezone()
        assert drafted.strftime("%H:%M") == "11:00", timeline.drafted_at
        gap = datetime.fromisoformat(timeline.render_at) - datetime.fromisoformat(
            timeline.drafted_at
        )
        assert gap.total_seconds() >= 4 * 3600, f"草稿只等了 {gap}"

    def test_both_moments_are_stated_and_read_the_same_way(self, timeline: Timeline) -> None:
        """「现在是」是发出的钟（16:00），「写于」是草稿动笔的钟（11:00）。

        两个都要在，而且格式必须一样——不能放在一起比的两个时刻等于没给。
        """
        assert "16:00" in timeline.render_clock, timeline.render_clock
        assert "11:00" in timeline.written_stamp, timeline.written_stamp
        for stamp in (timeline.render_clock, timeline.written_stamp):
            assert re.match(STAMP, stamp), stamp

    def test_the_drifting_word_is_named(self, timeline: Timeline) -> None:
        """点名比"注意时间"有用：模型知道要换算的是哪个词。"""
        hint = timeline.prompt.split("留意：", 1)[1].splitlines()[0]
        assert DRIFTED in hint, hint
        assert "写于" in hint, hint

    def test_the_clock_is_stated_as_an_instruction(self, timeline: Timeline) -> None:
        """两个时刻都在演出那一段里；背景块里一个都没有。"""
        prompt = timeline.prompt
        header_at = prompt.index(TASK_HEADER)
        ruling_at = prompt.index(BACKGROUND_RULING)
        clock_at = prompt.index(CLOCK_LINE.search(prompt).group(0))
        written_at = prompt.index(WRITTEN_LINE.search(prompt).group(0))
        assert header_at < min(clock_at, written_at), "时刻还留在背景里"
        assert min(clock_at, written_at) > ruling_at, "时刻仍被『不是这一句该怎么回』罩着"
        for prefix in BLOCK_CLOCK_PREFIXES:
            assert prefix not in prompt[:header_at], f"注入块里还留着时刻：{prefix}"

    def test_the_background_still_carries_cognition(self, timeline: Timeline) -> None:
        """搬走的只有钟。"距上次多久"是认知状态，它留在原处。"""
        assert BACKGROUND_KEEPS in timeline.prompt
        assert BACKGROUND_RULING in timeline.prompt

    def test_the_render_prompt_repeats_the_draft_verbatim(self, timeline: Timeline) -> None:
        """任务那一行是 5 小时前存的原文——换算发生在写正文那一步，不是改草稿。"""
        task = TASK_LINE.search(timeline.prompt)
        assert task is not None, "渲染提示词里没有『我想做的』"
        assert task.group("intent").strip() == DRAFT


class TestWhatTheUserGets:
    """残差就在这里：换算最终由渲染模型做，代码只保证信息齐全。"""

    def test_the_stub_model_still_copies_the_draft(self, timeline: Timeline) -> None:
        """替身模型不看时刻，只按『我想做的』造句，于是把"明天"原样带了出去。

        这不是提示词的锅：两个时刻都在，偏差也被点名了。它说明的是**这条防线止于"可查"**——
        真正把"明天"换成"今天中午"的那一步在渲染模型手里。要硬保证就得回到代码换算，
        而那会重新引入"抹掉内容"的问题。
        """
        assert timeline.sent, "没有发出的正文可查"
        assert any(FINGERPRINT in text for text in timeline.sent), timeline.sent

    @pytest.mark.xfail(
        strict=True,
        reason="已知残差：替身模型不换算，于是 16:00 的消息仍带着 11:00 的『明天』。"
        "这一条要变绿，靠的是渲染模型照时间口径办，或者把换算挪回代码。",
    )
    def test_the_drifting_word_does_not_reach_the_user(self, timeline: Timeline) -> None:
        assert not any(DRIFTED in text for text in timeline.sent), timeline.sent
