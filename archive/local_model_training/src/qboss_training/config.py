"""配置模型与加载。

配置来源优先级（后者覆盖前者）：
  CLI 参数  >  环境变量  >  YAML 文件  >  内置默认值

绝不从配置文件读取 API key：只认环境变量 DEEPSEEK_API_KEY。
配置文件里若出现 api_key/base_url 之外的凭据字段会被显式拒绝。
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping, TypeVar, get_type_hints

from .errors import ConfigError

try:  # PyYAML 是运行依赖，但保持 import 失败时可读的报错
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ConfigError("需要 PyYAML：pip install PyYAML") from exc


T = TypeVar("T")

#: 任何配置里都不允许出现的字段名（防止把密钥写进仓库）
FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_token",
        "token",
        "secret",
        "password",
        "authorization",
        "auth_token",
        "access_token",
    }
)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_GEN_MODEL = "deepseek-chat"


def _check_forbidden(node: Any, path: str = "") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            key_str = str(key)
            if key_str.lower() in FORBIDDEN_CONFIG_KEYS:
                raise ConfigError(
                    f"配置项 {path}{key_str!r} 被禁止：本工程绝不把 API key/凭据写入文件。"
                    f"请改用环境变量 DEEPSEEK_API_KEY。"
                )
            _check_forbidden(value, f"{path}{key_str}.")


def _coerce(value: Any, target_type: Any) -> Any:
    """把 YAML/CLI 的宽松值收敛到 dataclass 字段类型。"""
    if value is None:
        return None
    origin = getattr(target_type, "__origin__", None)

    if target_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is str:
        return str(value)
    if origin is tuple or target_type is tuple:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return tuple(value)
    if origin is list or target_type is list:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return list(value)
    if origin is dict or target_type is dict:
        return dict(value)
    return value


def _from_mapping(cls: type[T], data: Mapping[str, Any], path: str = "") -> T:
    """把嵌套 dict 构造成 dataclass（支持嵌套 dataclass / dict / list）。"""
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置节 {path or '<root>'} 应为 mapping，实际为 {type(data).__name__}")

    known = {f.name: f for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(
            f"配置节 {path or '<root>'} 存在未知字段：{sorted(unknown)}；"
            f"可用字段：{sorted(known)}"
        )

    # 解析真实类型（字符串注解需要 get_type_hints 才能拿到类对象）
    try:
        hints = get_type_hints(cls)
    except Exception:  # pragma: no cover - 注解异常时退化为原始注解
        hints = {name: spec.type for name, spec in known.items()}

    kwargs: dict[str, Any] = {}
    for name, spec in known.items():
        if name not in data:
            continue
        raw = data[name]
        field_type = hints.get(name, spec.type)
        type_args = getattr(field_type, "__args__", ())

        # 嵌套 dataclass
        nested: type | None = None
        if isinstance(field_type, type) and is_dataclass(field_type):
            nested = field_type
        elif type_args:
            first = type_args[0]
            if isinstance(first, type) and is_dataclass(first):
                nested = first

        if nested is not None and isinstance(raw, Mapping):
            kwargs[name] = _from_mapping(nested, raw, f"{path}{name}.")
        elif nested is not None and isinstance(raw, list):
            kwargs[name] = [
                _from_mapping(nested, item, f"{path}{name}[].")
                if isinstance(item, Mapping)
                else item
                for item in raw
            ]
        else:
            kwargs[name] = _coerce(raw, field_type)
    return cls(**kwargs)  # type: ignore[call-arg]


def _merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并（override 覆盖 base），不修改入参。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, Mapping)
        ):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def deep_get(data: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def apply_overrides(data: dict[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """应用 ``a.b.c=value`` 形式的 CLI 覆盖。值按 YAML 字面量解析。"""
    result = copy.deepcopy(data)
    for dotted, raw in overrides.items():
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                parsed: Any = yaml.safe_load(raw)
            except yaml.YAMLError:
                parsed = raw
        else:
            parsed = raw

        parts = dotted.split(".")
        node = result
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = parsed
    return result


# --------------------------------------------------------------------------
# 生成器配置
# --------------------------------------------------------------------------

@dataclass
class RetryConfig:
    """重试与退避。"""

    max_attempts: int = 4
    initial_backoff_s: float = 1.5
    max_backoff_s: float = 30.0
    backoff_multiplier: float = 2.0
    jitter_s: float = 0.5
    #: 这些 HTTP 状态码可重试
    retry_status_codes: tuple[int, ...] = (408, 409, 425, 429, 500, 502, 503, 504)


@dataclass
class BudgetConfig:
    """预算守卫。任何一项超限即停止生成但保留断点。"""

    max_requests: int = 400
    max_prompt_tokens: int = 900_000
    max_completion_tokens: int = 600_000
    max_total_tokens: int = 1_400_000
    max_usd: float = 3.0
    #: DeepSeek 价格（USD / 1M tokens）。默认值仅为量级估算，可按官方价目表覆盖。
    usd_per_million_prompt_tokens: float = 0.27
    usd_per_million_completion_tokens: float = 1.10


@dataclass
class ClientConfig:
    """OpenAI 兼容客户端配置。凭据只来自环境变量。"""

    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = "DEEPSEEK_API_KEY"
    model: str = DEFAULT_GEN_MODEL
    request_timeout_s: float = 120.0
    max_connections: int = 8
    temperature: float = 1.0
    top_p: float = 0.95
    max_tokens: int = 512
    #: 让服务端强制返回 JSON（DeepSeek 支持 response_format=json_object）
    response_format_json: bool = True
    #: 额外请求头
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class GenerationConfig:
    """数据生成总配置。"""

    task: str = "event_eval"
    target_samples: int = 200
    batch_size: int = 8
    concurrency: int = 4
    #: 每个批次里让模型产出多少条（1 条 = 最稳；>1 容易格式崩坏）
    items_per_request: int = 1
    #: 拒绝采样：单条最多重试几次以通过 schema 校验
    max_sample_attempts: int = 3
    #: 生成教师输出后，是否再用一次请求做自检/修正（成本翻倍，默认关闭）
    self_verify: bool = False
    #: 种子场景采样
    seed_scenarios: tuple[str, ...] = ()
    seed: int = 20240607
    #: 近似去重阈值（字符 3-gram Jaccard）。
    #: 标定见 utils/dedup.py：短中文文本上 0.90 是"不误杀真数据"的拐点，
    #: 调到 0.85 以下会开始丢弃本质不同的样本。
    near_duplicate_threshold: float = 0.90
    #: 输出目录
    output_dir: str = "data/raw"
    #: 断点文件
    checkpoint_file: str = "data/raw/.checkpoint.json"
    client: ClientConfig = field(default_factory=ClientConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)


@dataclass
class SplitConfig:
    """数据集切分配置。"""

    train_ratio: float = 0.8
    val_ratio: float = 0.1
    test_ratio: float = 0.1
    seed: int = 42
    #: 分层键（按任务/方向/场景等）
    stratify_by: tuple[str, ...] = ("task",)
    #: 切分前去重阈值（0 表示跳过去重）
    dedupe_near_threshold: float = 0.95
    #: 切分后跨 split 泄漏检测阈值
    leakage_threshold: float = 0.9

    def ratios(self) -> tuple[float, float, float]:
        total = self.train_ratio + self.val_ratio + self.test_ratio
        if total <= 0:
            raise ConfigError("split 比例之和必须为正")
        return (
            self.train_ratio / total,
            self.val_ratio / total,
            self.test_ratio / total,
        )


@dataclass
class ProjectConfig:
    """顶层配置。"""

    generation: GenerationConfig = field(default_factory=GenerationConfig)
    split: SplitConfig = field(default_factory=SplitConfig)


def load_config_document(path: str | Path | None) -> dict[str, Any]:
    """读取 YAML 配置为 dict，并做凭据字段检查。"""
    if path is None:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在：{config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置文件根节点应为 mapping：{config_path}")
    _check_forbidden(data)
    return dict(data)


def load_project_config(
    path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> ProjectConfig:
    """加载完整项目配置。"""
    document = load_config_document(path)
    if overrides:
        document = apply_overrides(document, overrides)
    _check_forbidden(document)

    environ = os.environ if env is None else env
    _apply_env_overrides(document, environ)

    config = _from_mapping(ProjectConfig, document)
    _validate_project_config(config)
    return config


def _apply_env_overrides(document: dict[str, Any], environ: Mapping[str, str]) -> None:
    """环境变量覆盖（仅白名单，且不含任何凭据值）。"""
    mapping = {
        "QBOSS_GEN_TASK": "generation.task",
        "QBOSS_GEN_TARGET": "generation.target_samples",
        "QBOSS_GEN_BATCH_SIZE": "generation.batch_size",
        "QBOSS_GEN_CONCURRENCY": "generation.concurrency",
        "QBOSS_GEN_OUTPUT_DIR": "generation.output_dir",
        "QBOSS_GEN_MODEL": "generation.client.model",
        "QBOSS_GEN_BASE_URL": "generation.client.base_url",
        "QBOSS_GEN_MAX_USD": "generation.budget.max_usd",
        "QBOSS_GEN_MAX_REQUESTS": "generation.budget.max_requests",
        "QBOSS_SPLIT_SEED": "split.seed",
    }
    overrides: dict[str, Any] = {}
    for env_name, dotted in mapping.items():
        value = environ.get(env_name)
        if value:
            overrides[dotted] = value
    if overrides:
        document.update(apply_overrides(document, overrides))


def _validate_project_config(config: ProjectConfig) -> None:
    gen = config.generation
    if gen.task not in {"event_eval", "emotion_explain", "mixed"}:
        raise ConfigError(
            f"generation.task 必须是 event_eval / emotion_explain / mixed，收到 {gen.task!r}"
        )
    if gen.target_samples <= 0:
        raise ConfigError("generation.target_samples 必须为正")
    if gen.concurrency <= 0:
        raise ConfigError("generation.concurrency 必须为正")
    if gen.batch_size <= 0:
        raise ConfigError("generation.batch_size 必须为正")
    if gen.client.api_key_env.lower() in FORBIDDEN_CONFIG_KEYS:  # pragma: no cover
        raise ConfigError("api_key_env 必须是一个环境变量名，不是密钥本身")
    if not gen.client.base_url.startswith(("http://", "https://")):
        raise ConfigError(f"base_url 必须以 http(s):// 开头：{gen.client.base_url!r}")
    if not 0.0 <= gen.near_duplicate_threshold <= 1.0:
        raise ConfigError("near_duplicate_threshold 必须在 [0,1]")
    if config.split.dedupe_near_threshold and not (
        0.0 <= config.split.dedupe_near_threshold <= 1.0
    ):
        raise ConfigError("split.dedupe_near_threshold 必须在 [0,1]")
