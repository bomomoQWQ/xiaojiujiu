"""Verify the local Qwen3.5-2B text path without network access."""

from pathlib import Path

import torch
from transformers import AutoModelForImageTextToText, AutoTokenizer

model_path = Path("/root/models/Qwen3.5-2B")
tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
model = AutoModelForImageTextToText.from_pretrained(
    model_path,
    local_files_only=True,
    dtype=torch.bfloat16,
    device_map="cpu",
)
print(type(model).__name__)
print(sum(parameter.numel() for parameter in model.parameters()))
print(type(tokenizer).__name__)
print(tokenizer.decode(tokenizer("CPU text verification")["input_ids"]))
