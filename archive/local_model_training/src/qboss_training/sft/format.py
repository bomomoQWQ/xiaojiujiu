"""聊天 SFT 数据构建：chat 模板 + completion-only 掩码。

设计要点
--------
* **completion-only**：只在 assistant（JSON）段落上计算 loss；
  system + user 段的 label 全部置为 ``-100``。对"严格格式输出"任务，
  让模型去拟合输入的 JSON 只是浪费容量并鼓励复制输入。
* **思维链掩码**：Qwen3 系列模板在 assistant 段可能带 `` thinking...<｜end▁of▁thinking｜>``。
  本任务是确定性结构化输出，思维链没有监督价值，因此一并掩掉
  （只训练 ``<｜end▁of▁thinking｜>`` 之后的真实 JSON）。
* **模板能力探测**：``enable_thinking`` 只在部分模板上支持，构造时会探测，
  不支持就退回默认调用，避免在不同 transformers 版本上直接崩。
* **无模型可用时**：提供 :func:`format_chat_sample` 只产出 messages 的纯文本路径，
  离线测试（不装 transformers / 不下模型）也能覆盖数据转换逻辑。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..contracts import get_contract
from ..utils.secrets import utc_now_iso

LOGGER = logging.getLogger("qboss_training.sft")

IGNORE_INDEX = -100

#: JSON 序列化是否缩进。**训练侧与推理侧共用的唯一事实来源。**
#: 缩进能让小模型更容易生成结构正确的 JSON（每个字段独立一行），
#: 但它同时意味着训练与推理必须一致，否则分布不匹配。
#: 三处引用：本模块的 build_messages / SFTBuildConfig.pretty_json、
#: qboss_training.training.config.TrainingConfig.pretty_json、
#: qboss_training.inference.build_inference_messages 的 pretty 默认值。
DEFAULT_PRETTY_JSON = True

#: Qwen3 系列思维链边界标记
THINK_OPEN = " thinking"
THINK_CLOSE = "<｜end▁of▁thinking｜>"


@dataclass
class SFTStats:
    """格式化统计。"""

    total: int = 0
    by_task: dict[str, int] = field(default_factory=dict)
    token_lengths: list[int] = field(default_factory=list)
    completion_token_lengths: list[int] = field(default_factory=list)
    masked_ratio_sum: float = 0.0
    truncated: int = 0
    think_masked: int = 0
    over_max_length: int = 0
    template_used: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    @property
    def mean_tokens(self) -> float:
        return (
            sum(self.token_lengths) / len(self.token_lengths) if self.token_lengths else 0.0
        )

    @property
    def mean_completion_tokens(self) -> float:
        return (
            sum(self.completion_token_lengths) / len(self.completion_token_lengths)
            if self.completion_token_lengths
            else 0.0
        )

    @property
    def mean_masked_ratio(self) -> float:
        return self.masked_ratio_sum / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        lengths = sorted(self.token_lengths)
        completions = sorted(self.completion_token_lengths)

        def _pct(values: Sequence[int], q: float) -> int:
            if not values:
                return 0
            index = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
            return values[index]

        return {
            "total": self.total,
            "by_task": dict(self.by_task),
            "mean_tokens": round(self.mean_tokens, 2),
            "mean_completion_tokens": round(self.mean_completion_tokens, 2),
            "max_tokens": _pct(lengths, 1.0),
            "p95_tokens": _pct(lengths, 0.95),
            "p99_tokens": _pct(lengths, 0.99),
            "min_tokens": _pct(lengths, 0.0),
            "max_completion_tokens": _pct(completions, 1.0),
            "p95_completion_tokens": _pct(completions, 0.95),
            "mean_masked_ratio": round(self.mean_masked_ratio, 4),
            "truncated": self.truncated,
            "think_masked": self.think_masked,
            "over_max_length": self.over_max_length,
            "template_used": self.template_used,
            "created_at": self.created_at,
        }


# --------------------------------------------------------------------------
# 1) 记录 → 会话消息（不依赖任何 ML 库）
# --------------------------------------------------------------------------

def build_messages(
    record: Mapping[str, Any],
    *,
    pretty: bool = DEFAULT_PRETTY_JSON,
    system_prompt: str | None = None,
) -> list[dict[str, str]]:
    """把训练记录转成 chat messages（system / user / assistant）。

    user 段是**规范化后的输入 JSON**，assistant 段是**规范化后的输出 JSON**。
    规范化很关键：训练时与推理时的序列化方式必须完全一致，
    否则模型学到的是某一种空白/键序，推理时换个 dumps 就崩。

    ``pretty`` 的默认值取自 :data:`DEFAULT_PRETTY_JSON`（即
    :class:`SFTBuildConfig` 的 ``pretty_json`` 默认值），
    避免"函数默认值"与"配置默认值"各说一套。
    """
    task = str(record.get("task", ""))
    contract = get_contract(task)
    prompt = system_prompt if system_prompt is not None else contract.system_prompt

    model_input = record.get("input")
    output = record.get("output")
    if model_input is None or output is None:
        raise ValueError(f"记录 {record.get('id')!r} 缺少 input 或 output")

    indent = 1 if pretty else None
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(model_input, ensure_ascii=False, indent=indent),
        },
        {
            "role": "assistant",
            "content": json.dumps(output, ensure_ascii=False, indent=indent),
        },
    ]


def format_chat_sample(
    record: Mapping[str, Any], *, pretty: bool = False
) -> dict[str, Any]:
    """纯文本路径：产出 messages + 元信息（不需要 tokenizer）。"""
    messages = build_messages(record, pretty=pretty)
    return {
        "id": record.get("id"),
        "task": record.get("task"),
        "messages": messages,
        "meta": record.get("meta", {}),
    }


def to_prompt_completion(
    record: Mapping[str, Any], *, pretty: bool = False
) -> dict[str, Any]:
    """转成 TRL/其他框架通用的 prompt-completion 形态。"""
    messages = build_messages(record, pretty=pretty)
    system = messages[0]["content"]
    user = messages[1]["content"]
    completion = messages[2]["content"]
    return {
        "id": record.get("id"),
        "task": record.get("task"),
        "prompt": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "completion": [{"role": "assistant", "content": completion}],
        "meta": record.get("meta", {}),
    }


# --------------------------------------------------------------------------
# 2) 掩码计算（纯字符串，可在无 transformers 环境下测试）
# --------------------------------------------------------------------------

@dataclass
class MaskSplit:
    """完整文本与 assistant 段起点的字符偏移。"""

    full_text: str
    completion_start: int
    completion_text: str
    think_end_offset: int | None = None

    @property
    def has_think(self) -> bool:
        return self.think_end_offset is not None


def render_chat_text(
    tokenizer: Any,
    messages: Sequence[Mapping[str, str]],
    *,
    enable_thinking: bool | None = None,
    template_kwargs: Mapping[str, Any] | None = None,
) -> tuple[str, int, str, int | None]:
    """用 chat 模板渲染成完整文本，并定位 assistant 段的字符区间。

    Returns:
        ``(完整文本, completion 起点字符偏移, completion 文本, think 结束偏移或 None)``

    定位策略（由强到弱）：
      1. 在完整文本中查找 ``<|im_start|>assistant\\n`` 之类的 assistant 段起始标记；
      2. 退化为查找 assistant **内容**本身（content 较长时可靠）；
      3. 再退化为查找 content 的前 32 个字符。
    三级都失败时按"整个文本都是 completion"处理并告警 —— 这会让 loss 覆盖
    输入段，属于明显异常，因此同时打 warning 便于发现。
    """
    kwargs = dict(template_kwargs or {})
    if enable_thinking is not None:
        kwargs["enable_thinking"] = enable_thinking

    full_text = _apply_template(tokenizer, list(messages), kwargs)
    content = str(messages[-1]["content"])

    completion_start = _locate_assistant_start(full_text, content)
    think_end: int | None = None

    search_from = completion_start
    close_index = full_text.find(THINK_CLOSE, search_from)
    if close_index >= 0 and full_text.find(THINK_OPEN, search_from, close_index) >= 0:
        think_end = close_index + len(THINK_CLOSE)

    return full_text, completion_start, content, think_end


def _apply_template(
    tokenizer: Any, messages: list[dict[str, str]], kwargs: Mapping[str, Any]
) -> str:
    """调用 apply_chat_template，并在模板不支持额外关键字时优雅降级。"""
    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False, **kwargs
        )
    except TypeError:
        # 该模板/版本不支持 enable_thinking 等关键字
        if "enable_thinking" in kwargs:
            reduced = {k: v for k, v in kwargs.items() if k != "enable_thinking"}
            try:
                return tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=False, **reduced
                )
            except TypeError:
                pass
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )


_ASSISTANT_MARKERS: tuple[str, ...] = (
    "<|im_start|>assistant\n",
    "<|im_start|>assistant",
    "<start_of_turn>model\n",
)


def _locate_assistant_start(full_text: str, content: str) -> int:
    for marker in _ASSISTANT_MARKERS:
        index = full_text.rfind(marker)
        if index >= 0:
            return index + len(marker)
    if content:
        index = full_text.rfind(content)
        if index >= 0:
            return index
        head = content[:32]
        if head:
            index = full_text.rfind(head)
            if index >= 0:
                return index
    LOGGER.warning("无法定位 assistant 段起点，将把整段文本当作 completion")
    return 0


def char_mask(
    full_text: str, completion_start: int, think_end_offset: int | None = None
) -> list[bool]:
    """字符级掩码：``True`` 表示参与 loss。"""
    mask = [False] * len(full_text)
    start = max(0, completion_start)
    if think_end_offset is not None:
        start = max(start, think_end_offset)
    for index in range(start, len(full_text)):
        mask[index] = True
    return mask


def apply_completion_only_labels(
    input_ids: Sequence[int],
    offsets: Sequence[tuple[int, int]],
    full_text: str,
    completion_start: int,
    think_end_offset: int | None = None,
) -> tuple[list[int], int]:
    """按字符掩码生成 labels。

    Args:
        input_ids: token id 序列。
        offsets: 每个 token 对应的 ``(start, end)`` 字符区间（fast tokenizer 提供）。
        full_text: 模板渲染出的完整文本。
        completion_start: assistant 段起点字符偏移。
        think_end_offset: 思维链结束偏移（掩掉它之前的内容）。

    Returns:
        ``(labels, 参与 loss 的 token 数)``
    """
    mask = char_mask(full_text, completion_start, think_end_offset)
    labels: list[int] = []
    supervised = 0
    total_chars = len(mask)
    for token_id, (start, end) in zip(input_ids, offsets):
        keep = _token_is_supervised(int(start), int(end), mask, total_chars)
        if keep:
            labels.append(int(token_id))
            supervised += 1
        else:
            labels.append(IGNORE_INDEX)
    return labels, supervised


def _token_is_supervised(
    start: int, end: int, mask: Sequence[bool], total_chars: int
) -> bool:
    """判断一个 token 是否落在 supervised（参与 loss）区间内。

    * **正常 token**（``end > start``）：只要与 supervised 区间**有重叠**就算监督。
      用重叠而非"完全包含"是必须的 —— completion 的第一个 token 往往同时含
      模板换行与内容首字符，若要求完全包含，它会整段被掩掉，
      模型就学不到 response 的开头（表现为"开头总是漏字"）。
    * **零宽 token**（``end <= start``）：某些 tokenizer 对控制/特殊 token
      给出的 offset 是 ``(n, n)``，没有对应字符。这类 token **一律不监督**：
      它们通常来自提示词侧的模板标记，纳入 loss 会把模板串学进去。
      实测中它们恰好落在 completion 起点，若不排除会引入噪声。
      其余非零宽 token 的覆盖范围不受影响，所以"漏学开头"的风险不存在。
    """
    if total_chars == 0:
        return False
    if end <= start:
        return False
    for position in range(max(0, start), min(total_chars, end)):
        if mask[position]:
            return True
    return False


# --------------------------------------------------------------------------
# 3) 数据集构建（需要 transformers，只有在 train/smoke 时才 import）
# --------------------------------------------------------------------------

@dataclass
class SFTBuildConfig:
    """SFT 构建配置。

    注意 ``pretty_json`` 的默认值必须与
    :class:`~qboss_training.training.config.TrainingConfig` 的 ``pretty_json``
    以及 :func:`~qboss_training.inference.build_inference_messages` 的 ``pretty``
    默认值保持一致。三处任一漂移都会造成"训练与推理序列化不同"，
    表现为训练 loss 正常但评测变差，且极难排查。
    """

    model_name_or_path: str
    max_seq_length: int = 1024
    enable_thinking: bool = False
    pretty_json: bool = DEFAULT_PRETTY_JSON
    #: 生成 prompt 时用的模板参数
    template_kwargs: dict[str, Any] = field(default_factory=dict)
    #: 丢弃超过 max_seq_length 的样本（False 则截断）
    drop_over_length: bool = True
    #: 只保留 completion 长度 >= 该值的样本（防止空监督）
    min_completion_tokens: int = 3


def load_tokenizer(config: SFTBuildConfig, *, trust_remote_code: bool = False) -> Any:
    """加载 tokenizer（需要 transformers）。"""
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        config.model_name_or_path, trust_remote_code=trust_remote_code
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    return tokenizer


def build_tokenized_sample(
    record: Mapping[str, Any],
    tokenizer: Any,
    config: SFTBuildConfig,
) -> dict[str, Any] | None:
    """把一条记录编码成 ``input_ids / labels / attention_mask``。

    Returns:
        dict 或 None（样本被丢弃，原因写入统计）。
    """
    messages = build_messages(record, pretty=config.pretty_json)
    full_text, completion_start, _content, think_end = render_chat_text(
        tokenizer,
        messages,
        enable_thinking=config.enable_thinking,
        template_kwargs=config.template_kwargs,
    )

    encoded = tokenizer(
        full_text,
        add_special_tokens=False,
        return_offsets_mapping=True,
        truncation=False,
    )
    input_ids: list[int] = list(encoded["input_ids"])
    offsets: list[tuple[int, int]] = [tuple(item) for item in encoded["offset_mapping"]]

    total = len(input_ids)
    if total > config.max_seq_length:
        if config.drop_over_length:
            return None
        input_ids = input_ids[: config.max_seq_length]
        offsets = offsets[: config.max_seq_length]

    labels, supervised = apply_completion_only_labels(
        input_ids, offsets, full_text, completion_start, think_end
    )
    if supervised < config.min_completion_tokens:
        return None

    return {
        "id": record.get("id"),
        "task": record.get("task"),
        "input_ids": input_ids,
        "labels": labels,
        "attention_mask": [1] * len(input_ids),
        "supervised_tokens": supervised,
        "think_masked": think_end is not None,
        "total_chars": len(full_text),
        "completion_start_char": completion_start,
    }


def build_sft_dataset(
    records: Sequence[Mapping[str, Any]],
    tokenizer: Any,
    config: SFTBuildConfig,
) -> tuple[list[dict[str, Any]], SFTStats]:
    """批量编码并统计（用于 smoke test 与真实训练前的体检）。"""
    stats = SFTStats(template_used=getattr(tokenizer, "name_or_path", "") or "")
    samples: list[dict[str, Any]] = []

    for record in records:
        sample = build_tokenized_sample(record, tokenizer, config)
        stats.total += 1
        task = str(record.get("task", "?"))
        stats.by_task[task] = stats.by_task.get(task, 0) + 1

        if sample is None:
            # 区分"超长被丢"与"监督过短"，便于定位数据问题
            messages = build_messages(record, pretty=config.pretty_json)
            full_text, _, _, _ = render_chat_text(
                tokenizer,
                messages,
                enable_thinking=config.enable_thinking,
                template_kwargs=config.template_kwargs,
            )
            approx = len(tokenizer(full_text, add_special_tokens=False)["input_ids"])
            if approx > config.max_seq_length:
                stats.over_max_length += 1
                stats.truncated += 1
            continue

        stats.token_lengths.append(len(sample["input_ids"]))
        stats.completion_token_lengths.append(int(sample["supervised_tokens"]))
        stats.masked_ratio_sum += 1.0 - (
            sample["supervised_tokens"] / max(1, len(sample["input_ids"]))
        )
        if sample["think_masked"]:
            stats.think_masked += 1
        samples.append(sample)

    return samples, stats


# --------------------------------------------------------------------------
# 4) JSONL 落盘（无需 ML 库）
# --------------------------------------------------------------------------

def write_sft_jsonl(
    records: Sequence[Mapping[str, Any]],
    path: str,
    *,
    pretty: bool = False,
    mode: str = "messages",
) -> int:
    """写出 SFT JSONL。

    Args:
        mode: ``"messages"``（默认，含 messages 列表）或
              ``"prompt_completion"``（prompt/completion 两段式）。

    Returns:
        写出的行数。
    """
    from ..utils.io import write_jsonl

    rows: list[dict[str, Any]] = []
    for record in records:
        if mode == "messages":
            rows.append(format_chat_sample(record, pretty=pretty))
        elif mode == "prompt_completion":
            rows.append(to_prompt_completion(record, pretty=pretty))
        else:
            raise ValueError(f"未知 mode={mode!r}，可选 messages / prompt_completion")
    write_jsonl(path, rows, sort_by="id")
    return len(rows)


def masking_preview(
    record: Mapping[str, Any],
    tokenizer: Any,
    config: SFTBuildConfig,
) -> str:
    """人类可读的掩码预览：用 █ 标出参与 loss 的字符。

    用于人工确认 completion-only 是否真的只覆盖 assistant 段。
    """
    messages = build_messages(record, pretty=config.pretty_json)
    full_text, completion_start, _content, think_end = render_chat_text(
        tokenizer,
        messages,
        enable_thinking=config.enable_thinking,
        template_kwargs=config.template_kwargs,
    )
    mask = char_mask(full_text, completion_start, think_end)
    marked = "".join("█" if flag else "·" for flag in mask)
    lines = [
        f"记录 {record.get('id')}（task={record.get('task')}）",
        f"completion 起点字符偏移：{completion_start}",
        f"think 掩码：{'是' if think_end is not None else '否'}",
        "",
        "文本（█ = 参与 loss，· = 掩码）：",
        full_text,
        marked,
    ]
    return "\n".join(lines)
