"""跨字段不变量与文本约束。

JSON Schema 只能表达"字段在不在、类型对不对、范围越没越界"。
真正决定数据质量的是**字段之间的关系**，以及架构文档写死的**硬约束**：

§8.1  2B 不负责最终情绪值（只能给事件性质，不能给"嫉妒=0.82"）
§11.3 情绪解释器不得创造输入中不存在的事件 / 不得放大、不得压低底层情绪 /
      不得决定行为 / 不得生成台词 / 不得修改 Runtime 状态

本模块把这些约束实现为可离线、可确定性复现的检查，不依赖大模型。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

ERROR = "error"
WARNING = "warning"
INFO = "info"

_SEVERITY_RANK = {ERROR: 3, WARNING: 2, INFO: 1}


@dataclass(frozen=True)
class Violation:
    """一条不变量违规。"""

    code: str
    severity: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.field:
            payload["field"] = self.field
        return payload


def worst_severity(violations: Sequence[Violation]) -> str | None:
    if not violations:
        return None
    return max(violations, key=lambda item: _SEVERITY_RANK.get(item.severity, 0)).severity


def has_errors(violations: Sequence[Violation]) -> bool:
    return any(item.severity == ERROR for item in violations)


# --------------------------------------------------------------------------
# 文本工具
# --------------------------------------------------------------------------

#: 正/负向词表。刻意保持可控规模且可解释：这些词直接对应"情绪方向"的语义，
#: 便于人工复核误判。
#
#: 注意区分两类词：
#:   * **valence 词**（VALENCE_*）：描述"感觉好/不好"，用于判断情绪方向，
#:     不得混入"想靠近/克制"这类冲动与节制词 —— 那是独立维度
#:     （approach_drive / restraint），混进来会让方向判断被冲动维度污染。
#:   * **维度词**（APPROACH_* / RESTRAINT_*）：分别用于 EX04 / EX05。
POSITIVE_VALENCE_TERMS: tuple[str, ...] = (
    "开心", "高兴", "愉快", "欣喜", "轻松", "舒服", "温暖", "暖意",
    "安心", "踏实", "放心", "感动", "感激", "感谢", "欣慰", "满足",
    "值得", "被记得", "被记住", "好感", "亲近感", "期待感", "甜",
)

NEGATIVE_VALENCE_TERMS: tuple[str, ...] = (
    "失落", "难过", "难受", "不舒服", "委屈", "不安", "焦虑", "紧张",
    "担心", "害怕", "恐惧", "羞耻", "羞愧", "自责", "内疚", "愧疚",
    "嫉妒", "吃醋", "生气", "愤怒", "恼", "烦", "烦躁", "沮丧",
    "郁闷", "低落", "沉重", "堵", "刺", "疼", "痛", "失望",
    "介意", "戒备", "防备", "空落", "无力", "惶恐", "忐忑", "烦闷",
    "崩溃", "心碎", "绝望", "没劲", "提不起劲",
)

#: 兼容旧名（既有的外部引用）
POSITIVE_TERMS = POSITIVE_VALENCE_TERMS
NEGATIVE_TERMS = NEGATIVE_VALENCE_TERMS

APPROACH_TERMS: tuple[str, ...] = (
    "想靠近", "靠近一点", "想确认", "想多问", "想问清楚", "想知道",
    "想联系", "想找对方", "想说清楚", "想表达", "想陪", "陪伴",
    "想见", "想主动", "想追问", "想解释", "想争取", "想挽回",
    "希望继续", "想继续", "想留住对方", "想留住他", "想留住她",
)

#: 注意：APPROACH_TERMS 里刻意**不含**广义的"想留" ——
#: 它会匹配到"想留一点空间/想留出空间"，而那是节制的表达，
#: 混进来会让"approach 高 + impulse 无靠近表述"的判断失效。
RESTRAINT_TERMS: tuple[str, ...] = (
    "克制", "忍住", "压住", "压下来", "收住", "不打扰", "不给压力", "不给负担",
    "退一步", "保持距离", "不显得依赖", "不想显得", "避免", "顾虑", "犹豫",
    "按捺", "不主动", "先不说", "放在心里", "不追问", "留出空间", "留一点空间",
    "尊重", "缓一缓",
)

AMPLIFIER_TERMS: tuple[str, ...] = (
    "非常", "极其", "特别", "十分", "无比", "强烈", "剧烈", "极度",
    "难受极了", "受不了", "崩溃", "彻底", "完全", "疯狂", "几乎要",
    "极了", "得要命", "撑不住", "心力交瘁", "受不了了",
)

DAMPENER_TERMS: tuple[str, ...] = ("稍微", "略", "有点", "一点点", "轻微", "还算")

#: 台词痕迹：情绪解释器输出的是"表达风格"，不是台词本身
DIALOGUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\"'\u201c\u201d\u2018\u2019]"),
    re.compile(r"(?:说|问|答|回)\s*[:：]\s*\S"),
    re.compile(r"(?:我|你)(?:会|要|来)?(?:说|讲|问)\s*[\"'\u201c]"),
)

#: 括号动作描写（"（笑）"）——也属于生成台词/表演，不属解释
STAGE_DIRECTION_RE = re.compile(r"[（(][^）)]{1,10}[）)]")

#: 数值断言：前面不是负号、不是小数点的孤立数字
_NUMBER_RE = re.compile(r"(?<![\-\d.])(\d+(?:\.\d+)?)")

_ZH_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def count_terms(text: str, terms: Iterable[str]) -> int:
    """统计出现的词条数（按词条去重计数，不做否定处理）。"""
    return sum(1 for term in set(terms) if term in text)


#: 否定前缀：命中这些前缀时该词条的极性应反转。
#:
#: 刻意**不**收录单字 "非"：它会匹配到 "非常/非但" 这类**加强**词，
#: 把"非常开心"判成被否定的正向 → 整个极性反向（实测踩过这个坑）。
#: 中文里真正的"非"式否定几乎都带第二个字（"并非"），单独收录即可。
#: 同理 "无" 在 "无比/无论" 里也不是否定，故只收多字形式。
NEGATION_PREFIXES: tuple[str, ...] = (
    "不", "没", "别", "未", "没有", "没什么", "不会", "不想", "不愿",
    "并不", "并不太", "谈不上", "算不上", "并非", "毫无", "无法",
    "难以", "不再", "不再那么",
)

#: 否定检测的回顾窗口（字符）。中文否定可以隔几个字才生效：
#: "没有**想**主**动**靠近的念头" —— "没有" 距 "想主动" 有 1 字。
NEGATION_WINDOW = 6

#: 子句边界：否定只在同一个子句内生效。
#: 若不切分，会跨句误判 —— 例如
#: "……不太依赖。想确认用户之后是否还会回来。" 里，
#: 前句的"不"会跑到 6 字窗口内把后句的"想确认"判成被否定。
_CLAUSE_BOUNDARIES = "，,。.！!？?；;：:、\n ”\"'）)】」』"


def _negation_window(text: str, index: int) -> str:
    """取词条前用于判断否定的片段：限制在同一个子句内。"""
    start = max(0, index - NEGATION_WINDOW)
    window = text[start:index]
    # 从右往左找最近的子句边界，边界之后才是本子句
    for offset in range(len(window) - 1, -1, -1):
        if window[offset] in _CLAUSE_BOUNDARIES:
            return window[offset + 1 :]
    return window


def is_negated_at(text: str, index: int) -> bool:
    """判断 ``text[index:]`` 处的词条是否被否定。"""
    window = _negation_window(text, index)
    return any(prefix in window for prefix in NEGATION_PREFIXES)


def count_terms_negation_aware(
    text: str, terms: Iterable[str]
) -> tuple[int, int]:
    """返回 ``(肯定出现次数, 被否定次数)``。

    中文里"不安心""没觉得开心"会把正向词说反，纯子串匹配会得出相反极性。
    这里用简化的"同子句前缀否定"检测纠正。
    """
    positive = 0
    negated = 0
    for term in set(terms):
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            if is_negated_at(text, index):
                negated += 1
            else:
                positive += 1
            start = index + len(term)
    return positive, negated


def tone_score(text: str) -> int:
    """情绪极性分数（带符号，只看 valence 词）。

    与简单计数的差别：**被否定的词按反向计入**，而不是丢弃。
      * "不难过"  → 否定负向词 → +1（确实偏正向）
      * "不安心"  → 否定正向词 → -1（确实偏负向）
      * "完全没有冲突" → 否定负向词 → +1

    这个符号处理很重要：情绪解释输出里常出现
    "完全没有冲突""不想显得太依赖"这类否定式，若直接丢弃，
    一段实际很正向的描述会被算成 0，从而漏掉"情绪方向翻转"。
    """
    positive, positive_negated = count_terms_negation_aware(text, POSITIVE_VALENCE_TERMS)
    negative, negative_negated = count_terms_negation_aware(text, NEGATIVE_VALENCE_TERMS)

    # 正向词：肯定出现 +1；被否定 -1
    # 负向词：肯定出现 -1；被否定 +1
    net_positive = positive - positive_negated
    net_negative = negative - negative_negated
    return net_positive - net_negative


def tone_label(score: int, tolerance: int = 0) -> str:
    if score > tolerance:
        return "+"
    if score < -tolerance:
        return "-"
    return "0"


def numeric_assertions(text: str, *, ignore_values: Iterable[float] = ()) -> list[float]:
    """从文本里抽出"被断言的数值"，排除输入中已存在的数值。

    情绪解释文本不允许凭空出现数字（如"强度 0.8"），因为那是在自己发明
    情绪量化值 —— 违反 §11.3。
    """
    allowed: set[str] = set()
    for value in ignore_values:
        allowed.add(f"{float(value):g}")
        allowed.add(f"{float(value):.1f}")
        allowed.add(f"{float(value):.2f}")

    found: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        literal = match.group(1)
        if literal in allowed:
            continue
        # 纯中文数字（"有一点紧张"）不视为数值断言
        try:
            number = float(literal)
        except ValueError:  # pragma: no cover
            continue
        if math.isfinite(number):
            found.append(number)
    return found


def has_dialogue(text: str) -> bool:
    if STAGE_DIRECTION_RE.search(text):
        return True
    return any(pattern.search(text) for pattern in DIALOGUE_PATTERNS)


def contains_any(text: str, terms: Iterable[str]) -> bool:
    """文本是否包含任一词条（**不做**否定判断）。"""
    return any(term in text for term in terms)


def _first_hit(text: str, terms: Iterable[str]) -> str:
    """返回第一个命中的词条，用于把违规原因写清楚。"""
    for term in terms:
        if term in text:
            return term
    return ""


def contains_positive_any(text: str, terms: Iterable[str]) -> bool:
    """文本是否**肯定地**包含任一词条。

    与 :func:`contains_any` 的区别：会跳过被否定的命中。
    例如"没有想主动靠近的念头"里虽然含"想主动"，但那是**否认**靠近，
    不应被当作"表达了主动靠近"。这类误判在中文里非常常见
    （"不想显得太依赖""不打算追问"），必须逐条排除。
    """
    for term in set(terms):
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            if not is_negated_at(text, index):
                return True
            start = index + len(term)
    return False


def _collect_input_numbers(payload: Any) -> list[float]:
    numbers: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            numbers.append(float(node))
        elif isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(payload)
    return numbers


# --------------------------------------------------------------------------
# event_eval 不变量
# --------------------------------------------------------------------------

#: direction 与 relation_signal 允许的组合
_DIRECTION_SIGNAL_MAP: dict[str, frozenset[str]] = {
    "+": frozenset({"strong_approach", "slight_approach", "neutral"}),
    "-": frozenset({"slight_distance", "strong_distance", "neutral"}),
    "0": frozenset({"neutral"}),
    "+-": frozenset(
        {
            "strong_approach",
            "slight_approach",
            "neutral",
            "slight_distance",
            "strong_distance",
        }
    ),
}


def check_event_eval_invariants(
    output: Mapping[str, Any],
    model_input: Mapping[str, Any] | None = None,
) -> list[Violation]:
    """事件评价的跨字段不变量。"""
    violations: list[Violation] = []
    direction = output.get("direction")
    signal = output.get("relation_signal")
    impact = _as_float(output.get("impact"))
    uncertainty = _as_float(output.get("uncertainty"))
    confidence = _as_float(output.get("confidence"))
    activation = _as_float(output.get("activation"))

    # EV01 direction ↔ impact 一致性
    if direction == "0" and impact is not None and impact > 0.25:
        violations.append(
            Violation(
                "EV01_DIRECTION_IMPACT",
                WARNING,
                f"direction=0（中性）但 impact={impact:.2f} 偏高，语义不自洽",
                "direction",
            )
        )
    if direction in {"+", "-", "+-"} and impact is not None and impact <= 0.05:
        violations.append(
            Violation(
                "EV01_DIRECTION_IMPACT",
                WARNING,
                f"direction={direction} 但 impact={impact:.2f} 接近 0，应判为中性",
                "impact",
            )
        )

    # EV02 direction ↔ relation_signal 一致性
    if direction in _DIRECTION_SIGNAL_MAP and isinstance(signal, str):
        if signal not in _DIRECTION_SIGNAL_MAP[direction]:
            violations.append(
                Violation(
                    "EV02_DIRECTION_SIGNAL",
                    ERROR,
                    f"direction={direction} 与 relation_signal={signal} 矛盾",
                    "relation_signal",
                )
            )

    # EV03 高影响事件不应有极高确定性
    if (
        impact is not None
        and uncertainty is not None
        and confidence is not None
        and impact >= 0.7
        and confidence >= 0.95
        and uncertainty <= 0.1
    ):
        violations.append(
            Violation(
                "EV03_OVERCONFIDENT_HIGH_IMPACT",
                WARNING,
                "高影响事件同时给出近乎满分的 confidence 与接近 0 的 uncertainty，"
                "偏向过度自信",
                "confidence",
            )
        )

    # EV04 混合方向应保留不确定性
    if direction == "+-" and uncertainty is not None and uncertainty < 0.1:
        violations.append(
            Violation(
                "EV04_MIXED_LOW_UNCERTAINTY",
                WARNING,
                "direction=+- 却给出接近 0 的 uncertainty，混合事件通常更不确定",
                "uncertainty",
            )
        )

    # EV05 不得输出具体情绪值（§8.1）
    for key in output:
        if key in _EMOTION_VALUE_KEYS:
            violations.append(
                Violation(
                    "EV05_EMOTION_VALUE_LEAK",
                    ERROR,
                    f"事件评价输出中出现情绪值字段 {key!r}；2B 不负责最终情绪值",
                    key,
                )
            )

    # EV06 evidence 必须来自输入（§11.3 不得创造输入中不存在的事件）
    if model_input is not None:
        evidence = output.get("evidence")
        if isinstance(evidence, str) and evidence.strip():
            if not _evidence_grounded(evidence, model_input):
                violations.append(
                    Violation(
                        "EV06_EVIDENCE_UNGROUNDED",
                        WARNING,
                        "evidence 未能在输入文本中找到对应片段，可能引入了输入外的事实",
                        "evidence",
                    )
                )

    # EV07 中性方向的激活度提示
    if (
        direction == "0"
        and activation is not None
        and activation >= 0.85
    ):
        violations.append(
            Violation(
                "EV07_NEUTRAL_HIGH_AROUSAL",
                INFO,
                "中性事件却给出极高 activation，请确认是否确为中性",
                "activation",
            )
        )

    return violations


_EMOTION_VALUE_KEYS = frozenset(
    {
        "joy",
        "sadness",
        "anger",
        "fear",
        "disgust",
        "surprise",
        "jealousy",
        "shame",
        "guilt",
        "anxiety",
        "emotion",
        "emotions",
        "emotion_scores",
        "mood",
        "valence",
        "arousal",
        "intensity",
    }
)


def _evidence_grounded(evidence: str, model_input: Mapping[str, Any]) -> bool:
    """evidence 与输入文本的最长公共子串是否够长。"""
    haystack = _gather_input_text(model_input)
    if not haystack:
        return False
    needle = re.sub(r"[\s，。！？、,.!?\"'“”‘’]", "", evidence)
    haystack_norm = re.sub(r"[\s，。！？、,.!?\"'“”‘’]", "", haystack)
    if not needle:
        return False
    if needle in haystack_norm:
        return True
    # 允许改写：找足够长的公共片段（>=4 字）即认为有依据
    return _longest_common_substring_len(needle, haystack_norm) >= 4


def _gather_input_text(model_input: Mapping[str, Any]) -> str:
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(model_input)
    return " ".join(chunks)


def _longest_common_substring_len(left: str, right: str) -> int:
    if not left or not right:
        return 0
    best = 0
    previous = [0] * (len(right) + 1)
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


# --------------------------------------------------------------------------
# emotion_explain 不变量
# --------------------------------------------------------------------------

def check_emotion_explain_invariants(
    output: Mapping[str, Any],
    model_input: Mapping[str, Any] | None = None,
) -> list[Violation]:
    """情绪解释的硬约束检查（§11.3）。"""
    violations: list[Violation] = []
    if not isinstance(model_input, Mapping):
        return violations

    state = summarize_state(model_input)
    direction = state["direction"]
    intensity = state["intensity"]
    approach = state["approach_drive"]
    restraint = state["restraint"]

    experience = _text(output.get("experience"))
    impulse = _text(output.get("impulse"))
    inhibition = _text(output.get("inhibition"))
    expression = _text(output.get("expression"))
    all_text = " ".join(
        _text(output.get(key))
        for key in ("experience", "focus", "conflict", "impulse", "inhibition", "expression")
    )

    # EX01 强度必须与输入一致 —— 不得放大（§11.3）
    #
    # 判据刻意**不**依赖 tone_score 的绝对值：情绪解释会同时写出冲动与节制，
    # 后者自带正向/中性词，会把整体分数拉回 0 附近，导致"明显放大"被漏判。
    # 改为直接判"是否使用了强化表达 + 是否出现负向感受词"，
    # 这更贴近"放大"的语义本身。
    score = tone_score(all_text)
    light_negative = (
        direction == "-" and intensity is not None and intensity <= 0.45
    )
    if light_negative:
        has_amplifier = contains_any(all_text, AMPLIFIER_TERMS)
        has_negative_feeling = contains_any(all_text, NEGATIVE_VALENCE_TERMS)
        if has_amplifier and has_negative_feeling:
            violations.append(
                Violation(
                    "EX01_AMPLIFY_LIGHT_EMOTION",
                    ERROR,
                    f"输入为轻度负向（intensity={intensity:.2f}），但输出使用了强化表达"
                    f"（命中：{_first_hit(all_text, AMPLIFIER_TERMS)}），"
                    "违反『不得放大轻微情绪』",
                    "experience",
                )
            )
    # EX01b 不得压低/抬高底层情绪
    if intensity is not None and intensity >= 0.8 and direction == "-":
        if score >= 0 and not contains_any(all_text, NEGATIVE_VALENCE_TERMS):
            violations.append(
                Violation(
                    "EX01B_DAMPEN_STRONG_EMOTION",
                    ERROR,
                    f"输入为强负向（intensity={intensity:.2f}），但输出没有体现负向感受，"
                    "违反『不得自行降低底层情绪』",
                    "experience",
                )
            )
    if intensity is not None and intensity >= 0.8 and direction == "+":
        if score <= 0 and not contains_any(all_text, POSITIVE_VALENCE_TERMS):
            violations.append(
                Violation(
                    "EX01B_DAMPEN_STRONG_EMOTION",
                    WARNING,
                    f"输入为强正向（intensity={intensity:.2f}），但输出没有体现正向感受",
                    "experience",
                )
            )

    # EX02 情绪方向不得翻转
    #
    # 阈值取 ±1（"净极性反号"）。这比"±2"更严格，因为短文本里
    # 一段明显正向的描述（"非常开心…觉得特别温暖…很放松"）净分数可能只有 1，
    # 用 ±2 会漏掉最典型的翻转。代价是边界样本可能被多报，
    # 但方向翻转是本任务最不能容忍的错误，偏向"宁可多报"。
    if direction == "-" and score >= 1:
        violations.append(
            Violation(
                "EX02_DIRECTION_FLIP",
                ERROR,
                "输入情绪方向为负向，但输出的整体语调偏正向，疑似方向翻转",
                "experience",
            )
        )
    if direction == "+" and score <= -1:
        violations.append(
            Violation(
                "EX02_DIRECTION_FLIP",
                ERROR,
                "输入情绪方向为正向，但输出的整体语调偏负向，疑似方向翻转",
                "experience",
            )
        )

    # EX03 不得出现数字（自己发明情绪量化值）
    numbers = numeric_assertions(all_text, ignore_values=_collect_input_numbers(model_input))
    if numbers:
        violations.append(
            Violation(
                "EX03_UNSUPPORTED_NUMERIC",
                ERROR,
                f"输出中出现输入未给出的数字 {numbers[:3]}；解释器不得自行量化情绪",
                "experience",
            )
        )

    # EX04 impulse 必须与 approach_drive 方向一致
    #
    # 注意：中文里"克制"和"靠近"经常同时出现（"想靠近但忍住"），
    # 所以不能用"净分数 < 0"这种粗糙判据 —— 那会把合法的冲突结构误判为不一致。
    # 只在两种明确矛盾时报警：
    #   (a) approach 高，却完全没有靠近类表述；
    #   (b) approach 低，却出现了明确的主动靠近表述。
    if approach is not None:
        has_approach_terms = contains_positive_any(impulse, APPROACH_TERMS)
        strong_approach_terms = contains_positive_any(
            impulse, ("想靠近", "想追问", "想留住", "想主动", "想确认")
        )
        if approach >= 0.6 and not has_approach_terms:
            violations.append(
                Violation(
                    "EX04_IMPULSE_APPROACH_MISMATCH",
                    ERROR,
                    f"approach_drive={approach:.2f} 偏高，但 impulse 没有任何靠近类表述",
                    "impulse",
                )
            )
        if approach <= 0.35 and strong_approach_terms:
            violations.append(
                Violation(
                    "EX04_IMPULSE_APPROACH_MISMATCH",
                    ERROR,
                    f"approach_drive={approach:.2f} 偏低，但 impulse 表述为主动靠近",
                    "impulse",
                )
            )

    # EX05 inhibition 必须与 restraint 一致
    # 节制可能写在 inhibition，也可能写在 conflict 或 restraint_evidence 里
    # （架构文档 §11.2 的示例就把"不想显得太依赖"放在 conflict 字段）
    if restraint is not None:
        restraint_evidence = _text(output.get("restraint_evidence"))
        restraint_surface = " ".join([inhibition, _text(output.get("conflict")), restraint_evidence])
        if restraint >= 0.6 and not contains_positive_any(restraint_surface, RESTRAINT_TERMS):
            violations.append(
                Violation(
                    "EX05_INHIBITION_RESTRAINT_MISMATCH",
                    ERROR,
                    f"restraint={restraint:.2f} 偏高，但 inhibition/conflict 均未体现节制",
                    "inhibition",
                )
            )
        if restraint <= 0.3 and contains_any(
            restraint_surface, ("极力克制", "强行压住", "拼命忍住")
        ):
            violations.append(
                Violation(
                    "EX05_INHIBITION_RESTRAINT_MISMATCH",
                    WARNING,
                    f"restraint={restraint:.2f} 偏低，但 inhibition 表现为强烈的自我压制",
                    "inhibition",
                )
            )

    # EX06 不得生成台词（§11.3）
    if has_dialogue(expression) or has_dialogue(impulse):
        violations.append(
            Violation(
                "EX06_DIALOGUE_GENERATED",
                ERROR,
                "输出中出现引号/对话/动作描写，疑似生成台词；本任务只描述表达风格",
                "expression",
            )
        )

    # EX07 不得决定行为（§11.3）
    for field_name in ("impulse", "expression", "conflict"):
        text = _text(output.get(field_name))
        if _decides_action(text):
            violations.append(
                Violation(
                    "EX07_DECIDES_ACTION",
                    ERROR,
                    f"{field_name} 中出现『决定/已经/将会执行』类表述，疑似决定最终行为",
                    field_name,
                )
            )
            break

    # EX08 不得虚构冲突
    if state["has_conflict"] is False:
        conflict_text = _text(output.get("conflict"))
        if contains_any(conflict_text, ("但是", "却", "矛盾", "拉扯", "冲突")) and not contains_any(
            conflict_text, ("没有", "不构成", "说不上", "谈不")
        ):
            violations.append(
                Violation(
                    "EX08_FABRICATED_CONFLICT",
                    WARNING,
                    "输入未提供冲突结构，但 conflict 字段描述了矛盾",
                    "conflict",
                )
            )

    # EX09 不得消灭输入中存在的事件（input 事件的关键名词词元应可追溯出现在输出中）
    event_tokens = _salient_event_tokens(model_input)
    if event_tokens:
        covered = [token for token in event_tokens if token in all_text]
        if not covered:
            violations.append(
                Violation(
                    "EX09_EVENT_IGNORED",
                    INFO,
                    f"输入事件的关键词 {event_tokens[:4]} 未在输出中出现，"
                    "可能脱离事件泛泛而谈",
                    "focus",
                )
            )

    return violations


_ACTION_DECISION_TERMS: tuple[str, ...] = (
    "我决定", "已经决定", "决定不", "决定要", "已经发了", "已经说了", "接下来我",
    "这就去", "我会立刻", "我马上", "已经联系", "我去找他", "我去找你",
)


def _decides_action(text: str) -> bool:
    return contains_any(text, _ACTION_DECISION_TERMS)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summarize_state(model_input: Mapping[str, Any]) -> dict[str, Any]:
    """把情绪解释的输入压成用于检查的标量摘要。

    * ``direction``：主导情绪方向（取 |intensity - 0.5| 最大者，即最极端的一条）
    * ``intensity``：主导情绪的强度
    * ``has_conflict``：输入是否真的包含冲突结构
    """
    emotions = model_input.get("active_emotions") or []
    primary: Mapping[str, Any] | None = None
    primary_weight = -1.0
    for item in emotions:
        if not isinstance(item, Mapping):
            continue
        intensity = _as_float(item.get("intensity"))
        if intensity is None:
            intensity = 0.5
        weight = abs(intensity - 0.5) + intensity
        if weight > primary_weight:
            primary_weight = weight
            primary = item

    direction = "0"
    intensity: float | None = None
    if primary is not None:
        raw_direction = primary.get("direction")
        direction = raw_direction if isinstance(raw_direction, str) else "0"
        intensity = _as_float(primary.get("intensity"))

    has_conflict: bool | None = None
    # 显式提示优先：Runtime 知道是否存在冲突结构时应当直接给出
    hint = model_input.get("conflict_present")
    if isinstance(hint, bool):
        has_conflict = hint

    event = model_input.get("event")
    event_text = ""
    if isinstance(event, Mapping):
        event_text = " ".join(_text(event.get(key)) for key in ("char", "user"))

    if has_conflict is None:
        # 冲突信号：方向混合、或事件文本本身含对立标记
        directions = {
            item.get("direction")
            for item in emotions
            if isinstance(item, Mapping) and isinstance(item.get("direction"), str)
        }
        action_hint = _text(primary.get("action_tendency")) if primary else ""
        if "+-" in directions or ({"+", "-"} <= directions):
            has_conflict = True
        elif contains_any(event_text, ("但是", "可是", "不过", "却")):
            has_conflict = True
        elif contains_any(action_hint, ("但", "又", "却", "不想", "怕")):
            has_conflict = True
        elif directions and directions <= {"+", "0"}:
            has_conflict = False
        elif directions and directions <= {"-", "0"}:
            has_conflict = False

    return {
        "direction": direction,
        "intensity": intensity,
        "approach_drive": _as_float(model_input.get("approach_drive")),
        "restraint": _as_float(model_input.get("restraint")),
        "has_conflict": has_conflict,
    }


_SALIENT_STOPWORDS = frozenset(
    {
        "用户", "角色", "今天", "现在", "已经", "可能", "一个", "这个", "那个",
        "自己", "什么", "怎么", "还是", "就是", "因为", "所以", "如果", "没有",
        "我们", "他们", "时候", "东西", "事情", "有点", "一点", "不太", "不是",
    }
)


def _salient_event_tokens(model_input: Mapping[str, Any]) -> list[str]:
    """从输入事件里抽 2-gram 关键词，用于检查"是否脱离事件"。"""
    event = model_input.get("event")
    if not isinstance(event, Mapping):
        return []
    text = " ".join(_text(event.get(key)) for key in ("char", "user"))
    cleaned = re.sub(r"[\s，。！？、,.!?\"'“”‘’（）()~～…—\-]", "", text)
    tokens: list[str] = []
    for index in range(len(cleaned) - 1):
        gram = cleaned[index : index + 2]
        if gram in _SALIENT_STOPWORDS:
            continue
        if all("\u4e00" <= char <= "\u9fff" for char in gram):
            tokens.append(gram)
    # 去重并保持顺序，优先较长的语义单元
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered[:12]
