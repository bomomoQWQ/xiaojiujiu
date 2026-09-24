"""合成测试夹具：不需要网络、不需要大模型、不需要 GPU。

用途
----
离线端到端测试（validate → split → sft → eval → bench）需要一份"看起来像真数据"
的样本。这里用**参数化模板**合成记录，保证：

  * 每条都通过 JSON Schema；
  * 同时覆盖正例与**故意违规的负例**（用于验证不变量真的能抓到问题）；
  * 样本之间不重复（否则会被去重器吃掉，测试就测不到东西）；
  * 生成是确定性的（固定 seed）。

注意：本夹具只用于测试与自检，**不能**当作训练数据 —— 它的句式极其有限，
拿它训练只会得到过拟合的模型。真实数据必须来自 ``qboss gen``。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------
# 事件评价夹具
# --------------------------------------------------------------------------

#: (方向, 关系信号, 责任) 组合，必须是合法组合
_EVENT_COMBOS: tuple[tuple[str, str, str], ...] = (
    ("-", "slight_distance", "unclear"),
    ("-", "strong_distance", "other"),
    ("-", "neutral", "self"),
    ("+", "slight_approach", "other"),
    ("+", "strong_approach", "user_or_other_placeholder"),
    ("+", "neutral", "shared"),
    ("0", "neutral", "situation"),
    ("+-", "slight_distance", "shared"),
    ("+-", "slight_approach", "unclear"),
)

_EVENT_TEXTS: tuple[str, ...] = (
    "今晚想自己待着，不聊天了。",
    "我今天面试通过了，第一时间想告诉你。",
    "你上次答应我的事，好像忘了。",
    "刚才那个问题你能再讲一遍吗，我有点没听懂。",
    "我不想你总是主动找我，我需要一点空间。",
    "今天路上看到一只很像你以前说的那只猫。",
    "算了，当我没说。",
    "谢谢你记得我提过的事，我有点意外。",
    "我最近很累，不太想说话。",
    "你是不是根本没在听我说话？",
    "明天开始我要出差一周，可能联系不上。",
    "我刚才态度不好，对不起。",
)

#: 话题 × 行为 的组合空间，用于生成**互不相同**的事件文本。
#: 夹具必须保证样本之间语义不同，否则会被去重器吃掉，
#: 导致"用夹具测去重/切分"的测试实际上什么都没测到。
_TOPICS: tuple[str, ...] = (
    "工作上的项目",
    "明天的面试",
    "家里的猫",
    "昨晚的睡眠",
    "新买的耳机",
    "周末的行程",
    "和同事的那件事",
    "体检报告",
    "朋友聚会",
    "租的房子",
    "刚看完的电影",
    "一直想学的那门课",
)

_ACTS: tuple[str, ...] = (
    "我今晚想自己待着，先不聊{topic}了。",
    "{topic}有结果了，我第一时间想告诉你。",
    "关于{topic}，你上次答应我的事好像忘了。",
    "{topic}我还是没听明白，能再讲一遍吗？",
    "我不想总是聊{topic}，我需要一点空间。",
    "今天路过时想起{topic}，有点走神。",
    "{topic}的事算了，当我没说。",
    "谢谢你记得{topic}，我有点意外。",
    "最近想到{topic}就很累，不太想说话。",
    "你是不是根本没在听我说{topic}？",
    "因为{topic}，我下周可能联系不上。",
    "关于{topic}我剛才态度不好，对不起。",
)


def _make_event_text(index: int) -> str:
    """按"话题 × 行为"生成互不相同的事件文本。"""
    topic = _TOPICS[(index // len(_ACTS)) % len(_TOPICS)]
    act = _ACTS[index % len(_ACTS)]
    return act.format(topic=topic)


def _grounded_evidence(text: str, max_chars: int = 14) -> str:
    """从事件文本里截一段作为 evidence。

    这样夹具的 evidence 一定"有依据"，不会因为文本与证据列表错位
    而触发 EV06_EVIDENCE_UNGROUNDED（那是给真实模型输出用的检查）。
    """
    stripped = text.strip().rstrip("。！？!?")
    return stripped[:max_chars]


def make_event_eval_record(
    index: int, rng: random.Random, *, make_invalid: bool = False
) -> dict[str, Any]:
    """合成一条事件评价记录。"""
    combo = _EVENT_COMBOS[index % len(_EVENT_COMBOS)]
    direction, signal, responsibility = combo
    if responsibility == "user_or_other_placeholder":
        responsibility = "other"

    text = _make_event_text(index)
    evidence = _grounded_evidence(text)

    impact = round(rng.uniform(0.15, 0.85), 2)
    if direction == "0":
        impact = round(rng.uniform(0.02, 0.2), 2)
    activation = round(rng.uniform(0.1, 0.9), 2)
    uncertainty = round(rng.uniform(0.2, 0.9), 2)
    if direction == "+-":
        uncertainty = round(rng.uniform(0.55, 0.95), 2)
    confidence = round(rng.uniform(0.5, 0.95), 2)

    valence = round(rng.uniform(-0.6, 0.6), 2)
    arousal = round(rng.uniform(0.1, 0.9), 2)

    output: dict[str, Any] = {
        "direction": direction,
        "impact": impact,
        "activation": activation,
        "uncertainty": uncertainty,
        "relation_signal": signal,
        "responsibility": responsibility,
        "confidence": confidence,
        "evidence": evidence,
    }

    if make_invalid:
        # 必须**确定性地**产出违规样本，否则"用夹具验证校验器"的测试会假绿。
        # `direction=0` 与任何非 neutral 的 relation_signal 都是矛盾组合
        # （见 invariants._DIRECTION_SIGNAL_MAP），因此改方向最可靠 ——
        # 改 relation_signal 在 direction="+-" 时是无效的，因为 +- 允许所有取值。
        output["direction"] = "0"
        if output.get("relation_signal") == "neutral":
            output["relation_signal"] = "strong_distance"
        output["impact"] = 0.9

    return {
        "id": f"ee_fixture_{index:04d}",
        "task": "event_eval",
        "input": {
            "current_event": {"speaker": "user", "text": text},
            "context_turns": []
            if index % 3 == 0
            else [
                {"speaker": "char", "text": "嗯，我在听。"},
                {"speaker": "user", "text": "那我继续说了。"},
            ],
            "background_mood": {"valence": valence, "arousal": arousal},
            "character_values": {
                "autonomy": round(rng.uniform(0.2, 0.9), 2),
                "boundary": round(rng.uniform(0.2, 0.9), 2),
                "relatedness": round(rng.uniform(0.2, 0.9), 2),
                "stability": round(rng.uniform(0.2, 0.9), 2),
                "honesty": round(rng.uniform(0.2, 0.9), 2),
            },
            "known_facts": ["用户上周连续三天深夜找角色聊天"]
            if index % 4 == 0
            else [],
        },
        "output": output,
        "meta": {
            "scenario_id": f"fixture_ee_{index:04d}",
            "event_kind": [
                "user_busy",
                "good_news_user",
                "broken_promise_user",
                "misunderstanding",
                "boundary_setting",
                "routine_share",
                "topic_change",
                "gift_or_help",
                "vulnerable_request",
                "criticism",
                "time_gap",
                "apology",
            ][index % 12],
            "source": "fixture",
            "generator_version": "fixture-1.0.0",
        },
    }


# --------------------------------------------------------------------------
# 情绪解释夹具
# --------------------------------------------------------------------------

#: (角色开场白模板, 用户回应模板, 话题占位) —— 组合出互不相同的对话片段
_EX_DIALOGUES: tuple[tuple[str, str], ...] = (
    ("关于{topic}，你今晚还会回来吗？", "不知道，可能没时间。"),
    ("{topic}的事，我今天有点撑不住了。", "我在，慢慢说。"),
    ("你是不是觉得我在{topic}上很烦？", "没有，我只是不知道该怎么接。"),
    ("{topic}有结果了，我第一时间想告诉你。", "真的？那太好了。"),
    ("{topic}我不想再提了。", "好，那我们说别的。"),
    ("以后在{topic}上你还会陪我吗？", "会，但我不能保证每次都及时。"),
    ("谢谢你一直记得{topic}。", "嗯，我记着。"),
    ("因为{topic}，我明天要出差一周。", "那这周我就不打扰你了。"),
    ("{topic}我搞砸了，别问了。", "好，我不问。"),
    ("关于{topic}，你是不是早就不耐烦了？", "不是的，我只是在想怎么回答。"),
    ("{topic}让我最近睡不好。", "要不要说出来一点？"),
    ("我今天在{topic}上被夸了。", "那挺好啊，你值得。"),
)

_EX_TOPIC_WORDS: tuple[str, ...] = (
    "工作",
    "面试",
    "家里的事",
    "睡眠",
    "那个项目",
    "周末的安排",
    "和同事的关系",
    "体检",
    "朋友那边",
    "搬家",
    "看电影",
    "学新东西",
)


def _make_dialogue(index: int) -> tuple[str, str]:
    """按"话题 × 对话模板"生成互不相同的对话片段。"""
    topic = _EX_TOPIC_WORDS[(index // len(_EX_DIALOGUES)) % len(_EX_TOPIC_WORDS)]
    char_template, user_template = _EX_DIALOGUES[index % len(_EX_DIALOGUES)]
    return char_template.format(topic=topic), user_template

#: 与 (方向, 强度档) 匹配的心理语言素材
_EX_TEMPLATES: dict[str, dict[str, tuple[str, ...]]] = {
    "-": {
        "mild": (
            "有一点失落，不太明显但确实在。",
            "心里稍微有点空，还算平静。",
        ),
        "moderate": (
            "有些失落，也有一点不确定。",
            "不太舒服，说不上是难过还是介意。",
        ),
        "strong": (
            "很难受，堵得厉害，一时顺不过来。",
            "失落感很重，几乎提不起劲做别的。",
        ),
    },
    "+": {
        "mild": (
            "有一点高兴，心情轻松了些。",
            "觉得温暖，虽然并不强烈。",
        ),
        "moderate": (
            "挺高兴的，心里舒服了不少。",
            "有点感动，也觉得踏实。",
        ),
        "strong": (
            "非常高兴，整个人都轻快起来。",
            "特别开心，暖意一直没散。",
        ),
    },
    "0": {
        "mild": (
            "没什么特别的波动，比较平。",
            "情绪上没什么起伏。",
        ),
        "moderate": (
            "说不出什么感觉，暂时算平静。",
            "整体是平的，没有明显起伏。",
        ),
        "strong": (
            "仍然说不上有什么感觉，比较平淡。",
            "没有明显情绪，就是平静。",
        ),
    },
    "+-": {
        "mild": (
            "有点高兴也有点拿不准。",
            "既觉得暖，又有一点不确定。",
        ),
        "moderate": (
            "高兴里混着不安，说不清哪边更重。",
            "既觉得温暖，又有点介意。",
        ),
        "strong": (
            "又暖又沉重，两种感觉都很强。",
            "高兴和失落同时压着，很难受。",
        ),
    },
}

_EX_FOCUS: tuple[str, ...] = (
    "比较在意今晚的交流是否会就此中断。",
    "主要在想对方刚才那句话的意思。",
    "注意力落在这件事会不会有下文。",
    "在意自己是不是说了不合适的话。",
    "关注对方现在的状态好不好。",
)

_EX_CONFLICT: tuple[str, ...] = (
    "想确认之后还会不会继续交流，但又不想显得太依赖。",
    "想说清楚，又怕把事情说得更糟。",
    "倾向于自责，但也知道责任不全在自己。",
    "想马上回应，又觉得应该先缓一缓。",
)

_EX_CONFLICT_NONE: tuple[str, ...] = (
    "当前没有明显冲突，状态比较平顺。",
    "说不上有什么拉扯，暂时不构成冲突。",
)

_EX_IMPULSE_HIGH: tuple[str, ...] = (
    "想确认对方之后是否还会回来。",
    "想多问一句，想知道更多细节。",
    "想靠近一点，把话说明白。",
)

_EX_IMPULSE_LOW: tuple[str, ...] = (
    "暂时没有想主动靠近的念头。",
    "先不打算追问，想留一点空间。",
)

_EX_INHIBITION_HIGH: tuple[str, ...] = (
    "不希望给对方增加压力，先不说。",
    "克制着，不想显得太依赖。",
    "忍住不追问，给对方留出空间。",
)

_EX_INHIBITION_LOW: tuple[str, ...] = (
    "没什么需要压着的，可以自然表达。",
    "不太需要克制，想说就说。",
)

_EX_EXPRESSION: tuple[str, ...] = (
    "表达上会稍微显得舍不得，但整体仍然克制。",
    "语气会平和一些，不刻意强调情绪。",
    "会表达一点亲近，但不追问。",
    "说得比较淡，不渲染。",
)


def _intensity_band(intensity: float) -> str:
    if intensity < 0.45:
        return "mild"
    if intensity < 0.8:
        return "moderate"
    return "strong"


def make_emotion_explain_record(
    index: int, rng: random.Random, *, make_invalid: bool = False
) -> dict[str, Any]:
    """合成一条情绪解释记录。"""
    char_text, user_text = _make_dialogue(index)
    direction = ["-", "+", "0", "+-"][index % 4]
    intensity = round(rng.uniform(0.55, 0.92) if index % 5 == 0 else rng.uniform(0.15, 0.9), 2)
    band = _intensity_band(intensity)
    target = ["user", "self", "situation", "third_party"][index % 4]
    approach = round(rng.uniform(0.05, 0.95), 2)
    restraint = round(rng.uniform(0.05, 0.95), 2)
    conflict_present = index % 3 != 2

    experience_pool = _EX_TEMPLATES[direction][band]
    experience = experience_pool[index % len(experience_pool)]
    conflict_pool = _EX_CONFLICT if conflict_present else _EX_CONFLICT_NONE
    conflict = conflict_pool[index % len(conflict_pool)]
    impulse_pool = _EX_IMPULSE_HIGH if approach >= 0.5 else _EX_IMPULSE_LOW
    impulse = impulse_pool[index % len(impulse_pool)]
    inhibition_pool = _EX_INHIBITION_HIGH if restraint >= 0.5 else _EX_INHIBITION_LOW
    inhibition = inhibition_pool[index % len(inhibition_pool)]

    output: dict[str, Any] = {
        "experience": experience,
        "focus": _EX_FOCUS[index % len(_EX_FOCUS)],
        "conflict": conflict,
        "impulse": impulse,
        "inhibition": inhibition,
        "expression": _EX_EXPRESSION[index % len(_EX_EXPRESSION)],
    }

    if make_invalid:
        # 必须**确定性地**违规：把输入方向强制为负向，同时给出明显正向感受，
        # 保证触发 EX02_DIRECTION_FLIP。
        # 若只在"正向"情况下改文案，direction 恰为 "+" 时反而变为合法（实测踩过）。
        input_direction = "-"
        output["experience"] = "非常开心，被温暖到了。"
        output["focus"] = "觉得很满足，很放松。"
        output["conflict"] = "完全没有冲突，很平顺。"
    else:
        input_direction = direction

    return {
        "id": f"ex_fixture_{index:04d}",
        "task": "emotion_explain",
        "input": {
            "event": {"char": char_text, "user": user_text},
            "background_mood": {
                "valence": round(rng.uniform(-0.6, 0.6), 2),
                "arousal": round(rng.uniform(0.1, 0.9), 2),
            },
            "active_emotions": [
                {
                    "target": target,
                    "cause": f"用户说了：{user_text}",
                    "direction": input_direction,
                    "intensity": intensity,
                    "semantic_label": None,
                    "action_tendency": conflict,
                }
            ],
            "approach_drive": approach,
            "restraint": restraint,
            "conflict_present": conflict_present,
        },
        "output": output,
        "meta": {
            "scenario_id": f"fixture_ex_{index:04d}",
            "event_kind": [
                "testing_probe",
                "vulnerable_request",
                "criticism",
                "good_news_user",
                "topic_change",
                "boundary_setting",
                "gift_or_help",
                "time_gap",
            ][index % 8],
            "source": "fixture",
            "generator_version": "fixture-1.0.0",
        },
    }


# --------------------------------------------------------------------------
# 批量生成
# --------------------------------------------------------------------------

def build_fixture_records(
    *,
    event_eval_count: int = 24,
    emotion_explain_count: int = 24,
    seed: int = 1234,
    include_invalid: bool = False,
) -> list[dict[str, Any]]:
    """生成混合夹具数据集。"""
    rng = random.Random(seed)
    records: list[dict[str, Any]] = []
    for index in range(event_eval_count):
        records.append(
            make_event_eval_record(
                index, rng, make_invalid=include_invalid and index % 8 == 7
            )
        )
    for index in range(emotion_explain_count):
        records.append(
            make_emotion_explain_record(
                index, rng, make_invalid=include_invalid and index % 8 == 7
            )
        )
    return records


def write_fixture(
    path: str | Path,
    *,
    event_eval_count: int = 24,
    emotion_explain_count: int = 24,
    seed: int = 1234,
    include_invalid: bool = False,
) -> Path:
    """把夹具写成 JSONL。"""
    from .utils.io import write_jsonl

    records = build_fixture_records(
        event_eval_count=event_eval_count,
        emotion_explain_count=emotion_explain_count,
        seed=seed,
        include_invalid=include_invalid,
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(target, records, sort_by="id")
    return target


def main(argv: Iterable[str] | None = None) -> int:
    """CLI：``python -m qboss_training.fixtures make --output data/fixtures.jsonl``。"""
    import argparse

    parser = argparse.ArgumentParser(description="生成离线测试夹具数据")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make", help="写出夹具 JSONL")
    make.add_argument("--output", required=True)
    make.add_argument("--event-eval", type=int, default=24)
    make.add_argument("--emotion-explain", type=int, default=24)
    make.add_argument("--seed", type=int, default=1234)
    make.add_argument(
        "--include-invalid",
        action="store_true",
        help="混入故意违规的样本（用于验证校验器）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    path = write_fixture(
        args.output,
        event_eval_count=args.event_eval,
        emotion_explain_count=args.emotion_explain,
        seed=args.seed,
        include_invalid=args.include_invalid,
    )
    print(f"已写出夹具：{path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
