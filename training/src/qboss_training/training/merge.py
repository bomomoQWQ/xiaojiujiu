"""合并 LoRA adapter 到基座权重。

为什么必须合并（而不是直接拿 adapter 去量化）
--------------------------------------------
llama.cpp 的 ``convert_hf_to_gguf.py`` 只能读**标准 HF 权重**，不认识
``adapter_config.json`` / ``adapter_model.safetensors``。所以
"训练 → GGUF → CPU 部署"这条链路里，合并是必经步骤：

    LoRA adapter ──merge──> 合并后 HF 权重 ──convert──> GGUF f16 ──quantize──> Q4_K_M

实现上优先用 ``peft`` 官方合并，失败时回退到手写合并——
手写回退的价值在于：即使 peft 版本变化或 merge 报错，
仍有一条可解释、可调试的路径把 ``W + (alpha/r)·BA`` 算出来。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import TrainingError
from ..utils.io import write_json
from ..utils.secrets import utc_now_iso

LOGGER = logging.getLogger("qboss_training.merge")


@dataclass
class MergeReport:
    """合并结果报告。"""

    base_model: str = ""
    adapter_path: str = ""
    output_dir: str = ""
    method: str = ""
    dtype: str = ""
    merged_at: str = field(default_factory=utc_now_iso)
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_model": self.base_model,
            "adapter_path": self.adapter_path,
            "output_dir": self.output_dir,
            "method": self.method,
            "dtype": self.dtype,
            "merged_at": self.merged_at,
            "files": self.files,
            "warnings": self.warnings,
        }


def read_adapter_base(adapter_path: str | Path) -> str | None:
    """从 ``adapter_config.json`` 读基座模型路径。"""
    config_path = Path(adapter_path) / "adapter_config.json"
    if not config_path.exists():
        return None
    with config_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("base_model_name_or_path")


def read_adapter_scaling(adapter_path: str | Path) -> tuple[float, int]:
    """读 LoRA 的 ``lora_alpha`` 与 ``r``，用于手写合并。"""
    config_path = Path(adapter_path) / "adapter_config.json"
    if not config_path.exists():
        raise TrainingError(f"不是有效的 adapter 目录（缺少 adapter_config.json）：{adapter_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    alpha = float(payload.get("lora_alpha", 1.0))
    rank = int(payload.get("r", 1))
    if rank <= 0:
        raise TrainingError(f"adapter 的 r 非法：{rank}")
    return alpha, rank


def resolve_base_model(adapter_path: str | Path, explicit: str | None = None) -> str:
    base = explicit or read_adapter_base(adapter_path)
    if not base:
        raise TrainingError(
            f"无法确定基座模型。请在命令里显式传入 --base-model，"
            f"或确认 {adapter_path}/adapter_config.json 里有 base_model_name_or_path。"
        )
    return base


def merge_with_peft(
    adapter_path: str | Path,
    output_dir: str | Path,
    *,
    base_model: str,
    dtype: str = "bfloat16",
    device: str = "cpu",
    safe_serialization: bool = True,
) -> MergeReport:
    """用 peft 官方接口合并（首选路径）。"""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    dtype_map = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    torch_dtype = dtype_map.get(dtype, torch.bfloat16)

    LOGGER.info("加载基座 %s（dtype=%s）", base_model, dtype)
    model = AutoModelForCausalLM.from_pretrained(
        base_model, torch_dtype=torch_dtype, device_map=None
    )

    LOGGER.info("加载 adapter %s", adapter_path)
    peft_model = PeftModel.from_pretrained(model, str(adapter_path))

    LOGGER.info("合并权重（merge_and_unload）")
    merged = peft_model.merge_and_unload()
    merged = merged.to(dtype=torch_dtype)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(
        str(output_path), safe_serialization=safe_serialization, max_shard_size="4GB"
    )

    tokenizer_source = adapter_path if (Path(adapter_path) / "tokenizer_config.json").exists() else base_model
    tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_source))
    tokenizer.save_pretrained(str(output_path))

    report = MergeReport(
        base_model=base_model,
        adapter_path=str(adapter_path),
        output_dir=str(output_path),
        method="peft.merge_and_unload",
        dtype=dtype,
        files=sorted(path.name for path in output_path.iterdir() if path.is_file()),
    )
    return report


def merge_manually(
    adapter_path: str | Path,
    output_dir: str | Path,
    *,
    base_model: str,
    dtype: str = "bfloat16",
) -> MergeReport:
    """手写合并回退：``W' = W + (alpha/r) · B @ A``。

    只在 peft 合并失败时使用。实现假设 LoRA 作用在 ``nn.Linear`` 上，
    这是本工程 target_modules（q/k/v/o_proj 等）的实际形态。
    """
    import torch
    from safetensors.torch import load_file
    from transformers import AutoModelForCausalLM, AutoTokenizer

    dtype_map = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    torch_dtype = dtype_map.get(dtype, torch.bfloat16)
    scaling = _load_scaling(adapter_path)

    LOGGER.info("手工合并：加载基座 %s", base_model)
    model = AutoModelForCausalLM.from_pretrained(base_model, torch_dtype=torch_dtype)

    weights_path = Path(adapter_path) / "adapter_model.safetensors"
    if not weights_path.exists():
        raise TrainingError(
            f"找不到 {weights_path}；手写合并只支持 safetensors 格式的 adapter。"
        )
    adapter_weights = load_file(str(weights_path))

    # 按模块归组：key 形如 base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight
    grouped: dict[str, dict[str, torch.Tensor]] = {}
    for key, tensor in adapter_weights.items():
        if ".lora_A." in key:
            module = key.split(".lora_A.")[0]
            grouped.setdefault(module, {})["A"] = tensor
        elif ".lora_B." in key:
            module = key.split(".lora_B.")[0]
            grouped.setdefault(module, {})["B"] = tensor

    named = dict(model.named_parameters())
    merged_count = 0
    warnings: list[str] = []
    with torch.no_grad():
        for module, parts in grouped.items():
            if "A" not in parts or "B" not in parts:
                warnings.append(f"{module}: 缺少 lora_A 或 lora_B，已跳过")
                continue
            target_name = _candidate_parameter_names(module, named)
            if target_name is None:
                warnings.append(f"{module}: 在基座里找不到对应参数，已跳过")
                continue

            target = named[target_name]
            delta = (parts["B"].to(torch.float32) @ parts["A"].to(torch.float32)) * scaling
            target.data = (target.data.to(torch.float32) + delta).to(target.dtype)
            merged_count += 1

    if merged_count == 0:
        raise TrainingError(
            "手工合并没有匹配到任何参数。请改用 peft 合并路径，或检查 adapter 的 "
            "target_modules 与基座结构是否一致。"
        )

    LOGGER.info("手工合并了 %d 个模块（scaling=%.4f）", merged_count, scaling)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(output_path), safe_serialization=True, max_shard_size="4GB")

    tokenizer_source = adapter_path if (Path(adapter_path) / "tokenizer_config.json").exists() else base_model
    tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_source))
    tokenizer.save_pretrained(str(output_path))

    return MergeReport(
        base_model=base_model,
        adapter_path=str(adapter_path),
        output_dir=str(output_path),
        method="manual_merge",
        dtype=dtype,
        files=sorted(path.name for path in output_path.iterdir() if path.is_file()),
        warnings=warnings,
    )


def _load_scaling(adapter_path: str | Path) -> float:
    alpha, rank = read_adapter_scaling(adapter_path)
    return alpha / rank


def _candidate_parameter_names(
    module: str, named: dict[str, Any]
) -> str | None:
    """把 adapter 的模块名映射到基座参数名。

    不同 peft/transformers 版本的前缀不一致（``base_model.model.`` /
    ``base_model.`` / 无前缀），因此做多候选尝试。
    """
    trimmed = module
    for prefix in ("base_model.model.", "base_model.", "model."):
        if trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix) :]
            break

    candidates = [
        module + ".weight",
        f"base_model.model.{trimmed}.weight",
        f"base_model.{trimmed}.weight",
        f"{trimmed}.weight",
        f"model.{trimmed}.weight",
    ]
    for candidate in candidates:
        if candidate in named:
            return candidate
    # 后缀匹配兜底
    suffix = f"{trimmed}.weight"
    for name in named:
        if name.endswith(suffix):
            return name
    return None


def merge_adapter(
    adapter_path: str | Path,
    output_dir: str | Path,
    *,
    base_model: str | None = None,
    dtype: str = "bfloat16",
    method: str = "auto",
) -> MergeReport:
    """合并 adapter 到基座。``method`` 取 auto / peft / manual。"""
    resolved_base = resolve_base_model(adapter_path, base_model)
    output_path = Path(output_dir)
    if output_path.exists() and any(output_path.iterdir()):
        LOGGER.warning("输出目录非空，将覆盖同名文件：%s", output_path)

    if method in ("auto", "peft"):
        try:
            report = merge_with_peft(
                adapter_path, output_path, base_model=resolved_base, dtype=dtype
            )
        except Exception as exc:
            if method == "peft":
                raise
            LOGGER.warning("peft 合并失败（%s），回退到手工合并", type(exc).__name__)
            report = merge_manually(
                adapter_path, output_path, base_model=resolved_base, dtype=dtype
            )
            report.warnings.append(f"peft 合并失败，已回退：{exc}")
    else:
        report = merge_manually(
            adapter_path, output_path, base_model=resolved_base, dtype=dtype
        )

    write_json(output_path / "merge_report.json", report.to_dict())
    return report


def verify_merged_model(output_dir: str | Path) -> list[str]:
    """检查合并产物是否具备转换为 GGUF 的最低前提。"""
    path = Path(output_dir)
    problems: list[str] = []

    if not (path / "config.json").exists():
        problems.append("缺少 config.json：不是完整的 HF 模型目录")

    has_weights = any(
        path.glob(pattern)
        for pattern in ("*.safetensors", "pytorch_model*.bin")
    )
    if not has_weights:
        problems.append("缺少权重文件（*.safetensors 或 pytorch_model*.bin）")

    if (path / "adapter_config.json").exists():
        problems.append(
            "目录里还有 adapter_config.json：这看起来仍是 adapter 而非合并后的模型，"
            "llama.cpp 转换脚本无法处理"
        )

    tokenizer_files = [
        name
        for name in ("tokenizer.json", "tokenizer_config.json", "vocab.json")
        if (path / name).exists()
    ]
    if not tokenizer_files:
        problems.append("缺少 tokenizer 文件，转换时无法写入词表元数据")

    return problems
