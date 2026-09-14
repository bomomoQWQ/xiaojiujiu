"""Print GGUF metadata for a converted model (development aid)."""

from __future__ import annotations

import sys
from pathlib import Path

from gguf import GGUFReader


def main() -> int:
    """Print the block count and architecture of the given GGUF file."""
    path = Path(sys.argv[1])
    reader = GGUFReader(str(path))
    print("architecture:", reader.get_field("general.architecture").contents())
    for field in reader.fields.values():
        name = field.name
        if any(key in name for key in ("block_count", "attention.head_count", "context_length")):
            try:
                print(f"{name} = {field.contents()}")
            except Exception as exc:  # noqa: BLE001
                print(f"{name} = <unreadable: {exc}>")
    blocks = sorted({tensor.name.split(".")[1] for tensor in reader.tensors if tensor.name.startswith("blk.")})
    print("block indices:", len(blocks), blocks[:3], "...", blocks[-3:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
