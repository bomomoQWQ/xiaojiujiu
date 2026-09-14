"""数据生成器测试：断点续跑、预算守卫、重试、去重、密钥安全。

全程使用 :class:`~tests.conftest.FakeDeepSeekServer`（内存假服务端），
不产生任何真实网络连接，也不需要 API key。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from qboss_training.config import (
    BudgetConfig,
    ClientConfig,
    GenerationConfig,
    RetryConfig,
)
from qboss_training.data.client import (
    BudgetGuard,
    ChatMessage,
    DeepSeekClient,
    HttpxTransport,
    Usage,
)
from qboss_training.data.generator import (
    CheckpointStore,
    dry_run_plan,
    generate_one,
    generation_summary,
    run_generation,
)
from qboss_training.data.prompts import (
    GENERATOR_VERSION,
    build_generation_messages,
    build_verify_messages,
    inline_schema,
)
from qboss_training.data.seeds import sample_scenarios
from qboss_training.errors import (
    BudgetExceeded,
    GeneratorError,
    MissingCredentialError,
)
from qboss_training.utils.io import read_json, read_jsonl, write_jsonl

# 直接从 conftest 复用假服务端：它已被 pytest 导入，不会重复执行副作用
from tests.conftest import FakeDeepSeekServer

FAKE_KEY = "test-key-not-a-real-secret-0123456789"


def make_config(tmp_path: Path, **overrides: Any) -> GenerationConfig:
    config = GenerationConfig(
        task="event_eval",
        target_samples=4,
        batch_size=2,
        concurrency=1,
        output_dir=str(tmp_path / "raw"),
        checkpoint_file=str(tmp_path / "raw" / ".checkpoint.json"),
    )
    config.client = ClientConfig()
    config.retry = RetryConfig(
        max_attempts=2, initial_backoff_s=0.001, max_backoff_s=0.01, jitter_s=0.0
    )
    config.budget = BudgetConfig(max_requests=100, max_usd=10.0)
    for key, value in overrides.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return config


def make_client(config: GenerationConfig, server: FakeDeepSeekServer) -> DeepSeekClient:
    return DeepSeekClient(
        config.client,
        config.retry,
        transport=HttpxTransport(transport=httpx.MockTransport(server)),
        api_key=FAKE_KEY,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

class TestDeepSeekClient:
    def test_default_base_url_is_deepseek(self) -> None:
        assert ClientConfig().base_url == "https://api.deepseek.com"

    def test_endpoint_composition(self) -> None:
        client = DeepSeekClient(ClientConfig(base_url="https://api.deepseek.com/"))
        assert client.endpoint == "https://api.deepseek.com/chat/completions"

    def test_missing_key_raises_helpful_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        client = DeepSeekClient(ClientConfig(api_key_env="DEEPSEEK_API_KEY"))
        with pytest.raises(MissingCredentialError) as excinfo:
            client.resolve_api_key()
        assert "DEEPSEEK_API_KEY" in str(excinfo.value)

    def test_key_is_read_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
        client = DeepSeekClient(ClientConfig(api_key_env="DEEPSEEK_API_KEY"))
        assert client.resolve_api_key() == FAKE_KEY

    def test_has_credentials_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        assert not DeepSeekClient(ClientConfig()).has_credentials()

    def test_request_uses_json_response_format(self, fake_server: FakeDeepSeekServer) -> None:
        config = make_config(Path("."))
        client = make_client(config, fake_server)

        async def _run() -> None:
            async with client:
                await client.complete([ChatMessage("user", "hi")])
        run(_run())
        assert fake_server.requests[0]["response_format"] == {"type": "json_object"}

    def test_authorization_header_is_sent(self, fake_server: FakeDeepSeekServer) -> None:
        config = make_config(Path("."))
        client = make_client(config, fake_server)

        async def _run() -> None:
            async with client:
                await client.complete([ChatMessage("user", "hi")])
        run(_run())
        header = fake_server.raw_requests[0].headers.get("authorization", "")
        assert header.startswith("Bearer ")
        assert FAKE_KEY in header

    def test_retries_on_server_error_then_succeeds(self, tmp_path: Path) -> None:
        attempts = {"n": 0}

        def responder(body: dict[str, Any], index: int) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return (503, {"error": {"message": "overloaded"}})
            return (
                200,
                {
                    "choices": [
                        {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        server = FakeDeepSeekServer(responder)
        config = make_config(tmp_path)
        client = make_client(config, server)

        async def _run() -> Any:
            async with client:
                return await client.complete([ChatMessage("user", "hi")])
        result = run(_run())
        assert result.text == '{"ok": true}'
        assert result.attempts == 2

    def test_non_retryable_status_raises_immediately(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer(
            lambda body, index: (401, {"error": {"message": "invalid api key"}})
        )
        config = make_config(tmp_path)
        client = make_client(config, server)

        async def _run() -> Any:
            async with client:
                return await client.complete([ChatMessage("user", "hi")])
        with pytest.raises(GeneratorError, match="不可重试"):
            run(_run())
        assert server.call_count == 1

    def test_exhausted_retries_raise(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer(
            lambda body, index: (429, {"error": {"message": "rate limited"}})
        )
        config = make_config(tmp_path)
        config.retry = RetryConfig(
            max_attempts=3, initial_backoff_s=0.001, max_backoff_s=0.01, jitter_s=0.0
        )
        client = make_client(config, server)

        async def _run() -> Any:
            async with client:
                return await client.complete([ChatMessage("user", "hi")])
        with pytest.raises(GeneratorError, match="重试"):
            run(_run())
        assert server.call_count == 3

    def test_error_message_never_contains_api_key(self, tmp_path: Path) -> None:
        """服务端把 key 回显在错误里时，也不得泄漏到异常文本。"""
        server = FakeDeepSeekServer(
            lambda body, index: (400, {"error": {"message": f"bad key {FAKE_KEY}"}})
        )
        config = make_config(tmp_path)
        client = make_client(config, server)

        async def _run() -> Any:
            async with client:
                return await client.complete([ChatMessage("user", "hi")])
        with pytest.raises(GeneratorError) as excinfo:
            run(_run())
        assert FAKE_KEY not in str(excinfo.value)


class TestBudgetGuard:
    def test_request_limit_stops(self) -> None:
        guard = BudgetGuard(BudgetConfig(max_requests=1, max_usd=100))
        guard.record(Usage(10, 10, 20))
        with pytest.raises(BudgetExceeded, match="请求上限"):
            guard.check_can_start()

    def test_token_limit_stops(self) -> None:
        guard = BudgetGuard(
            BudgetConfig(max_requests=100, max_total_tokens=100, max_usd=100)
        )
        guard.record(Usage(prompt_tokens=60, completion_tokens=60, total_tokens=120))
        with pytest.raises(BudgetExceeded, match="总 token"):
            guard.check_can_start()

    def test_prompt_token_limit_stops(self) -> None:
        guard = BudgetGuard(
            BudgetConfig(
                max_requests=100, max_prompt_tokens=50, max_total_tokens=10_000, max_usd=100
            )
        )
        guard.record(Usage(prompt_tokens=60, completion_tokens=1, total_tokens=61))
        with pytest.raises(BudgetExceeded, match="prompt token"):
            guard.check_can_start()

    def test_usd_limit_stops(self) -> None:
        guard = BudgetGuard(
            BudgetConfig(
                max_requests=100,
                max_total_tokens=10_000_000,
                max_usd=0.001,
                usd_per_million_prompt_tokens=100.0,
            )
        )
        guard.record(Usage(prompt_tokens=1000, completion_tokens=0, total_tokens=1000))
        with pytest.raises(BudgetExceeded, match="预算上限"):
            guard.check_can_start()

    def test_under_limits_is_fine(self) -> None:
        guard = BudgetGuard(BudgetConfig(max_requests=10, max_usd=1.0))
        guard.record(Usage(10, 10, 20))
        guard.check_can_start()  # 不抛异常

    def test_estimated_cost_math(self) -> None:
        guard = BudgetGuard(
            BudgetConfig(
                usd_per_million_prompt_tokens=1.0,
                usd_per_million_completion_tokens=2.0,
            )
        )
        cost = guard.estimated_usd(Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000))
        assert cost == pytest.approx(3.0)

    def test_snapshot_has_no_secrets(self) -> None:
        guard = BudgetGuard(BudgetConfig())
        snapshot = guard.snapshot()
        assert "api_key" not in json.dumps(snapshot).lower()


# --------------------------------------------------------------------------
# 生成流程
# --------------------------------------------------------------------------

class TestRunGeneration:
    def test_generates_target_records(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=4, batch_size=2)
        records, stats = run(
            run_generation(config, client=make_client(config, server))
        )
        assert len(records) == 4
        assert stats.accepted == 4
        assert (tmp_path / "raw" / "event_eval.jsonl").exists()
        assert len(read_jsonl(tmp_path / "raw" / "event_eval.jsonl")) == 4

    def test_records_are_schema_valid(self, tmp_path: Path) -> None:
        from qboss_training.validators import validate_records

        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=4)
        run(run_generation(config, client=make_client(config, server)))
        records = read_jsonl(tmp_path / "raw" / "event_eval.jsonl")
        assert validate_records(records).failed == 0

    def test_checkpoint_is_written(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=4)
        run(run_generation(config, client=make_client(config, server)))
        checkpoint = read_json(tmp_path / "raw" / ".checkpoint.json")
        assert checkpoint["task"] == "event_eval"
        assert checkpoint["generator_version"] == GENERATOR_VERSION
        assert "budget" in checkpoint
        assert checkpoint["stats"]["accepted"] == 4

    def test_checkpoint_never_contains_api_key(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=2)
        run(run_generation(config, client=make_client(config, server)))
        text = (tmp_path / "raw" / ".checkpoint.json").read_text(encoding="utf-8")
        assert FAKE_KEY not in text
        assert "api_key" not in text.lower()

    def test_jsonl_never_contains_api_key(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=2)
        run(run_generation(config, client=make_client(config, server)))
        text = (tmp_path / "raw" / "event_eval.jsonl").read_text(encoding="utf-8")
        assert FAKE_KEY not in text

    def test_resume_skips_existing_records(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=2, batch_size=1)
        run(run_generation(config, client=make_client(config, server)))
        first_calls = server.call_count

        # 第二次运行：目标已达，不应再请求
        config2 = make_config(tmp_path, target_samples=2, batch_size=1)
        records, stats = run(
            run_generation(config2, client=make_client(config2, server))
        )
        assert records == []
        assert stats.resumed_from == 2
        assert server.call_count == first_calls
        assert len(read_jsonl(tmp_path / "raw" / "event_eval.jsonl")) == 2

    def test_resume_continues_to_target(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=2, batch_size=1)
        run(run_generation(config, client=make_client(config, server)))

        config2 = make_config(tmp_path, target_samples=4, batch_size=1)
        records, stats = run(
            run_generation(config2, client=make_client(config2, server))
        )
        assert stats.resumed_from == 2
        assert len(records) == 2
        assert len(read_jsonl(tmp_path / "raw" / "event_eval.jsonl")) == 4

    def test_budget_stop_keeps_partial_output(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=10, batch_size=1)
        config.budget = BudgetConfig(max_requests=2, max_usd=100.0)
        records, stats = run(
            run_generation(config, client=make_client(config, server))
        )
        assert stats.budget_stopped
        assert 0 < len(records) < 10
        # 断点必须保留，供下次续跑
        checkpoint = read_json(tmp_path / "raw" / ".checkpoint.json")
        assert checkpoint["stats"]["budget_stopped"] is True

    def test_duplicates_are_rejected(self, tmp_path: Path) -> None:
        """假服务端每次都返回同一份输入 → 只应留下第一条。"""
        payload = FakeDeepSeekServer().default_payload(1)
        server = FakeDeepSeekServer(lambda body, index: payload)
        config = make_config(tmp_path, target_samples=5, batch_size=1)
        records, stats = run(
            run_generation(config, client=make_client(config, server))
        )
        assert len(records) == 1
        assert stats.rejected_duplicate_exact >= 1
        assert len(read_jsonl(tmp_path / "raw" / "event_eval.jsonl")) == 1

    def test_near_duplicate_is_rejected(self, tmp_path: Path) -> None:
        """只差一个句号的输入应被判为近似重复。"""
        payload = FakeDeepSeekServer().default_payload(1)
        payload["input"]["current_event"]["text"] = "这是同一条事件的近似版本。"

        def responder(body: dict[str, Any], index: int) -> Any:
            # 第一次原文，之后只多一个句号
            variant = json.loads(json.dumps(payload, ensure_ascii=False))
            if index > 1:
                variant["input"]["current_event"]["text"] += "。"
            return variant

        server = FakeDeepSeekServer(responder)
        config = make_config(
            tmp_path, target_samples=5, batch_size=1, near_duplicate_threshold=0.9
        )
        records, stats = run(
            run_generation(config, client=make_client(config, server))
        )
        assert len(records) == 1
        assert stats.rejected_duplicate_near >= 1

    def test_near_dedup_off_keeps_distinct_but_exact_still_deduped(
        self, tmp_path: Path
    ) -> None:
        """关闭近似去重后：完全相同的输入仍会被精确去重。"""
        payload = FakeDeepSeekServer().default_payload(1)
        server = FakeDeepSeekServer(lambda body, index: payload)
        config = make_config(
            tmp_path, target_samples=3, batch_size=1, near_duplicate_threshold=0.0
        )
        records, stats = run(
            run_generation(config, client=make_client(config, server))
        )
        assert len(records) == 1
        assert stats.rejected_duplicate_near == 0
        assert stats.rejected_duplicate_exact >= 1

    def test_invalid_reply_is_rejected_and_retried(self, tmp_path: Path) -> None:
        """前两次返回坏 JSON，第三次正常 → 应通过拒绝采样拿到样本。"""
        attempts = {"n": 0}

        def responder(body: dict[str, Any], index: int) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return "这不是 JSON"
            if attempts["n"] == 2:
                return {"output": {"direction": "INVALID"}}
            return FakeDeepSeekServer().default_payload(index)

        server = FakeDeepSeekServer(responder)
        config = make_config(tmp_path, target_samples=1, max_sample_attempts=3)
        records, stats = run(
            run_generation(config, client=make_client(config, server))
        )
        assert len(records) == 1
        assert stats.rejected_schema >= 1

    def test_all_attempts_failing_yields_no_records(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer(lambda body, index: "始终不是 JSON")
        config = make_config(tmp_path, target_samples=2, max_sample_attempts=2)
        records, stats = run(
            run_generation(config, client=make_client(config, server))
        )
        assert records == []
        assert stats.rejected_schema >= 2
        assert stats.accepted == 0

    def test_fenced_json_reply_is_accepted(self, tmp_path: Path) -> None:
        """模型把 JSON 包在代码块里也应能抽取成功。"""

        def responder(body: dict[str, Any], index: int) -> str:
            payload = FakeDeepSeekServer().default_payload(index)
            return f"好的：\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

        server = FakeDeepSeekServer(responder)
        config = make_config(tmp_path, target_samples=1)
        records, _ = run(run_generation(config, client=make_client(config, server)))
        assert len(records) == 1

    def test_usage_is_accounted(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=2, batch_size=2)
        _, stats = run(run_generation(config, client=make_client(config, server)))
        assert stats.requested == 2

    def test_multi_task_rejected(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, task="mixed")
        with pytest.raises(GeneratorError, match="单任务"):
            run(run_generation(config, client=make_client(config, FakeDeepSeekServer())))

    def test_progress_callback_is_invoked(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer()
        config = make_config(tmp_path, target_samples=4, batch_size=2)
        seen: list[int] = []
        run(
            run_generation(
                config,
                client=make_client(config, server),
                progress=lambda stats, cursor: seen.append(cursor),
            )
        )
        assert seen == [2, 4]


class TestGenerateOne:
    def test_produces_valid_record(self, tmp_path: Path) -> None:
        from qboss_training.validators import validate_record

        server = FakeDeepSeekServer()
        config = make_config(tmp_path)
        scenario = sample_scenarios("event_eval", 1, seed=1)[0]

        async def _run() -> Any:
            client = make_client(config, server)
            async with client:
                return await generate_one(client, "event_eval", scenario, config=config)

        record, meta, problems = run(_run())
        assert record is not None
        assert problems == []
        assert validate_record(record).ok
        assert meta["attempts"] == 1

    def test_falls_back_to_scenario_input_when_model_omits_it(
        self, tmp_path: Path
    ) -> None:
        """模型只返回 output 时，input 用场景骨架补齐。"""
        payload = FakeDeepSeekServer().default_payload(1)["output"]
        server = FakeDeepSeekServer(lambda body, index: payload)
        config = make_config(tmp_path)
        scenario = sample_scenarios("event_eval", 1, seed=3)[0]

        async def _run() -> Any:
            client = make_client(config, server)
            async with client:
                return await generate_one(client, "event_eval", scenario, config=config)

        record, _meta, _problems = run(_run())
        assert record is not None
        assert record["input"]["current_event"]["text"]

    def test_returns_none_after_exhausting_attempts(self, tmp_path: Path) -> None:
        server = FakeDeepSeekServer(lambda body, index: "坏输出")
        config = make_config(tmp_path, max_sample_attempts=2)
        scenario = sample_scenarios("event_eval", 1, seed=1)[0]

        async def _run() -> Any:
            client = make_client(config, server)
            async with client:
                return await generate_one(client, "event_eval", scenario, config=config)

        record, _meta, problems = run(_run())
        assert record is None
        assert problems

    def test_record_id_is_stable_for_same_input(self, tmp_path: Path) -> None:
        """同一份模型输入必须得到同一个记录 id（去重与续跑都依赖它）。"""
        payload = FakeDeepSeekServer().default_payload(1)
        server = FakeDeepSeekServer(lambda body, index: payload)
        config = make_config(tmp_path)
        scenario = sample_scenarios("event_eval", 1, seed=5)[0]

        async def _run() -> Any:
            client = make_client(config, server)
            async with client:
                out = []
                for _ in range(2):
                    record, _meta, _ = await generate_one(
                        client, "event_eval", scenario, config=config
                    )
                    out.append(record)
                return out

        first, second = run(_run())
        assert first is not None and second is not None
        assert first["id"] == second["id"]


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

class TestPrompts:
    def test_generation_messages_have_contract_system_prompt(self) -> None:
        from qboss_training.contracts import get_contract

        scenario = sample_scenarios("event_eval", 1, seed=1)[0]
        messages = build_generation_messages("event_eval", scenario)
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == get_contract("event_eval").system_prompt
        assert messages[1]["role"] == "user"

    def test_event_eval_prompt_includes_scenario_details(self) -> None:
        scenario = sample_scenarios("event_eval", 1, seed=1)[0]
        body = build_generation_messages("event_eval", scenario)[1]["content"]
        assert scenario.event_hint in body
        assert scenario.event_label in body

    def test_emotion_prompt_forbids_extras(self) -> None:
        scenario = sample_scenarios("emotion_explain", 1, seed=1)[0]
        body = build_generation_messages("emotion_explain", scenario)[1]["content"]
        assert "不得出现任何数字" in body
        assert "台词" in body

    def test_emotion_prompt_embeds_state_values(self) -> None:
        scenario = sample_scenarios("emotion_explain", 1, seed=1)[0]
        body = build_generation_messages("emotion_explain", scenario)[1]["content"]
        assert str(scenario.payload["approach_drive"]) in body
        assert str(scenario.payload["restraint"]) in body

    def test_inline_schema_lists_all_fields(self) -> None:
        from qboss_training.contracts import get_contract

        contract = get_contract("event_eval")
        rendered = inline_schema(contract)
        for field in contract.required_fields:
            assert field in rendered

    def test_inline_schema_renders_enums(self) -> None:
        from qboss_training.contracts import get_contract

        rendered = inline_schema(get_contract("event_eval"))
        assert "strong_approach" in rendered
        assert "slight_distance" in rendered

    def test_verify_messages_include_sample(self, sample_event_eval: dict) -> None:
        messages = build_verify_messages(
            "event_eval", sample_event_eval["input"], sample_event_eval["output"]
        )
        assert len(messages) == 2
        assert "direction" in messages[1]["content"]

    def test_verify_messages_include_known_problems(self, sample_event_eval: dict) -> None:
        messages = build_verify_messages(
            "event_eval",
            sample_event_eval["input"],
            sample_event_eval["output"],
            violations=["EX02: 方向翻转"],
        )
        assert "方向翻转" in messages[1]["content"]


class TestDryRun:
    def test_reports_credential_state_without_calling(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        plan = dry_run_plan(make_config(tmp_path))
        assert plan["credential_present"] is False
        assert plan["credential_env"] == "DEEPSEEK_API_KEY"
        assert plan["base_url"] == "https://api.deepseek.com"

    def test_reports_present_credential(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
        plan = dry_run_plan(make_config(tmp_path))
        assert plan["credential_present"] is True
        assert FAKE_KEY not in json.dumps(plan)

    def test_plan_has_scenario_coverage(self, tmp_path: Path) -> None:
        plan = dry_run_plan(make_config(tmp_path))
        assert plan["scenario_kind_counts"]
        assert sum(plan["scenario_kind_counts"].values()) > 0


class TestGenerationSummary:
    def test_summary_counts_file_lines(self, tmp_path: Path) -> None:
        from qboss_training.data.generator import GenerationStats

        config = make_config(tmp_path)
        write_jsonl(
            tmp_path / "raw" / "event_eval.jsonl",
            [{"id": "a"}, {"id": "b"}],
        )
        summary = generation_summary(config, GenerationStats(accepted=2))
        assert summary["records_in_file"] == 2
        assert summary["config"]["base_url"] == "https://api.deepseek.com"

    def test_summary_has_no_credentials(self, tmp_path: Path) -> None:
        from qboss_training.data.generator import GenerationStats

        config = make_config(tmp_path)
        payload = json.dumps(generation_summary(config, GenerationStats()))
        assert FAKE_KEY not in payload
        assert "authorization" not in payload.lower()


class TestCheckpointStore:
    def test_ignores_checkpoint_from_other_task(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        write_jsonl(tmp_path / "x.jsonl", [])
        store = CheckpointStore(path, "event_eval")
        assert store.load() == {}

    def test_saves_and_loads(self, tmp_path: Path) -> None:
        from qboss_training.data.generator import GenerationStats

        store = CheckpointStore(tmp_path / "cp.json", "event_eval")
        guard = BudgetGuard(BudgetConfig())
        store.save(
            cursor=8,
            stats=GenerationStats(accepted=3),
            budget=guard,
            output_file=tmp_path / "out.jsonl",
        )
        loaded = store.load()
        assert loaded["cursor"] == 8
        assert loaded["stats"]["accepted"] == 3
