"""黑盒：模型写的那条草稿，最后有没有真的说到用户面前。

为什么单开一个文件——仓库里那套黑盒仿真（``scripts/blackbox_user_simulation.py``）跑的是
**关掉语义层**的标准部署：

    config.semantic.provider = "disabled"
    config.semantic.deep_refresh_enabled = False

而且有一条 check 把这个事实钉住（"the Runtime runs with no semantic provider"）。所以那套
77/77 对"模型写的草稿能不能落地"这条路径覆盖为零——两个样例形状的毛病能在生产里活下来，
正是因为生产配了远端语义端点，而唯一的黑盒跑的是没配的那种部署。

这里把那个缺口补上：真插件 + 真 Runtime + mock 语义端点 + 假平台，也就是框架的黑盒侧。
而"黑盒"的意思是断言只从外面看——**只读用户实际收到的消息**，不碰 projections、不查数据库、
不读候选池。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import pytest

from cf.clock import parse_duration
from cf.harness import Harness, HarnessConfig
from cf.mock_openai import MockReply, MockScript
from conftest import PROGRAM_SRC, program_available

pytestmark = pytest.mark.skipif(
    not program_available(), reason="the program checkout or uvicorn is not available"
)

#: 只有模型写得出来的一句话：规则层的模板是「询问〈事项标题〉」「聊起之前记过的事：〈记忆摘要〉」，
#: 都不会产出这个措辞。所以它出现在用户屏幕上，就等于模型写的那条草稿走完了全链路。
#: 它**不含任何时刻**——草稿带时刻的话，池子那一道闸会直接拒绝它（另一个模块测那个）。
DRAFT = "问他那家手冲店还去不去"
#: DRAFT 里的指纹词。
FINGERPRINT = "手冲"
USER_LINE = "我明天下午三点面试，结束了告诉你。"
START = "2026-09-15T03:00:00Z"


@dataclass
class Delivered:
    """用户屏幕上出现了什么。"""

    proactive: list[str]
    replies: list[str]
    render_prompt: str


@pytest.fixture(scope="module")
def delivered(tmp_path_factory) -> Delivered:
    """让模型写一条草稿，等几小时，看它会不会说到用户面前。"""
    run_dir = tmp_path_factory.mktemp("model-drafted")
    # 选择是 softmax 抽签；温度压到 ~0 让它变成 argmax，被渲染的就一定是模型写的那条。
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
        event_id = events["events"][-1]["event_id"]

        # 草稿的形状**取自提示词里那段样例**，只把措辞、类型和拉力换成这次要测的。
        # 这样样例一旦退回旧形状，这条黑盒测试也会跟着死。
        from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT

        example = DEEP_REFRESH_SYSTEM_PROMPT.split("格式样例：", 1)[1].lstrip()
        sample, _ = json.JSONDecoder().raw_decode(example)
        item = sample["candidate_intent_operations"][0]
        assert "payload" in item, f"样例退回旧形状了：{item}"
        candidate = item["payload"]["candidate"]
        candidate.update(
            {
                "type": "share",
                "intent": DRAFT,
                "goal": "约见面",
                "confidence": 0.95,
                "internal_need": 0.95,
                "unfinished_relevance": 1.0,
            }
        )
        grounded = json.loads(json.dumps(sample, ensure_ascii=False).replace("evt_x", event_id))

        harness.mock.script = MockScript([MockReply(payload=grounded)], repeat_last=True)
        result = harness.program.refresh(now="2026-09-15T04:00:00Z", major_event=True)
        assert result["violations"] == [], result["violations"]

        harness.clock.advance(parse_duration("5h"))
        acted = None
        for _ in range(8):
            outcome = (harness.endogenous({"force": True}).get("decision") or {}).get("outcome") or {}
            if outcome.get("acted"):
                acted = outcome
                break
        assert acted is not None, "她始终没有决定开口"

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if [m for m in harness.platform.messages if m.kind == "proactive"]:
                break
            time.sleep(0.5)

        transcripts = harness.platform.messages
        renders = [call for call in harness.llm.calls if call.kind == "render"]
        return Delivered(
            proactive=[m.text for m in transcripts if m.kind == "proactive"],
            replies=[m.text for m in transcripts if m.kind == "reply"],
            render_prompt=renders[-1].prompt if renders else "",
        )
    finally:
        harness.stop()


def test_the_draft_the_model_wrote_reaches_the_user(delivered: Delivered) -> None:
    """黑盒断言：用户屏幕上出现的那句话，带着只有模型写得出的指纹词。

    这条路径以前是断的（样例教了一个读取方吃不了的形状 → 每次都
    ``candidate_intent:ValueError`` → 候选池空 → 模型写的草稿从来没说过）。它断了这么久
    没人发现，是因为唯一那套黑盒跑的是没配语义端的部署。
    """
    assert delivered.proactive, "用户什么也没收到"
    spoken = "\n".join(delivered.proactive)
    assert FINGERPRINT in spoken, spoken
    assert DRAFT in delivered.render_prompt, delivered.render_prompt
