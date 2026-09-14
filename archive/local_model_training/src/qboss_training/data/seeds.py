"""种子场景采样：给数据生成器提供"生成什么"的骨架。

这是质量的第一道闸门。如果种子场景本身覆盖面窄，无论生成多少条
数据都会塌缩到少数几种句式上。因此这里刻意按**事件类型 × 关系信号 ×
心境区域**做笛卡尔式覆盖，并把采样结果固定下来（seed 可复现）。

每个场景只提供"骨架"（谁对谁做了什么、背景状态区间），
具体文本由教师模型（DeepSeek）填充，因此不会限制表达多样性。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..contracts import EMOTION_EXPLAIN, EVENT_EVAL, TASK_NAMES

# --------------------------------------------------------------------------
# 事件类型（决定"事件是什么性质"的语义空间）
# --------------------------------------------------------------------------

EVENT_KINDS: dict[str, dict[str, str]] = {
    "greeting_return": {
        "label": "久别重逢/重新联系",
        "hint": "用户隔了较长时间重新出现或主动打招呼",
    },
    "user_busy": {
        "label": "用户忙碌/暂时抽身",
        "hint": "用户表示今晚没时间、要忙、想自己待着",
    },
    "self_disclosure": {
        "label": "用户自我暴露/示弱",
        "hint": "用户讲自己的难处、失败、害怕的事",
    },
    "praise_affection": {
        "label": "用户表达喜爱/肯定",
        "hint": "用户夸角色、说想它、表达依赖或感谢",
    },
    "criticism": {
        "label": "用户批评/失望",
        "hint": "用户说角色做得不好、失望、比不上别人",
    },
    "boundary_setting": {
        "label": "用户设边界",
        "hint": "用户明确说不要主动找、不要问某些事、要减少频率",
    },
    "broken_promise_user": {
        "label": "用户失约",
        "hint": "用户答应过的事没有做到，或忘记约定",
    },
    "broken_promise_char": {
        "label": "角色失约",
        "hint": "角色自己答应过的事没做到，用户指出",
    },
    "ambiguous_silence": {
        "label": "模糊沉默/已读不回",
        "hint": "用户长时间不回或回复极短，原因不明",
    },
    "good_news_user": {
        "label": "用户报喜",
        "hint": "用户分享升职、通过考试、好消息",
    },
    "bad_news_user": {
        "label": "用户遭遇坏事",
        "hint": "用户讲被辞退、生病、失去重要的人",
    },
    "third_party": {
        "label": "出现第三方",
        "hint": "用户提到别人（新朋友、同事、别的人）获得角色原本的位置",
    },
    "misunderstanding": {
        "label": "误解/歧义",
        "hint": "用户误读了角色的话，或角色误读了用户",
    },
    "apology": {
        "label": "道歉",
        "hint": "一方为之前的事道歉",
    },
    "vulnerable_request": {
        "label": "脆弱求助",
        "hint": "用户在深夜/情绪低谷求助",
    },
    "routine_share": {
        "label": "日常闲聊分享",
        "hint": "用户分享今天吃什么、天气、路上看到的小事",
    },
    "time_gap": {
        "label": "长时间无互动",
        "hint": "数天没有交流，之后重新说话",
    },
    "topic_change": {
        "label": "话题突然切换/回避",
        "hint": "用户突然换话题，回避刚提到的事",
    },
    "gift_or_help": {
        "label": "用户为角色做事",
        "hint": "用户帮角色解决问题、送东西、记得细节",
    },
    "testing_probe": {
        "label": "试探/确认关系",
        "hint": "用户问角色是否在乎、会不会离开",
    },
    "system_event": {
        "label": "非对话环境事件",
        "hint": "时间流逝、节日、天气骤变、日程到点等 environment 事件",
    },
    "ambiguous_mixed": {
        "label": "正负混合",
        "hint": "同一句话里既有肯定也有距离，方向应判为混合",
    },
}

# --------------------------------------------------------------------------
# 背景心境区域
# --------------------------------------------------------------------------

MOOD_BANDS: dict[str, dict[str, float]] = {
    "low_flat": {"valence": -0.45, "arousal": 0.18},
    "low_tense": {"valence": -0.30, "arousal": 0.62},
    "neutral_calm": {"valence": 0.02, "arousal": 0.28},
    "neutral_alert": {"valence": -0.05, "arousal": 0.70},
    "high_warm": {"valence": 0.48, "arousal": 0.44},
    "high_excited": {"valence": 0.55, "arousal": 0.80},
}

# --------------------------------------------------------------------------
# 角色价值观画像（同一事件对不同角色影响不同 —— 架构文档 §10）
# --------------------------------------------------------------------------

VALUE_PROFILES: dict[str, dict[str, float]] = {
    "autonomy_high": {
        "autonomy": 0.85,
        "boundary": 0.80,
        "relatedness": 0.35,
        "stability": 0.55,
        "honesty": 0.60,
    },
    "relatedness_high": {
        "autonomy": 0.35,
        "boundary": 0.40,
        "relatedness": 0.88,
        "stability": 0.70,
        "honesty": 0.60,
    },
    "stability_high": {
        "autonomy": 0.45,
        "boundary": 0.50,
        "relatedness": 0.65,
        "stability": 0.90,
        "honesty": 0.62,
    },
    "honesty_high": {
        "autonomy": 0.55,
        "boundary": 0.60,
        "relatedness": 0.60,
        "stability": 0.60,
        "honesty": 0.92,
    },
    "balanced": {
        "autonomy": 0.55,
        "boundary": 0.55,
        "relatedness": 0.60,
        "stability": 0.60,
        "honesty": 0.60,
    },
}

KNOWN_FACT_POOL: tuple[str, ...] = (
    "用户上周连续三天深夜找角色聊天",
    "用户提过自己最近在准备一场重要考试",
    "用户曾说过不喜欢被连续追问",
    "用户说过希望角色别用客套话",
    "用户之前提到和同事关系变差",
    "用户说过自己睡眠一直不好",
    "用户上次答应过会告诉角色结果",
    "用户提过讨厌别人替他做决定",
    "用户说过很喜欢角色记得小细节",
    "用户曾经因为角色太主动而不高兴",
    "用户提过明天有个面试",
    "用户说自己不太会表达情绪",
)

# --------------------------------------------------------------------------
# 情绪解释专用：心理状态骨架
# --------------------------------------------------------------------------

TARGETS: tuple[str, ...] = ("user", "self", "situation", "third_party")

CONFLICT_SHAPES: dict[str, str] = {
    "approach_vs_restraint": "想靠近/想确认，但又不想显得依赖或给对方压力",
    "express_vs_protect": "想表达真实感受，又怕伤害对方或让对方负担",
    "honesty_vs_harmony": "想说真话，又不想破坏当下的和气",
    "self_blame_vs_fairness": "倾向于自责，但理性上知道责任不完全在自己",
    "no_conflict_calm": "当前没有明显冲突结构，状态比较平顺",
    "mixed_unknown": "存在张力但角色自己也说不清具体是哪一种",
}

INTENSITY_BANDS: dict[str, float] = {
    "whisper": 0.14,
    "mild": 0.32,
    "moderate": 0.54,
    "strong": 0.78,
    "overwhelming": 0.93,
}


@dataclass(frozen=True)
class SeedScenario:
    """一条种子场景骨架（与任务无关的公共部分 + 任务专属载荷）。"""

    task: str
    scenario_id: str
    event_kind: str
    event_label: str
    event_hint: str
    tag: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "scenario_id": self.scenario_id,
            "event_kind": self.event_kind,
            "event_label": self.event_label,
            "event_hint": self.event_hint,
            "tag": dict(self.tag),
            "payload": dict(self.payload),
        }


def _mood_with_jitter(band: str, rng: random.Random) -> dict[str, float]:
    base = MOOD_BANDS[band]
    valence = _clamp(base["valence"] + rng.uniform(-0.08, 0.08), -1.0, 1.0)
    arousal = _clamp(base["arousal"] + rng.uniform(-0.08, 0.08), 0.0, 1.0)
    return {"valence": round(valence, 2), "arousal": round(arousal, 2)}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _profile_with_jitter(name: str, rng: random.Random) -> dict[str, float]:
    base = VALUE_PROFILES[name]
    return {
        key: round(_clamp(value + rng.uniform(-0.06, 0.06), 0.0, 1.0), 2)
        for key, value in base.items()
    }


def sample_event_eval_scenarios(
    count: int, rng: random.Random
) -> list[SeedScenario]:
    """采样事件评价场景。

    保证事件类型均衡：先按 EVENT_KINDS 轮转，再随机填满余量，
    避免纯随机采样把长尾场景饿死。
    """
    kinds = list(EVENT_KINDS)
    rng.shuffle(kinds)
    chosen: list[str] = []
    while len(chosen) < count:
        remaining = count - len(chosen)
        chosen.extend(kinds[:remaining] if remaining < len(kinds) else kinds)

    scenarios: list[SeedScenario] = []
    for index, kind in enumerate(chosen[:count]):
        meta = EVENT_KINDS[kind]
        band = rng.choice(list(MOOD_BANDS))
        profile = rng.choice(list(VALUE_PROFILES))
        facts = rng.sample(KNOWN_FACT_POOL, k=rng.randint(0, 2))
        speaker = "environment" if kind == "system_event" else "user"
        context_len = rng.choice([0, 1, 2, 3, 4])
        scenarios.append(
            SeedScenario(
                task=EVENT_EVAL,
                scenario_id=f"ee_{index:05d}",
                event_kind=kind,
                event_label=meta["label"],
                event_hint=meta["hint"],
                tag={
                    "mood_band": band,
                    "value_profile": profile,
                    "speaker": speaker,
                },
                payload={
                    "mood_band": band,
                    "value_profile": profile,
                    "background_mood": _mood_with_jitter(band, rng),
                    "character_values": _profile_with_jitter(profile, rng),
                    "known_facts": facts,
                    "context_turns": context_len,
                    "speaker": speaker,
                },
            )
        )
    return scenarios


def sample_emotion_explain_scenarios(
    count: int, rng: random.Random
) -> list[SeedScenario]:
    """采样情绪解释场景。"""
    kinds = list(EVENT_KINDS)
    rng.shuffle(kinds)
    chosen: list[str] = []
    while len(chosen) < count:
        remaining = count - len(chosen)
        chosen.extend(kinds[:remaining] if remaining < len(kinds) else kinds)

    scenarios: list[SeedScenario] = []
    for index, kind in enumerate(chosen[:count]):
        meta = EVENT_KINDS[kind]
        band = rng.choice(list(MOOD_BANDS))
        conflict = rng.choice(list(CONFLICT_SHAPES))
        intensity_band = rng.choice(list(INTENSITY_BANDS))
        direction = rng.choice(["+", "-", "0", "+-"])
        target = rng.choice(TARGETS)
        approach = round(rng.uniform(0.05, 0.95), 2)
        restraint = round(rng.uniform(0.05, 0.95), 2)
        # 同一 target 上叠 1~2 条活跃情绪
        extra_count = rng.choice([0, 1])
        scenarios.append(
            SeedScenario(
                task=EMOTION_EXPLAIN,
                scenario_id=f"ex_{index:05d}",
                event_kind=kind,
                event_label=meta["label"],
                event_hint=meta["hint"],
                tag={
                    "mood_band": band,
                    "conflict": conflict,
                    "intensity_band": intensity_band,
                    "direction": direction,
                    "target": target,
                },
                payload={
                    "mood_band": band,
                    "background_mood": _mood_with_jitter(band, rng),
                    "primary": {
                        "target": target,
                        "direction": direction,
                        "intensity": INTENSITY_BANDS[intensity_band],
                        "action_tendency_hint": CONFLICT_SHAPES[conflict],
                    },
                    "extra_emotions": extra_count,
                    "approach_drive": approach,
                    "restraint": restraint,
                    "conflict": conflict,
                    "conflict_present": conflict != "no_conflict_calm",
                },
            )
        )
    return scenarios


SAMPLE_FUNCS = {
    EVENT_EVAL: sample_event_eval_scenarios,
    EMOTION_EXPLAIN: sample_emotion_explain_scenarios,
}


def sample_scenarios(
    task: str,
    count: int,
    *,
    seed: int = 20240607,
    scenarios_filter: tuple[str, ...] = (),
) -> list[SeedScenario]:
    """按任务采样 ``count`` 条种子场景，可复现。"""
    if task == "mixed":
        rng = random.Random(seed)
        half = count // 2
        left = sample_scenarios(EVENT_EVAL, count - half, seed=seed)
        right = sample_scenarios(EMOTION_EXPLAIN, half, seed=seed + 1)
        merged = left + right
        rng.shuffle(merged)
        return merged

    if task not in SAMPLE_FUNCS:
        raise KeyError(f"未知任务 {task!r}，可用：{', '.join(TASK_NAMES)} 或 mixed")

    rng = random.Random(seed)
    scenarios = SAMPLE_FUNCS[task](count, rng)
    if scenarios_filter:
        wanted = set(scenarios_filter)
        filtered = [item for item in scenarios if item.event_kind in wanted]
        # 过滤后不足时用原始列表补齐，保证数量契约
        if filtered:
            scenarios = filtered
    return scenarios


def iter_event_kinds() -> Iterator[str]:
    yield from EVENT_KINDS
