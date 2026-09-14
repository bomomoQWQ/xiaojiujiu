"""从模型回复中稳健地抽取 JSON。

小模型 + 本地推理常见的脏输出形态：
  * 前后有解释性文字（"好的，评价如下："）;
  * 被 ```json ... ``` 包裹；
  * 尾随逗号 `{"a":1,}`；
  * 中文全角引号 “” 或单引号；
  * 一次吐了多个 JSON，只有其中一个符合 schema。

本模块只做"抽取 + 修复"，不做语义判断；语义校验由 validator 负责。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterator

from ..errors import JsonExtractionError

_FENCE_RE = re.compile(
    r"```(?:json|JSON|Json)?\s*(?P<body>.*?)(?:```|$)", re.DOTALL
)

# 全角引号 / 全角括号 / 中文标点造成的常见非法 JSON
_TRANSLATE_TABLE = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "｛": "{",
        "｝": "}",
        "［": "[",
        "］": "]",
        "：": ":",
        "，": ",",
        "\u3000": " ",
    }
)

_TRAILING_COMMA_RE = re.compile(r",\s*(?P<close>[}\]])")


def extract_json(
    text: str,
    *,
    validate: Callable[[Any], bool] | None = None,
    prefer_object: bool = True,
) -> Any:
    """从 ``text`` 中抽取第一个可解析（且可选满足 ``validate``）的 JSON 值。

    Args:
        text: 模型原始回复。
        validate: 可选判定函数；返回 True 的候选才会被接受。
        prefer_object: 只接受 JSON object（本工程两个任务都是 object）。

    Raises:
        JsonExtractionError: 所有策略都失败。
    """
    if text is None:
        raise JsonExtractionError("回复为空")
    raw = str(text).strip()
    if not raw:
        raise JsonExtractionError("回复为空")

    errors: list[str] = []
    for candidate in _iter_candidates(raw):
        if prefer_object and not isinstance(candidate, dict):
            continue
        if validate is not None and not validate(candidate):
            continue
        return candidate

    # 全部候选都不合格时，给出最有信息量的报错
    raise JsonExtractionError(
        "无法从回复中抽取合法 JSON"
        + (f"；候选均未通过校验" if validate is not None else "")
        + (f"；解析错误：{errors[0]}" if errors else "")
        + f"；原文片段：{raw[:200]!r}"
    )


def _iter_candidates(raw: str) -> Iterator[Any]:
    """按可信度从高到低产出候选 JSON 值。"""
    seen: set[str] = set()

    def _emit(payload: str) -> Any:
        key = payload.strip()
        if not key or key in seen:
            return _SKIP
        seen.add(key)
        for variant in _variants(payload):
            try:
                return json.loads(variant)
            except (json.JSONDecodeError, ValueError):
                continue
        return _SKIP

    # 1) 整体就是 JSON
    yield from _yield(_emit(raw))

    # 2) fenced code block
    for match in _FENCE_RE.finditer(raw):
        yield from _yield(_emit(match.group("body")))

    # 3) 花括号平衡扫描
    for payload in _balanced_objects(raw):
        yield from _yield(_emit(payload))


class _Skip:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<skip>"


_SKIP = _Skip()


def _yield(value: Any) -> Iterator[Any]:
    if value is not _SKIP:
        yield value


def _variants(payload: str) -> Iterator[str]:
    """产出原文以及若干修复后的变体。"""
    yield payload
    fixed = payload.translate(_TRANSLATE_TABLE)
    if fixed != payload:
        yield fixed
    for source in (payload, fixed):
        repaired = _TRAILING_COMMA_RE.sub(lambda m: m.group("close"), source)
        if repaired != source:
            yield repaired


def _balanced_objects(text: str) -> Iterator[str]:
    """扫描出所有顶层花括号平衡片段（正确处理字符串与转义）。"""
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "{":
            index += 1
            continue
        depth = 0
        in_string = False
        escaped = False
        for cursor in range(index, length):
            char = text[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[index : cursor + 1]
                    index = cursor + 1
                    break
        else:
            # 未闭合，跳到下一个起点
            index += 1


def dumps_canonical(value: Any) -> str:
    """稳定序列化：用于哈希、去重与落盘。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
