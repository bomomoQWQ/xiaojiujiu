"""主模型这一侧：主动消息那次调用，到底有没有带上宿主人格。

背景（实测，见 ``docs/PROMPT_REVIEW.md`` 的四条路径表）：AstrBot 给**普通对话轮**拼
4210 字的系统提示词（人格 732 + Skills 块 + 工具提醒），而**主动消息渲染**那次
``llm_generate`` 一个字都没有——人格、工具、历史全缺。所以渲染出来的那句话，是一个
从没被告知自己是谁的模型写的。

修法落在插件侧：宿主的东西归宿主。插件在普通对话轮里记住宿主拼好的 ``req.system_prompt``
（按会话），渲染时把它当 ``system_prompt`` 传下去；Runtime 若明确指定了就用 Runtime 的，
都没学到最后就照旧（失败开放）。

这一条端到端验的就是"人格真的到了渲染那次调用"，不是"代码看起来接对了"：
框架的 ``HostContext.llm_generate`` 以前把 ``system_prompt`` 直接 ``del`` 掉，
所以这条测试同时也钉着"别再把它吞了"。
"""

from __future__ import annotations

import time

import pytest

from cf.clock import parse_duration
from cf.harness import Harness, HarnessConfig
from conftest import PROGRAM_SRC, program_available

pytestmark = pytest.mark.skipif(
    not program_available(), reason="the program checkout or uvicorn is not available"
)

START = "2026-09-15T03:00:00Z"
USER_LINE = "我明天下午三点面试，结束了告诉你。"
#: 一个有指纹的"宿主拼好的系统提示词"。真实部署里它就是 persona + skills + 工具提醒。
PERSONA = "人格设定：测试用角色\n- 一次只说一两句，30 字以内，不用 Markdown"


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Harness:
    """一个配了宿主系统提示词的框架黑盒。"""
    run_dir = tmp_path_factory.mktemp("host-persona")
    config = run_dir / "cfg.toml"
    config.write_text("[utility]\ntemperature = 0.01\n", encoding="utf-8")
    instance = Harness(
        HarnessConfig(
            run_dir=run_dir,
            program_src=PROGRAM_SRC,
            start_time=START,
            time_scale=0.0,
            heartbeat_interval_s=0,
            config_path=str(config),
            llm_system_prompt=PERSONA,
            echo_logs=False,
        )
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture(scope="module")
def rendered(harness: Harness) -> Harness:
    """让她先听一句话（插件据此学到宿主人格），再让她自己开口。"""
    harness.user_turn(USER_LINE)
    harness.clock.advance(parse_duration("5h"))
    for _ in range(8):
        outcome = (harness.endogenous({"force": True}).get("decision") or {}).get("outcome") or {}
        if outcome.get("acted"):
            break
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if [m for m in harness.platform.messages if m.kind == "proactive"]:
            break
        time.sleep(0.5)
    return harness


def test_the_host_persona_reaches_a_proactive_render(rendered: Harness) -> None:
    """渲染那次 ``llm_generate`` 带着宿主拼好的系统提示词。

    这是这条改动的全部意义：以前那次调用一个字都没有，所以主动消息不可能和回话同一个声音。
    """
    renders = [call for call in rendered.llm.calls if call.kind == "render"]
    assert renders, "没有发生渲染调用"
    assert renders[-1].system_prompt == PERSONA, renders[-1].system_prompt


def test_the_render_still_produces_a_message(rendered: Harness) -> None:
    """带上人格不许把渲染搞坏：消息照发，正文还是那条草稿。"""
    delivered = [m.text for m in rendered.platform.messages if m.kind == "proactive"]
    assert delivered, "没有主动消息发出"
    assert delivered[0].strip()


def test_a_chat_turn_does_not_get_a_per_call_system_prompt(rendered: Harness) -> None:
    """对照：普通对话轮不走这条路。

    它拿的是宿主/provider 那一层的默认系统提示词（真实 AstrBot 里就是那条 4210 字的），
    不是我们按调用塞进去的——所以这一栏是空的。两边的来源不同，这正是要区分的地方。
    """
    replies = [call for call in rendered.llm.calls if call.kind == "reply"]
    assert replies, "没有发生回话调用"
    assert replies[0].system_prompt == ""


def test_the_plugin_remembered_a_persona_worth_passing(rendered: Harness) -> None:
    """缓存里真的存着宿主人格，而且按会话存。

    上一条验的是"渲染带上了它"，这一条验的是"它是从哪儿来的"——两者坏掉的方式不一样：
    缓存空着是宿主没给（或我们的钩子跑在拼装之前），缓存有而渲染没带才是接线断了。
    插件在第一次学到时会打一行 INFO（多少字），线上靠它回答"宿主到底给了没有"。
    """
    from cf.host import SESSION_DEFAULT

    plugin = rendered.host.plugin
    assert plugin._host_system_prompt_for(SESSION_DEFAULT) == PERSONA
    # 别的会话没学过，就该是空的——人格是按会话的，不许串。
    assert plugin._host_system_prompt_for("webchat:FriendMessage:someone-else") == ""
