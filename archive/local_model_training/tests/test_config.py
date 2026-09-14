"""配置加载测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from qboss_training.config import (
    DEFAULT_BASE_URL,
    BudgetConfig,
    ClientConfig,
    GenerationConfig,
    ProjectConfig,
    SplitConfig,
    apply_overrides,
    deep_get,
    load_config_document,
    load_project_config,
)
from qboss_training.errors import ConfigError

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"


class TestDefaults:
    def test_default_base_url_is_deepseek(self) -> None:
        assert ClientConfig().base_url == DEFAULT_BASE_URL
        assert DEFAULT_BASE_URL == "https://api.deepseek.com"

    def test_default_api_key_env(self) -> None:
        assert ClientConfig().api_key_env == "DEEPSEEK_API_KEY"

    def test_default_budget_is_bounded(self) -> None:
        budget = BudgetConfig()
        assert budget.max_requests > 0
        assert budget.max_usd > 0
        assert budget.max_total_tokens > 0

    def test_json_response_format_on_by_default(self) -> None:
        assert ClientConfig().response_format_json is True

    def test_load_without_path_gives_defaults(self) -> None:
        config = load_project_config()
        assert config.generation.task == "event_eval"
        assert config.split.seed == 42


class TestLoadProjectConfig:
    def test_loads_generation_yaml(self) -> None:
        path = CONFIG_DIR / "generation.yaml"
        config = load_project_config(path)
        assert config.generation.client.base_url == DEFAULT_BASE_URL
        assert config.generation.client.api_key_env == "DEEPSEEK_API_KEY"
        assert config.generation.near_duplicate_threshold == pytest.approx(0.90)
        assert config.split.stratify_by == ("task", "direction")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="不存在"):
            load_project_config(tmp_path / "nope.yaml")

    def test_non_mapping_root_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("- 1\n- 2\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_project_config(path)

    def test_unknown_field_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("generation:\n  bogus_field: 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="未知字段"):
            load_project_config(path)

    def test_unknown_section_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("bogus_section:\n  a: 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="未知字段"):
            load_project_config(path)

    def test_nested_sections_are_constructed(self, tmp_path: Path) -> None:
        path = tmp_path / "ok.yaml"
        path.write_text(
            "generation:\n"
            "  task: emotion_explain\n"
            "  target_samples: 17\n"
            "  client:\n"
            "    model: deepseek-reasoner\n"
            "  budget:\n"
            "    max_usd: 1.25\n",
            encoding="utf-8",
        )
        config = load_project_config(path)
        assert config.generation.task == "emotion_explain"
        assert config.generation.target_samples == 17
        assert config.generation.client.model == "deepseek-reasoner"
        assert config.generation.budget.max_usd == pytest.approx(1.25)

    def test_tuple_fields_accept_lists(self, tmp_path: Path) -> None:
        path = tmp_path / "ok.yaml"
        path.write_text(
            "generation:\n  seed_scenarios: [user_busy, apology]\n",
            encoding="utf-8",
        )
        config = load_project_config(path)
        assert config.generation.seed_scenarios == ("user_busy", "apology")

    def test_list_parsing_from_comma_string(self, tmp_path: Path) -> None:
        path = tmp_path / "ok.yaml"
        path.write_text("generation:\n  seed_scenarios: a, b\n", encoding="utf-8")
        config = load_project_config(path)
        assert config.generation.seed_scenarios == ("a", "b")


class TestValidation:
    def test_invalid_task_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("generation:\n  task: nope\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="task"):
            load_project_config(path)

    def test_zero_target_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("generation:\n  target_samples: 0\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="target_samples"):
            load_project_config(path)

    def test_negative_concurrency_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("generation:\n  concurrency: -1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="concurrency"):
            load_project_config(path)

    def test_bad_base_url_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            "generation:\n  client:\n    base_url: api.deepseek.com\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="base_url"):
            load_project_config(path)

    def test_threshold_out_of_range_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            "generation:\n  near_duplicate_threshold: 1.5\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="near_duplicate_threshold"):
            load_project_config(path)


class TestOverrides:
    def test_cli_overrides_take_effect(self) -> None:
        config = load_project_config(
            None, {"generation.concurrency": 9, "generation.task": "emotion_explain"}
        )
        assert config.generation.concurrency == 9
        assert config.generation.task == "emotion_explain"

    def test_string_values_are_coerced(self) -> None:
        config = load_project_config(None, {"generation.target_samples": "25"})
        assert config.generation.target_samples == 25
        assert isinstance(config.generation.target_samples, int)

    def test_nested_override_creates_path(self) -> None:
        document = apply_overrides({}, {"generation.client.model": "x"})
        assert document == {"generation": {"client": {"model": "x"}}}

    def test_override_does_not_mutate_input(self) -> None:
        original = {"generation": {"target_samples": 1}}
        apply_overrides(original, {"generation.target_samples": 2})
        assert original["generation"]["target_samples"] == 1

    def test_deep_get(self) -> None:
        assert deep_get({"a": {"b": {"c": 3}}}, "a.b.c") == 3
        assert deep_get({"a": 1}, "a.b.c", default=None) is None

    def test_env_overrides_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QBOSS_GEN_TARGET", "77")
        monkeypatch.setenv("QBOSS_GEN_MODEL", "deepseek-chat")
        config = load_project_config(None, env={"QBOSS_GEN_TARGET": "77", "QBOSS_GEN_MODEL": "deepseek-chat"})
        assert config.generation.target_samples == 77
        assert config.generation.client.model == "deepseek-chat"

    def test_env_does_not_override_api_key(self) -> None:
        """即使设置了 DEEPSEEK_API_KEY，也只会改变"是否有凭据"，不会进入配置结构。"""
        config = load_project_config(None, env={"DEEPSEEK_API_KEY": "sk-should-not-land-here"})
        dumped = repr(config)
        assert "sk-should-not-land-here" not in dumped


class TestSplitConfig:
    def test_ratios_normalize(self) -> None:
        config = SplitConfig(train_ratio=2, val_ratio=1, test_ratio=1)
        assert config.ratios() == pytest.approx((0.5, 0.25, 0.25))

    def test_default_ratios_sum_to_one(self) -> None:
        assert sum(SplitConfig().ratios()) == pytest.approx(1.0)

    def test_zero_total_rejected(self) -> None:
        with pytest.raises(ConfigError):
            SplitConfig(train_ratio=0, val_ratio=0, test_ratio=0).ratios()


class TestConfigFilesAreConsistent:
    """仓库里的 YAML 配置必须是可加载的，避免"文档里有、实际跑不起来"。"""

    def test_generation_config_loads(self) -> None:
        assert load_project_config(CONFIG_DIR / "generation.yaml") is not None

    def test_lora_bf16_config_loads_and_freezes_everything(self) -> None:
        import yaml

        document = yaml.safe_load((CONFIG_DIR / "lora_bf16.yaml").read_text(encoding="utf-8"))
        freeze = document["training"]["freeze"]
        assert freeze["freeze_vision"] is True
        assert freeze["freeze_embeddings"] is True
        assert freeze["freeze_lm_head"] is True
        assert document["training"]["model"]["load_vision"] is False
        assert document["training"]["quantization"]["enabled"] is False
        assert document["training"]["packing"] is False

    def test_qlora_config_enables_4bit_and_paged_optimizer(self) -> None:
        import yaml

        document = yaml.safe_load(
            (CONFIG_DIR / "qlora_fallback.yaml").read_text(encoding="utf-8")
        )
        quantization = document["training"]["quantization"]
        assert quantization["enabled"] is True
        assert quantization["load_in_4bit"] is True
        assert quantization["optim"].startswith("paged_")
        assert document["training"]["optim"].startswith("paged_")
        # QLoRA 下更应冻结 embedding/lm_head
        assert document["training"]["freeze"]["freeze_embeddings"] is True
        assert document["training"]["freeze"]["freeze_lm_head"] is True

    def test_no_config_contains_credentials(self) -> None:
        for path in CONFIG_DIR.glob("*.yaml"):
            text = path.read_text(encoding="utf-8").lower()
            for forbidden in ("api_key:", "apikey:", "secret:", "password:"):
                assert forbidden not in text, f"{path.name} 出现疑似凭据字段 {forbidden}"
