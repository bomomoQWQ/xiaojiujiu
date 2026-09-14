"""Restore the untrained MTP head that PEFT drops when saving a merged model.

``Qwen3.5-2B`` ships a multi-token-prediction head (``mtp.*``). PEFT's
``merge_and_unload`` only saves the language model it wrapped, so the merged
checkpoint loses those tensors while the config still declares
``mtp_num_hidden_layers = 1``. The GGUF exporter then writes
``block_count = 25`` and llama.cpp fails with ``tensor 'blk.24.attn_norm.weight'
not found``.

The MTP head is never trained by our LoRA, so the correct fix is to copy it back
verbatim from the base checkpoint rather than to hide it from the metadata.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

BASE_INDEX = Path("/root/models/Qwen3.5-2B/model.safetensors.index.json")
BASE_WEIGHTS = Path("/root/models/Qwen3.5-2B/model.safetensors-00001-of-00001.safetensors")
MERGED_DIR = Path("/root/companion-training/outputs/v1-merged")
TARGET_DIR = Path("/root/companion-training/outputs/v1-merged-mtp")


def main() -> int:
    """Copy ``mtp.*`` tensors from the base checkpoint into a merged copy."""
    index = json.loads(BASE_INDEX.read_text())
    mtp_keys = [name for name in index["weight_map"] if name.startswith("mtp.")]
    if not mtp_keys:
        print("base checkpoint has no mtp tensors; nothing to restore", file=sys.stderr)
        return 1

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json"):
        source = MERGED_DIR / name
        if source.exists():
            shutil.copy2(source, TARGET_DIR / name)
    for extra in MERGED_DIR.glob("*.jinja"):
        shutil.copy2(extra, TARGET_DIR / extra.name)

    base = load_file(str(BASE_WEIGHTS), device="cpu")
    merged_path = MERGED_DIR / "model.safetensors"
    merged = load_file(str(merged_path), device="cpu")
    added = 0
    for key in mtp_keys:
        if key in merged:
            continue
        tensor = base.get(key)
        if tensor is None:
            print(f"missing in base shard: {key}", file=sys.stderr)
            return 1
        merged[key] = tensor.to(torch.bfloat16)
        added += 1

    save_file(merged, str(TARGET_DIR / "model.safetensors"), metadata={"format": "pt"})
    print(f"merged tensors: {len(merged) - added}, mtp restored: {added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
