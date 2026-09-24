"""训练侧测试：冻结策略、配置校验、GGUF 命令构造、CPU 基准。

这些测试**不加载任何真实模型**：
  * 冻结策略用假模型（``nn.Module`` 或轻量 stub）验证；
  * GGUF 只验证命令构造（纯函数）；
  * 基准用 echo 后端。
因此不需要 GPU、不需要权重、不需要 llama.cpp。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from qboss_training.benchmark import (
    BenchmarkConfig,
    BenchmarkReport,
    BenchmarkResult,
    LatencyStats,
    benchmark_backend,
    benchmark_report_to_jsonl,
    render_benchmark_summary,
    run_benchmark,
)
from qboss_training.errors import TrainingError
from qboss_training.inference import (
    EchoBackend,
    GenerationRequest,
    GenerationResponse,
    build_gold_replay_backend,
    build_inference_messages,
    resolve_gguf_files,
)
from qboss_training.training.config import (
    FreezeConfig,
    LoraConfigSpec,
    ModelConfig,
    QuantizationConfig,
    TrainingConfig,
    apply_freeze_policy,
    apply_qlora_fallback,
    apply_smoke_overrides,
    classify_parameter,
    describe_training_config,
    resolve_model_kwargs,
    should_freeze,
    validate_training_config,
)
from qboss_training.training.export_gguf import (
    DEFAULT_QUANT_TYPES,
    QUANT_MATRIX,
    ExportConfig,
    describe_matrix,
    estimate_ram_mb,
    build_convert_command,
    build_quantize_command,
    resolve_convert_script,
)
from qboss_training.training.merge import (
    read_adapter_base,
    read_adapter_scaling,
    resolve_base_model,
    verify_merged_model,
)


# --------------------------------------------------------------------------
# 参数分类与冻结
# --------------------------------------------------------------------------

class TestClassifyParameter:
    @pytest.mark.parametrize(
        "name",
        [
            "model.visual.patch_embed.weight",
            "vision_tower.blocks.0.attn.qkv.weight",
            "model.vision_model.encoder.layers.0.weight",
            "multi_modal_projector.linear_1.weight",
            "model.merger.mlp.0.weight",
            "audio_encoder.conv.weight",
        ],
    )
    def test_vision_like_names(self, name: str) -> None:
        assert classify_parameter(name) == "vision"

    @pytest.mark.parametrize(
        "name",
        [
            "model.embed_tokens.weight",
            "lm_head.weight",
            "model.layers.0.self_attn.q_proj.weight".replace("q_proj", "embed_tokens"),
            "wte.weight",
        ],
    )
    def test_embedding_like_names(self, name: str) -> None:
        assert classify_parameter(name) == "embedding"

    @pytest.mark.parametrize(
        "name",
        [
            "model.norm.weight",
            "model.layers.0.input_layernorm.weight",
            "model.layers.0.post_attention_layernorm.weight",
            "transformer.ln_f.weight",
        ],
    )
    def test_norm_like_names(self, name: str) -> None:
        assert classify_parameter(name) == "norm"

    @pytest.mark.parametrize(
        "name",
        [
            "model.layers.0.self_attn.q_proj.weight",
            "model.layers.0.mlp.gate_proj.weight",
        ],
    )
    def test_other_names(self, name: str) -> None:
        assert classify_parameter(name) == "other"

    def test_vision_takes_priority_over_embedding(self) -> None:
        """``vision_model.embeddings`` 同时像两个类别，必须判为 vision。"""
        assert classify_parameter("model.vision_model.embeddings.weight") == "vision"


class TestShouldFreeze:
    def test_default_policy_freezes_vision_embedding_lm_head_norm(self) -> None:
        config = FreezeConfig()
        for name, expected in (
            ("model.vision_tower.weight", "vision"),
            ("model.embed_tokens.weight", "embedding"),
            ("lm_head.weight", "lm_head"),
            ("model.norm.weight", "norm"),
        ):
            freeze, reason = should_freeze(name, config)
            assert freeze, name
            assert reason == expected

    def test_attention_weights_are_not_frozen(self) -> None:
        freeze, _ = should_freeze("model.layers.0.self_attn.q_proj.weight", FreezeConfig())
        assert not freeze

    def test_unfreeze_patterns_win(self) -> None:
        config = FreezeConfig(freeze_embeddings=True, unfreeze_patterns=("embed_tokens",))
        freeze, reason = should_freeze("model.embed_tokens.weight", config)
        assert not freeze
        assert reason == "unfreeze_patterns"

    def test_freeze_patterns_apply(self) -> None:
        config = FreezeConfig(freeze_patterns=("mlp",))
        freeze, reason = should_freeze("model.layers.0.mlp.gate_proj.weight", config)
        assert freeze
        assert reason == "freeze_patterns"

    def test_lm_head_can_be_unfrozen_independently(self) -> None:
        config = FreezeConfig(freeze_embeddings=True, freeze_lm_head=False)
        assert not should_freeze("lm_head.weight", config)[0]
        assert should_freeze("model.embed_tokens.weight", config)[0]


class FakeModel:
    """最小可用的假模型：只需 ``named_parameters``。"""

    def __init__(self, names: Sequence[str]) -> None:
        self._params: list[tuple[str, Any]] = []
        for index, name in enumerate(names):
            self._params.append((name, _FakeParameter(size=100 + index)))

    def named_parameters(self) -> Any:
        return iter(self._params)


class _FakeParameter:
    def __init__(self, size: int) -> None:
        self._size = size
        self.requires_grad = True

    def numel(self) -> int:
        return self._size


class TestApplyFreezePolicy:
    NAMES = (
        "model.embed_tokens.weight",
        "lm_head.weight",
        "model.norm.weight",
        "model.vision_tower.blocks.0.weight",
        "model.layers.0.self_attn.q_proj.weight",
        "model.layers.0.mlp.gate_proj.weight",
    )

    def test_freezes_expected_parameters(self) -> None:
        model = FakeModel(self.NAMES)
        report = apply_freeze_policy(model, FreezeConfig())
        assert report["frozen_params"] > 0
        # 只有 attention 与 mlp 仍可训练
        trainable = [
            name for name, parameter in model.named_parameters() if parameter.requires_grad
        ]
        assert trainable == [
            "model.layers.0.self_attn.q_proj.weight",
            "model.layers.0.mlp.gate_proj.weight",
        ]

    def test_report_breaks_down_by_kind(self) -> None:
        model = FakeModel(self.NAMES)
        report = apply_freeze_policy(model, FreezeConfig())
        assert set(report["by_kind"]) == {"vision", "embedding", "norm", "other"}
        assert report["by_kind"]["vision"]["frozen_params"] > 0

    def test_examples_record_reasons(self) -> None:
        model = FakeModel(self.NAMES)
        report = apply_freeze_policy(model, FreezeConfig())
        reasons = {item["reason"] for item in report["examples"]}
        assert {"vision", "embedding", "norm"} <= reasons

    def test_no_freezing_when_disabled(self) -> None:
        model = FakeModel(self.NAMES)
        report = apply_freeze_policy(
            model,
            FreezeConfig(
                freeze_vision=False,
                freeze_embeddings=False,
                freeze_lm_head=False,
                freeze_norms=False,
            ),
        )
        assert report["frozen_params"] == 0
        assert report["trainable_after"] == report["total_params"]


# --------------------------------------------------------------------------
# 训练配置
# --------------------------------------------------------------------------

class TestTrainingConfigDefaults:
    def test_default_model_is_qwen_2b(self) -> None:
        assert ModelConfig().name_or_path == "Qwen/Qwen3.5-2B"

    def test_vision_is_off_by_default(self) -> None:
        """纯文本任务默认不加载视觉模块。"""
        assert ModelConfig().load_vision is False

    def test_bf16_is_default(self) -> None:
        assert TrainingConfig().bf16 is True
        assert TrainingConfig().fp16 is False

    def test_quantization_is_off_by_default(self) -> None:
        assert QuantizationConfig().enabled is False

    def test_lora_targets_attention_by_default(self) -> None:
        spec = LoraConfigSpec()
        assert spec.resolved_target_modules() == ["q_proj", "k_proj", "v_proj", "o_proj"]

    def test_mlp_can_be_added(self) -> None:
        spec = LoraConfigSpec(include_mlp=True)
        modules = spec.resolved_target_modules()
        assert "gate_proj" in modules
        assert "q_proj" in modules
        assert len(modules) == len(set(modules))

    def test_packing_is_off(self) -> None:
        """packing 会破坏 completion-only 掩码，必须保持关闭。"""
        assert TrainingConfig().packing is False

    def test_gradient_checkpointing_on(self) -> None:
        assert TrainingConfig().gradient_checkpointing is True


class TestValidateTrainingConfig:
    def test_default_config_has_no_hard_problems(self) -> None:
        problems = validate_training_config(TrainingConfig())
        assert not any("load_vision" in item for item in problems)

    def test_load_vision_is_flagged(self) -> None:
        config = TrainingConfig()
        config.model.load_vision = True
        assert any("load_vision" in item for item in validate_training_config(config))

    def test_qlora_without_paged_optim_is_flagged(self) -> None:
        config = TrainingConfig()
        config.quantization.enabled = True
        config.optim = "adamw_torch"
        assert any("paged" in item for item in validate_training_config(config))

    def test_qlora_with_paged_optim_is_clean(self) -> None:
        config = apply_qlora_fallback(TrainingConfig())
        problems = validate_training_config(config)
        assert not any("paged" in item for item in problems)

    def test_bf16_and_fp16_conflict(self) -> None:
        config = TrainingConfig()
        config.bf16 = True
        config.fp16 = True
        assert any("fp16" in item for item in validate_training_config(config))

    def test_unfrozen_embeddings_is_flagged(self) -> None:
        config = TrainingConfig()
        config.freeze.freeze_embeddings = False
        assert any("freeze_embeddings" in item for item in validate_training_config(config))

    def test_packing_is_flagged(self) -> None:
        config = TrainingConfig()
        config.packing = True
        assert any("packing" in item for item in validate_training_config(config))

    def test_zero_batch_flagged(self) -> None:
        config = TrainingConfig()
        config.per_device_train_batch_size = 0
        assert any("batch_size" in item for item in validate_training_config(config))

    def test_negative_lr_flagged(self) -> None:
        config = TrainingConfig()
        config.learning_rate = -1.0
        assert any("learning_rate" in item for item in validate_training_config(config))


class TestOverrides:
    def test_smoke_overrides_are_tiny(self) -> None:
        config = apply_smoke_overrides(TrainingConfig())
        assert config.max_steps == 4
        assert config.per_device_train_batch_size == 1
        assert config.gradient_accumulation_steps == 1
        assert config.output_dir.endswith("smoke")

    def test_smoke_does_not_mutate_original(self) -> None:
        original = TrainingConfig()
        apply_smoke_overrides(original)
        assert original.max_steps == -1

    def test_smoke_keeps_freeze_policy(self) -> None:
        config = apply_smoke_overrides(TrainingConfig())
        assert config.freeze.freeze_vision is True
        assert config.freeze.freeze_embeddings is True
        assert config.verify_freeze is True

    def test_qlora_fallback_enables_4bit_and_forces_freeze(self) -> None:
        config = TrainingConfig()
        config.freeze.freeze_embeddings = False
        qlora = apply_qlora_fallback(config)
        assert qlora.quantization.enabled is True
        assert qlora.optim.startswith("paged_")
        # QLoRA 下强制冻结 embedding/lm_head
        assert qlora.freeze.freeze_embeddings is True
        assert qlora.freeze.freeze_lm_head is True

    def test_qlora_fallback_does_not_mutate_original(self) -> None:
        original = TrainingConfig()
        apply_qlora_fallback(original)
        assert original.quantization.enabled is False

    def test_describe_mentions_mode_and_targets(self) -> None:
        text = describe_training_config(TrainingConfig())
        assert "BF16-LoRA" in text
        assert "q_proj" in text
        assert TrainingConfig().model.name_or_path in text

    def test_describe_marks_qlora(self) -> None:
        text = describe_training_config(apply_qlora_fallback(TrainingConfig()))
        assert "QLoRA" in text


class TestResolveModelKwargs:
    def test_vision_disabled_by_default(self) -> None:
        kwargs = resolve_model_kwargs(ModelConfig())
        assert kwargs["_disable_vision"] is True
        assert kwargs["vision_config"] is None

    def test_vision_kwargs_omitted_when_enabled(self) -> None:
        kwargs = resolve_model_kwargs(ModelConfig(load_vision=True))
        assert "_disable_vision" not in kwargs
        assert "vision_config" not in kwargs


# --------------------------------------------------------------------------
# adapter 合并（不加载模型）
# --------------------------------------------------------------------------

class TestMergeHelpers:
    def _write_adapter(self, tmp_path: Path, **overrides: Any) -> Path:
        adapter = tmp_path / "adapter"
        adapter.mkdir(parents=True, exist_ok=True)
        payload = {
            "base_model_name_or_path": "Qwen/Qwen3.5-2B",
            "lora_alpha": 32,
            "r": 16,
            "target_modules": ["q_proj"],
        }
        payload.update(overrides)
        (adapter / "adapter_config.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return adapter

    def test_reads_base_model(self, tmp_path: Path) -> None:
        adapter = self._write_adapter(tmp_path)
        assert read_adapter_base(adapter) == "Qwen/Qwen3.5-2B"

    def test_reads_scaling(self, tmp_path: Path) -> None:
        adapter = self._write_adapter(tmp_path)
        assert read_adapter_scaling(adapter) == (32.0, 16)

    def test_resolve_prefers_explicit(self, tmp_path: Path) -> None:
        adapter = self._write_adapter(tmp_path)
        assert resolve_base_model(adapter, "custom/model") == "custom/model"

    def test_resolve_falls_back_to_config(self, tmp_path: Path) -> None:
        adapter = self._write_adapter(tmp_path)
        assert resolve_base_model(adapter) == "Qwen/Qwen3.5-2B"

    def test_missing_adapter_config_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(TrainingError, match="adapter_config.json"):
            read_adapter_scaling(empty)

    def test_zero_rank_raises(self, tmp_path: Path) -> None:
        adapter = self._write_adapter(tmp_path, r=0)
        with pytest.raises(TrainingError, match="r 非法"):
            read_adapter_scaling(adapter)

    def test_resolve_raises_without_any_source(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(TrainingError, match="无法确定基座模型"):
            resolve_base_model(empty)


class TestVerifyMergedModel:
    def test_flags_missing_config(self, tmp_path: Path) -> None:
        problems = verify_merged_model(tmp_path)
        assert any("config.json" in item for item in problems)

    def test_flags_adapter_leftover(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        (tmp_path / "model.safetensors").write_bytes(b"x")
        (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
        (tmp_path / "adapter_config.json").write_text("{}", encoding="utf-8")
        problems = verify_merged_model(tmp_path)
        assert any("adapter_config.json" in item for item in problems)

    def test_clean_merged_model_passes(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        (tmp_path / "model-00001-of-00001.safetensors").write_bytes(b"x")
        (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
        assert verify_merged_model(tmp_path) == []


# --------------------------------------------------------------------------
# GGUF 导出
# --------------------------------------------------------------------------

class TestConvertCommand:
    def test_command_shape(self, tmp_path: Path) -> None:
        command = build_convert_command(
            tmp_path / "convert_hf_to_gguf.py",
            tmp_path / "model",
            tmp_path / "out.gguf",
            outtype="f16",
            python_executable="python",
        )
        assert command[0] == "python"
        assert str(tmp_path / "convert_hf_to_gguf.py") in command
        assert "--outfile" in command
        assert "--outtype" in command
        assert command[command.index("--outtype") + 1] == "f16"

    def test_quantize_command_shape(self, tmp_path: Path) -> None:
        command = build_quantize_command(
            tmp_path / "llama-quantize",
            tmp_path / "f16.gguf",
            tmp_path / "q4.gguf",
            "Q4_K_M",
        )
        assert command[1].endswith("f16.gguf")
        assert command[2].endswith("q4.gguf")
        assert command[3] == "Q4_K_M"
        assert "--threads" not in command

    def test_quantize_threads_added_when_set(self, tmp_path: Path) -> None:
        command = build_quantize_command(
            tmp_path / "llama-quantize",
            tmp_path / "f16.gguf",
            tmp_path / "q4.gguf",
            "Q4_K_M",
            threads=4,
        )
        assert command[command.index("--threads") + 1] == "4"


class TestResolveConvertScript:
    def test_finds_at_root(self, tmp_path: Path) -> None:
        script = tmp_path / "convert_hf_to_gguf.py"
        script.write_text("", encoding="utf-8")
        assert resolve_convert_script(tmp_path) == script

    def test_finds_in_scripts_dir(self, tmp_path: Path) -> None:
        (tmp_path / "scripts").mkdir()
        script = tmp_path / "scripts" / "convert_hf_to_gguf.py"
        script.write_text("", encoding="utf-8")
        assert resolve_convert_script(tmp_path) == script

    def test_accepts_direct_script_path(self, tmp_path: Path) -> None:
        script = tmp_path / "my_convert.py"
        script.write_text("", encoding="utf-8")
        assert resolve_convert_script(script) == script

    def test_raises_when_missing(self, tmp_path: Path) -> None:
        with pytest.raises(TrainingError, match="convert_hf_to_gguf.py"):
            resolve_convert_script(tmp_path)

    def test_raises_without_dir(self) -> None:
        with pytest.raises(TrainingError, match="llama.cpp"):
            resolve_convert_script(None)


class TestQuantMatrix:
    def test_covers_design_doc_levels(self) -> None:
        """架构文档 §74.1：Q4 优先、Q3 内存极限、Q2 最后生存模式。"""
        for level in ("Q4_K_M", "Q3_K_M", "Q2_K"):
            assert level in QUANT_MATRIX

    def test_default_is_q4(self) -> None:
        assert DEFAULT_QUANT_TYPES == ("Q4_K_M",)

    def test_size_ratios_are_monotonic(self) -> None:
        order = ["Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q4_K_S", "Q3_K_M", "Q2_K"]
        ratios = [QUANT_MATRIX[item]["size_ratio"] for item in order]
        assert ratios == sorted(ratios, reverse=True)

    def test_matrix_renders_markdown(self) -> None:
        rendered = describe_matrix()
        assert "Q4_K_M" in rendered
        assert rendered.startswith("|")

    def test_estimate_ram_increases_with_precision(self) -> None:
        q2 = estimate_ram_mb(2.0, "Q2_K")
        q4 = estimate_ram_mb(2.0, "Q4_K_M")
        q8 = estimate_ram_mb(2.0, "Q8_0")
        assert q2 < q4 < q8

    def test_estimate_ram_is_plausible_for_2b_q4(self) -> None:
        """2B Q4 在弱 VPS 上应在 1~2GB 量级，否则估算模型有问题。"""
        ram = estimate_ram_mb(2.0, "Q4_K_M", context_tokens=2048)
        assert 700 < ram < 2000

    def test_unknown_quant_raises(self) -> None:
        with pytest.raises(TrainingError):
            estimate_ram_mb(2.0, "Q9_K_XL")


class TestExportConfig:
    def test_defaults(self) -> None:
        config = ExportConfig(model_dir="m", output_dir="o")
        assert config.outtype == "f16"
        assert config.quant_types == ("Q4_K_M",)
        assert config.dry_run is False
        assert config.convert_script is None
        assert config.quantize_binary is None

    def test_dry_run_does_not_need_tools(self, tmp_path: Path) -> None:
        """dry-run 在没有任何 llama.cpp 的环境下也必须能出计划。"""
        from qboss_training.training.export_gguf import run_export

        config = ExportConfig(
            model_dir=str(tmp_path / "model"),
            output_dir=str(tmp_path / "out"),
            dry_run=True,
        )
        report = run_export(config)
        assert report.dry_run is True
        assert len(report.commands) == 2  # convert + 1 quantize
        assert any("<" in str(item) for command in report.commands for item in command)
        assert any("工具未就绪" in note for note in report.notes)

    def test_explicit_convert_script_is_used(self, tmp_path: Path) -> None:
        from qboss_training.training.export_gguf import run_export

        script = tmp_path / "convert_hf_to_gguf.py"
        script.write_text("", encoding="utf-8")
        binary = tmp_path / "llama-quantize"
        binary.write_text("", encoding="utf-8")

        report = run_export(
            ExportConfig(
                model_dir=str(tmp_path / "m"),
                output_dir=str(tmp_path / "o"),
                convert_script=str(script),
                quantize_binary=str(binary),
                quant_types=("Q4_K_M", "Q3_K_M"),
                dry_run=True,
            )
        )
        assert len(report.commands) == 3
        assert str(script) in report.commands[0]
        assert str(binary) in report.commands[1]

    def test_missing_explicit_script_raises(self, tmp_path: Path) -> None:
        from qboss_training.training.export_gguf import run_export

        with pytest.raises(TrainingError, match="转换脚本不存在"):
            run_export(
                ExportConfig(
                    model_dir=str(tmp_path / "m"),
                    output_dir=str(tmp_path / "o"),
                    convert_script=str(tmp_path / "nope.py"),
                    dry_run=True,
                )
            )

    def test_real_run_without_tools_raises(self, tmp_path: Path) -> None:
        from qboss_training.training.export_gguf import run_export

        with pytest.raises(TrainingError):
            run_export(
                ExportConfig(
                    model_dir=str(tmp_path / "m"),
                    output_dir=str(tmp_path / "o"),
                )
            )

    def test_unknown_quant_type_raises(self, tmp_path: Path) -> None:
        from qboss_training.training.export_gguf import run_export

        with pytest.raises(TrainingError, match="未知量化类型"):
            run_export(
                ExportConfig(
                    model_dir=str(tmp_path / "m"),
                    output_dir=str(tmp_path / "o"),
                    quant_types=("Q9_K_XL",),
                    dry_run=True,
                )
            )


class TestResolveGgufFiles:
    def test_lists_gguf_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.gguf").write_bytes(b"x")
        (tmp_path / "b.gguf").write_bytes(b"x")
        (tmp_path / "c.txt").write_text("x", encoding="utf-8")
        found = resolve_gguf_files(tmp_path)
        assert [item.name for item in found] == ["a.gguf", "b.gguf"]

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert resolve_gguf_files(tmp_path) == []


# --------------------------------------------------------------------------
# CPU 基准
# --------------------------------------------------------------------------

class TestLatencyStats:
    def test_empty_samples(self) -> None:
        stats = LatencyStats.from_samples([])
        assert stats.runs == 0
        assert stats.mean == 0.0

    def test_single_sample(self) -> None:
        stats = LatencyStats.from_samples([0.5])
        assert stats.mean == 0.5
        assert stats.median == 0.5
        assert stats.stdev == 0.0

    def test_percentiles(self) -> None:
        stats = LatencyStats.from_samples([float(i) for i in range(1, 101)])
        assert stats.minimum == 1.0
        assert stats.maximum == 100.0
        assert stats.p90 >= 85.0
        assert stats.p99 >= 95.0

    def test_to_dict_keys(self) -> None:
        payload = LatencyStats.from_samples([0.1, 0.2]).to_dict()
        for key in ("mean_s", "median_s", "p90_s", "p99_s", "min_s", "max_s", "stdev_s"):
            assert key in payload


class TestBenchmarkBackend:
    def test_echo_backend_is_perfect(self, small_fixture_records: list[dict]) -> None:
        backend = build_gold_replay_backend(small_fixture_records)
        config = BenchmarkConfig(warmup_runs=1, repeat=1, samples_per_task=3)
        result = benchmark_backend(backend, small_fixture_records, config)
        assert result.schema_pass_rate == 1.0
        assert result.invariant_pass_rate == 1.0
        assert result.calls > 0

    def test_warmup_calls_are_excluded_from_stats(
        self, small_fixture_records: list[dict]
    ) -> None:
        backend = build_gold_replay_backend(small_fixture_records)
        config = BenchmarkConfig(warmup_runs=2, repeat=1, samples_per_task=2)
        result = benchmark_backend(backend, small_fixture_records, config)
        # 2 个任务 × 2 条 = 4 条输入，repeat=1 → 测量 4 次；暖机不计入
        assert result.calls == 4
        assert result.latency.runs == 4

    def test_empty_backend_lowers_quality_scores(
        self, small_fixture_records: list[dict]
    ) -> None:
        backend = EchoBackend({})
        config = BenchmarkConfig(warmup_runs=0, repeat=1, samples_per_task=2)
        result = benchmark_backend(backend, small_fixture_records, config)
        assert result.schema_pass_rate == 0.0

    def test_backend_errors_are_captured(self, small_fixture_records: list[dict]) -> None:
        class BrokenBackend:
            name = "broken"

            def generate(self, request: GenerationRequest) -> GenerationResponse:
                raise RuntimeError("模拟崩溃")

            def describe(self) -> dict[str, Any]:
                return {}

        result = benchmark_backend(
            BrokenBackend(),  # type: ignore[arg-type]
            small_fixture_records,
            BenchmarkConfig(warmup_runs=0, repeat=1),
        )
        assert result.failures
        assert result.calls == 0

    def test_meets_targets_reports_problems(self) -> None:
        result = BenchmarkResult(
            label="x",
            latency=LatencyStats(mean=20.0, p90=25.0),
            schema_pass_rate=0.5,
        )
        problems = result.meets_targets(BenchmarkConfig(target_latency_s=8.0))
        assert len(problems) == 2

    def test_meets_targets_passes(self) -> None:
        result = BenchmarkResult(
            label="x",
            latency=LatencyStats(mean=1.0, p90=1.5),
            schema_pass_rate=1.0,
        )
        assert result.meets_targets(BenchmarkConfig()) == []

    def test_empty_records_raises(self) -> None:
        with pytest.raises(TrainingError, match="基准数据为空"):
            benchmark_backend(
                build_gold_replay_backend([]), [], BenchmarkConfig()
            )


class TestBenchmarkReport:
    def test_run_benchmark_writes_report(
        self, tmp_path: Path, small_fixture_records: list[dict]
    ) -> None:
        backend = build_gold_replay_backend(small_fixture_records)
        report = run_benchmark(
            backend,
            small_fixture_records,
            BenchmarkConfig(warmup_runs=0, repeat=1, samples_per_task=2),
            output_dir=tmp_path,
            label="test",
        )
        assert (tmp_path / "benchmark_report.json").exists()
        payload = json.loads(
            (tmp_path / "benchmark_report.json").read_text(encoding="utf-8")
        )
        assert payload["results"][0]["label"] == "test"

    def test_best_by_latency(self) -> None:
        report = BenchmarkReport(
            results=[
                BenchmarkResult(label="slow", latency=LatencyStats(mean=5, p90=6)),
                BenchmarkResult(label="fast", latency=LatencyStats(mean=1, p90=1.2)),
            ]
        )
        best = report.best_by_latency()
        assert best is not None and best.label == "fast"

    def test_best_by_latency_empty(self) -> None:
        assert BenchmarkReport().best_by_latency() is None

    def test_summary_marks_status(self) -> None:
        report = BenchmarkReport(
            results=[
                BenchmarkResult(
                    label="ok", latency=LatencyStats(mean=1, p90=1.2), schema_pass_rate=1.0
                )
            ]
        )
        rendered = render_benchmark_summary(report)
        assert "达标" in rendered
        assert "判读建议" in rendered

    def test_summary_flags_unmet_targets(self) -> None:
        report = BenchmarkReport(
            results=[
                BenchmarkResult(
                    label="bad",
                    latency=LatencyStats(mean=30, p90=40),
                    schema_pass_rate=0.2,
                )
            ]
        )
        rendered = render_benchmark_summary(report)
        assert "未达标" in rendered

    def test_jsonl_append(self, tmp_path: Path, small_fixture_records: list[dict]) -> None:
        report = BenchmarkReport(
            results=[BenchmarkResult(label="a", latency=LatencyStats(mean=1, p90=1))]
        )
        target = tmp_path / "runs.jsonl"
        benchmark_report_to_jsonl(report, target)
        benchmark_report_to_jsonl(report, target)
        lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line]
        assert len(lines) == 2


class TestInferenceMessages:
    def test_matches_sft_serialization(self, sample_event_eval: dict) -> None:
        """推理提示词必须与 SFT 构建时完全一致，否则训练/推理分布不匹配。"""
        from qboss_training.sft.format import build_messages

        inference = build_inference_messages("event_eval", sample_event_eval["input"])
        training = build_messages(sample_event_eval, pretty=True)
        assert inference[0]["content"] == training[0]["content"]
        assert inference[1]["content"] == training[1]["content"]

    def test_default_pretty_matches_sft_default(self, sample_emotion_explain: dict) -> None:
        """**默认值**必须一致：训练侧 pretty_json 默认 True，推理侧也必须如此。

        这条断言是防"默认值漂移"的：两边默认值不同会让评测悄悄变差，
        而且极难排查（训练能过、推理不过）。
        """
        from qboss_training.sft.format import SFTBuildConfig, build_messages

        assert SFTBuildConfig(model_name_or_path="x").pretty_json is True

        inference = build_inference_messages(
            "emotion_explain", sample_emotion_explain["input"]
        )
        training = build_messages(sample_emotion_explain)
        assert inference[1]["content"] == training[1]["content"]

    def test_compact_mode_matches_compact_training(self, sample_event_eval: dict) -> None:
        from qboss_training.sft.format import build_messages

        inference = build_inference_messages(
            "event_eval", sample_event_eval["input"], pretty=False
        )
        training = build_messages(sample_event_eval, pretty=False)
        assert inference[1]["content"] == training[1]["content"]

    @pytest.mark.parametrize("task", ["event_eval", "emotion_explain"])
    def test_prompt_shape(self, task: str, sample_event_eval: dict, sample_emotion_explain: dict) -> None:
        record = sample_event_eval if task == "event_eval" else sample_emotion_explain
        messages = build_inference_messages(task, record["input"])
        assert [item["role"] for item in messages] == ["system", "user"]
        assert messages[0]["content"]

    def test_system_prompt_comes_from_contract(self, sample_event_eval: dict) -> None:
        from qboss_training.contracts import get_contract

        messages = build_inference_messages("event_eval", sample_event_eval["input"])
        assert messages[0]["content"] == get_contract("event_eval").system_prompt
