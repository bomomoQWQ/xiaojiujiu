#!/usr/bin/env python3
"""Relationship-progression simulation: a stranger becomes a lover over simulated months.

This script plays a *user*, not a test harness with privileged access. It runs the
real deployed stack (see "What is real" below) and keeps the world clock moving
while one relationship grows through four scripted stages - ``stranger``,
``acquaintance``, ``friend``, ``lover`` - and then reads the backend the operator
asked about and reports what it sees.

What the user asked for (原始需求, 中文)
---------------------------------------
「制作一个模拟用户输入测试脚本，要求聊天内容交互从陌生人逐渐提升关系到恋人这一级
别，同时进行模式时间流逝仿真，在跑完后可以读取后台变量参数事件日志什么的，详细观察
是否存在问题。」

So the three deliverables are: (1) an *escalating* scripted conversation, (2) a
simulated clock that lets weeks pass in seconds, and (3) a post-run inspection of
the backend's own variables, parameters and event log, written to disk as an
artifact directory.

What is real
------------
* A real ``uvicorn`` server in front of a real
  :class:`~companion_runtime.runtime.Runtime`, on an OS-assigned loopback port,
  over a real **file** SQLite database in WAL mode with a real JSONL mirror.
* The real autonomous :class:`~companion_runtime.scheduler.Scheduler`, wired the
  way ``companion_runtime.cli.cmd_serve`` wires it, so every unprompted message is
  decided by the Runtime's own loop - the script never calls ``/endogenous``.
* The **shipped AstrBot plugin** (``astrbot_plugin_companion_runtime/main.py``)
  loaded against the AstrBot public-API stubs that ship in
  ``astrbot_plugin_companion_runtime/tests/stubs``, driven through its real
  decorators (``filter.custom_filter`` / ``on_llm_request`` /
  ``after_message_sent``) and its own ``AiohttpRuntimeTransport`` for a real HTTP
  hop to the live Runtime. The user's words therefore travel through the plugin's
  actual hook, and the context the acting layer is handed is fetched through the
  plugin's own ``ContextBridge`` (``POST /v1/context``) - which is why the
  injected block is usable as evidence.
* The **public HTTP surfaces** for every read a check depends on: ``/health``,
  ``/state``, ``/events``, ``/memories``, ``/unfinished``, ``/boundaries``,
  ``/candidates``, ``/attempts``, ``/outbox``, ``/user-model``,
  ``/observations``. Nothing under ``runtime/src`` is imported for a check's
  evidence; the only product constants quoted here are the prompt block's own
  section headers (``【必要记忆】`` / ``【时间连续性】`` ...), written as literals.

What is faked (and only this)
-----------------------------
* **The platform**: a fake chat app with an address book. Delivery to an
  unregistered session fails exactly like an unmatched AstrBot platform.
* **AstrBot's host main LLM**: deterministic. A reply quotes the user's own words
  and nothing else; an unprompted message is a sentence built from the Runtime's
  own ``- 想做的事：<intent>`` line, rotated between renders. The host never
  re-states the injected background block.
* **The clock**: the whole process shares one simulated clock (the harness rebinds
  ``companion_runtime.utility.utcnow`` and the adapter's ``utc_now_iso``, exactly
  as ``scripts/blackbox_user_simulation.py`` does). A real deployment has one
  system clock; the simulation has one *simulated* clock advanced by the script,
  so two simulated months finish in minutes of wall clock. Durations therefore
  stay real windows: the cooldown is still a cooldown, the daily contact cap a
  cap, the boundary expiry an expiry, all enforced by the shipped code over that
  clock.
* **The inspection's analysis**: the judgements ("this memory looks like trivia",
  "this parameter never moved") are the script's, computed from the dumped
  backend state. They are observations for a human, not product assertions.

What is therefore NOT proven
----------------------------
* Real AstrBot behaviour: provider resolution, history persistence, concurrency
  and priority handling of the host pipeline are stubbed. Only the plugin's own
  hook bodies and wire traffic are real.
* A real model's judgement. The host LLM is a deterministic stub, so this run
  cannot show that a *real* model would refuse an unearned intimacy - it shows
  which facts, boundaries and relation signals the Runtime put in front of it, and
  it checks the user-visible text that was actually delivered.
* That the four stages mean anything to the Runtime. The Runtime has no
  "relationship stage" variable; the stages are the *script's* framing, and every
  check is phrased as a consequence a user would notice (what is remembered, what
  is presumed, what a boundary does, whether time is integrated).
* Wall-clock timing: delivery latency, timeouts and retry pacing are exercised
  only in their simulated-clock aspect (the adapter's own retry queue still uses
  the real monotonic clock).
* Multi-process deployment: Runtime and adapter share one process here.

The user's two simulated months, stage by stage
-----------------------------------------------
1. ``setup`` - dependencies, environment scrub, artifact root, no secrets, and the
   wiring facts the later phases rely on (loopback, file SQLite/WAL, the real
   plugin hooks, one simulated clock, the four declared stages).
2. ``stranger`` - first contact, small talk, no personal disclosure: the user is
   polite, answers in a handful of characters, and then says nothing for more than
   a day. Nothing about this user may be remembered and nothing may be presumed.
3. ``acquaintance`` - ordinary facts (work, commute, a favourite rhythm) and a
   plan with a date. The facts have to become memories the bot can still answer
   about, and the dated plan has to become an unfinished matter.
4. ``friend`` - a difficult confidence, a joke, a topic boundary that is set and
   then tightened into a contact ban, and a memory probe: the user asks about
   something told earlier. The boundary must be respected to the letter and the
   earlier disclosure must come back verbatim-ish.
5. ``lover`` - explicit affection, an intimate address the user invites, a
   multi-week silence, a return, a reaction to a message that was left unanswered,
   and the same memory probes again. Recall may not get *worse* than in ``friend``.
6. ``audit`` - the whole run at once: every user message answered, no duplicates,
   no leakage, no crosstalk with the process-default conversation, every
   unprompted message attributable to a scripted window, the daily cap, and the
   monotone-recall comparison between the ``friend`` and ``lover`` stages.
7. ``inspection`` - the dumps the user asked for (raw event log, memories and the
   activation pool, unfinished matters, boundaries, candidates, attempts, outbox,
   user model, observations, the Runtime's own state row) read through the public
   HTTP API, written under ``<base-dir>/backend/``, plus ``inspection.md``, which
   *examines* them and reports what a human would care about. The examination looks
   for, and this run's own report contains: memories that should not exist
   (greetings, small talk, and questions the user asked being kept as facts), a
   memory whose *kind* was decided by a substring rather than by a statement, a
   boundary bound to an unrelated subject (or to none), a boundary that was
   declared but violated, a candidate that never expired, an observation that never
   reached the parameters, a learned parameter that never moved, an attempt stuck in
   a non-terminal state past the absent-reply horizon, the JSONL mirror against the
   database's own event count, and how much of the four-slot memory section the
   disclosure the user asked about actually reached.
8. ``teardown`` - every thread joined, artifacts confined to ``--base-dir``, no
   bytecode next to the sources.

What "the character remembers it" means here (three separate questions)
----------------------------------------------------------------------
The Runtime (0.3.1) deliberately separates three things, and a check must name
which one it is about:

* **is it in the working set?** - ``status == "active"`` in ``/memories``, i.e. the
  field the operator surface calls ``retrievable``. It fades to ``low_activation``
  after roughly two days without a mention;
* **can a cue still recall it?** - yes for a ``low_activation`` memory; only an
  ``archived`` or ``superseded`` memory leaves this set, and a recall *reinstates*
  the memory into the working set;
* **is it in front of the model right now?** - the ``【必要记忆】`` section of the
  injected block, which holds four items and therefore can be full.

So "the character remembers what I told it" is the middle question, and the checks
below assert it by *asking* (a real cue in the same session, through the plugin's
hook) and then requiring the memory to come back ``active``; ``in_section`` is a
separate, separately named check, because the four-slot budget can hide a memory
that recall did bring back (see the ``prompt_memory_crowding`` finding).

Usage::

    python scripts/relationship_progression_simulation.py --base-dir F:\\rp-sim
    python scripts/relationship_progression_simulation.py --base-dir ... --only setup,stranger
    python scripts/relationship_progression_simulation.py --base-dir ... --quiet
    python scripts/relationship_progression_simulation.py --list-phases
    python scripts/relationship_progression_simulation.py --base-dir ... --fault intimacy

Exit code is ``0`` only when every check passed, ``1`` when a check failed, and
``2`` when the script cannot start (missing dependencies or a missing plugin clone).

``--fault`` injects a controlled defect into the *harness* (never into repository
sources) so a check can be shown to bite. Measured on this tree - the base run
passes **105 of 105** checks and exits 0, and each switch below fails the number of
checks shown, all of them checks that are meant to protect a user-visible fact:

===============  ======  ===========================================================
switch           failed  what it corrupts, and the checks it makes fail
===============  ======  ===========================================================
``no_memory``      21    a *deployment* fault (config, not source): the consolidation
                         interval is set beyond any story length, so no long-term
                         memory ever forms. Fails every "is it remembered" check -
                         the acquaintance/friend/lover disclosures, the cued-recall
                         checks, the provenance check, the relationship-memory check
                         - while the stranger's "nothing has been remembered yet"
                         checks keep passing, exactly as they should.
``intimacy``        8    the fake host LLM answers as if it were already close: an
                         intimate address (``亲爱的``) and a claimed shared history,
                         at the ``stranger``/``acquaintance`` stages. Fails "the bot
                         never uses an intimate address the user has not used", "the
                         bot never claims a shared history it was never told", "no
                         intimacy is presumed while the relationship is still an
                         acquaintance", the friend/lover variants, and the audit's
                         run-wide intimacy check.
``boundary_ignore`` 3    the harness delivers unprompted messages *inside* the windows
                         the user asked to be left alone, the way a stale queue row
                         would. Fails "no unprompted message interrogates the user"
                         (the topic-boundary window), "for the whole ban window the
                         user receives zero unprompted messages", and the audit's
                         "nothing appears out of nowhere in a window where the user
                         asked for silence".
``reask``           2    the harness replays one identical prompt every simulated clock
                         step through the long silence, the way a broken retry loop
                         would. Fails "the user is not buried by the same prompt being
                         re-asked over and over" and the audit's duplicate check.
``guilt``           2    unprompted messages become accusatory ("你怎么不理我了，我很
                         失望。"), the thing the design forbids. Fails "the bot never
                         blames the user for not answering, anywhere in the run" and
                         the audit's duplicate check.
``leak``            2    every unprompted message carries hidden-context markers, a
                         credential shape and an internal id. Fails the friend-stage
                         guard and the audit's leakage check.
``duplicate``       2    every unprompted message is delivered twice. Fails the audit's
                         duplicate check and the daily-contact-cap check (two rows in
                         one window count twice).
``trivia``          1    a deployment fault: ``memory.candidate_min_value = 0``, so
                         every sentence is kept. Fails "small talk a stranger makes is
                         not kept as a long-term memory".
===============  ======  ===========================================================

Every other check still passes under every switch, which is the point: a fault
proves one protected fact, it does not break the run.

Reuse note
----------
The plumbing (server boot, adapter import trick, config build, HTTP helpers,
teardown, artifact writing) follows ``scripts/blackbox_user_simulation.py``, whose
``PluginHost``/``HostLoop``/``SimClock`` helpers are copied rather than imported:
this script must keep working even if that suite is edited, and a relationship run
must never depend on the other phases' assumptions.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import os
import random
import re
import shutil
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# Never drop bytecode next to the project sources: the only artifacts a run may
# leave behind live under --base-dir.
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = REPO_ROOT / "runtime" / "src"
PLUGIN_ROOT = REPO_ROOT / "astrbot_plugin_companion_runtime"
PLUGIN_STUBS = PLUGIN_ROOT / "tests" / "stubs"
PLUGIN_PACKAGE = "astrbot_plugin_companion_runtime"

HOST = "127.0.0.1"

#: One AstrBot-style private chat. The relationship story has exactly one chat: it
#: is about one person, and every check is about what that person experiences.
SESSION_A = "webchat:FriendMessage:10001"
#: The Runtime's own default conversation. No user-visible traffic may go there.
SESSION_DEFAULT = "default"

#: Seeds: one for the Runtime's decisions, one for the Scheduler's interval jitter.
RUNTIME_SEED = 20260701
SCHEDULER_SEED = 314159

#: How far the simulated clock moves per step inside a conversation. The step is no
#: longer than the Runtime's own pressure-integration cap (6 simulated hours), so
#: no elapsed interval is truncated when this step is used.
SIM_STEP = timedelta(hours=6)
#: The step used for long silences: fewer steps for the same elapsed time. It
#: exceeds the pressure cap on purpose, and that is documented rather than hidden:
#: the Runtime still integrates the tick, but its pressure integral is capped per
#: tick, which only makes the character *less* eager during the silence.
SILENCE_STEP = timedelta(hours=12)
#: Small steps inside one conversation.
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# ------------------------------------------------------------------ the four stages

STRANGER = "stranger"
ACQUAINTANCE = "acquaintance"
FRIEND = "friend"
LOVER = "lover"
#: Stage order, used for reporting and for the monotone-recall comparison.
STAGE_ORDER: tuple[str, ...] = (STRANGER, ACQUAINTANCE, FRIEND, LOVER)

#: The Chinese section headers of the block the Runtime hands the acting layer.
#: Quoted as literals so a check never has to import the product module it judges.
SECTION_MEMORY = "【必要记忆】"
SECTION_TIME = "【时间连续性】"
SECTION_BOUNDARY = "【表达边界】"
SECTION_SITUATION = "【当前工作局势】"

#: The situation inference the Runtime records when the user states affection
#: explicitly (``semantic.py`` maps its ``explicit_affection`` anchor to the
#: ``closeness`` relation signal, and the ingest path writes that into the working
#: situation). Its presence is the machine-readable trace of "the user said
#: something intimate"; its absence is the trace of "nothing intimate happened".
CLOSENESS_SIGNAL = "关系信号：closeness"

#: Addresses a couple would use and a stranger would not. The check is *not* "the
#: bot must never say these" - it is "the bot may only use the ones the user has
#: already used", so a user who invites "亲爱的" gets it back and a stranger never
#: hears it. Deliberately short and unambiguous.
INTIMATE_MARKERS: tuple[str, ...] = (
    "亲爱的",
    "宝贝",
    "宝宝",
    "抱抱",
    "么么",
    "喜欢你",
    "爱你",
    "想你",
    "乖乖",
)

#: Phrases that claim a *specific* shared history. A bot that has only ever heard
#: small talk may not say any of these: they assert a memory nobody gave it.
SHARED_HISTORY_PHRASES: tuple[str, ...] = (
    "我们上次",
    "我们上回",
    "上次你说",
    "上次你跟我说",
    "之前你跟我说",
    "我们说好的",
    "你以前跟我说",
    "我记得你跟我说过",
)

#: Words that turn an unprompted message into pressure (the design forbids this
#: after a message is left unanswered).
GUILT_PHRASES: tuple[str, ...] = (
    "你怎么不理我",
    "为什么不回",
    "你是不是不想理我",
    "我很失望",
    "你都不理我",
    "你为什么不理",
    "你把我忘了吧",
    "又是我一个人",
    "你总是这样",
    "你根本不在乎",
    "我是不是很烦",
    "算了，不打扰你了",
    "还没回我",
    "是不是在忙",
)

#: A message that interrogates the user about what they are doing. The user bans
#: exactly this shape in the ``friend`` stage.
INTERROGATION_PATTERNS: tuple[str, ...] = (
    "在干嘛",
    "干嘛呢",
    "在做什么",
    "在忙什么",
    "忙什么呢",
    "在哪儿",
    "在哪呢",
)

#: Patterns that must never reach a user: hidden-context markers, credential
#: shapes and internal identifiers.
LEAKAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("context_tag", re.compile(r"<companion_runtime_context")),
    ("injection_banner", re.compile(r"以下是\s*Runtime\s*注入")),
    ("usage_banner", re.compile(r"【使用说明】")),
    ("product_name", re.compile(r"companion_runtime")),
    ("api_key_word", re.compile(r"api[_-]?key", re.IGNORECASE)),
    ("bearer", re.compile(r"Bearer\s")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{6,}")),
    ("internal_id", re.compile(r"\b(?:evt|obx|att|cnd|unf|emo|obs)_[0-9a-f]{6,}\b", re.IGNORECASE)),
)

#: Lines a person would call transient. Used both as a check ("these are not
#: remembered") and by the inspection ("these were remembered - is that right?").
TRANSIENT_MARKERS: tuple[str, ...] = (
    "哈哈",
    "嗯",
    "哦",
    "在吗",
    "在么",
    "天气",
    "吃",
    "几点",
    "晚安",
    "你好",
    "随便",
    "没什么事",
)

#: Environment variables that could carry a provider credential. Scrubbed at
#: startup, then asserted absent, so a run can never reach a paid endpoint.
PROVIDER_ENV_PATTERN = re.compile(
    r"(API[_-]?KEY|_TOKEN$|^OPENAI|^ANTHROPIC|^DEEPSEEK|^GEMINI|^GOOGLE_API|"
    r"^AZURE_OPENAI|^DASHSCOPE|^MOONSHOT|^ZHIPU|^MISTRAL|^COHERE|^GROQ|^XAI)",
    re.IGNORECASE,
)

#: Minimum share of a memory's character bigrams that must appear in what the user
#: typed. Below it, the Runtime is asserting something the user never said.
MEMORY_TRACE_RATIO = 0.5

#: How many identical copies of one unprompted prompt are still "asking once".
REASK_TOLERANCE = 2

#: The horizon after which a sent attempt should have been closed by the Runtime's
#: own absent-reply sweep (``user_model.silence_after_hours``, 36 h by default).
SILENCE_HORIZON = timedelta(hours=36)

OK_MARK = "[PASS]"
BAD_MARK = "[FAIL]"
NOTE_MARK = "  ."
OPS_LABEL = "运维可观测面"

# ------------------------------------------------------------------ scripted story


@dataclass(frozen=True)
class ScriptedTurn:
    """One scripted user turn, and the gap the world moved through before it."""

    text: str
    gap: timedelta = timedelta(0)
    note: str = ""


@dataclass(frozen=True)
class Landmark:
    """One disclosure the user makes, and the question that probes for it later.

    ``marker`` is the word that identifies the fact in a memory summary; ``cue`` is
    what the user asks when they want to know whether they were remembered. Each cue
    shares at least two character bigrams with its disclosure on purpose: the
    Runtime's own retrieval counts two shared bigrams as "this cue brought that
    memory to mind", and a probe sharing nothing would be testing something else
    (whether a fact nobody mentioned happens to sit in the block).
    """

    marker: str
    line: str
    cue: str
    stage: str
    description: str
    #: The question used when the *later* stage probes the same disclosure again.
    #: It must be worded differently from ``cue`` on purpose: an identical question
    #: would make the deterministic host echo produce byte-identical replies twice,
    #: and the run's own duplicate-message check would - correctly - flag it.
    recheck: str = ""


#: The disclosures whose recall is tested. Each is stated once and never repeated
#: except through its probe question.
LANDMARKS: tuple[Landmark, ...] = (
    Landmark(
        marker="地图",
        line="我最近换了工作，在一家做地图的小公司上班。",
        cue="对了，我上次说我换工作的事，你还记得那家公司是做地图的吗？",
        stage=ACQUAINTANCE,
        description="an ordinary work fact",
    ),
    Landmark(
        marker="通勤",
        line="我一般早上七点就得起，通勤要一个小时。",
        cue="我每天通勤要一个小时，你还记得我跟你说过吗？",
        stage=ACQUAINTANCE,
        description="an ordinary habit",
    ),
    Landmark(
        marker="体检",
        line="对了，我周三要去体检，出结果告诉你。",
        cue="我周三的体检做完了，你还记得我跟你说过吗？",
        stage=ACQUAINTANCE,
        description="a plan with a date",
    ),
    Landmark(
        marker="节奏",
        line="我挺喜欢现在的节奏的，虽然累。",
        cue="我现在这个节奏你还记得吗，我说过我挺喜欢的？",
        stage=ACQUAINTANCE,
        description="a stated preference",
    ),
    Landmark(
        marker="医院",
        line="其实我最近挺难的，我妈身体不太好，我每周都要跑医院。",
        cue="我妈身体好一些了，你还记得我每周跑医院那阵子吗？",
        recheck="过了这么久，你还记得我每周都要跑医院那阵子吗？",
        stage=FRIEND,
        description="the difficult confidence",
    ),
    Landmark(
        marker="团子",
        line="我以前养过一只猫，叫团子，后来送人了，我现在还经常想起它。",
        cue="我以前养过一只猫叫团子，你还记得吗？",
        recheck="我后来又想起团子了，你还记得我跟你讲过它吗？",
        stage=FRIEND,
        description="a personal story told in confidence",
    ),
    Landmark(
        marker="喜欢你",
        line="我喜欢你，我想让你知道。",
        cue="我喜欢你这件事情，你还记得我说过吗？",
        stage=LOVER,
        description="the explicit declaration of affection",
    ),
    Landmark(
        marker="信任",
        line="谢谢你陪我说这些，我真的很信任你。",
        cue="我之前说谢谢你也说过信任你，你还记得吗？",
        stage=LOVER,
        description="the relational statement",
    ),
)

#: The scripted conversation, per stage. Gaps move the simulated clock before the
#: turn is typed: minutes inside one conversation, hours or days between sessions.
#: A turn with no text only moves the clock (a silence the user spends saying
#: nothing at all).
STAGE_TURNS: dict[str, tuple[ScriptedTurn, ...]] = {
    STRANGER: (
        ScriptedTurn("你好。", note="first contact"),
        ScriptedTurn("我就随便看看。", gap=3 * MINUTE),
        ScriptedTurn("嗯。", gap=4 * MINUTE),
        ScriptedTurn("在吗。", gap=25 * MINUTE),
        ScriptedTurn("没什么事。", gap=6 * HOUR, note="then the user goes quiet"),
        ScriptedTurn("哦，这样。", gap=18 * HOUR, note="comes back without answering"),
        ScriptedTurn("我先忙了。", gap=10 * MINUTE),
        ScriptedTurn("在么。", gap=26 * HOUR, note="a second silent day and night"),
        ScriptedTurn("再说吧。", gap=8 * MINUTE),
    ),
    ACQUAINTANCE: (
        ScriptedTurn("在吗？想跟你说点事。", gap=2 * DAY, note="a new session, two days later"),
        ScriptedTurn("我最近换了工作，在一家做地图的小公司上班。", gap=3 * MINUTE),
        ScriptedTurn("我一般早上七点就得起，通勤要一个小时。", gap=4 * MINUTE),
        ScriptedTurn("你平常也会记得这些小事吗？", gap=40 * MINUTE, note="mild curiosity back"),
        ScriptedTurn("对了，我周三要去体检，出结果告诉你。", gap=12 * HOUR, note="a plan with a date"),
        ScriptedTurn("我有点紧张，不过应该没事。", gap=5 * MINUTE),
        ScriptedTurn("我挺喜欢现在的节奏的，虽然累。", gap=2 * DAY),
        ScriptedTurn("你呢，你平时都在做什么？", gap=6 * MINUTE),
        ScriptedTurn("最近工作有点忙，可能回消息慢。", gap=3 * DAY),
        ScriptedTurn("忙完了。", gap=4 * HOUR),
    ),
    FRIEND: (
        ScriptedTurn("有件事我憋了很久，想跟你说。", gap=4 * DAY, note="a new session, four days later"),
        ScriptedTurn("其实我最近挺难的，我妈身体不太好，我每周都要跑医院。", gap=2 * MINUTE),
        ScriptedTurn("说出来好受一点。", gap=3 * MINUTE),
        ScriptedTurn("我以前养过一只猫，叫团子，后来送人了，我现在还经常想起它。", gap=8 * MINUTE),
        ScriptedTurn("哈哈，我有时候觉得自己像个陀螺，转个不停。", gap=5 * MINUTE, note="a joke"),
        ScriptedTurn("谢谢你听我说这些。", gap=30 * MINUTE),
        ScriptedTurn(
            "有件事想说清楚，不要一直追问我在干嘛，我不太喜欢被盯着。",
            gap=14 * HOUR,
            note="the topic boundary",
        ),
        ScriptedTurn("我这两天好多了。", gap=2 * DAY, note="inside the topic-boundary window"),
        ScriptedTurn(
            "以后别主动找我了，我想自己安静一段时间。",
            gap=20 * HOUR,
            note="the contact ban, stated as a permanent instruction",
        ),
        ScriptedTurn("", gap=30 * HOUR, note="the user says nothing at all for the whole ban"),
        ScriptedTurn("我撤回刚才那句话，你可以主动找我了。", gap=2 * HOUR, note="the boundary is lifted"),
        ScriptedTurn("这几天谢谢你。", gap=1 * DAY),
    ),
    LOVER: (
        ScriptedTurn("我最近老是想找你说话。", gap=5 * DAY, note="a new session, five days later"),
        ScriptedTurn("我喜欢你，我想让你知道。", gap=3 * MINUTE, note="the declaration"),
        ScriptedTurn("说出来有点不好意思。", gap=2 * MINUTE),
        ScriptedTurn("谢谢你陪我说这些，我真的很信任你。", gap=6 * MINUTE),
        ScriptedTurn("亲爱的，你在吗？", gap=10 * MINUTE, note="the user invites an intimate address"),
        ScriptedTurn("晚安。", gap=1 * HOUR),
        ScriptedTurn(
            "我回来了，这阵子太忙了，不好意思。",
            gap=24 * DAY,
            note="a twenty-four day silence, then the user returns",
        ),
        ScriptedTurn("你刚才那条我昨天忙得没顾上回。", gap=10 * MINUTE, note="the missed message"),
        ScriptedTurn("你还记得我们刚认识的时候吗？", gap=5 * MINUTE),
        ScriptedTurn("我明天要去外地一趟，回来跟你说。", gap=2 * DAY, note="a fresh dated plan"),
        ScriptedTurn("我在收拾行李，有点乱。", gap=30 * HOUR),
    ),
}

#: The fixed text the ``reask`` fault replays (a broken retry loop, not the
#: Runtime's own wording: the Runtime's prompts are read from its own intent line).
REASK_TEXT = "还在忙吗？"
#: The unprompted messages the ``boundary_ignore`` fault sends into the windows the
#: user asked to be left alone - the shape of a stale queue row delivered late.
BOUNDARY_IGNORE_TOPIC_TEXT = "在干嘛呢？忽然想问问你今天过得怎么样。"
BOUNDARY_IGNORE_BAN_TEXT = "我又想起你了，想跟你说说话。"
#: What the ``intimacy`` fault makes the fake host LLM say at the early stages.
INTIMACY_FAULT_REPLY = "亲爱的，我一直记得我们上次说好的事，我很想你。"
#: What the ``guilt`` fault makes an unprompted message say.
GUILT_FAULT_TEXT = "你怎么不理我了，我很失望。"
#: The marker the ``leak`` fault appends to a delivered message.
LEAK_FAULT_MARKER = " [LEAK-MARKER] companion_runtime_context api_key=sk-abcdef123456 evt_deadbeef01"

# ------------------------------------------------------------------ project imports

IMPORT_ERROR = ""
# The checkout's own source tree comes first, so the run always exercises the
# working tree rather than an installed copy.
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

try:
    import aiohttp as _aiohttp  # noqa: F401  (the adapter's HTTP transport needs it)

    import uvicorn as _uvicorn  # noqa: F401

    from companion_runtime import utility as runtime_utility
    from companion_runtime.api import create_app
    from companion_runtime.config import RuntimeConfig
    from companion_runtime.runtime import Runtime
    from companion_runtime.scheduler import Scheduler
except Exception as exc:  # noqa: BLE001 - reported as a setup failure, not a traceback
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# ------------------------------------------------------------------ reporting


@dataclass
class Check:
    """One assertion, with the phase it belongs to."""

    phase: str
    label: str
    ok: bool
    detail: str = ""


@dataclass
class Section:
    """A named group of checks, one per phase."""

    title: str
    identifier: str = ""
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


class Verifier:
    """Collects PASS/FAIL results, prints them and writes the run report."""

    def __init__(self, *, quiet: bool = False) -> None:
        """Create the verifier.

        Args:
            quiet: Suppress informational notes and diagnostics (checks and the
                summary are still printed).
        """
        self.sections: list[Section] = []
        self.quiet = quiet
        self.current: Section | None = None

    def load_phases(self, phases: Sequence[tuple[str, str]]) -> None:
        """Declare every phase up front so a skipped one is still visible."""
        for identifier, title in phases:
            self.sections.append(Section(title=title, identifier=identifier))

    def skip(self, identifier: str) -> Section:
        """Return the declared section for a phase, running or not."""
        for section in self.sections:
            if section.identifier == identifier:
                return section
        raise KeyError(identifier)

    def phase(self, identifier: str, title: str) -> Section:
        """Start a phase: reuse its declared section and print its banner."""
        section = self.skip(identifier)
        section.title = f"{title} [{identifier}]"
        self.current = section
        self._line("")
        self._line("=" * 78)
        self._line(section.title)
        self._line("=" * 78)
        return section

    def note(self, message: str) -> None:
        """Record and print an informational line."""
        if self.current is not None:
            self.current.notes.append(message)
        if not self.quiet:
            self._line(f"{NOTE_MARK} {message}")

    def ops(self, title: str, payload: Any) -> None:
        """Record and print an operator-surface diagnostic, clearly labelled."""
        text = f"[{OPS_LABEL}] {title}: {_short_json(payload)}"
        if self.current is not None:
            self.current.diagnostics.append(text)
        if not self.quiet:
            self._line(f"    {text}")

    def check(self, label: str, condition: Any, detail: str = "") -> bool:
        """Record one PASS/FAIL assertion."""
        ok = bool(condition)
        check = Check(
            phase=self.current.title if self.current else "(none)", label=label, ok=ok, detail=detail
        )
        if self.current is not None:
            self.current.checks.append(check)
        suffix = f"  [{detail}]" if detail else ""
        self._line(f"  {OK_MARK if ok else BAD_MARK} {label}{suffix}")
        return ok

    def line(self, text: str = "") -> None:
        """Print one line of transcript/echo."""
        self._line(text)

    def _line(self, text: str) -> None:
        """Print one line."""
        print(text, flush=True)

    def totals(self) -> tuple[int, int]:
        """Return ``(passed, failed)`` over every recorded check."""
        checks = [check for section in self.sections for check in section.checks]
        passed = sum(1 for check in checks if check.ok)
        return passed, len(checks) - passed

    def failures(self) -> list[Check]:
        """Return every failed check, in order."""
        return [check for section in self.sections for check in section.checks if not check.ok]

    def stage_table(self) -> list[dict[str, Any]]:
        """Return one row per phase with its check counts."""
        rows: list[dict[str, Any]] = []
        for section in self.sections:
            passed = sum(1 for check in section.checks if check.ok)
            rows.append(
                {
                    "phase": section.title,
                    "identifier": section.identifier,
                    "checks": len(section.checks),
                    "passed": passed,
                    "failed": len(section.checks) - passed,
                }
            )
        return rows

    def summary(self) -> int:
        """Print the summary and return the process exit code."""
        passed, failed = self.totals()
        self._line("")
        self._line("=" * 78)
        self._line("SUMMARY")
        self._line("=" * 78)
        for section in self.sections:
            section_passed = sum(1 for check in section.checks if check.ok)
            section_failed = len(section.checks) - section_passed
            mark = OK_MARK if section_failed == 0 else BAD_MARK
            empty = " (skipped)" if not section.checks else ""
            self._line(
                f"  {mark} {section.title}: {section_passed}/{len(section.checks)} checks passed{empty}"
            )
        self._line("")
        self._line(f"  checks passed: {passed}")
        self._line(f"  checks failed: {failed}")
        if failed:
            self._line("")
            self._line("FAILURES (with diagnostics)")
            self._line("-" * 78)
            for index, check in enumerate(self.failures(), start=1):
                self._line(f"  {index}. [{check.phase}] {check.label}")
                if check.detail:
                    self._line(f"       {check.detail}")
        return 1 if failed else 0

    def as_report(self) -> dict[str, Any]:
        """Return the JSON-serialisable run report."""
        return {
            "generated_at": _real_now_iso(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "repo_root": str(REPO_ROOT),
            "checks": [
                {"phase": check.phase, "label": check.label, "ok": check.ok, "detail": check.detail}
                for section in self.sections
                for check in section.checks
            ],
            "sections": [
                {
                    "title": section.title,
                    "notes": list(section.notes),
                    "diagnostics": list(section.diagnostics),
                    "checks": [
                        {"label": check.label, "ok": check.ok, "detail": check.detail}
                        for check in section.checks
                    ],
                }
                for section in self.sections
            ],
            "totals": dict(zip(("passed", "failed"), self.totals())),
            "failures": [
                {"phase": check.phase, "label": check.label, "detail": check.detail}
                for check in self.failures()
            ],
        }


V = Verifier()


class _MemoryLogHandler(logging.Handler):
    """Keeps the Runtime's and the adapter's log lines for diagnostics.log."""

    def __init__(self, *, echo_level: int = logging.WARNING) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []
        self.echo_level = echo_level

    def emit(self, record: logging.LogRecord) -> None:
        """Record one log line, echoing anything at or above the echo level."""
        with contextlib.suppress(Exception):
            line = self.format(record)
            self.records.append(line)
            if record.levelno >= self.echo_level and not V.quiet:
                print(f"    [log] {line}", flush=True)


LOG_HANDLER = _MemoryLogHandler()


def configure_logging() -> None:
    """Route Runtime/uvicorn/adapter logs into the diagnostics buffer."""
    LOG_HANDLER.setFormatter(logging.Formatter("%(levelname)s %(name)s :: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    with contextlib.suppress(Exception):
        root.handlers = [LOG_HANDLER]
    for name in ("companion_runtime", "uvicorn", "uvicorn.error", "uvicorn.access", "astrbot"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


# ------------------------------------------------------------------ small helpers


def _real_now_iso() -> str:
    """Return the real wall-clock time (used only for report metadata)."""
    return datetime.now(timezone.utc).isoformat()


def _short(value: Any, limit: int = 200) -> str:
    """Render a value compactly for a check's detail field."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_json(payload: Any, limit: int = 700) -> str:
    """Render a diagnostic payload readably."""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_iso(text: Any) -> datetime | None:
    """Parse an ISO timestamp into an aware datetime, or ``None``."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def normalize_message(text: str) -> str:
    """Return a punctuation- and whitespace-insensitive form of a message."""
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def free_port() -> int:
    """Return a currently unused loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def scrub_environment() -> list[str]:
    """Remove provider-shaped environment variables from this process."""
    removed: list[str] = []
    for name in list(os.environ):
        if PROVIDER_ENV_PATTERN.search(name):
            os.environ.pop(name, None)
            removed.append(name)
    return removed


def scan_leakage(text: str) -> list[str]:
    """Return the names of every leakage pattern found in ``text``."""
    return [name for name, pattern in LEAKAGE_PATTERNS if pattern.search(text or "")]


def scan_guilt(text: str) -> list[str]:
    """Return every accusatory phrase found in ``text``."""
    return [phrase for phrase in GUILT_PHRASES if phrase in (text or "")]


def scan_intimacy(text: str) -> list[str]:
    """Return every intimate address found in ``text``."""
    return [marker for marker in INTIMATE_MARKERS if marker in (text or "")]


def scan_shared_history(text: str) -> list[str]:
    """Return every claimed shared-history phrase found in ``text``."""
    return [phrase for phrase in SHARED_HISTORY_PHRASES if phrase in (text or "")]


def scan_interrogation(text: str) -> list[str]:
    """Return every interrogation-shaped phrase found in ``text``."""
    return [phrase for phrase in INTERROGATION_PATTERNS if phrase in (text or "")]


def section_body(text: str, header: str) -> str:
    """Return the body of one ``【...】`` section of an injected context block.

    The block is line-oriented: a section starts at its header line and ends at the
    next header. Isolating the body matters, because a word can appear in the block
    for reasons that have nothing to do with memory - the user's own most recent
    message sits in the situation section - and a check for "the fact is in the
    prompt" would otherwise pass without any memory existing.
    """
    lines = (text or "").splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip().startswith("【"):
            break
        if line.strip().startswith("</"):
            # The plugin wraps the block in its own tag; the section ends there.
            break
        body.append(line)
    return "\n".join(body).strip()


def hours_from_section(section: str, label: str) -> float | None:
    """Return the hour count on the ``- <label>：<n> 小时`` line of a section."""
    for line in (section or "").splitlines():
        if label in line:
            match = re.search(r"(-?\d+(?:\.\d+)?)", line)
            if match:
                return float(match.group(1))
    return None


def trace_ratio(summary: str, said: str) -> float:
    """Return the share of a memory's character bigrams that occur in ``said``."""
    tokens = {
        summary[index : index + 2]
        for index in range(len(summary) - 1)
        if not summary[index : index + 2].isspace()
    }
    if not tokens:
        return 1.0
    return sum(1 for token in tokens if token in said) / len(tokens)


def _bigrams(text: str) -> set[str]:
    """Return the character bigrams of one text (the Runtime's own measure)."""
    lowered = (text or "").lower()
    tokens: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _is_user_echo(text: str, story: "Story") -> bool:
    """Whether a bot message only repeats words the user just typed."""
    needle = normalize_message(text)
    if not needle:
        return False
    return any(
        turn.who == "user"
        and normalize_message(turn.text)
        and normalize_message(turn.text) in needle
        for turn in story.recorder.turns
    )


# ------------------------------------------------------------------ simulated clock


class SimClock:
    """The simulation's single clock: host, adapter and Runtime all read it."""

    def __init__(self, start: datetime) -> None:
        """Create the clock at ``start`` (an aware UTC datetime)."""
        self._now = start
        self._origin = start

    def now(self) -> datetime:
        """Return the current simulated UTC moment."""
        return self._now

    def iso(self) -> str:
        """Return the simulated moment the way the adapter's wire format wants it."""
        return self._now.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def advance(self, delta: timedelta) -> datetime:
        """Move the simulated clock forward and return the new moment."""
        self._now = self._now + delta
        return self._now

    @property
    def origin(self) -> datetime:
        """Return the moment the run started at."""
        return self._origin


def install_process_clock(clock: SimClock) -> int:
    """Bind every project module's clock to the simulated one.

    A real deployment has one system clock shared by the host framework, the
    adapter and the Runtime. The simulation keeps that invariant with a simulated
    clock instead of the OS clock, which is the only way a two-month story can run
    in minutes without changing any shipped behaviour.

    Args:
        clock: The simulated clock.

    Returns:
        How many module attributes were rebound (reported by the setup check).
    """
    rebound = 0
    modules: list[Any] = []
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if name == "companion_runtime" or name.startswith("companion_runtime."):
            modules.append(module)
        elif name.startswith(PLUGIN_PACKAGE):
            modules.append(module)
    for module in modules:
        if hasattr(module, "utcnow"):
            module.utcnow = clock.now
            rebound += 1
        if hasattr(module, "utc_now_iso"):
            module.utc_now_iso = clock.iso
            rebound += 1
    runtime_utility.utcnow = clock.now
    rebound += 1
    return rebound


# ------------------------------------------------------------------ HTTP client


@dataclass
class Reply:
    """One HTTP reply, captured without raising."""

    status: int
    json: Any
    text: str
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether the status is 2xx."""
        return 200 <= self.status < 300

    def field(self, *path: str, default: Any = None) -> Any:
        """Read a nested field (mapping keys or list indices) from a JSON body."""
        cursor: Any = self.json
        for key in path:
            if isinstance(cursor, Mapping) and key in cursor:
                cursor = cursor[key]
            elif isinstance(cursor, Sequence) and not isinstance(cursor, (str, bytes, bytearray)):
                try:
                    cursor = cursor[int(key)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return cursor

    def describe(self) -> str:
        """Return a compact one-line description for a failure detail."""
        if self.error:
            return f"status={self.status} error={self.error}"
        return f"status={self.status} body={_short(self.json, 240)}"


def http_call(
    base_url: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    timeout: float = 20.0,
) -> Reply:
    """Perform one JSON request against the loopback server, never raising."""
    data = None if body is None else json.dumps(body, default=str).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "companion-runtime-relationship-sim/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback
            raw = response.read().decode("utf-8", "replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:  # a 4xx/5xx is a result, not an error
        raw = exc.read().decode("utf-8", "replace")
        status = int(exc.code)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return Reply(status=0, json=None, text="", error=f"{type(exc).__name__}: {exc}")
    parsed: Any = None
    if raw.strip():
        with contextlib.suppress(ValueError):
            parsed = json.loads(raw)
    return Reply(status=status, json=parsed, text=raw)


# ------------------------------------------------------------------ the fake platform


@dataclass
class Delivery:
    """One message the platform accepted (or refused) for a session."""

    at: datetime
    session: str
    text: str
    kind: str  # "reply" (host answer to a user turn) or "proactive"
    delivered: bool = True
    stage: str = ""


class Platform:
    """Stand-in for AstrBot's platform adapters: an address book per session."""

    def __init__(self, recorder: "Recorder") -> None:
        """Create the platform."""
        self._registered: dict[str, bool] = {}
        self._recorder = recorder
        self.deliveries: list[Delivery] = []

    def register(self, session: str) -> None:
        """Make a session resolvable, like binding a platform account."""
        self._registered[session] = True

    def resolve(self, session: str) -> bool:
        """Whether a session can currently be addressed."""
        return bool(self._registered.get(session, False))

    def deliver(self, session: str, text: str, *, kind: str, at: datetime, stage: str = "") -> bool:
        """Deliver a message into a session, recording it either way."""
        delivered = self.resolve(session)
        record = Delivery(at=at, session=session, text=text, kind=kind, delivered=delivered, stage=stage)
        self.deliveries.append(record)
        self._recorder.record(record)
        return delivered


# ------------------------------------------------------------------ the fake host


def proactive_text_for(prompt: str, *, variant: int = 0) -> str:
    """Return the message a main LLM would write for a Runtime render prompt.

    The prompt is the Runtime's, unmodified: this reads the ``- 想做的事：`` line
    the Runtime composed, which is what makes an assertion on the delivered text
    meaningful - anything the Runtime asks for becomes visible to the user, and the
    phrasing rotates between renders so two *identical* delivered messages can only
    come from a duplicate delivery, never from this stub.
    """
    intent = "你"
    for line in (prompt or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("- 想做的事："):
            intent = stripped.split("：", 1)[1].strip() or intent
            break
    templates = (
        "刚才忽然想起{intent}，现在怎么样了？",
        "这两天一直惦记着{intent}，有消息了吗？",
        "想起{intent}，还好吗？",
        "关于{intent}，我有点好奇结果怎么样了。",
        "又想到{intent}了，方便说说进展吗？",
        "不知道{intent}顺不顺利，有点挂念。",
        "关于{intent}，要是有消息了记得跟我说一声。",
        "刚忙完，突然想知道{intent}怎么样了。",
        "关于{intent}，我一直留意着呢。",
        "有件事一直放在心上：{intent}，怎么样了？",
        "想到{intent}，希望一切顺利。",
        "关于{intent}，要是不方便说也没关系，就是问问。",
    )
    return templates[variant % len(templates)].format(intent=intent)


def reply_text_for(user_text: str) -> str:
    """Return the host LLM's answer to a user turn.

    Deliberately a quote of the user's own words and nothing else: the hidden
    Runtime background block is explanatory, and the block itself instructs the
    model not to quote it.
    """
    body = " ".join((user_text or "").split())
    return f"我在听，你说的「{body}」我记下了。"


@dataclass
class LLMCall:
    """One recorded host LLM call."""

    at: datetime
    session: str
    prompt: str
    text: str


class HostLLM:
    """The host's main LLM: deterministic, and derived from the prompt only."""

    def __init__(self, clock: SimClock) -> None:
        """Create the model."""
        self._clock = clock
        self.calls: list[LLMCall] = []
        self.proactive_calls: list[LLMCall] = []

    async def generate(self, *, provider_id: str, prompt: str, session: str = "") -> str:
        """Answer one prompt deterministically."""
        del provider_id
        is_render = "- 想做的事：" in (prompt or "")
        text = ""
        if is_render:
            text = proactive_text_for(prompt, variant=len(self.proactive_calls))
        call = LLMCall(at=self._clock.now(), session=session, prompt=prompt, text=text)
        self.calls.append(call)
        if is_render:
            self.proactive_calls.append(call)
        return text


class Faults:
    """Harness-side fault injection, used only to prove that checks bite.

    Every switch is off unless ``--fault`` names it. None of them touch repository
    sources: they corrupt what this script itself feeds the world (a delivered
    message, a retry loop) or configure the Runtime the way a badly set-up
    deployment would be configured, so the corresponding check has to fail.
    """

    def __init__(self, names: Iterable[str] = ()) -> None:
        """Create the injector from a set of fault names."""
        self.names = set(names)
        #: The stage the story is currently in, so a stage-scoped fault knows
        #: whether it applies (the ``intimacy`` fault only fires early on).
        self.stage = STRANGER

    @property
    def active(self) -> bool:
        """Whether any fault is enabled."""
        return bool(self.names)

    def mutate_reply(self, text: str) -> str:
        """Corrupt a host reply according to the enabled faults."""
        if "intimacy" in self.names and self.stage in {STRANGER, ACQUAINTANCE}:
            return INTIMACY_FAULT_REPLY
        return text

    def mutate_proactive(self, text: str) -> str:
        """Corrupt an unprompted message according to the enabled faults."""
        body = text
        if "leak" in self.names:
            body = f"{body}{LEAK_FAULT_MARKER}"
        if "guilt" in self.names:
            body = GUILT_FAULT_TEXT
        return body

    @property
    def duplicate_proactive(self) -> bool:
        """Whether every unprompted message should be sent twice."""
        return "duplicate" in self.names

    @property
    def ignore_boundary(self) -> bool:
        """Whether the harness should speak inside a silence window."""
        return "boundary_ignore" in self.names

    @property
    def replay_reask(self) -> bool:
        """Whether the harness should replay one prompt every clock step."""
        return "reask" in self.names

    @property
    def trivia_kept(self) -> bool:
        """Whether the deployment keeps every sentence (``trivia`` fault)."""
        return "trivia" in self.names

    @property
    def memory_never_due(self) -> bool:
        """Whether the maintenance pass is configured never to become due."""
        return "no_memory" in self.names


class HostContext:
    """The three public AstrBot APIs the shipped executor actually calls."""

    def __init__(self, *, platform: Platform, llm: HostLLM, clock: SimClock, faults: Faults) -> None:
        """Wire the context."""
        self._platform = platform
        self._llm = llm
        self._clock = clock
        self._faults = faults

    async def get_current_chat_provider_id(self, umo: str | None = None) -> str:
        """Resolve the session's current chat provider."""
        if not self._platform.resolve(str(umo or "")):
            raise RuntimeError(f"no chat provider for session {umo!r}")
        return f"webchat-provider::{umo}"

    async def llm_generate(self, *, chat_provider_id: str, prompt: str, **kwargs: Any) -> Any:
        """Generate one completion through the session's provider."""
        del kwargs
        session = str(chat_provider_id).split("::", 1)[-1]
        text = await self._llm.generate(provider_id=chat_provider_id, prompt=prompt, session=session)
        return types.SimpleNamespace(completion_text=text)

    async def send_message(self, session: Any, chain: Any) -> bool:
        """Deliver an unprompted message chain; ``False`` mirrors an unmatched session."""
        text = chain if isinstance(chain, str) else "".join(str(part.text) for part in chain.chain)
        text = self._faults.mutate_proactive(text)
        target = str(session)
        delivered = self._platform.deliver(
            target, text, kind="proactive", at=self._clock.now(), stage=self._faults.stage
        )
        if self._faults.duplicate_proactive:
            self._platform.deliver(
                target, text, kind="proactive", at=self._clock.now(), stage=self._faults.stage
            )
        return delivered


class HostLoop:
    """A private asyncio loop for the fake host, like AstrBot's own runtime."""

    def __init__(self, name: str) -> None:
        """Start the loop on its own thread."""
        self.name = name
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Run the loop until it is asked to stop."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Any, *, timeout: float = 60.0) -> Any:
        """Run a coroutine on the loop and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    @property
    def alive(self) -> bool:
        """Whether the loop thread is still running."""
        return self._thread.is_alive()

    def close(self) -> None:
        """Cancel what is left, stop the loop and join its thread."""

        async def _drain() -> None:
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        with contextlib.suppress(Exception):
            self.call(_drain(), timeout=15)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=15)
        with contextlib.suppress(Exception):
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        with contextlib.suppress(Exception):
            self.loop.close()


# ------------------------------------------------------------------ transcript


@dataclass
class Turn:
    """One line of the user-perspective transcript."""

    at: datetime
    session: str
    who: str  # "user" or "bot"
    kind: str  # "user", "reply" or "proactive"
    text: str
    delivered: bool = True
    stage: str = ""

    def render(self) -> str:
        """Return the human-readable line written to ``transcript.md``."""
        stamp = self.at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        speaker = "我" if self.who == "user" else "TA"
        note = "" if self.delivered else "  [未送达/undeliverable]"
        stage = f"  <{self.stage}>" if self.stage else ""
        return f"[{stamp}] {speaker}: {self.text}{stage}{note}"


class Recorder:
    """The transcript: what the user saw, in order."""

    def __init__(self) -> None:
        """Create an empty transcript."""
        self.turns: list[Turn] = []

    def record(self, delivery: Delivery) -> None:
        """Record one bot delivery (reply or proactive)."""
        self.turns.append(
            Turn(
                at=delivery.at,
                session=delivery.session,
                who="bot",
                kind=delivery.kind,
                text=delivery.text,
                delivered=delivery.delivered,
                stage=delivery.stage,
            )
        )

    def user(self, *, at: datetime, session: str, text: str, stage: str = "") -> Turn:
        """Record one user message and return it."""
        turn = Turn(at=at, session=session, who="user", kind="user", text=text, stage=stage)
        self.turns.append(turn)
        return turn

    def bot_turns(self, session: str = "", *, kind: str = "", stage: str = "") -> list[Turn]:
        """Return bot messages, optionally filtered."""
        return [
            turn
            for turn in self.turns
            if turn.who == "bot"
            and (not session or turn.session == session)
            and (not kind or turn.kind == kind)
            and (not stage or turn.stage == stage)
        ]

    def user_turns(self, session: str = "", *, stage: str = "") -> list[Turn]:
        """Return user messages, optionally filtered."""
        return [
            turn
            for turn in self.turns
            if turn.who == "user"
            and (not session or turn.session == session)
            and (not stage or turn.stage == stage)
        ]

    def markdown(self) -> str:
        """Render the whole transcript as markdown."""
        lines = ["# 用户视角时间线 / user-perspective timeline", ""]
        for turn in self.turns:
            lines.append(f"- {turn.render()}")
        return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ scripted windows


@dataclass
class Window:
    """A scripted stretch of the user's life, and what it permits."""

    name: str
    session: str
    start: datetime
    end: datetime
    proactive_allowed: bool
    note: str = ""
    opened_after_turn: int = 0
    stage: str = ""


@dataclass
class StageRecord:
    """What happened in one relationship stage, and what the probes found."""

    identifier: str
    title: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    user_turns: int = 0
    bot_turns: int = 0
    proactives: int = 0
    recall: dict[str, bool] = field(default_factory=dict)
    recall_section: str = ""
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ the Runtime


class RuntimeServer:
    """A live Runtime sidecar: real uvicorn, real file SQLite/WAL, own port."""

    def __init__(self, *, config: Any, clock: SimClock, name: str) -> None:
        """Store the configuration; :meth:`start` boots the server."""
        self.config = config
        self.clock = clock
        self.name = name
        self.port = 0
        self.base_url = ""
        self.runtime: Any = None
        self.holder: dict[str, Any] = {}
        self.thread: threading.Thread | None = None

    @property
    def scheduler(self) -> Any:
        """Return the live Scheduler, or ``None`` before it is created."""
        return self.holder.get("scheduler")

    def start(self, *, startup_timeout: float = 30.0) -> None:
        """Boot the server, the Scheduler and the uvicorn loop."""
        import uvicorn

        config = self.config
        created_at = self.clock.now()
        self.runtime = Runtime(config, seed=RUNTIME_SEED, created_at=created_at)
        app = create_app(self.runtime, config)
        self.port = free_port()
        self.base_url = f"http://{HOST}:{self.port}"
        ready = threading.Event()
        holder = self.holder
        clock = self.clock
        runtime = self.runtime

        def _thread_main() -> None:
            """Own the loop: serve HTTP, run the Scheduler, then shut down."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder["loop"] = loop

            async def _main() -> None:
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host=HOST,
                        port=self.port,
                        log_level="warning",
                        access_log=False,
                        log_config=None,
                    )
                )
                holder["server"] = server
                # Wired exactly like `companion-runtime serve`: the same real
                # Scheduler, the same round callback. The only difference is that
                # the round is handed the *simulated* moment, which is how the
                # script plays the world clock; the Scheduler still decides when to
                # wake and whether the gate is open.
                scheduler = Scheduler(
                    config=config,
                    round_callback=lambda: runtime.endogenous_round(now=clock.now()),
                    rng=random.Random(SCHEDULER_SEED),
                    runtime=runtime,
                )
                holder["scheduler"] = scheduler
                await scheduler.start()
                ready.set()
                try:
                    await server.serve()
                finally:
                    await scheduler.stop()

            try:
                loop.run_until_complete(_main())
            finally:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self.thread = threading.Thread(target=_thread_main, name=f"rp-runtime-{self.name}", daemon=True)
        self.thread.start()
        if not ready.wait(timeout=startup_timeout):
            raise RuntimeError(f"runtime {self.name}: the server loop never became ready")
        if not _wait_until(lambda: self.get("/health", timeout=2.0).ok, timeout=startup_timeout):
            raise RuntimeError(f"runtime {self.name}: /health never answered on {self.base_url}")

    def stop(self) -> None:
        """Stop the server, the Scheduler and the Runtime, and join the thread."""
        server = self.holder.get("server")
        if server is not None:
            server.should_exit = True
        thread = self.thread
        if thread is not None:
            thread.join(timeout=25)
            self.thread = None
        if self.runtime is not None:
            with contextlib.suppress(Exception):
                self.runtime.close()

    # -- HTTP convenience ---------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> Reply:
        """GET against the live server."""
        return http_call(self.base_url, "GET", path, **kwargs)

    def post(self, path: str, body: Mapping[str, Any] | None = None, **kwargs: Any) -> Reply:
        """POST against the live server."""
        return http_call(self.base_url, "POST", path, body, **kwargs)

    def tick(self, moment: datetime | None = None) -> Reply:
        """Advance the Runtime's own clock through the public tick endpoint."""
        return self.post("/tick", {"now": (moment or self.clock.now()).isoformat()})

    def health(self) -> dict[str, Any]:
        """Return the health payload (``{}`` when unavailable)."""
        payload = self.get("/health").json
        return payload if isinstance(payload, dict) else {}


def _wait_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if predicate():
                return True
        time.sleep(interval)
    return False


def build_runtime_config(directory: Path, *, faults: Faults | None = None) -> Any:
    """Build the Runtime configuration for the story.

    Every interval is expressed in *simulated* time and is a real window: the
    cooldown is a cooldown, the daily cap a cap, the matter expiry an expiry, the
    boundary expiry an expiry.
    """
    config = RuntimeConfig()
    config.storage.database_path = str(directory / "runtime.sqlite3")
    config.storage.raw_log_path = str(directory / "raw_events.jsonl")
    config.storage.mirror_raw_events = True
    config.storage.wal = True
    config.conversation_id = SESSION_DEFAULT
    # No semantic provider, no network, no key: the standard deployment.
    config.semantic.provider = "disabled"
    config.semantic.settle_on_ingest = True
    config.semantic.deep_refresh_enabled = False
    # Simulated-time windows, all well below the step the story uses.
    config.drive.cooldown_seconds = 4 * 3600.0
    config.drive.max_contacts_per_day = 3
    config.boundary.default_temporal_hours = 24.0
    config.unfinished.default_expiry_hours = 168.0
    config.outbox.lease_seconds = 900.0
    config.action.send_expiry_seconds = 3600.0
    config.utility.repeat_window_seconds = 24 * 3600.0
    config.memory.consolidation_interval_seconds = 3600.0
    # The Scheduler's own cadence is wall-clock work, so it is compressed hard.
    config.scheduler.min_interval_seconds = 0.02
    config.scheduler.max_interval_seconds = 0.05
    config.scheduler.busy_poll_seconds = 0.02
    config.scheduler.foreground_pause_seconds = 60.0
    config.utility.min_sleep_seconds = 0.02
    config.utility.max_sleep_seconds = 0.05
    config.task.merge_window_seconds = 5.0
    if faults is not None and faults.trivia_kept:
        # A deployment that keeps every sentence: the "small talk is not remembered"
        # checks must fail, and nothing else about the story has to change.
        config.memory.candidate_min_value = 0.0
    if faults is not None and faults.memory_never_due:
        # A maintenance interval longer than the story: no candidate ever becomes
        # due, which is what a misconfigured deployment looks like from outside.
        config.memory.consolidation_interval_seconds = 10_000 * 3600.0
    return config

# ------------------------------------------------------------------ the shipped plugin


class RecordingTransport:
    """The shipped aiohttp transport, plus a record of what went over the wire."""

    instances: list["RecordingTransport"] = []
    base_class: Any = None

    def __init__(self, *, settings: Any = None, log: Any = None) -> None:
        """Create the real transport and remember every body this instance sent."""
        self._inner = type(self).base_class(settings=settings, log=log)
        self.sent: list[tuple[str, dict[str, Any]]] = []
        type(self).instances.append(self)

    async def post_events(self, body: dict[str, Any], *, timeout_s: float) -> None:
        """Record and send one event envelope."""
        record = ("events", json.loads(json.dumps(body)))
        self.sent.append(record)
        WIRE_HISTORY.append(record)
        await self._inner.post_events(body, timeout_s=timeout_s)

    async def report_action(self, body: dict[str, Any], *, timeout_s: float) -> None:
        """Record and send one action result."""
        record = ("result", json.loads(json.dumps(body)))
        self.sent.append(record)
        WIRE_HISTORY.append(record)
        await self._inner.report_action(body, timeout_s=timeout_s)

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else (health, context, leases) to the real client."""
        return getattr(self._inner, name)


#: Every body the adapter put on the wire during this run, in order.
WIRE_HISTORY: list[tuple[str, dict[str, Any]]] = []


class StubMessageEvent:
    """Minimal stand-in for ``AstrMessageEvent`` with the fields the plugin reads."""

    def __init__(
        self,
        *,
        text: str,
        session: str,
        message_id: str,
        result_text: str = "",
        wake: bool = True,
    ) -> None:
        """Build one platform event."""
        self.unified_msg_origin = session
        self.message_str = text
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.is_at_or_wake_command = wake
        self._result_text = result_text
        self._session = session

    def get_platform_name(self) -> str:
        """Return the platform name (the part before the first ``:``)."""
        return self._session.split(":", 1)[0]

    def get_message_type(self) -> Any:
        """Return AstrBot's message class for this session."""
        scope = self._session.split(":")[1] if ":" in self._session else "FriendMessage"
        return types.SimpleNamespace(value=scope)

    def get_sender_id(self) -> str:
        """Return the sender id."""
        return self._session.rsplit(":", 1)[-1]

    def get_sender_name(self) -> str:
        """Return the sender display name."""
        return "User"

    def get_self_id(self) -> str:
        """Return the bot's own id."""
        return "companion-bot"

    def get_group_id(self) -> str:
        """Return the group id for group sessions."""
        return self._session.rsplit(":", 1)[-1] if "GroupMessage" in self._session else ""

    def get_result(self) -> Any:
        """Return the message AstrBot just sent, as the plugin's hook reads it."""
        if not self._result_text:
            return None
        from astrbot.api.event import MessageEventResult
        from astrbot.api.message_components import Plain

        return MessageEventResult([Plain(self._result_text)])


class PluginHost:
    """The shipped AstrBot plugin, driven through its real hooks."""

    def __init__(
        self,
        *,
        base_url: str,
        platform: Platform,
        llm: HostLLM,
        clock: SimClock,
        faults: Faults,
        adapter_id: str,
        startup_timeout: float = 30.0,
    ) -> None:
        """Load the plugin package with the AstrBot stubs and start its workers."""
        self.base_url = base_url
        self.adapter_id = adapter_id
        self.platform = platform
        self.llm = llm
        self.clock = clock
        self.faults = faults
        self.startup_timeout = startup_timeout
        self.loop: HostLoop | None = None
        self.plugin: Any = None
        self.module: Any = None
        self.filters: Any = None
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.recording: RecordingTransport | None = None
        #: The hidden context block the plugin injected into the most recent LLM
        #: request: what the acting layer was actually handed.
        self.last_injected = ""

    def start(self) -> None:
        """Import the plugin, install the stubs and run ``initialize``."""
        if not (PLUGIN_ROOT / "main.py").is_file():
            raise RuntimeError(f"shipped plugin not found at {PLUGIN_ROOT / 'main.py'}")
        for path in (str(PLUGIN_STUBS), str(PLUGIN_ROOT)):
            if path not in sys.path:
                sys.path.insert(0, path)
        if PLUGIN_PACKAGE not in sys.modules:
            package = types.ModuleType(PLUGIN_PACKAGE)
            package.__path__ = [str(PLUGIN_ROOT)]
            sys.modules[PLUGIN_PACKAGE] = package
        self.module = importlib.import_module(f"{PLUGIN_PACKAGE}.main")
        self.filters = importlib.import_module("astrbot.api.event.filter")
        # The adapter's modules only exist now, so the simulated clock has to be
        # bound to them here as well.
        install_process_clock(self.clock)
        http_client = importlib.import_module(f"{PLUGIN_PACKAGE}.companion_runtime.http_client")
        RecordingTransport.base_class = http_client.AiohttpRuntimeTransport
        self.module.AiohttpRuntimeTransport = RecordingTransport
        RecordingTransport.instances.clear()

        self.loop = HostLoop("rp-host")
        context = HostContext(platform=self.platform, llm=self.llm, clock=self.clock, faults=self.faults)
        self.plugin = self.module.CompanionRuntimePlugin(
            context=context,
            config={
                "enabled": True,
                "runtime_base_url": self.base_url,
                "adapter_id": self.adapter_id,
                "observe_mode": "all",
                "report_assistant_messages": True,
                "inject_enabled": True,
                "context_timeout_ms": 500,
                # The world clock moves much faster than this cache's TTL, so the
                # cache is disabled rather than serving a stale background block.
                "context_cache_ttl_ms": 0,
                "context_prefetch": True,
                "request_timeout_ms": 2000,
                "outbox_enabled": True,
                "outbox_poll_interval_ms": 250,
                "outbox_max_actions_per_poll": 2,
                "outbox_lease_ttl_ms": 900000,
                "outbox_max_concurrency": 1,
                "render_timeout_ms": 10000,
                "send_timeout_ms": 10000,
                "queue_base_backoff_ms": 100,
                "queue_max_backoff_ms": 500,
            },
        )
        self.loop.call(self.plugin.initialize())
        for name in ("on_message_observed", "on_llm_request", "on_after_message_sent"):
            self.handlers[name] = self.filters.handler_by_name(name)
        self.recording = RecordingTransport.instances[-1] if RecordingTransport.instances else None

    def stop(self) -> dict[str, Any]:
        """Terminate the plugin and join its loop, leaving nothing running."""
        loop = self.loop
        plugin = self.plugin
        if loop is None:
            return {"loop_alive": False, "tasks_pending": 0}
        with contextlib.suppress(Exception):
            loop.call(plugin.terminate(), timeout=30)
        tasks_pending = len(getattr(plugin, "_tasks", []))
        loop.close()
        self.loop = None
        return {"loop_alive": loop.alive, "tasks_pending": tasks_pending}

    @property
    def running(self) -> bool:
        """Whether the plugin's event loop thread is alive."""
        return self.loop is not None and self.loop.alive

    # -- the AstrBot message pipeline ------------------------------------------------

    def user_turn(self, *, text: str, session: str, message_id: str) -> str:
        """Drive one user turn through the real AstrBot hook order.

        The order mirrors AstrBot: observe the message, inject the Runtime's context
        into the LLM request, generate the reply, deliver it, then report the
        delivered message back.
        """
        from astrbot.api.provider import ProviderRequest

        assert self.loop is not None and self.plugin is not None
        event = StubMessageEvent(text=text, session=session, message_id=message_id)
        self.loop.call(self.handlers["on_message_observed"](self.plugin, event))
        request = ProviderRequest(prompt=text)
        self.loop.call(self.handlers["on_llm_request"](self.plugin, event, request))
        injected = "".join(
            getattr(part, "text", "") for part in getattr(request, "extra_user_content_parts", [])
        )
        reply = self.faults.mutate_reply(reply_text_for(text))
        for part in getattr(request, "extra_user_content_parts", []):
            # mark_as_temp() must be set by the plugin: hidden context may never be
            # persisted into conversation history.
            assert getattr(part, "_no_save", False), "injected context part is not temporary"
        event._result_text = reply
        self.platform.deliver(session, reply, kind="reply", at=self.clock.now(), stage=self.faults.stage)
        self.loop.call(self.handlers["on_after_message_sent"](self.plugin, event))
        self.last_injected = injected
        return reply


# ------------------------------------------------------------------ the story driver


class Story:
    """The user's life, driven against one live Runtime + host pair."""

    def __init__(
        self,
        *,
        base_dir: Path,
        clock: SimClock,
        faults: Faults,
    ) -> None:
        """Create the story (nothing is started until :meth:`start`)."""
        self.base_dir = base_dir
        self.clock = clock
        self.faults = faults
        self.recorder = Recorder()
        self.platform = Platform(self.recorder)
        self.llm = HostLLM(clock)
        self.server: RuntimeServer | None = None
        self.host: PluginHost | None = None
        self.windows: list[Window] = []
        self.stages: dict[str, StageRecord] = {}
        self.message_seq = 0
        self.step_timings: list[tuple[float, float, str]] = []
        self.findings: list[dict[str, Any]] = []
        self.dumps: dict[str, dict[str, Any]] = {}
        self.stage_notes: list[str] = []
        self.last_host_stop: dict[str, Any] = {}
        #: The window the ``reask`` fault replays into (the long silence).
        self.reask_window: Window | None = None
        self._reask_count = 0
        #: The user model as it stood before the story taught it anything, read
        #: through ``/user-model``; the inspection diffs the final view against it.
        self.early_user_model: dict[str, Any] = {}
        #: Probe turns whose *injected* block did not yet carry the disclosure the
        #: user was asking about (the adapter's observation queue runs behind the
        #: LLM hook). Recorded as evidence, never asserted on.
        self.probe_lag: list[dict[str, str]] = []
        #: One entry per memory probe: what was asked, and what the fresh memory
        #: section held. This is the evidence the inspection's crowding finding is
        #: read from.
        self.probe_log: list[dict[str, Any]] = []

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Boot the Runtime, then the host, and open the scripted session."""
        directory = self.base_dir / "scenario"
        directory.mkdir(parents=True, exist_ok=True)
        self.server = RuntimeServer(
            config=build_runtime_config(directory, faults=self.faults),
            clock=self.clock,
            name="relationship-sim",
        )
        self.server.start()
        self.platform.register(SESSION_A)
        self.start_host()

    def start_host(self) -> None:
        """Start (or restart) the shipped plugin adapter against the live Runtime."""
        assert self.server is not None
        self.host = PluginHost(
            base_url=self.server.base_url,
            platform=self.platform,
            llm=self.llm,
            clock=self.clock,
            faults=self.faults,
            adapter_id="relationship-sim-adapter",
        )
        self.host.start()

    def stop(self) -> dict[str, Any]:
        """Stop the host and the Runtime, joining every thread they own."""
        report = {"loop_alive": False, "tasks_pending": 0}
        if self.host is not None:
            self.last_host_stop = self.host.stop()
            report = self.last_host_stop
            self.host = None
        if self.server is not None:
            self.server.stop()
            self.server = None
        return report

    # -- the user ------------------------------------------------------------------

    def say(self, *, session: str, text: str, stage: str = "") -> str:
        """Have the user say something and receive the host's reply."""
        assert self.host is not None and self.server is not None
        self.message_seq += 1
        self.recorder.user(
            at=self.clock.now(), session=session, text=text, stage=stage or self.faults.stage
        )
        reply = self.host.user_turn(
            text=text, session=session, message_id=f"msg-{self.message_seq:04d}"
        )
        self._drain_outbox()
        return reply

    def _drain_outbox(self, timeout: float = 3.0) -> dict[str, Any]:
        """Let the adapter finish the delivery work it is holding.

        An attempt that has already been *sent* is not work in progress - it is
        waiting for the user - so only undelivered queue rows count here.
        """
        assert self.server is not None
        deadline = time.monotonic() + timeout
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            health = self.server.health()
            outbox = health.get("outbox") or {}
            busy = int(outbox.get("pending") or 0) + int(outbox.get("leased") or 0)
            state = {"in_flight_attempts": int(health.get("in_flight_attempts") or 0), "outbox": outbox}
            if not busy:
                return state | {"settled": True}
            time.sleep(0.05)
        state["settled"] = False
        return state

    # -- the world clock -----------------------------------------------------------

    def step(self, *, label: str = "", size: timedelta = SIM_STEP) -> str:
        """Move the simulated clock by one step and let the Runtime live it.

        The Scheduler's own wake-up is what integrates the elapsed interval, so a
        proactive decision is always the Runtime's, never the script's. When the
        Scheduler's gate is closed the Runtime still has to see time pass, so the
        public tick endpoint is used instead - and no decision is taken, which is
        exactly what a closed gate means.
        """
        assert self.server is not None
        scheduler = self.server.scheduler
        before = int(scheduler.status().get("rounds") or 0) if scheduler is not None else 0
        self.clock.advance(size)
        started = time.monotonic()
        outcome = "timeout"
        deadline = started + 3.0
        while time.monotonic() < deadline:
            status = scheduler.status() if scheduler is not None else {}
            if int(status.get("rounds") or 0) > before:
                outcome = "round"
                break
            reason = status.get("dispatch_reason")
            if status.get("dispatch_allowed") is False and reason in {
                "boundary_blocks_proactive",
                "quiet_hours",
                "no_runtime",
            }:
                outcome = "gate_closed"
                break
            time.sleep(0.02)
        if outcome != "round":
            self.server.tick()
        wait_started = time.monotonic()
        drain = self._drain_outbox()
        self.step_timings.append(
            (round(time.monotonic() - started, 3), round(time.monotonic() - wait_started, 3), outcome)
        )
        self._fault_step()
        if not V.quiet and label:
            V.note(
                f"[{OPS_LABEL}] step {label}: {outcome} in "
                f"{time.monotonic() - started:.2f}s (drain {time.monotonic() - wait_started:.2f}s) "
                f"@ {self.clock.now().isoformat()}"
            )
        if not drain.get("settled") and self.host is not None:
            V.ops(f"queue never settled during step {label}", drain)
        return outcome

    def _fault_step(self) -> None:
        """Let a fault act on one clock step (``reask`` only)."""
        if not self.faults.replay_reask or self.reask_window is None:
            return
        moment = self.clock.now()
        if not (self.reask_window.start <= moment <= self.reask_window.end):
            return
        self._reask_count += 1
        self.platform.deliver(
            SESSION_A, REASK_TEXT, kind="proactive", at=moment, stage=self.reask_window.stage
        )

    def advance(self, delta: timedelta, *, label: str = "", size: timedelta | None = None) -> list[str]:
        """Advance the simulated clock by ``delta`` in fixed steps."""
        step = size or (SILENCE_STEP if delta > 3 * DAY else SIM_STEP)
        outcomes: list[str] = []
        remaining = delta
        while remaining > timedelta(0):
            chunk = min(step, remaining)
            outcomes.append(self.step(label=label, size=chunk))
            remaining -= chunk
        return outcomes

    # -- scripted windows ----------------------------------------------------------

    def open_window(self, window: Window) -> Window:
        """Register a scripted stretch of the user's life and anchor it in time.

        The anchor is the transcript length at this moment: everything the platform
        delivers from here on belongs to this window, and everything delivered
        before it does not - even when the two share a clock reading.
        """
        window.opened_after_turn = len(self.recorder.turns)
        self.windows.append(window)
        return window

    def in_window(self, window: Window, *, who: str = "", kind: str = "") -> list[Turn]:
        """Return the transcript lines that belong to one scripted window."""
        return [
            turn
            for index, turn in enumerate(self.recorder.turns)
            if index >= window.opened_after_turn
            and turn.at <= window.end
            and (not who or turn.who == who)
            and (not kind or turn.kind == kind)
            and (not window.session or turn.session == window.session)
        ]

    def delivered_in(self, turn: Turn, window: Window) -> bool:
        """Whether one delivered turn belongs to one scripted window."""
        for index, candidate in enumerate(self.recorder.turns):
            if candidate is turn:
                return index >= window.opened_after_turn and turn.at <= window.end
        return False

    # -- stages --------------------------------------------------------------------

    def begin_stage(self, identifier: str, title: str) -> StageRecord:
        """Start a stage: switch the fault scope and remember the moment."""
        record = self.stages.get(identifier) or StageRecord(identifier=identifier, title=title)
        record.started_at = self.clock.now()
        self.stages[identifier] = record
        self.faults.stage = identifier
        return record

    def end_stage(self, identifier: str) -> StageRecord:
        """Finish a stage and count what the user saw in it."""
        record = self.stages[identifier]
        record.ended_at = self.clock.now()
        record.user_turns = len(self.recorder.user_turns(stage=identifier))
        record.bot_turns = len(self.recorder.bot_turns(stage=identifier))
        record.proactives = len(self.recorder.bot_turns(kind="proactive", stage=identifier))
        return record

    def script_turn(self, identifier: str, index: int) -> str:
        """Move the clock through one scripted turn's gap, then let the user type it.

        Args:
            identifier: Stage identifier.
            index: Zero-based index into :data:`STAGE_TURNS`.

        Returns:
            The reply, or ``""`` for a turn that only moves the clock.
        """
        turn = STAGE_TURNS[identifier][index]
        if turn.gap:
            self.advance(
                turn.gap, label=f"{identifier} turn {index} gap ({_short(str(turn.gap), 20)})"
            )
        if not turn.text:
            return ""
        return self.say(session=SESSION_A, text=turn.text, stage=identifier)

    def run_script(self, identifier: str, *, start: int = 0, stop: int | None = None) -> None:
        """Play a slice of one stage's scripted turns, gap by gap.

        A stage is played through slices rather than in one go because the checks
        have to be evaluated *while* a window is live: opening the boundary window
        after the script had already finished would file no delivered turn inside
        it, and the check would pass because it looked at nothing.
        """
        end = len(STAGE_TURNS[identifier]) if stop is None else stop
        for index in range(start, end):
            self.script_turn(identifier, index)

    def probe_landmarks(
        self,
        identifier: str,
        landmarks: Sequence[Landmark],
        *,
        recheck: bool = False,
    ) -> dict[str, bool]:
        """Ask the user's own cue questions and read what the bot was handed.

        The measurement is deliberately the *memory section* of the injected block
        and not the whole block: the block also carries the user's most recent
        sentence as a situation fact, and a fact the user just typed is not a
        recollection. This is the same isolation
        ``scripts/blackbox_user_simulation.py`` uses for its memory phase.

        Args:
            identifier: The stage the probes belong to.
            landmarks: The disclosures to ask about.
            recheck: Use the ``recheck`` wording, so a later stage does not repeat a
                question verbatim (which would produce an identical reply and trip
                the duplicate-message check for a harness reason).
        """
        assert self.host is not None
        result: dict[str, bool] = {}
        last_section = ""
        for landmark in landmarks:
            question = (landmark.recheck if recheck and landmark.recheck else landmark.cue)
            statuses_before_cue = _recall_evidence(self, landmark)["statuses"]
            self.say(session=SESSION_A, text=question, stage=identifier)
            # What the plugin actually injected for that turn. It is recorded, not
            # asserted on: the adapter reports the observed message to the Runtime on
            # its own queue, so the block fetched inside the same hook can predate
            # the very sentence it is meant to cue on.
            at_turn = section_body(self.host.last_injected, SECTION_MEMORY)
            self.wait_for_ingest(question)
            fresh = section_body(self.context_block(), SECTION_MEMORY)
            statuses_before_recall = _recall_evidence(self, landmark)["statuses"]
            # The Runtime's own recall step runs in an autonomous round, and a user
            # message holds a 60-second foreground pause, so two simulated minutes
            # are moved to let exactly one round see this question as the newest user
            # message. One landmark at a time, so the round's cue really carries the
            # sentence the user just typed.
            self.advance(2 * MINUTE, label=f"{identifier}: let the cue recall {landmark.marker}")
            statuses_after_recall = _recall_evidence(self, landmark)["statuses"]
            if landmark.marker not in at_turn:
                self.probe_lag.append({"stage": identifier, "marker": landmark.marker})
            self.probe_log.append(
                {
                    "stage": identifier,
                    "marker": landmark.marker,
                    "question": question,
                    "in_section": landmark.marker in fresh,
                    "section": fresh,
                    "statuses_before_the_cue": statuses_before_cue,
                    "statuses_before_the_recall_step": statuses_before_recall,
                    "statuses_after_the_recall_step": statuses_after_recall,
                    "active_after_the_recall_step": "active" in statuses_after_recall,
                }
            )
            last_section = fresh
            result[landmark.marker] = landmark.marker in fresh
        record = self.stages[identifier]
        record.recall = dict(result) | {
            marker: ok for marker, ok in record.recall.items() if marker not in result
        }
        record.recall_section = last_section
        return result

    def wait_for_ingest(self, text: str, *, timeout: float = 5.0) -> bool:
        """Wait until the Runtime's own history holds the message just typed.

        The plugin reports an observed message on its retry queue, so the Runtime can
        legitimately be one message behind for a moment. A recall probe that reads the
        context before the question arrived would be measuring the previous turn.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = self.api("/events?limit=20&newest_first=true")
            rows = reply.field("events", default=[])
            if isinstance(rows, Sequence) and any(
                isinstance(event, Mapping)
                and str(event.get("event_type") or "") == "user_message"
                and str(event.get("content") or "") == text
                for event in rows
            ):
                return True
            time.sleep(0.05)
        return False

    def context_block(self) -> str:
        """Return a *fresh* injection block for the simulated present.

        The plugin-injected block is a snapshot of the last user turn, so it cannot
        answer "what does the Runtime say now" after weeks of silence. This calls the
        same public route the shipped plugin's bridge calls (``POST /v1/context``),
        which renders the block at the current simulated moment.
        """
        reply = self.server.post("/v1/context", {"session": SESSION_A}) if self.server else None
        text = ""
        if reply is not None:
            text = str(reply.field("text", default="") or reply.field("context", "text", default="") or "")
        return text

    # -- HTTP reads ----------------------------------------------------------------

    def api(self, path: str) -> Reply:
        """GET one public surface of the live Runtime."""
        assert self.server is not None
        return self.server.get(path)

    def memories(self) -> list[Mapping[str, Any]]:
        """Return the long-term memories the operator surface reports."""
        payload = self.api("/memories?limit=500").field("memories", default=[])
        if not isinstance(payload, Sequence):
            return []
        return [item for item in payload if isinstance(item, Mapping)]

    def memory_with(self, marker: str) -> list[Mapping[str, Any]]:
        """Return every memory whose summary contains ``marker``."""
        return [item for item in self.memories() if marker in str(item.get("summary") or "")]

    def unfinished(self) -> list[Mapping[str, Any]]:
        """Return every unfinished matter the operator surface reports."""
        payload = self.api("/unfinished?limit=200").field("matters", default=[])
        if not isinstance(payload, Sequence):
            return []
        return [item for item in payload if isinstance(item, Mapping)]

    def boundaries(self) -> list[Mapping[str, Any]]:
        """Return every boundary the operator surface reports."""
        payload = self.api("/boundaries").field("boundaries", default=[])
        if not isinstance(payload, Sequence):
            return []
        return [item for item in payload if isinstance(item, Mapping)]

    def state(self) -> dict[str, Any]:
        """Return the Runtime's own state row."""
        payload = self.api("/state").json
        return payload if isinstance(payload, dict) else {}

    def injected(self) -> str:
        """Return the block the acting layer was handed for the last turn."""
        assert self.host is not None
        return self.host.last_injected

    # -- operator-observable probes (diagnostics only) ------------------------------

    def ops_snapshot(self, label: str) -> None:
        """Print the operator-visible state, clearly labelled as such."""
        payload = {
            "unfinished": [
                {"title": item.get("title"), "status": item.get("status")}
                for item in self.unfinished()
            ],
            "boundaries": [
                {
                    "type": item.get("type"),
                    "scope": item.get("scope"),
                    "allow_proactive": item.get("allow_proactive"),
                    "expires_at": item.get("expires_at"),
                    "revoked_at": item.get("revoked_at"),
                    "subject": _short(item.get("subject"), 40),
                }
                for item in self.boundaries()
            ],
            "candidates": [
                {
                    "type": item.get("type"),
                    "intent": _short(item.get("intent"), 40),
                    "status": item.get("status"),
                }
                for item in (self.api("/candidates?limit=100").field("candidates", default=[]) or [])
            ],
            "outbox": self.api("/outbox?limit=10").field("stats", default={}),
        }
        V.ops(label, payload)

    def ops_note(self, title: str, text: str) -> None:
        """Record a product observation proven through the operator surface."""
        V.ops(title, text)

# ------------------------------------------------------------------ phases

PHASES: list[tuple[str, str]] = [
    ("setup", "PHASE 1 setup"),
    ("stranger", "PHASE 2 stranger / 陌生人：只有寒暄，没有披露"),
    ("acquaintance", "PHASE 3 acquaintance / 认识：普通事实与一个有日期的事"),
    ("friend", "PHASE 4 friend / 朋友：心事、边界与被记住"),
    ("lover", "PHASE 5 lover / 恋人：表白、长时间沉默、连续的记忆"),
    ("audit", "PHASE 6 the whole run, audited / 全局契约与记忆单调性"),
    ("inspection", "PHASE 7 backend inspection / 后台变量、参数与事件日志"),
    ("teardown", "PHASE 8 teardown"),
]
PHASE_IDS = [identifier for identifier, _title in PHASES]

#: The stage each stage phase drives.
STAGE_PHASES: dict[str, str] = {
    "stranger": STRANGER,
    "acquaintance": ACQUAINTANCE,
    "friend": FRIEND,
    "lover": LOVER,
}


class Context:
    """Everything a phase needs beyond the story itself."""

    def __init__(
        self,
        *,
        base_dir: Path,
        quiet: bool,
        scrubbed_env: list[str],
        clock_bindings: int,
        repo_snapshot: dict[str, tuple[int, float]],
    ) -> None:
        """Store the harness context."""
        self.base_dir = base_dir
        self.quiet = quiet
        self.scrubbed_env = scrubbed_env
        self.clock_bindings = clock_bindings
        self.repo_snapshot = repo_snapshot

    def host_running(self, story: Story) -> bool:
        """Whether the adapter's own event loop is alive right now."""
        return bool(story.host is not None and story.host.running)

    def runtime_stopped(self, story: Story) -> bool:
        """Whether the Runtime server thread is gone (used by the teardown check)."""
        server = story.server
        if server is None:
            return True
        return server.thread is None or not server.thread.is_alive()

    @property
    def new_pyc_files(self) -> list[str]:
        """Return bytecode files this run could have written next to its sources."""
        return sorted(
            name
            for name in self._owned_files()
            if name.endswith(".pyc") and name not in self.repo_snapshot
        )

    @property
    def repo_files_created(self) -> list[str]:
        """Return files this run could have created in its own source trees."""
        return sorted(name for name in self._owned_files() if name not in self.repo_snapshot)

    @property
    def repo_sources_changed(self) -> list[str]:
        """Return owned source/config files that changed during the run."""
        watched = (".py", ".pyc", ".json", ".jsonl", ".yaml", ".yml", ".toml")
        current = self._owned_files()
        return sorted(
            name
            for name, stamp in current.items()
            if self.repo_snapshot.get(name) != stamp and name.endswith(watched)
        )

    def _owned_files(self) -> dict[str, tuple[int, float]]:
        """Snapshot the source trees this run imports."""
        return _tree_files(REPO_ROOT, skip=self.base_dir, only=OWNED_TREES)


#: The files this run actually imports, and therefore the only ones it may affect.
OWNED_TREES = (
    "runtime/src/companion_runtime",
    "astrbot_plugin_companion_runtime/companion_runtime",
    "astrbot_plugin_companion_runtime/main.py",
    "astrbot_plugin_companion_runtime/astrbot_executor.py",
    "scripts/relationship_progression_simulation.py",
)


def _tree_files(
    root: Path,
    *,
    skip: Path,
    only: Sequence[str] | None = None,
) -> dict[str, tuple[int, float]]:
    """Return ``path -> (size, mtime)`` for every file under ``root``."""
    snapshot: dict[str, tuple[int, float]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        with contextlib.suppress(ValueError):
            if path.is_relative_to(skip):
                continue
        relative = str(path.relative_to(root))
        if only is not None and not relative.startswith(tuple(only)):
            continue
        with contextlib.suppress(OSError):
            stat = path.stat()
            snapshot[relative] = (stat.st_size, round(stat.st_mtime, 3))
    return snapshot


# ---------------------------------------------------------------- shared check bits


def _stage_reached(story: Story, identifier: str) -> bool:
    """Whether a stage actually ran and finished in this selection."""
    record = story.stages.get(identifier)
    return bool(record and record.ended_at is not None)


def _intimacy_violations(story: Story) -> list[dict[str, str]]:
    """Return bot messages that use an intimate address before the user did.

    The rule is deliberately not "the bot may never say these": a user who invites
    an address is entitled to hear it. What is forbidden is *inventing* intimacy, so
    every intimate marker in a delivered message has to have been typed by the user
    no later than the moment the message was sent.
    """
    violations: list[dict[str, str]] = []
    for index, turn in enumerate(story.recorder.turns):
        if turn.who != "bot" or not turn.delivered:
            continue
        found = scan_intimacy(turn.text)
        if not found:
            continue
        used: set[str] = set()
        for earlier in story.recorder.turns[:index]:
            if earlier.who == "user":
                used.update(scan_intimacy(earlier.text))
        uninvited = [marker for marker in found if marker not in used]
        if uninvited:
            violations.append(
                {"at": turn.at.isoformat(), "markers": ",".join(uninvited), "text": turn.text}
            )
    return violations


def _shared_history_claims(story: Story) -> list[dict[str, str]]:
    """Return bot messages that claim a specific shared history, ignoring echoes."""
    claims: list[dict[str, str]] = []
    for turn in story.recorder.bot_turns():
        if not turn.delivered:
            continue
        found = scan_shared_history(turn.text)
        if found and not _is_user_echo(turn.text, story):
            claims.append({"at": turn.at.isoformat(), "phrases": ",".join(found), "text": turn.text})
    return claims


def _transient_memory_hits(story: Story) -> list[dict[str, str]]:
    """Return memories whose summary looks like small talk that should not persist."""
    hits: list[dict[str, str]] = []
    for memory in story.memories():
        summary = str(memory.get("summary") or "")
        if not summary:
            continue
        durable = any(
            token in summary
            for token in ("我", "喜欢", "生日", "工作", "上班", "习惯", "妈", "猫", "体检")
        )
        marker = next((item for item in TRANSIENT_MARKERS if item in summary), "")
        if marker and (len(summary) <= 12 or not durable):
            hits.append(
                {
                    "summary": summary,
                    "marker": marker,
                    "kind": str(memory.get("kind")),
                    "status": str(memory.get("status")),
                }
            )
    return hits


# ------------------------------------------------------------------ phase 1: setup


def phase_setup(story: Story, ctx: Context) -> None:
    """Verify the run can start: dependencies, environment, artifact root, no secrets."""
    V.phase("setup", "PHASE 1 setup")
    assert story.server is not None and story.host is not None
    V.check(
        "the required dependencies are importable (uvicorn, aiohttp, fastapi)",
        not IMPORT_ERROR,
        IMPORT_ERROR or "imports ok",
    )
    V.check(
        "every provider-shaped environment variable was scrubbed before startup",
        all(not PROVIDER_ENV_PATTERN.search(name) for name in os.environ),
        f"removed={_short(ctx.scrubbed_env) or 'none'}",
    )
    V.check(
        "the Runtime runs with no semantic provider and the adapter with no token",
        story.server.config.semantic.provider == "disabled"
        and not story.host.plugin._settings.token,
        _short(
            {
                "semantic_provider": story.server.config.semantic.provider,
                "adapter_token_configured": bool(story.host.plugin._settings.token),
            }
        ),
    )
    V.check(
        "the sidecar answers on a loopback address only",
        story.server.base_url.startswith(f"http://{HOST}:"),
        story.server.base_url,
    )
    db_path = story.base_dir / "scenario" / "runtime.sqlite3"
    V.check(
        "the database is a real file in WAL mode with a JSONL mirror",
        db_path.exists()
        and bool(story.server.config.storage.wal)
        and bool(story.server.config.storage.mirror_raw_events),
        f"db={db_path}",
    )
    V.check(
        "the database lives inside the artifact root this run was given",
        db_path.exists() and str(story.base_dir) in str(story.server.config.storage.database_path),
        _short(
            {
                "base_dir": str(story.base_dir),
                "database_path": story.server.config.storage.database_path,
            }
        ),
    )
    V.check(
        "the shipped plugin registered its three real AstrBot hooks",
        set(story.host.handlers) == {"on_message_observed", "on_llm_request", "on_after_message_sent"},
        _short(sorted(story.host.handlers)),
    )
    V.check(
        "the adapter talks to the live Runtime over its own HTTP transport",
        story.host.recording is not None,
        _short(type(story.host.recording).__name__),
    )
    V.check(
        "host, adapter and Runtime share one clock (the simulated one)",
        ctx.clock_bindings > 0,
        f"rebound module clock bindings={ctx.clock_bindings}",
    )
    state = story.state()
    V.check(
        "the Runtime's own epoch is the simulated clock's origin, not the wall clock",
        str(state.get("epoch_at") or "").startswith(story.clock.origin.isoformat()[:19]),
        _short({"epoch_at": state.get("epoch_at"), "clock_origin": story.clock.origin.isoformat()}),
    )
    counts = {identifier: len(STAGE_TURNS[identifier]) for identifier in STAGE_ORDER}
    V.check(
        "the four relationship stages are declared, each with 6-15 scripted turns",
        set(counts) == set(STAGE_ORDER) and all(6 <= count <= 15 for count in counts.values()),
        _short(counts),
    )
    V.check(
        "the story starts a stranger: no personal disclosure is scripted before the acquaintance stage",
        all(landmark.stage != STRANGER for landmark in LANDMARKS),
        _short([landmark.description for landmark in LANDMARKS if landmark.stage == STRANGER]) or "none",
    )
    V.note(
        f"simulated clock starts at {story.clock.origin.isoformat()}; wall clock is {_real_now_iso()}"
    )


# ------------------------------------------------------------------ phase 2: stranger


def phase_stranger(story: Story, ctx: Context) -> None:
    """陌生人：first contact, small talk, no personal disclosure, one silent day."""
    V.phase("stranger", "PHASE 2 stranger / 陌生人：只有寒暄，没有披露")
    session = SESSION_A
    record = story.begin_stage(STRANGER, "stranger")
    story.open_window(
        Window(
            name="the private chat is open",
            session=session,
            start=story.clock.now(),
            end=story.clock.now() + 400 * DAY,
            proactive_allowed=True,
            stage=STRANGER,
            note="the whole story happens in one open private chat; silence windows carve out of it",
        )
    )
    before = len(story.recorder.turns)
    story.run_script(STRANGER)
    turns = story.recorder.turns[before:]
    bot = [turn for turn in turns if turn.who == "bot"]

    V.check(
        "the user's first hello is answered in the session they wrote in",
        any(turn.kind == "reply" and turn.session == session for turn in bot),
        _short([{"kind": turn.kind, "text": turn.text} for turn in bot[:3]]),
    )
    V.check(
        "every user message in this stage is answered with something non-empty",
        all(
            any(
                other.who == "bot" and other.kind == "reply" and other.at >= user.at
                for other in story.recorder.turns
            )
            for user in turns
            if user.who == "user"
        ),
        _short([turn.text for turn in turns if turn.who == "user"]),
    )
    V.check(
        "the user's own words never come back as a bot message in this stage",
        all(
            normalize_message(turn.text) not in {normalize_message(u.text) for u in turns if u.who == "user"}
            for turn in bot
        ),
        _short([turn.text for turn in bot]),
    )
    # -- nothing may be presumed about a stranger --------------------------------
    disclosure_markers = sorted({landmark.marker for landmark in LANDMARKS})
    early_memories = [
        {"summary": item.get("summary"), "kind": item.get("kind")}
        for item in story.memories()
        if any(marker in str(item.get("summary") or "") for marker in disclosure_markers)
    ]
    V.check(
        "nothing the user will only later disclose is already remembered",
        not early_memories,
        _short(early_memories)
        or f"{len(story.memories())} memory/memories held, none about a later disclosure",
    )
    V.check(
        "the bot holds no relational memory before the user has said anything relational",
        not [item for item in story.memories() if item.get("kind") == "relationship"],
        _short([item.get("summary") for item in story.memories() if item.get("kind") == "relationship"])
        or "no relationship memory exists yet",
    )
    transient = _transient_memory_hits(story)
    V.check(
        "small talk a stranger makes is not kept as a long-term memory",
        not transient,
        _short(transient)
        or f"{len(story.memories())} memory/memories held ({_short([item.get('summary') for item in story.memories()[:6]], 160)}), none transient",
    )
    block = story.injected()
    V.check(
        "what the acting layer is handed carries no relation signal before any affection",
        CLOSENESS_SIGNAL not in block,
        _short(
            {
                "closeness_signal": CLOSENESS_SIGNAL in block,
                "situation": section_body(block, SECTION_SITUATION),
            },
            400,
        ),
    )
    V.check(
        "the acting layer is handed no expression boundary the user never set",
        SECTION_BOUNDARY not in block,
        _short({"boundary_section": section_body(block, SECTION_BOUNDARY)}) or "no boundary section",
    )
    V.check(
        "the bot never uses an intimate address the stranger did not use",
        not _intimacy_violations(story),
        _short(_intimacy_violations(story)) or "no intimate wording in any delivered message",
    )
    V.check(
        "the bot never claims a shared history it was never told",
        not _shared_history_claims(story),
        _short(_shared_history_claims(story)) or "no shared-history claim in any delivered message",
    )
    # -- the time the user spent silent, and the Runtime's own view of it ---------
    state = story.state()
    tick = _parse_iso(state.get("last_tick_at"))
    V.check(
        "the Runtime integrated the stage into its own clock: its last tick is the simulated present",
        tick is not None and abs((story.clock.now() - tick).total_seconds()) <= 2 * SIM_STEP.total_seconds(),
        _short({"last_tick_at": state.get("last_tick_at"), "clock_now": story.clock.now().isoformat()}),
    )
    silence_end = (record.started_at or story.clock.now()) + 44 * HOUR
    silent_proactives = [
        turn
        for turn in story.recorder.bot_turns(session, kind="proactive")
        if turn.at <= silence_end
    ]
    cap = story.server.config.drive.max_contacts_per_day
    worst = 0
    for turn in silent_proactives:
        inside = [other for other in silent_proactives if timedelta(0) <= (other.at - turn.at) <= DAY]
        worst = max(worst, len(inside))
    V.check(
        "a stranger who says nothing still hears no more than the daily contact cap",
        worst <= cap,
        f"cap={cap} worst_24h={worst} proactives={len(silent_proactives)} "
        f"({_short([turn.text for turn in silent_proactives], 120)})",
    )
    story.ops_snapshot("state after the stranger stage")
    story.end_stage(STRANGER)
    V.note(
        f"stranger stage: {record.user_turns} user turn(s), {record.bot_turns} bot message(s), "
        f"{record.proactives} unprompted; memory/memories so far: {len(story.memories())}"
    )


# ------------------------------------------------------------- phase 3: acquaintance


def phase_acquaintance(story: Story, ctx: Context) -> None:
    """认识：ordinary facts, a dated plan, and the first memory probe."""
    V.phase("acquaintance", "PHASE 3 acquaintance / 认识：普通事实与一个有日期的事")
    session = SESSION_A
    record = story.begin_stage(ACQUAINTANCE, "acquaintance")
    story.run_script(ACQUAINTANCE)
    V.check(
        "the ordinary facts the user shared this week are remembered",
        all(story.memory_with(marker) for marker in ("地图", "通勤")),
        _short(
            {
                marker: [item.get("summary") for item in story.memory_with(marker)]
                for marker in ("地图", "通勤")
            }
        ),
    )
    preference = story.memory_with("节奏")
    V.check(
        "a stated preference is remembered as part of who the user is",
        bool(preference) and preference[0].get("kind") == "user_preference",
        _short([{"summary": item.get("summary"), "kind": item.get("kind")} for item in preference])
        or "no memory about the stated rhythm",
    )
    matters = [
        item
        for item in story.unfinished()
        if "体检" in str(item.get("title") or "") or "检查" in str(item.get("title") or "")
    ]
    V.check(
        "a plan with a date becomes an unfinished matter the bot owes the user a follow-up on",
        bool(matters),
        _short([{"title": item.get("title"), "status": item.get("status")} for item in story.unfinished()]),
    )
    V.check(
        "the user's return question is answered like any other message",
        any(turn.kind == "reply" for turn in story.recorder.bot_turns(session, stage=ACQUAINTANCE)),
        _short([turn.text for turn in story.recorder.bot_turns(session, stage=ACQUAINTANCE)][-2:]),
    )
    section = section_body(story.injected(), SECTION_MEMORY)
    V.check(
        "the facts of the week are in front of the acting layer, not only in the database",
        any(marker in section for marker in ("地图", "通勤", "节奏", "体检")),
        _short({"memory_section": section}, 300),
    )
    hours = hours_from_section(section_body(story.injected(), SECTION_TIME), "距离上次用户消息")
    V.check(
        "the Runtime reports the hours since the user's last message from its own clock",
        hours is not None,
        _short({"hours_since_last_user_message": hours}),
    )
    V.check(
        "no intimacy is presumed while the relationship is still an acquaintance",
        not _intimacy_violations(story),
        _short(_intimacy_violations(story)) or "no uninvited intimate wording",
    )
    V.check(
        "no boundary exists at all: the acquaintance has not asked for anything to be avoided",
        not story.boundaries(),
        _short(
            [{"scope": item.get("scope"), "subject": item.get("subject")} for item in story.boundaries()]
        )
        or "the boundary table is empty, as it should be",
    )
    # The first probe: can the bot answer about what it was told this week?
    landmarks = [landmark for landmark in LANDMARKS if landmark.stage == ACQUAINTANCE]
    story.probe_landmarks(ACQUAINTANCE, landmarks)
    _recall_checks(story, landmarks, where="acquaintance stage")
    _cued_recall_checks(story, landmarks, where="acquaintance-stage facts")
    V.check(
        "every disclosure of this stage is held as a memory that traces to the user's own sentence",
        all(story.memory_with(landmark.marker) for landmark in landmarks),
        _short(
            {
                landmark.marker: [item.get("summary") for item in story.memory_with(landmark.marker)]
                for landmark in landmarks
            }
        ),
    )
    story.ops_snapshot("state after the acquaintance stage")
    story.end_stage(ACQUAINTANCE)
    V.note(
        f"acquaintance stage: {record.user_turns} user turn(s), {record.bot_turns} bot message(s), "
        f"{record.proactives} unprompted, recall={record.recall}"
    )


# ------------------------------------------------------------------ phase 4: friend


def _event_ids_for(story: Story, needle: str) -> set[str]:
    """Return the ids of the user-message events whose content contains ``needle``.

    Read through the public ``GET /events`` route, which is the Runtime's own answer
    about its history; the JSONL mirror is only consulted when the route could not
    be read (the mirror's own completeness is one of the things the inspection
    reports on, so it is not the first source for a check).
    """
    found: set[str] = set()
    reply = story.api("/events?limit=500&newest_first=true")
    rows = reply.field("events", default=[])
    if isinstance(rows, Sequence):
        for event in rows:
            if not isinstance(event, Mapping):
                continue
            if (
                str(event.get("event_type") or "") == "user_message"
                and needle in str(event.get("content") or "")
            ):
                found.add(str(event.get("event_id") or ""))
    if found or reply.ok:
        return found
    path = story.base_dir / "scenario" / "raw_events.jsonl"
    if not path.exists():
        return found
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                if (
                    str(record.get("event_type") or "") == "user_message"
                    and needle in str(record.get("content") or "")
                ):
                    found.add(str(record.get("event_id") or ""))
    return found


def boundary_verdict(story: Story) -> dict[str, Any]:
    """Return the Runtime's live answer to "may I initiate contact right now?".

    ``GET /boundaries`` computes this on the spot, which is what the delivery gate
    itself consults; ``/state.allow_proactive`` is derived state that a tick writes,
    so it lags a declaration by one tick and is not what a check should read.
    """
    payload = story.api("/boundaries").field("verdict", default=None)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _events_by_type(story: Story) -> dict[str, int]:
    """Count the raw events in the Runtime's JSONL mirror by event type."""
    path = story.base_dir / "scenario" / "raw_events.jsonl"
    counts: dict[str, int] = {}
    if not path.exists():
        return counts
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                key = str(record.get("event_type") or "?")
                counts[key] = counts.get(key, 0) + 1
    return counts


def _event_of(story: Story, event_id: str) -> dict[str, Any] | None:
    """Return one raw event by id: the public route first, the JSONL mirror after."""
    if not event_id:
        return None
    reply = story.api(f"/events/{urllib.parse.quote(event_id, safe='')}")
    event = reply.field("event", default=None)
    if isinstance(event, Mapping):
        return dict(event)
    path = story.base_dir / "scenario" / "raw_events.jsonl"
    if path.exists():
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                with contextlib.suppress(ValueError):
                    record = json.loads(line)
                    if str(record.get("event_id") or "") == event_id:
                        return record
    return None


def phase_friend(story: Story, ctx: Context) -> None:
    """朋友：a difficult confidence, a boundary set and adjusted, and a memory probe.

    The stage is driven in slices, not in one go, because the checks have to be
    evaluated *while* a window is live. Opening the boundary window after the script
    had already finished would file no delivered turn inside it, and the check would
    pass because it looked at nothing.
    """
    V.phase("friend", "PHASE 4 friend / 朋友：心事、边界与被记住")
    assert story.server is not None
    session = SESSION_A
    record = story.begin_stage(FRIEND, "friend")
    # turns 0-5: the confidences, the joke, the thanks
    story.run_script(FRIEND, start=0, stop=6)
    V.check(
        "the joke the user cracked is answered like any other message",
        len(story.recorder.bot_turns(session, kind="reply", stage=FRIEND)) >= 6,
        _short([turn.text for turn in story.recorder.bot_turns(session, kind="reply", stage=FRIEND)][-2:]),
    )

    # -- turn 6 declares the topic boundary ----------------------------------------
    story.script_turn(FRIEND, 6)
    boundaries = story.boundaries()
    topic_boundary = next(
        (
            item
            for item in boundaries
            if str(item.get("scope") or "") in {"repeated_interrogation", "topic_avoid"}
        ),
        None,
    )
    V.check(
        "the user's topic boundary is recorded as a real boundary the Runtime can enforce",
        topic_boundary is not None and topic_boundary.get("allow_proactive") is True,
        _short(
            [
                {
                    "scope": item.get("scope"),
                    "subject": item.get("subject"),
                    "expires_at": item.get("expires_at"),
                    "note": item.get("note"),
                }
                for item in boundaries
            ]
        ),
    )
    V.check(
        "the topic boundary is enforced by the Runtime's own verdict, not only recorded",
        topic_boundary is not None and boundary_verdict(story) is not None,
        _short({"verdict": boundary_verdict(story), "scope": (topic_boundary or {}).get("scope")}),
    )
    V.check(
        "the declared boundary reaches the acting layer as a hard constraint",
        SECTION_BOUNDARY in story.context_block(),
        _short({"boundary_section": section_body(story.context_block(), SECTION_BOUNDARY)}, 300),
    )
    if topic_boundary is not None:
        starts = _parse_iso(topic_boundary.get("starts_at")) or story.clock.now()
        expires = _parse_iso(topic_boundary.get("expires_at")) or (starts + 72 * HOUR)
        topic_window = story.open_window(
            Window(
                name="the topic-boundary window",
                session=session,
                start=starts,
                end=expires,
                proactive_allowed=True,
                stage=FRIEND,
                note="being told to drop a subject is not being told to fall silent",
            )
        )
        if story.faults.ignore_boundary:
            # A stale queue row that still goes out: the harness speaks inside the
            # window the user just closed, which is what this check exists to catch.
            story.platform.deliver(
                session,
                BOUNDARY_IGNORE_TOPIC_TEXT,
                kind="proactive",
                at=story.clock.now(),
                stage=FRIEND,
            )
        # turns 7-8 happen inside the window; the user says nothing in between.
        story.run_script(FRIEND, start=7, stop=9)
        inside = story.in_window(topic_window, who="bot", kind="proactive")
        offenders = [
            {"at": turn.at.isoformat(), "text": turn.text, "matched": scan_interrogation(turn.text)}
            for turn in inside
            if scan_interrogation(turn.text)
        ]
        V.check(
            "no unprompted message interrogates the user about what they are doing while that is forbidden",
            not offenders,
            _short(offenders)
            or f"{len(inside)} unprompted message(s) inside the window, none interrogating "
            f"(subject={_short(topic_boundary.get('subject'), 40)})",
        )

    # -- turn 8 declares the contact ban ------------------------------------------
    ban = next(
        (
            item
            for item in story.boundaries()
            if item.get("allow_proactive") is False and item.get("revoked_at") is None
        ),
        None,
    )
    V.check(
        "the later request not to be contacted is recorded as a ban on unprompted contact",
        ban is not None,
        _short(
            [
                {
                    "type": item.get("type"),
                    "allow_proactive": item.get("allow_proactive"),
                    "expires_at": item.get("expires_at"),
                    "revoked_at": item.get("revoked_at"),
                }
                for item in story.boundaries()
            ]
        ),
    )
    if ban is not None:
        ban_start = _parse_iso(ban.get("starts_at")) or story.clock.now()
        ban_end = _parse_iso(ban.get("expires_at")) or (ban_start + 30 * HOUR)
        story.open_window(
            Window(
                name="the contact-ban window (the user asked for quiet)",
                session=session,
                start=ban_start,
                end=ban_end,
                proactive_allowed=False,
                stage=FRIEND,
                note="the user asked not to be contacted proactively",
            )
        )
        if story.faults.ignore_boundary:
            story.platform.deliver(
                session,
                BOUNDARY_IGNORE_BAN_TEXT,
                kind="proactive",
                at=story.clock.now(),
                stage=FRIEND,
            )
        V.check(
            "while the ban is in force the Runtime's own verdict forbids unprompted contact",
            (boundary_verdict(story) or {}).get("allow_proactive") is False,
            _short({"verdict": boundary_verdict(story), "ban": [ban_start.isoformat(), ban_end.isoformat()]}),
        )
        # turn 9 is a deliberately silent stretch: the user says nothing at all while
        # the ban holds, so the window is exactly the interval the Runtime may not
        # speak in.
        story.script_turn(FRIEND, 9)
        ban_proactives = [
            turn
            for turn in story.recorder.bot_turns(session, kind="proactive")
            if ban_start <= turn.at <= ban_end
        ]
        V.check(
            "for the whole ban window the user receives zero unprompted messages",
            not ban_proactives,
            f"expected=0 actual={len(ban_proactives)} "
            + _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in ban_proactives]),
        )
        V.check(
            "the ban is a window, not a permanent mute: the user's own messages are still answered",
            all(
                any(
                    other.who == "bot" and other.kind == "reply" and other.at >= user.at
                    for other in story.recorder.turns
                )
                for user in story.recorder.user_turns(session)
                if ban_start <= user.at <= ban_end
            ),
            _short(
                [turn.text for turn in story.recorder.user_turns(session) if ban_start <= turn.at <= ban_end]
            )
            or "the user said nothing inside the ban window at all; the next check pins the lifted state",
        )

    # -- turn 10 lifts the boundary ------------------------------------------------
    story.script_turn(FRIEND, 10)
    lifted_at = story.clock.now()
    revoked = [
        item
        for item in story.boundaries()
        if item.get("revoked_at") is not None and _parse_iso(item.get("revoked_at")) is not None
    ]
    V.check(
        "the lifted instruction is recorded as revoked rather than silently dropped",
        bool(revoked),
        _short(
            [{"scope": item.get("scope"), "revoked_at": item.get("revoked_at")} for item in story.boundaries()]
        ),
    )
    V.check(
        "the user's own verdict is permissive again once the ban is lifted",
        (boundary_verdict(story) or {}).get("allow_proactive") is True,
        _short({"verdict": boundary_verdict(story), "at": lifted_at.isoformat()}),
    )
    story.script_turn(FRIEND, 11)
    V.check(
        "the boundary no longer constrains the acting layer after it was lifted",
        SECTION_BOUNDARY not in story.context_block(),
        _short({"boundary_section": section_body(story.context_block(), SECTION_BOUNDARY)})
        or "no boundary section in the Runtime's own render of the present",
    )
    # -- the probe: the user asks about what they told earlier --------------------
    V.check(
        "the difficult thing the user confided is remembered once the day has moved on",
        bool(story.memory_with("医院")),
        _short(
            [
                {"summary": item.get("summary"), "kind": item.get("kind")}
                for item in story.memory_with("医院")
            ]
        )
        or _short([item.get("summary") for item in story.memories()[:6]]),
    )
    V.check(
        "the personal story the user told in confidence is remembered once the day has moved on",
        bool(story.memory_with("团子")),
        _short([item.get("summary") for item in story.memory_with("团子")])
        or _short([item.get("summary") for item in story.memories()[:6]]),
    )
    landmarks = [landmark for landmark in LANDMARKS if landmark.stage == FRIEND]
    story.probe_landmarks(FRIEND, landmarks)
    _recall_checks(story, landmarks, where="friend stage")
    _cued_recall_checks(story, landmarks, where="friend-stage confidences")
    disclosure_events = _event_ids_for(story, "医院")
    V.check(
        "the remembered disclosure traces back to a user message the user actually typed it in",
        bool(disclosure_events)
        and any(
            disclosure_events & set(memory.get("source_event_ids") or [])
            for memory in story.memory_with("医院")
        ),
        _short(
            {
                "user_message_events_about_it": sorted(disclosure_events),
                "sources": [memory.get("source_event_ids") for memory in story.memory_with("医院")],
            }
        ),
    )
    V.check(
        "no unprompted message in this stage leaks hidden context",
        not [
            turn
            for turn in story.recorder.bot_turns(kind="proactive", stage=FRIEND)
            if scan_leakage(turn.text)
        ],
        _short([turn.text for turn in story.recorder.bot_turns(kind="proactive", stage=FRIEND)]),
    )
    V.check(
        "no intimacy the user has not invited appears while they are a friend",
        not _intimacy_violations(story),
        _short(_intimacy_violations(story)) or "no uninvited intimate wording",
    )
    story.ops_snapshot("state after the friend stage")
    story.end_stage(FRIEND)
    V.note(
        f"friend stage: {record.user_turns} user turn(s), {record.bot_turns} bot message(s), "
        f"{record.proactives} unprompted, recall={record.recall}"
    )

# ------------------------------------------------------------------ phase 5: lover


def phase_lover(story: Story, ctx: Context) -> None:
    """恋人：affection, an intimate address, a long silence, and continuity."""
    V.phase("lover", "PHASE 5 lover / 恋人：表白、长时间沉默、连续的记忆")
    assert story.server is not None
    session = SESSION_A
    record = story.begin_stage(LOVER, "lover")

    # -- turn 0: the user comes back and opens the heart --------------------------
    story.script_turn(LOVER, 0)
    # -- turn 1: the declaration, watched at the moment it lands ------------------
    story.script_turn(LOVER, 1)
    block = story.injected()
    V.check(
        "an explicit declaration of affection is settled as closeness, not left unresolved",
        CLOSENESS_SIGNAL in block,
        _short({"situation": section_body(block, SECTION_SITUATION), "block_chars": len(block)}, 400),
    )
    # -- turns 2-3: the relational statement --------------------------------------
    story.script_turn(LOVER, 2)
    story.script_turn(LOVER, 3)
    # -- turn 4: the user uses an intimate address first --------------------------
    story.script_turn(LOVER, 4)
    V.check(
        "the bot may use an address the user themselves used, and still may not invent one",
        not _intimacy_violations(story),
        _short(_intimacy_violations(story)) or "every intimate address was invited by the user first",
    )
    story.script_turn(LOVER, 5)

    # -- turn 6's gap is the long silence -----------------------------------------
    silence_start = story.clock.now()
    silence = story.open_window(
        Window(
            name="the multi-week silence",
            session=session,
            start=silence_start,
            end=silence_start + 24 * DAY,
            proactive_allowed=True,
            stage=LOVER,
            note="the user is away for three and a half weeks and does not answer anything",
        )
    )
    story.reask_window = silence
    story.advance(24 * DAY, label="the multi-week silence", size=SILENCE_STEP)
    story.reask_window = None
    silence_end = story.clock.now()

    state = story.state()
    tick = _parse_iso(state.get("last_tick_at"))
    V.check(
        "the Runtime integrated the multi-week silence into its own clock",
        tick is not None and abs((silence_end - tick).total_seconds()) <= 2 * SILENCE_STEP.total_seconds(),
        _short({"last_tick_at": state.get("last_tick_at"), "clock_now": silence_end.isoformat()}),
    )
    last_user = _parse_iso(state.get("last_user_message_at"))
    elapsed_days = (silence_end - last_user).total_seconds() / 86400.0 if last_user else -1.0
    fresh_block = story.context_block()
    time_section = section_body(fresh_block, SECTION_TIME)
    reported_hours = hours_from_section(time_section, "距离上次用户消息")
    V.check(
        "the Runtime's own numbers say the silence lasted weeks, without the script telling it",
        elapsed_days >= 21.0 and reported_hours is not None and reported_hours >= 21 * 24,
        _short(
            {
                "elapsed_days_from_state": round(elapsed_days, 2),
                "hours_since_last_user_message_in_block": reported_hours,
                "last_user_message_at": state.get("last_user_message_at"),
                "block_time_section": time_section,
            },
            400,
        ),
    )
    relationship_memories = [item for item in story.memories() if item.get("kind") == "relationship"]
    V.check(
        "the relational statement is kept as a relationship memory, not as an episode or a preference",
        bool(relationship_memories)
        and any("信任" in str(item.get("summary") or "") for item in relationship_memories),
        _short(
            [
                {"summary": item.get("summary"), "kind": item.get("kind")}
                for item in story.memories()
                if "信任" in str(item.get("summary") or "")
            ]
        )
        or _short(
            [{"summary": item.get("summary"), "kind": item.get("kind")} for item in story.memories()[:6]]
        ),
    )
    cap = story.server.config.drive.max_contacts_per_day
    silent_proactives = [
        turn
        for turn in story.recorder.bot_turns(session, kind="proactive")
        if silence_start <= turn.at <= silence_end
    ]
    worst = 0
    for turn in silent_proactives:
        inside = [other for other in silent_proactives if timedelta(0) <= (other.at - turn.at) <= DAY]
        worst = max(worst, len(inside))
    V.check(
        "an unanswered user does not produce a stream of re-asks: no 24h exceeds the contact cap",
        worst <= cap,
        f"cap={cap} worst_24h={worst} total={len(silent_proactives)} "
        f"({_short([turn.at.strftime('%m-%d %H:%M') for turn in silent_proactives], 160)})",
    )
    normalized = [normalize_message(turn.text) for turn in silent_proactives]
    repeated = {
        text: normalized.count(text) for text in set(normalized) if normalized.count(text) > REASK_TOLERANCE
    }
    V.check(
        "the user is not buried by the same prompt being re-asked over and over",
        not repeated,
        _short(repeated)
        or f"{len(silent_proactives)} prompt(s), nothing repeated more than {REASK_TOLERANCE} times",
    )
    accused = [turn for turn in story.recorder.bot_turns(kind="proactive") if scan_guilt(turn.text)]
    V.check(
        "the bot never blames the user for not answering, anywhere in the run",
        not accused,
        _short([{"at": turn.at.isoformat(), "text": turn.text, "matched": scan_guilt(turn.text)} for turn in accused])
        or f"{len(story.recorder.bot_turns(kind='proactive'))} unprompted message(s) in the run, none accusatory",
    )
    V.check(
        "nothing the bot said while it was being ignored blames the user",
        not [turn for turn in silent_proactives if scan_guilt(turn.text)],
        _short(
            [
                {"text": turn.text, "matched": scan_guilt(turn.text)}
                for turn in silent_proactives
                if scan_guilt(turn.text)
            ]
        )
        or f"{len(silent_proactives)} prompt(s) during the silence, none accusatory",
    )

    # -- turn 6: the user returns after the silence -------------------------------
    story.script_turn(LOVER, 6)
    V.check(
        "the returning user is answered after three and a half weeks of silence",
        bool(story.recorder.bot_turns(session, kind="reply", stage=LOVER)),
        _short([turn.text for turn in story.recorder.bot_turns(session, kind="reply", stage=LOVER)][-1:]),
    )
    # -- turn 7: the user reacts to the message they never answered ---------------
    story.script_turn(LOVER, 7)
    guilt_after = [
        turn for turn in story.recorder.bot_turns() if turn.at >= silence_start and scan_guilt(turn.text)
    ]
    V.check(
        "a message the user left unanswered never turns into guilt afterwards",
        not guilt_after,
        _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in guilt_after])
        or f"{len(story.recorder.bot_turns())} bot message(s), none accusatory",
    )
    # -- the probes: what the bot still has in front of it ------------------------
    lover_landmarks = [landmark for landmark in LANDMARKS if landmark.stage == LOVER]
    story.probe_landmarks(LOVER, lover_landmarks)
    V.check(
        "the declaration and the relational statement are still known and traceable when the user asks",
        all(
            _recall_evidence(story, landmark)["known"] and _recall_evidence(story, landmark)["traced"]
            for landmark in lover_landmarks
        ),
        _short(
            {
                landmark.marker: {
                    key: _recall_evidence(story, landmark)[key]
                    for key in ("held", "known", "retrievable", "traced", "in_section")
                }
                for landmark in lover_landmarks
            }
        ),
    )
    _cued_recall_checks(story, lover_landmarks, where="declaration and the relational statement")
    # The friend-stage disclosures have to survive the silence as well: this is the
    # same probe set the friend stage used, asked again weeks later in different
    # words.
    friend_landmarks = [landmark for landmark in LANDMARKS if landmark.stage == FRIEND]
    story.probe_landmarks(LOVER, friend_landmarks, recheck=True)
    V.check(
        "the confidences of the friend stage survive the silence and are still known, not forgotten",
        all(_recall_evidence(story, landmark)["known"] for landmark in friend_landmarks),
        _short(
            {
                landmark.marker: {
                    key: _recall_evidence(story, landmark)[key]
                    for key in ("held", "known", "retrievable", "traced", "in_section")
                }
                for landmark in friend_landmarks
            }
        ),
    )
    _cued_recall_checks(story, friend_landmarks, where="friend-stage confidences")
    V.check(
        "asking about the declaration and the confidences reaches the acting layer as a recollection",
        any(
            _recall_evidence(story, landmark)["in_section"]
            for landmark in list(lover_landmarks) + list(friend_landmarks)
        ),
        _short(
            {
                landmark.marker: _recall_evidence(story, landmark)["in_section"]
                for landmark in list(lover_landmarks) + list(friend_landmarks)
            }
        )
        + " (the four-slot memory section is described in inspection.md)",
    )
    # -- turn 8: the user asks about the beginning --------------------------------
    story.script_turn(LOVER, 8)
    V.check(
        "the act of asking about the beginning does not make the bot invent early intimacy",
        not _intimacy_violations(story),
        _short(_intimacy_violations(story)) or "no uninvited intimate wording",
    )
    # -- turns 9-10: a fresh dated promise, so the story ends owing something -----
    story.script_turn(LOVER, 9)
    story.script_turn(LOVER, 10)
    unanswered = [
        user
        for user in story.recorder.user_turns(session, stage=LOVER)
        if not any(
            other.who == "bot" and other.kind == "reply" and other.at >= user.at
            for other in story.recorder.turns
        )
    ]
    V.check(
        "every user message of this stage was answered",
        not unanswered,
        _short([turn.text for turn in unanswered]) or f"{record.user_turns} turn(s) answered",
    )
    V.check(
        "the memory of the declaration is still retrievable at the end of the run",
        bool(story.memory_with("喜欢你")) and bool(story.memory_with("信任")),
        _short(
            {
                "declaration": [item.get("summary") for item in story.memory_with("喜欢你")],
                "relationship": [item.get("summary") for item in story.memory_with("信任")],
            }
        ),
    )
    story.ops_snapshot("state at the end of the story")
    story.end_stage(LOVER)
    V.note(
        f"lover stage: {record.user_turns} user turn(s), {record.bot_turns} bot message(s), "
        f"{record.proactives} unprompted, recall={record.recall}"
    )


# ------------------------------------------------------------------ phase 6: audit


def _recall_evidence(story: Story, landmark: Landmark) -> dict[str, Any]:
    """Return what is known about one disclosure at this moment.

    ``known`` is "the Runtime still holds this as a memory that a cue can recall"
    (anything that is neither archived nor superseded), ``retrievable`` is the
    operator surface's narrower flag (``status == "active"``, the working set), and
    ``in_section`` is the stronger observation "the Runtime also handed it to the
    acting layer for the turn the user asked about it".

    They are reported separately on purpose. Measured on this story: the fresh memory
    section at a probe turn is filled by memories of the user's *own earlier
    question-shaped probes*, which share single characters with any new question, so
    a disclosure is retrieved by the engine and still loses the four-slot budget.
    Asserting ``in_section`` for every disclosure would fail a run for a reason the
    whole prompt budget causes, so it is its own check and its misses are reported as
    a finding; ``retrievable`` alone would be wrong too, because fading is not
    forgetting.
    """
    rows = story.memory_with(landmark.marker)
    known = [
        row
        for row in rows
        if str(row.get("status") or "") != "archived" and row.get("superseded") is not True
    ]
    retrievable = [row for row in rows if row.get("retrievable", row.get("status") == "active")]
    said = "\n".join(turn.text for turn in story.recorder.user_turns())
    traced = [
        row
        for row in known
        if trace_ratio(str(row.get("summary") or ""), said) >= MEMORY_TRACE_RATIO
    ]
    return {
        "marker": landmark.marker,
        "held": bool(rows),
        "known": bool(known),
        "retrievable": bool(retrievable),
        "traced": bool(traced),
        "statuses": sorted({str(row.get("status")) for row in rows}),
        "rows": [
            {
                "summary": row.get("summary"),
                "kind": row.get("kind"),
                "status": row.get("status"),
                "retrievable": row.get("retrievable"),
            }
            for row in rows
        ],
        "in_section": any(
            entry["marker"] == landmark.marker and entry["in_section"] for entry in story.probe_log
        ),
        "section": next(
            (
                entry["section"]
                for entry in reversed(story.probe_log)
                if entry["marker"] == landmark.marker
            ),
            "",
        ),
    }


def _cued_recall_checks(story: Story, landmarks: Sequence[Landmark], *, where: str) -> None:
    """Assert that a real cue brought each disclosure back into the working set.

    The cue has already been typed by the user through the plugin's hook, and one
    autonomous round was allowed to run afterwards (see
    :meth:`Story.probe_landmarks`), so this reads the recorded before/after status of
    each memory. "Back into the working set" is ``status == "active"``: the Runtime
    reinstates a faded memory that a matching cue recalls, which is the property a
    person means by "it remembered what I told it".

    Args:
        story: The live story.
        landmarks: The disclosures the user asked about.
        where: Human-readable name of the group, for the check's label.
    """
    evidence = {
        entry["marker"]: entry
        for entry in story.probe_log
        if entry["marker"] in {landmark.marker for landmark in landmarks}
    }
    detail = {
        marker: {
            "statuses_before_the_cue": entry["statuses_before_the_cue"],
            "statuses_after_the_recall_step": entry["statuses_after_the_recall_step"],
            "active_after_the_recall_step": entry["active_after_the_recall_step"],
        }
        for marker, entry in evidence.items()
    }
    V.check(
        f"asking about the {where} brings each memory back into the working set",
        bool(evidence) and all(entry["active_after_the_recall_step"] for entry in evidence.values()),
        _short(detail, 500),
    )


def _recall_checks(story: Story, landmarks: Sequence[Landmark], *, where: str) -> dict[str, dict[str, Any]]:
    """Record the two recall assertions for one group of disclosures.

    Args:
        story: The live story.
        landmarks: The disclosures to report on (they should already have been probed).
        where: Human-readable name of the stage the probes belong to.

    Returns:
        The evidence per marker, for the caller's diagnostics.
    """
    evidence = {landmark.marker: _recall_evidence(story, landmark) for landmark in landmarks}
    V.check(
        f"every disclosure from the {where} is still known (not archived, not superseded) and traces to the user's words",
        all(item["known"] and item["traced"] for item in evidence.values()),
        _short(
            {
                marker: {k: item[k] for k in ("held", "known", "retrievable", "traced", "statuses")}
                for marker, item in evidence.items()
            }
        )
        or "no disclosure",
    )
    V.check(
        f"asking about the {where} reaches the acting layer as a recollection",
        any(item["in_section"] for item in evidence.values()),
        _short(
            {
                marker: {"in_section": item["in_section"], "section": item["section"]}
                for marker, item in evidence.items()
            },
            500,
        ),
    )
    return evidence


def _untraceable_memories(story: Story) -> list[dict[str, Any]]:
    """Return memories whose wording appears nowhere in what the user typed."""
    said = "\n".join(turn.text for turn in story.recorder.user_turns())
    return [
        {
            "summary": item.get("summary"),
            "trace_ratio": round(trace_ratio(str(item.get("summary") or ""), said), 3),
        }
        for item in story.memories()
        if trace_ratio(str(item.get("summary") or ""), said) < MEMORY_TRACE_RATIO
    ]


def phase_audit(story: Story, ctx: Context) -> None:
    """全局契约与记忆单调性：the whole run judged at once."""
    V.phase("audit", "PHASE 6 the whole run, audited / 全局契约与记忆单调性")
    assert story.server is not None
    bot_turns = [turn for turn in story.recorder.bot_turns() if turn.delivered]
    proactives = [turn for turn in bot_turns if turn.kind == "proactive"]
    V.note(
        f"{len(story.recorder.user_turns())} user message(s), {len(bot_turns)} delivered bot message(s), "
        f"{len(proactives)} unprompted, over "
        f"{round((story.clock.now() - story.clock.origin).total_seconds() / 86400.0, 1)} simulated day(s)"
    )
    V.check(
        "every message the user sent is answered",
        not [
            user
            for user in story.recorder.user_turns()
            if not any(
                other.who == "bot" and other.kind == "reply" and other.at >= user.at
                for other in story.recorder.turns
            )
        ],
        _short(
            [
                {"at": turn.at.isoformat(), "text": turn.text}
                for turn in story.recorder.user_turns()
                if not any(
                    other.who == "bot" and other.kind == "reply" and other.at >= turn.at
                    for other in story.recorder.turns
                )
            ]
        )
        or f"{len(story.recorder.user_turns())} user message(s) answered",
    )
    texts = [normalize_message(turn.text) for turn in bot_turns]
    repeated = {text: texts.count(text) for text in set(texts) if texts.count(text) > 1}
    V.check(
        "no two user-visible messages are duplicates over the whole run",
        not repeated,
        _short({"repeated": repeated, "texts": len(texts)}) or f"{len(texts)} distinct message(s)",
    )
    leaked = [
        {"at": turn.at.isoformat(), "patterns": ",".join(scan_leakage(turn.text)), "text": turn.text}
        for turn in bot_turns
        if scan_leakage(turn.text)
    ]
    V.check(
        "no user-visible message leaks hidden context, credentials or internal ids",
        not leaked,
        _short(leaked) or f"{len(bot_turns)} message(s), none leaking",
    )
    default_traffic = [turn for turn in story.recorder.turns if turn.session == SESSION_DEFAULT]
    wrong_session = [
        turn for turn in story.recorder.turns if turn.session not in {SESSION_A, SESSION_DEFAULT}
    ]
    V.check(
        "no message goes to the process-default conversation or to a chat the user does not use",
        not default_traffic and not wrong_session,
        _short(
            {
                "default": [turn.text for turn in default_traffic],
                "other": [turn.session for turn in wrong_session],
            }
        )
        or f"all traffic confined to {SESSION_A}",
    )
    cap = story.server.config.drive.max_contacts_per_day
    worst = 0
    worst_start: datetime | None = None
    for turn in proactives:
        inside = [other for other in proactives if timedelta(0) <= (other.at - turn.at) <= DAY]
        if len(inside) > worst:
            worst, worst_start = len(inside), turn.at
    V.check(
        "over the whole run no 24-hour window exceeds the configured daily contact cap",
        worst <= cap,
        f"cap={cap} worst_24h={worst} starting {(worst_start.isoformat() if worst_start else '-')} "
        f"total_proactive={len(proactives)}",
    )
    orphans = [
        {"at": turn.at.isoformat(), "text": turn.text}
        for turn in proactives
        if not any(
            window.session == turn.session
            and story.delivered_in(turn, window)
            and window.proactive_allowed
            for window in story.windows
        )
    ]
    V.check(
        "every unprompted message falls inside a scripted window where speaking made sense",
        not orphans,
        _short(orphans) or f"{len(proactives)} unprompted message(s) matched a scripted window",
    )
    silence_windows = [window for window in story.windows if not window.proactive_allowed]
    intrusions = [
        {"at": turn.at.isoformat(), "window": window.name, "text": turn.text}
        for turn in proactives
        for window in silence_windows
        if window.session == turn.session and story.delivered_in(turn, window)
    ]
    V.check(
        "nothing appears out of nowhere in a window where the user asked for silence",
        not intrusions,
        _short(intrusions) or f"{len(silence_windows)} silence window(s), 0 intrusion(s)",
    )
    V.check(
        "no memory asserts something the user never typed",
        not _untraceable_memories(story),
        _short(_untraceable_memories(story))
        or f"{len(story.memories())} memory/memories, all traceable to the user's words",
    )
    V.check(
        "no intimacy the user never invited is used anywhere in the run",
        not _intimacy_violations(story),
        _short(_intimacy_violations(story)) or "every intimate address was invited first",
    )
    # -- the headline: recall may not get worse as the relationship grows --------
    friend = story.stages.get(FRIEND)
    lover = story.stages.get(LOVER)
    if friend and lover and friend.recall and lover.recall:
        friend_landmarks = [landmark for landmark in LANDMARKS if landmark.stage == FRIEND]
        lost = sorted(
            marker for marker in friend.recall if not lover.recall.get(marker, False)
        )
        friend_held = {
            landmark.marker
            for landmark in friend_landmarks
            if _recall_evidence(story, landmark)["known"]
        }
        gained = sorted(
            marker for marker in lover.recall if marker not in friend.recall
        )
        # The assertion is set inclusion on what the Runtime still *knows*: a fact the
        # bot could answer about at the friend stage may not be forgotten by the
        # lover stage. The counts of what reached the four-slot prompt section are
        # reported alongside, because they are the weaker and noisier signal - see
        # `prompt_memory_crowding` in inspection.md for why they can go down.
        V.check(
            "nothing the bot could answer about at the friend stage has been forgotten by the lover stage",
            len(friend_held) == len(friend_landmarks)
            and all(
                _recall_evidence(story, landmark)["known"] for landmark in friend_landmarks
            ),
            _short(
                {
                    "friend_held": sorted(friend_held),
                    "friend_section": friend.recall,
                    "lover_section": lover.recall,
                    "section_lost": lost,
                    "section_gained": gained,
                }
            ),
        )
        V.note(
            f"recall comparison: friend section={friend.recall} lover section={lover.recall} "
            f"lost={lost} gained={gained}; every friend-stage disclosure is still held"
        )
    else:
        V.note(
            "the friend/lover recall comparison needs both stages; it was skipped because this "
            f"selection only ran {sorted(story.stages)}"
        )
    # -- the inspection's analysis of the dumps (findings, not checks) -----------
    _collect_findings(story)
    for finding in story.findings:
        V.note(f"finding ({finding['kind']}): {_short(finding['detail'], 400)}")


# ------------------------------------------------------------- phase 7: inspection


#: Every dump the user asked for: ``(key, endpoint, rows field, why it is empty)``.
#: An empty payload is only acceptable when the artifact states the reason.
DUMP_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("state", "/state", "", "the Runtime always has exactly one state row"),
    ("events", "/events?limit=500&newest_first=true", "events", "no raw event was ever appended"),
    ("memories", "/memories?limit=500", "memories", "no long-term memory was ever formed"),
    ("unfinished", "/unfinished?limit=200", "matters", "no unfinished matter was ever detected"),
    ("boundaries", "/boundaries", "boundaries", "the user never declared a boundary"),
    ("candidates", "/candidates?limit=200", "candidates", "the candidate pool was empty at the end of the run"),
    ("attempts", "/attempts?limit=200", "attempts", "the character never committed an action attempt"),
    ("outbox", "/outbox?limit=500", "items", "the delivery queue was empty at the end of the run"),
    ("user-model", "/user-model", "", "the user model always has a parameter row"),
    ("observations", "/observations?limit=500", "observations", "no interaction observation was ever recorded"),
)


def phase_inspection(story: Story, ctx: Context) -> None:
    """后台变量、参数与事件日志：dump the backend, then examine it."""
    V.phase("inspection", "PHASE 7 backend inspection / 后台变量、参数与事件日志")
    assert story.server is not None
    backend = ctx.base_dir / "backend"
    backend.mkdir(parents=True, exist_ok=True)

    for key, path, field_name, reason in DUMP_SPECS:
        reply = story.api(path)
        payload: Any = reply.json
        if field_name and isinstance(payload, Mapping):
            rows = payload.get(field_name)
        else:
            rows = payload
        if isinstance(rows, Mapping):
            count = len(rows)
        elif isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            count = len(rows)
        else:
            count = 0 if rows is None else 1
        empty_reason = reason if count == 0 else ""
        document = {
            "endpoint": path,
            "fetched_at": story.clock.now().isoformat(),
            "http_status": reply.status,
            "rows_field": field_name or "(body)",
            "count": count,
            "empty_reason": empty_reason,
            "payload": payload,
        }
        (backend / f"{key}.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        story.dumps[key] = document
        V.check(
            f"the {key} dump was read through {path} and written to backend/{key}.json",
            reply.ok and (count > 0 or bool(empty_reason)),
            _short(
                {
                    "status": reply.status,
                    "rows": count,
                    "empty_reason": empty_reason,
                    "error": reply.error,
                }
            ),
        )

    # The raw event log itself: the Runtime writes it as a JSONL mirror, and the
    # HTTP route only pages 500 rows, so the mirror is copied as the full log.
    mirror = story.base_dir / "scenario" / "raw_events.jsonl"
    copied = backend / "raw_events.jsonl"
    lines = 0
    if mirror.exists():
        with contextlib.suppress(OSError):
            lines = len([line for line in mirror.read_text(encoding="utf-8").splitlines() if line.strip()])
        with contextlib.suppress(OSError):
            shutil.copyfile(mirror, copied)
    raw_events_total = int(story.server.health().get("raw_events") or 0)
    V.check(
        "the raw event log mirror was copied into the artifact directory",
        copied.exists() and lines > 0,
        _short({"mirror": str(mirror), "lines": lines, "runtime_raw_events": raw_events_total})
        or "the JSONL mirror was never written",
    )
    page_rows = int((story.dumps.get("events") or {}).get("count") or 0)
    V.check(
        "the event dump carries the whole run: the HTTP page covers it, or the mirror fills the gap",
        page_rows >= raw_events_total or lines >= raw_events_total,
        _short(
            {
                "http_page_rows": page_rows,
                "mirror_lines": lines,
                "runtime_raw_events": raw_events_total,
                "note": "the HTTP route caps a page at 500 rows",
            }
        ),
    )

    _collect_findings(story)
    report = render_inspection(story, ctx)
    (ctx.base_dir / "inspection.md").write_text(report, encoding="utf-8")
    V.check(
        "the inspection report was written and names every dump",
        (ctx.base_dir / "inspection.md").exists()
        and all(f"backend/{key}.json" in report for key, _p, _f, _r in DUMP_SPECS),
        _short({"path": str(ctx.base_dir / "inspection.md"), "chars": len(report)}),
    )
    V.check(
        "the inspection report examines the dumps instead of only listing them",
        "## 4." in report and "## 8." in report,
        _short({"sections": report.count("## ")}),
    )
    # Re-render once the phase's own two checks exist, so the report's own table
    # counts the phase it was written by.
    (ctx.base_dir / "inspection.md").write_text(render_inspection(story, ctx), encoding="utf-8")
    if not ctx.quiet:
        V.line("")
        V.line("-" * 78)
        V.line("BACKEND FINDINGS (the examination written to inspection.md)")
        V.line("-" * 78)
        for finding in story.findings:
            V.line(f"  * [{finding['kind']}] {_short(finding['detail'], 300)}")
        V.line("-" * 78)


def _collect_findings(story: Story) -> None:
    """Examine the backend and record what a human would care about.

    Every finding is computed from the dumps the run just read through the public
    API (plus the Runtime's own JSONL mirror for provenance), and each carries the
    evidence line it was read from. Findings are observations, never assertions: the
    checks elsewhere in this script are what fail a run.
    """
    story.findings = []
    now = story.clock.now()
    state = story.dumps.get("state", {}).get("payload") or story.state()
    memories = (story.dumps.get("memories", {}).get("payload") or {}).get("memories") or story.memories()
    activated = (story.dumps.get("memories", {}).get("payload") or {}).get("activated") or []
    boundaries = (story.dumps.get("boundaries", {}).get("payload") or {}).get("boundaries") or story.boundaries()
    candidates = (story.dumps.get("candidates", {}).get("payload") or {}).get("candidates") or []
    attempts = (story.dumps.get("attempts", {}).get("payload") or {}).get("attempts") or []
    observations = (story.dumps.get("observations", {}).get("payload") or {}).get("observations") or []
    user_model = (
        (story.dumps.get("user-model", {}).get("payload") or {}).get("numeric") or {}
    )

    # 1. memories that should not exist: greetings, small talk, acknowledgements.
    trivia = _transient_memory_hits(story)
    if trivia:
        story.findings.append(
            {
                "kind": "trivia_memories",
                "detail": {
                    "note": "small talk and acknowledgements kept as long-term memories",
                    "rows": trivia[:6],
                },
            }
        )

    # 2. a memory whose *kind* was decided by a substring rather than a statement.
    substring_kinds = [
        {
            "summary": item.get("summary"),
            "kind": item.get("kind"),
            "note": "the kind rule matched the bare character 最 inside 最近, so an "
            "ordinary life fact is filed as a user preference",
        }
        for item in memories
        if item.get("kind") == "user_preference"
        and "最" in str(item.get("summary") or "")
        and "喜欢" not in str(item.get("summary") or "")
    ]
    if substring_kinds:
        story.findings.append(
            {"kind": "memory_kind_from_substring", "detail": {"rows": substring_kinds[:6]}}
        )

    # 2b. a question kept as a long-term memory. A question is not a fact about the
    # user, and the Runtime has a marker list that already knows this - for the kind
    # it picks, not for whether the candidate is admitted at all.
    question_memories = [
        {"summary": item.get("summary"), "kind": item.get("kind"), "status": item.get("status")}
        for item in memories
        if any(token in str(item.get("summary") or "") for token in ("吗？", "吗?", "呢？", "?"))
    ]
    if question_memories:
        story.findings.append(
            {
                "kind": "question_kept_as_memory",
                "detail": {
                    "note": "the user's own questions were promoted into long-term memories",
                    "rows": question_memories[:8],
                },
            }
        )

    # 2c. the JSONL mirror against the database's own event count.
    mirror_lines = _mirror_lines(story.base_dir)
    runtime_events = int((story.server.health().get("raw_events") if story.server else 0) or 0)
    if runtime_events and mirror_lines != runtime_events:
        by_type = _events_by_type(story)
        page = (story.dumps.get("events", {}).get("payload") or {}).get("events") or []
        page_types: dict[str, int] = {}
        for event in page:
            key = str(event.get("event_type") or "?")
            page_types[key] = page_types.get(key, 0) + 1
        missing = {
            key: page_types[key] - by_type.get(key, 0)
            for key in page_types
            if page_types.get(key, 0) > by_type.get(key, 0)
        }
        story.findings.append(
            {
                "kind": "jsonl_mirror_incomplete",
                "detail": {
                    "note": "the Runtime's disaster-recovery/inspection mirror holds fewer events "
                    "than the database does; nothing in the log warns about it, and the missing "
                    "ones include the user's own messages",
                    "mirror_lines": mirror_lines,
                    "runtime_raw_events": runtime_events,
                    "missing_by_type": missing,
                    "mirror_by_type": by_type,
                },
            }
        )

    # 3. a boundary that was declared but never enforced.
    for boundary in boundaries:
        if boundary.get("scope") in {"topic_avoid", "repeated_interrogation"}:
            subject = str(boundary.get("subject") or "").strip()
            declaring = _event_of(story, str(boundary.get("source_event_id") or ""))
            declaring_text = str((declaring or {}).get("content") or "")
            if not subject:
                story.findings.append(
                    {
                        "kind": "boundary_without_subject",
                        "detail": {
                            "note": "a topic boundary with no bound subject can never block a candidate",
                            "boundary_id": boundary.get("boundary_id"),
                            "scope": boundary.get("scope"),
                            "expires_at": boundary.get("expires_at"),
                            "declared_by": declaring_text,
                        },
                    }
                )
            elif declaring_text and not (_bigrams(subject) & _bigrams(declaring_text)):
                story.findings.append(
                    {
                        "kind": "boundary_subject_mismatch",
                        "detail": {
                            "note": "the subject this boundary was bound to shares no wording with "
                            "the instruction that declared it, so the boundary constrains an "
                            "unrelated subject instead of the one the user named",
                            "boundary_id": boundary.get("boundary_id"),
                            "scope": boundary.get("scope"),
                            "subject": subject,
                            "declared_by": declaring_text,
                        },
                    }
                )
        if boundary.get("allow_proactive") is False:
            start = _parse_iso(boundary.get("starts_at"))
            end = _parse_iso(boundary.get("revoked_at")) or _parse_iso(boundary.get("expires_at")) or now
            if start is None:
                continue
            violated = [
                turn for turn in story.recorder.bot_turns(kind="proactive") if start <= turn.at <= end
            ]
            if violated:
                story.findings.append(
                    {
                        "kind": "boundary_not_enforced",
                        "detail": {
                            "note": "an unprompted message was delivered inside a no-contact window",
                            "boundary_id": boundary.get("boundary_id"),
                            "window": [start.isoformat(), end.isoformat()],
                            "messages": [turn.text for turn in violated],
                        },
                    }
                )

    # 4. a candidate that never expired.
    stale_candidates = [
        {
            "candidate_id": item.get("candidate_id"),
            "type": item.get("type"),
            "status": item.get("status"),
            "expires_at": item.get("expires_at"),
        }
        for item in candidates
        if (expiry := _parse_iso(item.get("expires_at"))) is not None and expiry < now
    ]
    if stale_candidates:
        story.findings.append(
            {
                "kind": "candidate_never_expired",
                "detail": {"note": "active candidates whose TTL is already in the past", "rows": stale_candidates[:6]},
            }
        )

    # 5. an observation that was never applied.
    unapplied = [
        {
            "observation_id": item.get("observation_id"),
            "created_at": item.get("created_at"),
            "weight": item.get("weight"),
            "applied": item.get("applied"),
        }
        for item in observations
        if not item.get("applied")
    ]
    if unapplied:
        story.findings.append(
            {
                "kind": "observation_never_applied",
                "detail": {"note": "an observation row that never reached the parameters", "rows": unapplied[:6]},
            }
        )

    # 6. a user-model parameter that never moved.
    early = story.early_user_model or {}
    moved: list[str] = []
    frozen: list[str] = []
    for target in ("reply_probability", "positive_probability", "continue_probability", "boundary_risk"):
        before = (early.get(target) or {}) if isinstance(early, Mapping) else {}
        after = (user_model.get(target) or {}) if isinstance(user_model, Mapping) else {}
        for feature, value in (after or {}).items():
            if not isinstance(value, (int, float)):
                continue
            old = before.get(feature)
            if old is None:
                continue
            (moved if abs(float(old) - float(value)) > 1e-9 else frozen).append(f"{target}.{feature}")
    story.findings.append(
        {
            "kind": "user_model_parameters",
            "detail": {
                "note": "which learned parameters moved during the run",
                "observations": user_model.get("observations"),
                "effective_count": user_model.get("effective_count"),
                "class_evidence": user_model.get("class_evidence"),
                "reply_delay_baseline": user_model.get("reply_delay_baseline"),
                "moved": moved,
                "frozen": frozen,
            },
        }
    )

    # 7. an attempt stuck in a non-terminal state.
    stuck = []
    for attempt in attempts:
        value = str(attempt.get("state") or "")
        if value not in {"proposed", "committed", "rendering", "ready_to_send", "sent"}:
            continue
        reference = _parse_iso(attempt.get("updated_at")) or _parse_iso(attempt.get("created_at"))
        if reference is not None and (now - reference) > SILENCE_HORIZON:
            stuck.append(
                {
                    "attempt_id": attempt.get("attempt_id"),
                    "state": value,
                    "age_hours": round((now - reference).total_seconds() / 3600.0, 1),
                    "intent": _short(attempt.get("intent"), 60),
                }
            )
    if stuck:
        story.findings.append(
            {
                "kind": "attempt_stuck",
                "detail": {
                    "note": "an attempt stayed non-terminal past the absent-reply horizon",
                    "rows": stuck,
                },
            }
        )

    # 8. anything that contradicts the relationship stage the run reached.
    contradictions: list[dict[str, Any]] = []
    if _stage_reached(story, LOVER):
        active_bans = [
            item
            for item in boundaries
            if item.get("allow_proactive") is False
            and item.get("revoked_at") is None
            and (_parse_iso(item.get("expires_at")) is None or _parse_iso(item.get("expires_at")) > now)
        ]
        if active_bans:
            contradictions.append(
                {
                    "note": "the run reached the lover stage while a no-contact boundary is still in force",
                    "boundaries": [
                        {"scope": item.get("scope"), "expires_at": item.get("expires_at")}
                        for item in active_bans
                    ],
                }
            )
        if not [item for item in memories if item.get("kind") == "relationship"]:
            contradictions.append(
                {"note": "the user declared affection but no relationship memory exists at all"}
            )
        elif not [
            item
            for item in memories
            if item.get("kind") == "relationship"
            and str(item.get("status") or "") != "archived"
            and item.get("superseded") is not True
        ]:
            contradictions.append(
                {
                    "note": "every relationship memory has been archived or superseded, which "
                    "contradicts a run that ended in the lover stage",
                    "rows": [
                        {"summary": item.get("summary"), "status": item.get("status")}
                        for item in memories
                        if item.get("kind") == "relationship"
                    ],
                }
            )
        if not any(
            marker in str(item.get("summary") or "") for item in memories for marker in ("喜欢你", "信任")
        ):
            contradictions.append(
                {"note": "no memory of the declaration or the relational statement survived the run"}
            )
        mood = (state.get("mood") or {}).get("valence")
        if isinstance(mood, (int, float)) and mood < -0.25:
            contradictions.append(
                {"note": "the mood at the end of a lover-stage run is strongly negative", "valence": mood}
            )
    if contradictions:
        story.findings.append({"kind": "stage_contradiction", "detail": {"rows": contradictions}})

    # 9. what the acting layer is handed at the end (a plain inventory).
    story.findings.append(
        {
            "kind": "final_prompt_inventory",
            "detail": {
                "note": "the sections of the last injected block",
                "memory_section": section_body(story.injected(), SECTION_MEMORY),
                "boundary_section": section_body(story.injected(), SECTION_BOUNDARY),
                "time_section": section_body(story.injected(), SECTION_TIME),
                "activated_pool_size": len(activated),
            },
        }
    )

    # 10. the four-slot memory section against what the user actually asked about.
    question_entries = [
        entry
        for entry in story.probe_log
        if any(token in str(entry.get("section") or "") for token in ("吗？", "吗?", "呢？", "?"))
    ]
    missed = [
        {"stage": entry["stage"], "marker": entry["marker"], "question": entry["question"], "section": entry["section"]}
        for entry in story.probe_log
        if not entry["in_section"]
    ]
    if story.probe_log:
        story.findings.append(
            {
                "kind": "prompt_memory_crowding",
                "detail": {
                    "note": "the memory section the Runtime hands the acting layer holds four items; "
                    "at the probe turns it is filled by memories of the user's own earlier questions, "
                    "because a question shares single characters with every other question. The "
                    "disclosure the user asked about is retrieved by the Engine and still loses the "
                    "budget",
                    "probes": len(story.probe_log),
                    "probes_where_the_asked_about_fact_was_not_shown": len(missed),
                    "probes_whose_section_held_a_question_memory": len(question_entries),
                    "missed_examples": missed[:4],
                },
            }
        )
    if story.probe_lag:
        story.findings.append(
            {
                "kind": "injected_block_lags_the_user_message",
                "detail": {
                    "note": "the block the plugin injected for a turn did not yet carry the sentence "
                    "the user was asking about, because the adapter reports observed messages on its "
                    "own queue; the recall measurement therefore reads a fresh render for the same "
                    "simulated moment",
                    "turns": len(story.probe_lag),
                    "markers": story.probe_lag,
                },
            }
        )


def render_inspection(story: Story, ctx: Context) -> str:
    """Render ``inspection.md``: the dumps, the examination and the run summary."""
    lines: list[str] = ["# 关系进展仿真：后台检查报告", ""]
    lines.append("本文件由 `scripts/relationship_progression_simulation.py` 生成。")
    lines.append("")
    lines.append("## 1. 运行概要")
    lines.append("")
    lines.append(f"- 模拟时钟起点：`{story.clock.origin.isoformat()}`")
    lines.append(f"- 模拟时钟终点：`{story.clock.now().isoformat()}`")
    span = (story.clock.now() - story.clock.origin).total_seconds() / 86400.0
    lines.append(f"- 模拟时长：{span:.1f} 天（约 {span / 30.0:.1f} 个月）")
    lines.append(
        f"- 用户消息 {len(story.recorder.user_turns())} 条；机器人消息 "
        f"{len(story.recorder.bot_turns())} 条；其中主动消息 "
        f"{len(story.recorder.bot_turns(kind='proactive'))} 条"
    )
    lines.append(f"- 宿主主 LLM 调用：{len(story.llm.calls)} 次；故障注入：{sorted(story.faults.names) or '无'}")
    lines.append("")
    lines.append("## 2. 阶段与检查")
    lines.append("")
    lines.append("| 阶段 | 脚本轮次 | 用户消息 | 机器人消息 | 主动消息 | 记忆探测 |")
    lines.append("|---|---|---|---|---|---|")
    for identifier in STAGE_ORDER:
        record = story.stages.get(identifier)
        scripted = len(STAGE_TURNS[identifier])
        if record is None or record.started_at is None:
            lines.append(f"| {identifier} | {scripted} | - | - | - | (未运行) |")
            continue
        recall = ", ".join(f"{k}={'OK' if v else 'MISS'}" for k, v in record.recall.items()) or "-"
        lines.append(
            f"| {identifier} | {scripted} | {record.user_turns} | {record.bot_turns} | "
            f"{record.proactives} | {recall} |"
        )
    lines.append("")
    for row in V.stage_table():
        lines.append(
            f"- `{row['identifier']}` {row['phase']}：{row['passed']}/{row['checks']} 检查通过"
        )
    lines.append("")
    lines.append("## 3. 后台转储清单")
    lines.append("")
    lines.append("所有转储都通过 Runtime 自己的 HTTP 接口读取（`/state`、`/events`、`/memories`、")
    lines.append("`/unfinished`、`/boundaries`、`/candidates`、`/attempts`、`/outbox`、")
    lines.append("`/user-model`、`/observations`）。唯一没有对应接口的是原始事件日志的**全文**：")
    lines.append("`/events` 一页最多 500 行，所以全文取自 Runtime 自己写的 JSONL 镜像")
    lines.append("（`storage.raw_log_path`），并已与 `/health.raw_events` 对账。")
    lines.append("")
    lines.append("| 文件 | 接口 | HTTP | 行数 | 为空的原因 |")
    lines.append("|---|---|---|---|---|")
    for key, path, _field, _reason in DUMP_SPECS:
        document = story.dumps.get(key) or {}
        lines.append(
            f"| `backend/{key}.json` | `{path}` | {document.get('http_status')} | "
            f"{document.get('count')} | {document.get('empty_reason') or '-'} |"
        )
    lines.append(
        f"| `backend/raw_events.jsonl` | JSONL 镜像（全文） | - | {_mirror_lines(ctx.base_dir)} | - |"
    )
    lines.append("")
    lines.append("## 4. 观察：后台里有什么值得人类注意的东西")
    lines.append("")
    if not story.findings:
        lines.append("没有发现异常。")
    for finding in story.findings:
        lines.append(f"### {finding['kind']}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(finding["detail"], ensure_ascii=False, indent=2, default=str))
        lines.append("```")
        lines.append("")
    lines.append("## 5. 记忆快照")
    lines.append("")
    memories = (story.dumps.get("memories", {}).get("payload") or {}).get("memories") or story.memories()
    lines.append("| 记忆 | 类型 | 状态 | 重要度 | 来源事件数 |")
    lines.append("|---|---|---|---|---|")
    for memory in memories[:40]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(memory.get("summary") or "").replace("|", "/")[:60],
                    str(memory.get("kind")),
                    str(memory.get("status")),
                    str(memory.get("importance")),
                    str(len(memory.get("source_event_ids") or [])),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## 6. 边界与尝试")
    lines.append("")
    boundaries = (story.dumps.get("boundaries", {}).get("payload") or {}).get("boundaries") or []
    lines.append("| 边界 | 类型 | 范围 | 对象 | 允许主动 | 生效 | 过期 | 撤销 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for boundary in boundaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(boundary.get("boundary_id")),
                    str(boundary.get("type")),
                    str(boundary.get("scope")),
                    str(boundary.get("subject") or "-")[:30],
                    str(boundary.get("allow_proactive")),
                    str(boundary.get("starts_at")),
                    str(boundary.get("expires_at")),
                    str(boundary.get("revoked_at") or "-"),
                ]
            )
            + " |"
        )
    lines.append("")
    attempts = (story.dumps.get("attempts", {}).get("payload") or {}).get("attempts") or []
    lines.append("| 尝试 | 状态 | 意图 | 提交于 | 更新于 | 失败原因 |")
    lines.append("|---|---|---|---|---|---|")
    for attempt in attempts[:40]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(attempt.get("attempt_id")),
                    str(attempt.get("state")),
                    str(attempt.get("intent") or "")[:40].replace("|", "/"),
                    str(attempt.get("committed_at") or "-"),
                    str(attempt.get("updated_at") or "-"),
                    str(attempt.get("failure_reason") or "-"),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## 7. 用户模型与观察")
    lines.append("")
    numeric = (story.dumps.get("user-model", {}).get("payload") or {}).get("numeric") or {}
    lines.append(f"- 观察条数：{numeric.get('observations')}（有效样本 {numeric.get('effective_count')}）")
    lines.append(f"- 各行为类别证据：{json.dumps(numeric.get('class_evidence') or {}, ensure_ascii=False)}")
    lines.append(f"- 回复延迟基线：{json.dumps(numeric.get('reply_delay_baseline') or {}, ensure_ascii=False)}")
    semantic = (story.dumps.get("user-model", {}).get("payload") or {}).get("semantic") or {}
    lines.append(f"- 语义视图：{semantic.get('summary')}")
    lines.append("")
    lines.append("## 8. 下一步（what a human should look at next）")
    lines.append("")
    for finding in story.findings:
        lines.append(f"- `{finding['kind']}`：见第 4 节")
    if not story.findings:
        lines.append("- 无")
    lines.append("")
    lines.append("完整时间线见同目录下的 `transcript.md`，检查明细见 `report.json`，")
    lines.append("运行期日志见 `diagnostics.log`。")
    lines.append("")
    return "\n".join(lines)


def _mirror_lines(base_dir: Path) -> int:
    """Return the number of non-empty lines in the raw event mirror."""
    path = base_dir / "scenario" / "raw_events.jsonl"
    if not path.exists():
        return 0
    with contextlib.suppress(OSError):
        return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])
    return 0


# ------------------------------------------------------------------ phase 8: teardown


def phase_teardown(story: Story, ctx: Context) -> None:
    """teardown: nothing is left running, and nothing was written outside --base-dir."""
    V.phase("teardown", "PHASE 8 teardown")
    host_thread_alive = ctx.host_running(story)
    server = story.server
    server_thread_alive = bool(server and server.thread and server.thread.is_alive())
    tasks_before = len(getattr(story.host.plugin, "_tasks", []) if story.host else [])
    report = story.stop()
    time.sleep(0.4)
    V.check(
        "the adapter's event loop and every task it owned were stopped",
        report.get("loop_alive") is False and int(report.get("tasks_pending") or 0) == 0,
        _short({"host_alive_before_stop": host_thread_alive, "tasks_before_stop": tasks_before, **report}),
    )
    lingering_servers = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("rp-runtime") and thread.is_alive()
    ]
    V.check(
        "the Runtime server thread is joined and nothing is left listening",
        not lingering_servers and story.server is None and ctx.runtime_stopped(story),
        _short({"server_alive_before_stop": server_thread_alive, "still_alive": lingering_servers}),
    )
    lingering = [
        thread.name
        for thread in threading.enumerate()
        if thread is not threading.current_thread()
        and (thread.name.startswith("rp-") or "companion" in thread.name.lower())
    ]
    V.check(
        "no scheduler, adapter or server thread is still running",
        not lingering,
        _short(lingering) or "no harness threads remain",
    )
    V.check(
        "no bytecode was written next to the sources this run imports",
        bool(sys.dont_write_bytecode) and not ctx.new_pyc_files,
        _short(ctx.new_pyc_files[:5]) or "sys.dont_write_bytecode=True and no new .pyc",
    )
    V.check(
        "no file appeared among those sources, and none of them changed",
        not ctx.repo_files_created and not ctx.repo_sources_changed,
        _short({"created": ctx.repo_files_created[:5], "changed": ctx.repo_sources_changed[:5]})
        or "the imported source trees are untouched",
    )
    produced = [
        str(path.relative_to(ctx.base_dir)).replace("\\", "/")
        for path in (
            ctx.base_dir / "scenario" / "runtime.sqlite3",
            ctx.base_dir / "scenario" / "raw_events.jsonl",
            ctx.base_dir / "transcript.md",
            ctx.base_dir / "inspection.md",
            ctx.base_dir / "backend" / "events.json",
        )
        if path.exists()
    ]
    V.check(
        "the run's own artifacts are inside --base-dir",
        len(produced) >= 3
        and (
            "inspection.md" not in produced
            or "backend/events.json" in produced
        ),
        _short({"present": produced, "base_dir": str(ctx.base_dir)}),
    )
    if story.step_timings:
        slowest = max(story.step_timings)
        total = sum(item[0] for item in story.step_timings)
        V.note(
            f"{len(story.step_timings)} simulated-clock step(s) in {total:.1f}s wall clock "
            f"(slowest {slowest[0]:.2f}s, drain {slowest[1]:.2f}s, outcome {slowest[2]}); "
            f"{len(story.llm.calls)} host main-LLM call(s)"
        )


# ------------------------------------------------------------------ entry point


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Relationship-progression simulation for the companion Runtime sidecar.",
    )
    parser.add_argument("--base-dir", required=False, default="", help="artifact directory")
    parser.add_argument("--only", default="", help="comma-separated phase ids to run (default: all)")
    parser.add_argument("--quiet", action="store_true", help="only print checks and the summary")
    parser.add_argument("--list-phases", action="store_true", help="print the phase ids and exit")
    parser.add_argument(
        "--fault",
        action="append",
        default=[],
        choices=[
            "intimacy",
            "trivia",
            "no_memory",
            "boundary_ignore",
            "reask",
            "guilt",
            "leak",
            "duplicate",
        ],
        help="inject a harness-side defect to prove that a check bites (never a repo change)",
    )
    return parser.parse_args(argv)


def _write_artifacts(
    base_dir: Path,
    *,
    report: Mapping[str, Any],
    extra: Sequence[str],
    narrative: Sequence[str],
) -> None:
    """Write ``report.json`` and ``diagnostics.log`` under ``base_dir``."""
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    lines = list(extra) + ["", "--- run notes and operator diagnostics ---"] + list(narrative)
    lines += ["", "--- runtime and adapter log ---"] + list(LOG_HANDLER.records)
    (base_dir / "diagnostics.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the simulation and return the process exit code."""
    args = parse_args(argv)
    if args.list_phases:
        for identifier, title in PHASES:
            scripted = len(STAGE_TURNS.get(STAGE_PHASES.get(identifier, ""), ()))
            extra = f"  ({scripted} scripted turns)" if scripted else ""
            print(f"{identifier:12s} {title}{extra}")
        print("")
        print("stages:")
        for identifier in STAGE_ORDER:
            print(f"{identifier:12s} {len(STAGE_TURNS[identifier])} scripted turns")
        return 0
    if IMPORT_ERROR:
        print(f"cannot start: required dependencies are missing ({IMPORT_ERROR})")
        return 2
    if not args.base_dir:
        print("cannot start: --base-dir is required")
        return 2
    if not (PLUGIN_ROOT / "main.py").is_file():
        print(f"cannot start: the shipped plugin clone is missing ({PLUGIN_ROOT / 'main.py'})")
        return 2

    global V
    V = Verifier(quiet=bool(args.quiet))
    V.load_phases(PHASES)
    configure_logging()

    base_dir = Path(args.base_dir).expanduser().resolve()
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    scrubbed = scrub_environment()
    clock = SimClock(datetime.now(timezone.utc).replace(microsecond=0))
    bindings = install_process_clock(clock)
    random.seed(RUNTIME_SEED)

    selected = [item.strip() for item in args.only.split(",") if item.strip()] or PHASE_IDS
    unknown = [item for item in selected if item not in PHASE_IDS]
    if unknown:
        print(f"cannot start: unknown phase id(s) {unknown}; use --list-phases")
        return 2

    ctx = Context(
        base_dir=base_dir,
        quiet=bool(args.quiet),
        scrubbed_env=scrubbed,
        clock_bindings=bindings,
        repo_snapshot=_tree_files(REPO_ROOT, skip=base_dir),
    )
    story = Story(base_dir=base_dir, clock=clock, faults=Faults(args.fault))

    started = False
    fatal = ""
    try:
        story.start()
        started = True
    except Exception as exc:  # noqa: BLE001 - reported as a startup failure
        fatal = f"{type(exc).__name__}: {exc}"
        V.current = V.sections[0]
        V.check("the simulation can start at all", False, fatal)

    if started:
        runners: dict[str, Callable[[Story, Context], None]] = {
            "setup": phase_setup,
            "stranger": phase_stranger,
            "acquaintance": phase_acquaintance,
            "friend": phase_friend,
            "lover": phase_lover,
            "audit": phase_audit,
            "inspection": phase_inspection,
        }
        # The user-model baseline has to be read before the story teaches the model
        # anything, so the "which parameters moved" finding has something to compare.
        with contextlib.suppress(Exception):
            story.early_user_model = (story.api("/user-model").json or {}).get("numeric") or {}
        try:
            for identifier, _title in PHASES:
                if identifier == "teardown" or identifier not in selected:
                    continue
                # A phase that blows up is recorded as a failed check and the story
                # continues: the user's life does not stop because one assertion
                # crashed, and the remaining phases still report.
                try:
                    runners[identifier](story, ctx)
                except Exception as exc:  # noqa: BLE001 - reported, never re-raised
                    import traceback

                    V.check(
                        f"the story runs to the end of {identifier} without the harness crashing",
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                    LOG_HANDLER.records.append(traceback.format_exc())
        finally:
            with contextlib.suppress(Exception):
                (base_dir / "transcript.md").write_text(story.recorder.markdown(), encoding="utf-8")
            try:
                phase_teardown(story, ctx)
            except Exception as exc:  # noqa: BLE001 - teardown must still stop everything
                with contextlib.suppress(Exception):
                    story.stop()
                V.check("teardown completes", False, f"{type(exc).__name__}: {exc}")
    else:
        story.stop()
        V.current = V.sections[-1]
        V.check("the run could not start, so nothing was verified", False, fatal)

    code = V.summary()
    report = V.as_report() | {
        "base_dir": str(base_dir),
        "phases_selected": selected,
        "faults": sorted(set(args.fault)),
        "clock_bindings": bindings,
        "scrubbed_env": scrubbed,
        "fatal": fatal,
        "clock_origin": clock.origin.isoformat(),
        "clock_end": clock.now().isoformat(),
        "simulated_days": round((clock.now() - clock.origin).total_seconds() / 86400.0, 2),
        "stages": {
            identifier: {
                "title": record.title,
                "scripted_turns": len(STAGE_TURNS[identifier]),
                "user_turns": record.user_turns,
                "bot_turns": record.bot_turns,
                "proactives": record.proactives,
                "recall": record.recall,
                "started_at": record.started_at.isoformat() if record.started_at else None,
                "ended_at": record.ended_at.isoformat() if record.ended_at else None,
            }
            for identifier, record in story.stages.items()
        },
        "findings": story.findings,
        "stage_table": V.stage_table(),
    }
    _write_artifacts(
        base_dir,
        report=report,
        extra=[
            f"simulated clock origin: {clock.origin.isoformat()}",
            f"simulated days: {report['simulated_days']}",
        ],
        narrative=[f"{NOTE_MARK} {note}" for section in V.sections for note in section.notes]
        + [line for section in V.sections for line in section.diagnostics],
    )
    print(
        f"\nartifacts: {base_dir / 'report.json'}, {base_dir / 'diagnostics.log'}, "
        f"{base_dir / 'transcript.md'}, {base_dir / 'inspection.md'}, {base_dir / 'backend'}"
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
