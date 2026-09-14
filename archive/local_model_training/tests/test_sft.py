"""SFT 构建测试：消息形态、completion-only 掩码、思维链掩码。

掩码是本工程最容易出错也最贵的地方：如果掩码错了，
训练照样跑完、loss 也照样下降，但模型学到的是"复述输入"。
因此这里用**假 tokenizer**（字符级，不依赖 transformers）精确验证
哪些 token 参与 loss。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest

from qboss_training.contracts import get_contract
from qboss_training.sft.format import (
    IGNORE_INDEX,
    SFTBuildConfig,
    apply_completion_only_labels,
    build_messages,
    char_mask,
    format_chat_sample,
    format_chat_sample as _format,
    masking_preview,
    render_chat_text,
    to_prompt_completion,
    write_sft_jsonl,
)
from qboss_training.utils.io import read_jsonl


# --------------------------------------------------------------------------
# 假 tokenizer：字符级，便于精确断言掩码
# --------------------------------------------------------------------------

class FakeTokenizer:
    """字符级 tokenizer，模拟 Qwen 的 chat 模板。

    每个字符 = 一个 token，offset_mapping 就是字符区间，
    因此可以精确验证"哪些字符参与了 loss"。
    """

    name_or_path = "fake-char-tokenizer"
    pad_token = "<pad>"
    eos_token = "<eos>"
    pad_token_id = 0
    eos_token_id = 1

    def __init__(self, *, support_enable_thinking: bool = True) -> None:
        self.support_enable_thinking = support_enable_thinking
        self.template_calls: list[dict[str, Any]] = []

    def apply_chat_template(
        self,
        messages: Sequence[dict[str, str]],
        *,
        tokenize: bool = False,
        add_generation_prompt: bool = False,
        **kwargs: Any,
    ) -> str:
        if kwargs and not self.support_enable_thinking and "enable_thinking" in kwargs:
            raise TypeError("unexpected keyword argument 'enable_thinking'")
        self.template_calls.append(
            {"messages": list(messages), "kwargs": dict(kwargs), "generation": add_generation_prompt}
        )
        parts: list[str] = []
        for message in messages:
            parts.append(f"<|im_start|>{message['role']}\n{message['content']}<|im_end|>\n")
        if add_generation_prompt:
            parts.append("<|im_start|>assistant\n")
        return "".join(parts)

    def __call__(
        self,
        text: str,
        *,
        add_special_tokens: bool = False,
        return_offsets_mapping: bool = False,
        truncation: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"input_ids": list(range(len(text)))}
        if return_offsets_mapping:
            result["offset_mapping"] = [(index, index + 1) for index in range(len(text))]
        return result


@pytest.fixture
def tokenizer() -> FakeTokenizer:
    return FakeTokenizer()


# --------------------------------------------------------------------------
# 消息构造
# --------------------------------------------------------------------------

class TestBuildMessages:
    def test_three_roles_in_order(self, sample_event_eval: dict[str, Any]) -> None:
        messages = build_messages(sample_event_eval)
        assert [item["role"] for item in messages] == ["system", "user", "assistant"]

    def test_system_prompt_matches_contract(self, sample_event_eval: dict[str, Any]) -> None:
        messages = build_messages(sample_event_eval)
        assert messages[0]["content"] == get_contract("event_eval").system_prompt

    def test_user_content_is_input_json(self, sample_event_eval: dict[str, Any]) -> None:
        import json

        messages = build_messages(sample_event_eval, pretty=False)
        assert json.loads(messages[1]["content"]) == sample_event_eval["input"]

    def test_assistant_content_is_output_json(self, sample_event_eval: dict[str, Any]) -> None:
        import json

        messages = build_messages(sample_event_eval, pretty=False)
        assert json.loads(messages[2]["content"]) == sample_event_eval["output"]

    def test_pretty_flag_changes_serialization(self, sample_event_eval: dict[str, Any]) -> None:
        compact = build_messages(sample_event_eval, pretty=False)[1]["content"]
        pretty = build_messages(sample_event_eval, pretty=True)[1]["content"]
        assert "\n" not in compact
        assert "\n" in pretty

    def test_custom_system_prompt_overrides(self, sample_event_eval: dict[str, Any]) -> None:
        messages = build_messages(sample_event_eval, system_prompt="自定义")
        assert messages[0]["content"] == "自定义"

    def test_missing_output_raises(self, sample_event_eval: dict[str, Any]) -> None:
        broken = {key: value for key, value in sample_event_eval.items() if key != "output"}
        with pytest.raises(ValueError, match="缺少"):
            build_messages(broken)

    def test_unknown_task_raises(self) -> None:
        with pytest.raises(KeyError):
            build_messages({"task": "nope", "input": {}, "output": {}})

    def test_emotion_explain_uses_its_own_prompt(
        self, sample_emotion_explain: dict[str, Any]
    ) -> None:
        messages = build_messages(sample_emotion_explain)
        assert messages[0]["content"] == get_contract("emotion_explain").system_prompt
        assert "情绪解释器" in messages[0]["content"]


class TestOtherFormats:
    def test_format_chat_sample_shape(self, sample_event_eval: dict[str, Any]) -> None:
        payload = format_chat_sample(sample_event_eval)
        assert set(payload) == {"id", "task", "messages", "meta"}
        assert payload["id"] == sample_event_eval["id"]

    def test_prompt_completion_splits_correctly(
        self, sample_event_eval: dict[str, Any]
    ) -> None:
        payload = to_prompt_completion(sample_event_eval)
        assert len(payload["prompt"]) == 2
        assert payload["prompt"][0]["role"] == "system"
        assert payload["prompt"][1]["role"] == "user"
        assert payload["completion"][0]["role"] == "assistant"


# --------------------------------------------------------------------------
# 掩码
# --------------------------------------------------------------------------

class TestRenderChatText:
    def test_locates_assistant_segment(self, tokenizer: FakeTokenizer, sample_event_eval: dict) -> None:
        messages = build_messages(sample_event_eval)
        full_text, start, content, think_end = render_chat_text(tokenizer, messages)
        assert full_text[start : start + len(content)] == content
        assert think_end is None

    def test_detects_think_segment(self, tokenizer: FakeTokenizer) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": " thinking先想一下<｜end▁of▁thinking｜>{\"a\":1}"},
        ]
        full_text, start, _content, think_end = render_chat_text(tokenizer, messages)
        assert think_end is not None
        assert full_text[think_end:].startswith('{"a":1}')

    def test_enable_thinking_is_passed(self, tokenizer: FakeTokenizer, sample_event_eval: dict) -> None:
        render_chat_text(tokenizer, build_messages(sample_event_eval), enable_thinking=False)
        assert tokenizer.template_calls[-1]["kwargs"]["enable_thinking"] is False

    def test_falls_back_when_template_rejects_kwarg(self, sample_event_eval: dict) -> None:
        """不支持 enable_thinking 的模板应优雅降级，而不是崩掉。"""
        tokenizer = FakeTokenizer(support_enable_thinking=False)
        _full, start, content, _ = render_chat_text(
            tokenizer, build_messages(sample_event_eval), enable_thinking=False
        )
        assert content

    def test_degrades_to_content_search_without_markers(self, sample_event_eval: dict) -> None:
        class PlainTokenizer(FakeTokenizer):
            def apply_chat_template(self, messages, **kwargs):  # type: ignore[override]
                return "\n".join(item["content"] for item in messages)

        tokenizer = PlainTokenizer()
        messages = build_messages(sample_event_eval)
        full_text, start, content, _ = render_chat_text(tokenizer, messages)
        assert full_text[start : start + len(content)] == content


class TestCharMask:
    def test_masks_prefix_only(self) -> None:
        mask = char_mask("AAAA BBBB", 5)
        assert mask == [False] * 5 + [True] * 4

    def test_think_offset_takes_precedence(self) -> None:
        text = "AAAA BBBB CCCC"  # 共 14 字符
        mask = char_mask(text, 5, think_end_offset=10)
        assert mask[:10] == [False] * 10
        assert mask[10:] == [True] * (len(text) - 10)

    def test_start_beyond_length_is_safe(self) -> None:
        assert char_mask("AB", 99) == [False, False]

    def test_negative_start_is_clamped(self) -> None:
        assert char_mask("AB", -5) == [True, True]


class TestApplyCompletionOnlyLabels:
    def test_supervised_tokens_match_completion(self, tokenizer: FakeTokenizer) -> None:
        full_text = "PROMPT_HERE_TAIL"
        completion_start = len("PROMPT_HERE_")
        input_ids = list(range(len(full_text)))
        offsets = [(index, index + 1) for index in range(len(full_text))]

        labels, supervised = apply_completion_only_labels(
            input_ids, offsets, full_text, completion_start
        )
        assert supervised == len("TAIL")
        # 前缀全部掩码
        assert labels[:completion_start] == [IGNORE_INDEX] * completion_start
        # 后缀保留原 token id
        assert labels[completion_start:] == input_ids[completion_start:]

    def test_think_prefix_is_masked(self, tokenizer: FakeTokenizer) -> None:
        full_text = "ABC  thinkingx<｜end▁of▁thinking｜>JSON"
        think_end = full_text.index("JSON")
        labels, supervised = apply_completion_only_labels(
            list(range(len(full_text))),
            [(index, index + 1) for index in range(len(full_text))],
            full_text,
            3,
            think_end_offset=think_end,
        )
        assert supervised == len("JSON")
        assert labels[:think_end] == [IGNORE_INDEX] * think_end

    def test_spans_overlapping_boundary_are_kept(self) -> None:
        """跨边界的 token 应算 supervised，否则 completion 首 token 会被整段掩掉。"""
        full_text = "ABCD"
        # token 2 覆盖字符 [1,3)，其中包含 supervised 的字符 2
        offsets = [(0, 1), (1, 2), (1, 3), (3, 4)]
        labels, supervised = apply_completion_only_labels(
            [10, 11, 12, 13], offsets, full_text, completion_start=2
        )
        assert labels == [IGNORE_INDEX, IGNORE_INDEX, 12, 13]
        assert supervised == 2

    def test_zero_width_offset_is_not_supervised(self) -> None:
        """零宽 offset 的控制 token 不参与 loss，相邻真实 token 正常参与。"""
        full_text = "ABCD"
        offsets = [(0, 1), (2, 2), (2, 3), (3, 4)]
        labels, supervised = apply_completion_only_labels(
            [10, 11, 12, 13], offsets, full_text, completion_start=2
        )
        assert labels == [IGNORE_INDEX, IGNORE_INDEX, 12, 13]
        assert supervised == 2

    def test_completion_first_token_is_supervised_when_text_starts_after_boundary(
        self,
    ) -> None:
        """起点之后的第一个真实 token 必须被监督，否则开头学不到。"""
        full_text = "<|im_start|>assistant\n{}"
        completion_start = full_text.index("{")
        offsets = [(index, index + 1) for index in range(len(full_text))]
        labels, supervised = apply_completion_only_labels(
            list(range(len(full_text))), offsets, full_text, completion_start
        )
        assert labels[completion_start] != IGNORE_INDEX
        assert supervised == 2


class TestMaskingPreview:
    def test_marks_supervised_and_masked(self, tokenizer: FakeTokenizer, sample_event_eval: dict) -> None:
        config = SFTBuildConfig(model_name_or_path="fake")
        text = masking_preview(sample_event_eval, tokenizer, config)
        assert "█" in text
        assert "·" in text
        # system prompt 段必须被掩掉
        assert "你是角色 Runtime 的事件评价器" in text


class TestWriteSftJsonl:
    def test_writes_sorted_jsonl(self, tmp_path: Path, small_fixture_records: list[dict]) -> None:
        target = tmp_path / "train.jsonl"
        count = write_sft_jsonl(small_fixture_records, str(target))
        assert count == len(small_fixture_records)
        rows = read_jsonl(target)
        assert [row["id"] for row in rows] == sorted(
            row["id"] for row in rows
        )
        assert all(len(row["messages"]) == 3 for row in rows)

    def test_prompt_completion_mode(self, tmp_path: Path, sample_event_eval: dict) -> None:
        target = tmp_path / "pc.jsonl"
        write_sft_jsonl([sample_event_eval], str(target), mode="prompt_completion")
        row = read_jsonl(target)[0]
        assert "prompt" in row and "completion" in row
        assert "messages" not in row

    def test_unknown_mode_raises(self, tmp_path: Path, sample_event_eval: dict) -> None:
        with pytest.raises(ValueError, match="未知 mode"):
            write_sft_jsonl(
                [sample_event_eval], str(tmp_path / "x.jsonl"), mode="nope"
            )

    def test_roundtrip_preserves_output(self, tmp_path: Path, sample_event_eval: dict) -> None:
        import json

        target = tmp_path / "t.jsonl"
        write_sft_jsonl([sample_event_eval], str(target), pretty=True)
        row = read_jsonl(target)[0]
        assistant = row["messages"][2]["content"]
        assert json.loads(assistant) == sample_event_eval["output"]


# --------------------------------------------------------------------------
# 需要 tokenizer 的批量构建（用假 tokenizer 也能跑）
# --------------------------------------------------------------------------

class TestBuildSftDataset:
    def test_builds_samples_and_stats(self, small_fixture_records: list[dict]) -> None:
        from qboss_training.sft.format import build_sft_dataset

        tokenizer = FakeTokenizer()
        config = SFTBuildConfig(model_name_or_path="fake", max_seq_length=8192)
        samples, stats = build_sft_dataset(small_fixture_records, tokenizer, config)

        # 小夹具里整段（system+user+assistant）很短，8192 下不应丢失
        assert len(samples) > 0
        assert stats.total == len(small_fixture_records)
        for sample in samples:
            assert len(sample["input_ids"]) == len(sample["labels"])
            assert len(sample["attention_mask"]) == len(sample["input_ids"])
            assert sample["supervised_tokens"] >= 3

    def test_drops_over_length_samples(self, small_fixture_records: list[dict]) -> None:
        from qboss_training.sft.format import build_sft_dataset

        tokenizer = FakeTokenizer()
        config = SFTBuildConfig(model_name_or_path="fake", max_seq_length=10)
        samples, stats = build_sft_dataset(small_fixture_records, tokenizer, config)
        assert samples == []
        assert stats.over_max_length == len(small_fixture_records)

    def test_stats_report_masked_ratio(self, small_fixture_records: list[dict]) -> None:
        from qboss_training.sft.format import build_sft_dataset

        config = SFTBuildConfig(model_name_or_path="fake", max_seq_length=8192)
        samples, stats = build_sft_dataset(
            small_fixture_records, FakeTokenizer(), config
        )
        payload = stats.to_dict()
        # 输入段远长于输出段，掩码比例应该比较高
        assert payload["mean_masked_ratio"] > 0.3
        assert payload["mean_completion_tokens"] > 0

    def test_labels_only_supervise_assistant_text(
        self, sample_event_eval: dict
    ) -> None:
        """核心断言：system/user 的字符一个都不能参与 loss。"""
        from qboss_training.sft.format import build_sft_dataset

        tokenizer = FakeTokenizer()
        config = SFTBuildConfig(model_name_or_path="fake", max_seq_length=8192)
        samples, _ = build_sft_dataset([sample_event_eval], tokenizer, config)
        sample = samples[0]

        messages = build_messages(sample_event_eval)
        full_text, start, content, _ = render_chat_text(tokenizer, messages)

        # 找出所有 supervised 的字符区间
        supervised = {
            index for index, label in enumerate(sample["labels"]) if label != IGNORE_INDEX
        }
        assert supervised, "必须至少有监督 token"
        # 监督范围必须落在 assistant 内容之内（允许含紧跟的模板尾标记）
        assert min(supervised) >= start
        # system prompt 的每个字符都不得被监督
        system_prefix_len = len(f"<|im_start|>system\n{messages[0]['content']}<|im_end|>\n")
        assert not (supervised & set(range(system_prefix_len)))

    def test_deterministic_output(self, sample_event_eval: dict) -> None:
        from qboss_training.sft.format import build_sft_dataset

        config = SFTBuildConfig(model_name_or_path="fake", max_seq_length=8192)
        first, _ = build_sft_dataset([sample_event_eval], FakeTokenizer(), config)
        second, _ = build_sft_dataset([sample_event_eval], FakeTokenizer(), config)
        assert first[0]["labels"] == second[0]["labels"]
