"""数据生成器：断点续跑 · 预算守卫 · 重试 · JSON 抽取 · 去重。

主流程::

    for batch in batches:
        budget.check_can_start()          # 任一上限命中 → 保存断点并优雅退出
        results = await gather(生成 + 校验 + 拒绝采样重试)
        dedup.accept(...)                 # 精确 + 近似去重
        append_jsonl(...)                 # 增量落盘
        checkpoint.save(...)              # 断点

断点里**只保存**计数、已用预算、种子游标与已完成 id，绝不保存 API key。

续跑语义：
  启动时读回已有 JSONL，重建去重器，然后把种子游标推进到上次进度；
  已经存在的 id 直接跳过，所以重复运行同一命令是幂等的。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import GenerationConfig
from ..contracts import EVENT_EVAL, TASK_NAMES, get_contract
from ..errors import BudgetExceeded, GeneratorError, JsonExtractionError
from ..utils.dedup import Deduplicator, input_fingerprint, stable_hash
from ..utils.io import append_jsonl, iter_jsonl, read_json, write_json
from ..utils.jsonx import extract_json
from ..utils.secrets import getenv_secret, redact, utc_now_iso
from ..validators import validate_output
from ..validators.invariants import ERROR, has_errors
from .client import BudgetGuard, ChatMessage, DeepSeekClient
from .prompts import GENERATOR_VERSION, build_generation_messages, build_verify_messages
from .seeds import SeedScenario, sample_scenarios

LOGGER = logging.getLogger("qboss_training.generator")

CHECKPOINT_VERSION = 2


@dataclass
class GenerationStats:
    """生成统计。"""

    requested: int = 0
    accepted: int = 0
    rejected_schema: int = 0
    rejected_invariant: int = 0
    rejected_duplicate_exact: int = 0
    rejected_duplicate_near: int = 0
    request_failures: int = 0
    retries: int = 0
    repaired: int = 0
    budget_stopped: bool = False
    resumed_from: int = 0
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "accepted": self.accepted,
            "rejected_schema": self.rejected_schema,
            "rejected_invariant": self.rejected_invariant,
            "rejected_duplicate_exact": self.rejected_duplicate_exact,
            "rejected_duplicate_near": self.rejected_duplicate_near,
            "request_failures": self.request_failures,
            "retries": self.retries,
            "repaired": self.repaired,
            "budget_stopped": self.budget_stopped,
            "resumed_from": self.resumed_from,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class CheckpointStore:
    """断点存取。"""

    def __init__(self, path: str | Path, task: str) -> None:
        self.path = Path(path)
        self.task = task

    def load(self) -> dict[str, Any]:
        payload = read_json(self.path, default=None)
        if not isinstance(payload, Mapping):
            return {}
        if payload.get("task") != self.task:
            # 不同任务的断点不通用，忽略以免污染
            return {}
        return dict(payload)

    def save(
        self,
        *,
        cursor: int,
        stats: GenerationStats,
        budget: BudgetGuard,
        output_file: str | Path,
        extra: Mapping[str, Any] | None = None,
    ) -> Path:
        payload = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "generator_version": GENERATOR_VERSION,
            "task": self.task,
            "cursor": cursor,
            "output_file": str(output_file),
            "system_prompt_fingerprint": _prompt_fingerprint(self.task),
            "stats": stats.to_dict(),
            "budget": budget.snapshot(),
            "updated_at": utc_now_iso(),
        }
        if extra:
            payload.update(extra)
        # 二次保证：写盘前整体脱敏
        return write_json(self.path, payload)


def _prompt_fingerprint(task: str) -> str:
    contract = get_contract(task)
    return stable_hash(
        {
            "system_prompt": contract.system_prompt,
            "generator_version": GENERATOR_VERSION,
            "schema": contract.output_schema(),
        },
        length=16,
    )


def _parse_generation_reply(text: str, task: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """把模型回复解析成 ``(input, output)``。"""
    contract = get_contract(task)

    def _looks_right(candidate: Any) -> bool:
        if not isinstance(candidate, Mapping):
            return False
        if "output" in candidate and isinstance(candidate["output"], Mapping):
            return True
        return set(contract.required_fields) <= set(candidate)

    payload = extract_json(text, validate=_looks_right)

    if isinstance(payload, Mapping) and isinstance(payload.get("output"), Mapping):
        model_input = payload.get("input")
        if not isinstance(model_input, Mapping):
            raise JsonExtractionError("回复缺少 input 对象")
        return dict(model_input), dict(payload["output"])

    # 模型只吐了 output：这是可修复的，交给调用方标注（输入由场景提供）
    if isinstance(payload, Mapping):
        return {}, dict(payload)

    raise JsonExtractionError("回复结构无法识别")


async def generate_one(
    client: DeepSeekClient,
    task: str,
    scenario: SeedScenario,
    *,
    config: GenerationConfig,
    budget: BudgetGuard | None = None,
    stats: GenerationStats | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[str]]:
    """生成并校验单条样本（含拒绝采样重试）。

    Returns:
        ``(record_or_None, meta, problems)``：
        * record 为 None 表示这条种子最终被丢弃；
        * meta 记录尝试次数、usage、失败原因，供统计与排查。
    """
    messages = [
        ChatMessage(item["role"], item["content"])
        for item in build_generation_messages(task, scenario)
    ]
    contract = get_contract(task)

    meta: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "event_kind": scenario.event_kind,
        "attempts": 0,
        "problems": [],
    }
    problems: list[str] = []

    for attempt in range(1, config.max_sample_attempts + 1):
        meta["attempts"] = attempt
        if budget is not None:
            budget.check_can_start()
        if stats is not None:
            stats.requested += 1

        try:
            result = await client.complete(messages)
        except GeneratorError as exc:
            if stats is not None:
                stats.request_failures += 1
            problems.append(f"请求失败：{exc}")
            meta["problems"] = list(problems)
            continue

        if budget is not None:
            budget.record(result.usage)
        meta["usage"] = {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
        }
        if result.finish_reason == "length":
            problems.append("输出被 max_tokens 截断")

        try:
            model_input, output = _parse_generation_reply(result.text, task)
        except JsonExtractionError as exc:
            problems.append(f"JSON 抽取失败：{exc}")
            meta["problems"] = list(problems)
            if stats is not None:
                stats.rejected_schema += 1
            continue

        # 模型没给 input 时用场景骨架兜底（种子场景已包含 mood/values 等）
        if not model_input:
            model_input = _fallback_input(task, scenario)

        violations = validate_output(task, output, model_input=model_input)
        hard = [item for item in violations if item.severity == ERROR]
        schema_level = sum(1 for item in hard if item.code == "SCHEMA")
        invariant_level = len(hard) - schema_level

        if hard:
            problems.extend(f"{item.code}: {item.message}" for item in hard)
            meta["problems"] = list(problems)
            if stats is not None:
                stats.rejected_schema += schema_level
                stats.rejected_invariant += invariant_level

            if config.self_verify:
                repaired = await _try_repair(
                    client, task, model_input, output, problems, budget=budget, stats=stats
                )
                if repaired is not None:
                    violations = validate_output(task, repaired, model_input=model_input)
                    if not has_errors(violations):
                        if stats is not None:
                            stats.repaired += 1
                        record = _build_record(task, scenario, model_input, repaired, meta)
                        return record, meta, []
            continue

        # 只带 warning 的样本也接受，但记录在 meta 里便于事后过滤
        warnings = [item.to_dict() for item in violations]
        if warnings:
            meta["warnings"] = warnings
        record = _build_record(task, scenario, model_input, output, meta)
        return record, meta, []

    return None, meta, problems


def _fallback_input(task: str, scenario: SeedScenario) -> dict[str, Any]:
    """场景骨架兜底输入（模型未返回 input 时使用）。"""
    if task == EVENT_EVAL:
        return {
            "current_event": {"speaker": "user", "text": "（生成器未返回 input，占位）"},
            "context_turns": [],
            "background_mood": scenario.payload.get(
                "background_mood", {"valence": 0.0, "arousal": 0.3}
            ),
            "character_values": scenario.payload.get("character_values", {}),
            "known_facts": list(scenario.payload.get("known_facts", [])),
        }
    return {
        "event": {"char": "（占位）", "user": "（占位）"},
        "background_mood": scenario.payload.get(
            "background_mood", {"valence": 0.0, "arousal": 0.3}
        ),
        "active_emotions": [
            {
                "target": scenario.payload.get("primary", {}).get("target", "user"),
                "cause": "（占位）",
                "direction": scenario.payload.get("primary", {}).get("direction", "0"),
                "intensity": scenario.payload.get("primary", {}).get("intensity", 0.5),
            }
        ],
        "approach_drive": scenario.payload.get("approach_drive", 0.5),
        "restraint": scenario.payload.get("restraint", 0.5),
    }


async def _try_repair(
    client: DeepSeekClient,
    task: str,
    model_input: Mapping[str, Any],
    output: Mapping[str, Any],
    problems: Sequence[str],
    *,
    budget: BudgetGuard | None,
    stats: GenerationStats | None,
) -> dict[str, Any] | None:
    """用一次额外请求做自检修正。失败返回 None。"""
    if budget is not None:
        try:
            budget.check_can_start()
        except BudgetExceeded:
            return None
    if stats is not None:
        stats.requested += 1

    messages = [
        ChatMessage(item["role"], item["content"])
        for item in build_verify_messages(task, model_input, output, violations=problems)
    ]
    try:
        result = await client.complete(messages)
    except GeneratorError:
        if stats is not None:
            stats.request_failures += 1
        return None

    if budget is not None:
        budget.record(result.usage)

    contract = get_contract(task)

    def _accept(candidate: Any) -> bool:
        return isinstance(candidate, Mapping) and set(contract.required_fields) <= set(
            candidate
        )

    try:
        payload = extract_json(result.text, validate=_accept)
    except JsonExtractionError:
        return None
    if isinstance(payload, Mapping) and isinstance(payload.get("output"), Mapping):
        payload = payload["output"]
    return dict(payload) if isinstance(payload, Mapping) else None


def _build_record(
    task: str,
    scenario: SeedScenario,
    model_input: Mapping[str, Any],
    output: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    fingerprint = input_fingerprint(task, dict(model_input))
    record_id = f"{task[:2]}_{fingerprint[:12]}"
    return {
        "id": record_id,
        "task": task,
        "input": dict(model_input),
        "output": dict(output),
        "meta": {
            "scenario_id": scenario.scenario_id,
            "event_kind": scenario.event_kind,
            "event_label": scenario.event_label,
            "generator_version": GENERATOR_VERSION,
            "prompt_fingerprint": _prompt_fingerprint(task),
            "created_at": utc_now_iso(),
            "attempts": meta.get("attempts"),
            "warnings": meta.get("warnings", []),
            "source": "deepseek",
        },
    }


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

async def run_generation(
    config: GenerationConfig,
    *,
    client: DeepSeekClient | None = None,
    scenarios: Sequence[SeedScenario] | None = None,
    progress: Callable[[GenerationStats, int], None] | None = None,
) -> tuple[list[dict[str, Any]], GenerationStats]:
    """执行数据生成，支持断点续跑。

    Args:
        config: 生成配置。
        client: 已构造的客户端（测试注入用）；None 时自行创建。
        scenarios: 预设种子场景；None 时按配置采样。
        progress: 回调 ``(stats, cursor)``，用于打印进度。

    Returns:
        ``(本次新增的记录, 统计)``。已有文件中的历史记录不计入返回值。
    """
    task = config.task
    if task not in TASK_NAMES:
        raise GeneratorError(
            f"生成器只支持单任务运行（{', '.join(TASK_NAMES)}），收到 {task!r}；"
            "如需两种任务，请分别运行两次。"
        )

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{task}.jsonl"
    checkpoint = CheckpointStore(config.checkpoint_file or output_dir / ".checkpoint.json", task)

    # -- 续跑：读回已有数据，重建去重器 ------------------------------------
    existing: list[dict[str, Any]] = []
    if output_file.exists():
        existing = list(iter_jsonl(output_file))
    deduper = Deduplicator(near_threshold=config.near_duplicate_threshold)
    for index, record in enumerate(existing):
        deduper.add(
            str(record.get("task", task)),
            record.get("input") or {},
            str(record.get("id") or f"existing_{index}"),
        )

    state = checkpoint.load()
    stats = GenerationStats(resumed_from=len(existing))
    if state:
        LOGGER.info(
            "从断点续跑：已有 %d 条，上次光标 %s",
            len(existing),
            state.get("cursor"),
        )

    remaining = max(0, config.target_samples - len(existing))
    if remaining == 0:
        LOGGER.info("目标数量已达（%d >= %d），无需生成", len(existing), config.target_samples)
        stats.accepted = 0
        stats.finished_at = utc_now_iso()
        return [], stats

    if scenarios is None:
        scenarios = sample_scenarios(
            task,
            config.target_samples,
            seed=config.seed,
            scenarios_filter=config.seed_scenarios,
        )

    # 跳过已完成的种子（按 scenario_id 去重，保证续跑幂等）
    done_scenarios = {
        str(record.get("meta", {}).get("scenario_id"))
        for record in existing
        if isinstance(record.get("meta"), Mapping)
    }
    queue = [
        item
        for item in scenarios
        if item.scenario_id not in done_scenarios
    ][:remaining]

    LOGGER.info(
        "任务 %s：目标 %d，已有 %d，本次计划 %d 条，并发 %d，批次 %d",
        task,
        config.target_samples,
        len(existing),
        len(queue),
        config.concurrency,
        config.batch_size,
    )

    budget = BudgetGuard(config.budget)
    owns_client = client is None
    active_client = client
    written: list[dict[str, Any]] = []

    try:
        if active_client is None:
            active_client = DeepSeekClient(config.client, config.retry)
            await active_client.__aenter__()

        for start in range(0, len(queue), config.batch_size):
            batch = queue[start : start + config.batch_size]
            try:
                budget.check_can_start()
            except BudgetExceeded as exc:
                LOGGER.warning("%s", exc)
                stats.budget_stopped = True
                break

            semaphore = asyncio.Semaphore(max(1, config.concurrency))

            async def _worker(scenario: SeedScenario):
                async with semaphore:
                    return await generate_one(
                        active_client,  # type: ignore[arg-type]
                        task,
                        scenario,
                        config=config,
                        budget=budget,
                        stats=stats,
                    )

            results = await asyncio.gather(
                *(_worker(scenario) for scenario in batch), return_exceptions=True
            )

            accepted_this_batch: list[dict[str, Any]] = []
            for scenario, outcome in zip(batch, results):
                if isinstance(outcome, BaseException):
                    stats.request_failures += 1
                    if isinstance(outcome, BudgetExceeded):
                        stats.budget_stopped = True
                    LOGGER.warning("种子 %s 生成异常：%s", scenario.scenario_id, redact(outcome))
                    continue

                record, _meta, problems = outcome
                if record is None:
                    if problems:
                        LOGGER.debug("种子 %s 被丢弃：%s", scenario.scenario_id, problems[:2])
                    continue

                # 本轮已接受过的种子不再重复接受（与历史记录同样处理）
                if scenario.scenario_id in done_scenarios:
                    stats.rejected_duplicate_exact += 1
                    continue

                ok, hit = deduper.accept(task, record.get("input") or {}, record["id"])
                if not ok:
                    if hit is not None and hit.kind == "exact":
                        stats.rejected_duplicate_exact += 1
                    else:
                        stats.rejected_duplicate_near += 1
                    LOGGER.debug(
                        "去重丢弃 %s（%s，相似度 %.3f）",
                        record["id"],
                        hit,
                        hit.similarity if hit else 0,
                    )
                    continue

                done_scenarios.add(scenario.scenario_id)
                accepted_this_batch.append(record)

            if accepted_this_batch:
                append_jsonl(output_file, accepted_this_batch)
                written.extend(accepted_this_batch)
                stats.accepted += len(accepted_this_batch)

            budget_extra = {"accepted_total": len(existing) + stats.accepted}
            checkpoint.save(
                cursor=start + len(batch),
                stats=stats,
                budget=budget,
                output_file=output_file,
                extra=budget_extra,
            )

            if progress is not None:
                progress(stats, start + len(batch))

            LOGGER.info(
                "进度 %d/%d：新增 %d，累计 %d/%d",
                start + len(batch),
                len(queue),
                len(accepted_this_batch),
                len(existing) + stats.accepted,
                config.target_samples,
            )

            try:
                budget.check_can_start()
            except BudgetExceeded as exc:
                LOGGER.warning("%s", exc)
                stats.budget_stopped = True
                break
    finally:
        if owns_client and active_client is not None:
            await active_client.aclose()
        stats.finished_at = utc_now_iso()
        checkpoint.save(
            cursor=len(queue),
            stats=stats,
            budget=budget,
            output_file=output_file,
            extra={"accepted_total": len(existing) + stats.accepted},
        )

    LOGGER.info(
        "生成结束：新增 %d，累计 %d，请求 %d，估算花费 $%.4f%s",
        stats.accepted,
        len(existing) + stats.accepted,
        budget.requests,
        budget.estimated_usd(),
        "（预算中止）" if stats.budget_stopped else "",
    )
    return written, stats


def generation_summary(
    config: GenerationConfig,
    stats: GenerationStats,
    *,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """生成一份可落盘的运行摘要（不含任何凭据）。"""
    directory = Path(output_dir or config.output_dir)
    output_file = directory / f"{config.task}.jsonl"
    return {
        "task": config.task,
        "generator_version": GENERATOR_VERSION,
        "prompt_fingerprint": _prompt_fingerprint(config.task),
        "output_file": str(output_file),
        "records_in_file": _safe_line_count(output_file),
        "stats": stats.to_dict(),
        "config": {
            "target_samples": config.target_samples,
            "batch_size": config.batch_size,
            "concurrency": config.concurrency,
            "max_sample_attempts": config.max_sample_attempts,
            "self_verify": config.self_verify,
            "seed": config.seed,
            "near_duplicate_threshold": config.near_duplicate_threshold,
            "model": config.client.model,
            "base_url": config.client.base_url,
        },
    }


def _safe_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def dry_run_plan(config: GenerationConfig) -> dict[str, Any]:
    """--dry-run：不调用 API，只报告计划与凭据状态。"""
    has_key = bool(getenv_secret(config.client.api_key_env))
    scenarios = sample_scenarios(
        config.task,
        min(config.target_samples, 200),
        seed=config.seed,
        scenarios_filter=config.seed_scenarios,
    )
    kind_counts: dict[str, int] = {}
    for item in scenarios:
        kind_counts[item.event_kind] = kind_counts.get(item.event_kind, 0) + 1
    return {
        "task": config.task,
        "target_samples": config.target_samples,
        "planned_requests": config.target_samples,
        "batch_size": config.batch_size,
        "concurrency": config.concurrency,
        "model": config.client.model,
        "base_url": config.client.base_url,
        "credential_env": config.client.api_key_env,
        "credential_present": has_key,
        "budget": {
            "max_requests": config.budget.max_requests,
            "max_total_tokens": config.budget.max_total_tokens,
            "max_usd": config.budget.max_usd,
        },
        "scenario_kind_counts": dict(sorted(kind_counts.items())),
    }
