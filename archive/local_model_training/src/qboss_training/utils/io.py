"""数据 IO：JSONL / JSON 读写，UTF-8，原子写入。

所有落盘都走原子替换（临时文件 + os.replace），避免中断留下半截文件，
这对"可断点续跑"的生成器是必需的。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """读取 JSONL；空行与以 # 开头的行会被忽略。"""
    records: list[dict[str, Any]] = []
    for line_no, line in _iter_lines(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        payload = _parse_line(path, line_no, stripped)
        records.append(payload)
    return records


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """流式读取 JSONL（大文件友好）。"""
    for line_no, line in _iter_lines(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield _parse_line(path, line_no, stripped)


def _parse_line(path: str | Path, line_no: int, stripped: str) -> dict[str, Any]:
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"{path}:{line_no} 应为 JSON object，实际为 {type(payload).__name__}"
        )
    return payload


def _iter_lines(path: str | Path) -> Iterator[tuple[int, str]]:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"文件不存在：{source}")
    with source.open("r", encoding="utf-8", newline="") as handle:
        for number, line in enumerate(handle, start=1):
            yield number, line


def dumps_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def write_jsonl(
    path: str | Path,
    records: Iterable[dict[str, Any]],
    *,
    sort_by: str | None = None,
) -> Path:
    """原子写入 JSONL。``sort_by`` 指定字段时按键排序，保证可复现。"""
    target = Path(path)
    ensure_dir(target.parent if str(target.parent) else ".")
    items = list(records)
    if sort_by:
        items.sort(key=lambda item: str(item.get(sort_by, "")))
    buffer = io.StringIO()
    for item in items:
        buffer.write(dumps_line(item))
        buffer.write("\n")
    _atomic_write_text(target, buffer.getvalue())
    return target


def append_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> Path:
    """追加写入（生成器增量落盘用，不做原子替换以保留已有内容）。"""
    target = Path(path)
    ensure_dir(target.parent if str(target.parent) else ".")
    with target.open("a", encoding="utf-8", newline="") as handle:
        for item in records:
            handle.write(dumps_line(item))
            handle.write("\n")
    return target


def read_json(path: str | Path, default: Any = None) -> Any:
    source = Path(path)
    if not source.exists():
        return default
    with source.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, payload: Any, *, indent: int = 2) -> Path:
    target = Path(path)
    ensure_dir(target.parent if str(target.parent) else ".")
    text = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False)
    _atomic_write_text(target, text + "\n")
    return target


def _atomic_write_text(target: Path, text: str) -> None:
    target = Path(target)
    ensure_dir(target.parent if str(target.parent) else ".")
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        delete=False,
        dir=str(target.parent or "."),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def file_sha256(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def count_lines(path: str | Path) -> int:
    total = 0
    with Path(path).open("r", encoding="utf-8") as handle:
        for _ in handle:
            total += 1
    return total
