"""推理后端抽象：评测与 benchmark 共用。

三个实现：
  * :class:`EchoBackend`     —— 不加载模型，回放给定输出。用于无模型/无网络的测试。
  * :class:`TransformersBackend` —— 本地 BF16 / LoRA / 合并后模型（需要 torch+transformers）。
  * :class:`LlamaCppBackend` —— llama.cpp GGUF，CPU 部署路径（需要 llama-cpp-python）。

提示词构造在两种后端之间共享（:func:`build_inference_messages`），
因此 CPU 量化后的行为与训练时看到的一致。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .contracts import get_contract
from .utils.jsonx import extract_json

LOGGER = logging.getLogger("qboss_training.inference")


@dataclass
class GenerationRequest:
    """一次生成请求。"""

    messages: list[dict[str, str]]
    max_new_tokens: int = 384
    temperature: float = 0.0
    top_p: float = 1.0
    stop: tuple[str, ...] = ()


@dataclass
class GenerationResponse:
    """一次生成结果（含耗时，供 benchmark 使用）。"""

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens_per_second(self) -> float:
        if self.latency_s <= 0 or not self.completion_tokens:
            return 0.0
        return self.completion_tokens / self.latency_s


class InferenceBackend(Protocol):
    """后端协议。"""

    name: str

    def generate(self, request: GenerationRequest) -> GenerationResponse: ...

    def describe(self) -> dict[str, Any]: ...


def build_inference_messages(
    task: str,
    model_input: Mapping[str, Any],
    *,
    pretty: bool = True,
) -> list[dict[str, str]]:
    """构造推理用消息。

    **必须与 SFT 构建时完全一致**（同样的 system prompt、同样的 JSON 序列化
    缩进策略），否则会出现"训练能过、推理不过"的诡异现象。
    """
    contract = get_contract(task)
    indent = 1 if pretty else None
    return [
        {"role": "system", "content": contract.system_prompt},
        {
            "role": "user",
            "content": json.dumps(model_input, ensure_ascii=False, indent=indent),
        },
    ]


def extract_output(text: str, task: str) -> tuple[dict[str, Any] | None, str | None]:
    """从生成文本里抽 JSON。

    Returns:
        ``(解析结果或 None, 错误信息或 None)``
    """
    contract = get_contract(task)

    def _accept(candidate: Any) -> bool:
        if not isinstance(candidate, Mapping):
            return False
        return set(contract.required_fields) <= set(candidate)

    try:
        payload = extract_json(text, validate=_accept)
    except Exception as exc:  # JsonExtractionError 及其它
        return None, str(exc)
    if isinstance(payload, Mapping) and isinstance(payload.get("output"), Mapping):
        payload = payload["output"]
    return (dict(payload) if isinstance(payload, Mapping) else None), None


class EchoBackend:
    """回放后端：按记录 id → 输出映射返回文本，不加载任何模型。

    用途：
      * 离线跑通评测/benchmark 流程（无网络、无 GPU、无权重）；
      * 作为评测脚本自身的回归测试夹具。
    """

    name = "echo"

    def __init__(
        self,
        outputs: Mapping[str, Any] | None = None,
        *,
        latency_s: float = 0.0,
        completion_tokens: int = 32,
        wrap_in_fence: bool = False,
    ) -> None:
        self._outputs = dict(outputs or {})
        self._latency = latency_s
        self._completion_tokens = completion_tokens
        self._wrap = wrap_in_fence
        self.calls: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.calls.append(request)
        user_content = ""
        for message in reversed(request.messages):
            if message.get("role") == "user":
                user_content = message.get("content", "")
                break
        text = self._resolve(user_content)
        if self._wrap:
            text = f"```json\n{text}\n```"
        return GenerationResponse(
            text=text,
            prompt_tokens=len(user_content),
            completion_tokens=self._completion_tokens,
            latency_s=self._latency,
        )

    def _resolve(self, user_content: str) -> str:
        if not self._outputs:
            return "{}"
        try:
            model_input = json.loads(user_content)
        except (json.JSONDecodeError, ValueError):
            return "{}"
        key = json.dumps(model_input, ensure_ascii=False, sort_keys=True)
        if key in self._outputs:
            return json.dumps(self._outputs[key], ensure_ascii=False)
        return "{}"

    def describe(self) -> dict[str, Any]:
        return {"backend": "echo", "canned_outputs": len(self._outputs)}


class TransformersBackend:
    """本地 transformers 推理（BF16 / LoRA adapter / 合并权重）。

    延迟导入 torch/transformers，未安装时构造即报清晰错误。
    """

    def __init__(
        self,
        model_path: str,
        *,
        adapter_path: str | None = None,
        device: str = "auto",
        dtype: str = "bfloat16",
        trust_remote_code: bool = False,
    ) -> None:
        import torch  # noqa: F401  (延迟导入，未安装时报错更清晰)
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.name = "transformers"
        self.model_path = model_path
        self.adapter_path = adapter_path

        torch_dtype = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }.get(dtype, torch.bfloat16)

        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, trust_remote_code=trust_remote_code
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch_dtype,
            device_map=device if device != "cpu" else None,
            trust_remote_code=trust_remote_code,
        )
        if device == "cpu":
            self.model = self.model.to("cpu")

        if adapter_path:
            from peft import PeftModel

            self.model = PeftModel.from_pretrained(self.model, adapter_path)

        self.model.eval()
        self.device = next(self.model.parameters()).device

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        import torch

        text = self._render(request.messages)

        inputs = self.tokenizer(text, return_tensors="pt", add_special_tokens=False)
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        prompt_tokens = int(inputs["input_ids"].shape[1])

        do_sample = request.temperature and request.temperature > 0
        generation_kwargs: dict[str, Any] = {
            "max_new_tokens": request.max_new_tokens,
            "do_sample": bool(do_sample),
            "pad_token_id": self.tokenizer.pad_token_id,
        }
        if do_sample:
            generation_kwargs["temperature"] = request.temperature
            generation_kwargs["top_p"] = request.top_p

        started = time.perf_counter()
        with torch.inference_mode():
            generated = self.model.generate(**inputs, **generation_kwargs)
        latency = time.perf_counter() - started

        new_tokens = generated[0][prompt_tokens:]
        decoded = self.tokenizer.decode(new_tokens, skip_special_tokens=True)
        for stop in request.stop:
            if stop in decoded:
                decoded = decoded.split(stop)[0]

        return GenerationResponse(
            text=decoded,
            prompt_tokens=prompt_tokens,
            completion_tokens=int(new_tokens.shape[0]),
            latency_s=latency,
        )

    def _render(self, messages: Sequence[Mapping[str, str]]) -> str:
        """渲染提示词；Qwen3 系模板支持 enable_thinking=False。"""
        try:
            return self.tokenizer.apply_chat_template(
                list(messages),
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
        except TypeError:
            return self.tokenizer.apply_chat_template(
                list(messages), tokenize=False, add_generation_prompt=True
            )

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "transformers",
            "model_path": self.model_path,
            "adapter_path": self.adapter_path,
            "device": str(self.device),
        }


class LlamaCppBackend:
    """llama.cpp GGUF 推理（CPU 部署路径）。"""

    name = "llama_cpp"

    def __init__(
        self,
        gguf_path: str,
        *,
        n_ctx: int = 2048,
        n_threads: int | None = None,
        n_batch: int = 256,
        chat_format: str | None = None,
        verbose: bool = False,
    ) -> None:
        from llama_cpp import Llama

        self.gguf_path = gguf_path
        self.llm = Llama(
            model_path=gguf_path,
            n_ctx=n_ctx,
            n_threads=n_threads,
            n_batch=n_batch,
            chat_format=chat_format,
            verbose=verbose,
            logits_all=False,
        )

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        started = time.perf_counter()
        result = self.llm.create_chat_completion(
            messages=list(request.messages),
            max_tokens=request.max_new_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            stop=list(request.stop) or None,
        )
        latency = time.perf_counter() - started

        choices = result.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = result.get("usage") or {}
        return GenerationResponse(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_s=latency,
            meta={"finish_reason": choices[0].get("finish_reason")},
        )

    def describe(self) -> dict[str, Any]:
        return {"backend": "llama_cpp", "gguf_path": self.gguf_path}


def load_backend(
    backend: str,
    *,
    model_path: str | None = None,
    adapter_path: str | None = None,
    gguf_path: str | None = None,
    device: str = "auto",
    dtype: str = "bfloat16",
    n_threads: int | None = None,
    n_ctx: int = 2048,
) -> InferenceBackend:
    """按名字构造后端。"""
    if backend == "transformers":
        if not model_path:
            raise ValueError("backend=transformers 需要 --model-path")
        return TransformersBackend(
            model_path, adapter_path=adapter_path, device=device, dtype=dtype
        )
    if backend == "llama_cpp":
        target = gguf_path or model_path
        if not target:
            raise ValueError("backend=llama_cpp 需要 --gguf-path")
        return LlamaCppBackend(target, n_threads=n_threads, n_ctx=n_ctx)
    if backend == "echo":
        return EchoBackend()
    raise ValueError(f"未知 backend={backend!r}，可选 transformers / llama_cpp / echo")


def build_gold_replay_backend(
    records: Sequence[Mapping[str, Any]], *, wrap_in_fence: bool = False
) -> "EchoBackend":
    """构造"用标注当模型回复"的回放后端。

    这等价于一个**完全正确的上界基线**：schema / 不变量 / 文本约束都应为 100%。
    两个用途：
      * 在无网络、无权重、无 GPU 的环境里验证整条评测/基准管线本身是否正常；
      * 作为真实模型的对照上界（真实模型若在某个指标上低于它，说明确有退化）。
    """
    canned: dict[str, Any] = {}
    for record in records:
        model_input = record.get("input") or {}
        key = json.dumps(model_input, ensure_ascii=False, sort_keys=True)
        canned[key] = record.get("output") or {}
    return EchoBackend(canned, wrap_in_fence=wrap_in_fence)


def resolve_gguf_files(directory: str | Path) -> list[Path]:
    """列出目录下的 GGUF 文件（供导出脚本与 benchmark 使用）。"""
    return sorted(Path(directory).glob("*.gguf"))
