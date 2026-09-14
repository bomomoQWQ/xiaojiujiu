"""数据 IO：JSONL / JSON 读写，UTF-8，**原子写入**。

为什么这里的"原子"值得较真
--------------------------
本工程的数据集与断点是**可以跑几十分钟、花真金白银**才得到的产物。
如果写入不是原子的，一次断电 / Ctrl-C / OOM 就可能留下：

* 半截 JSON 行（JSONL 尾部被截断）→ 下次读取直接抛 JSONDecodeError，
  **整个文件都用不了**；
* 半截 checkpoint → 续跑时读到损坏状态，可能重复生成或从错误位置继续；
* 两个进程同时写同一个文件 → 互相覆盖，数据静默丢失。

因此本模块提供三件事：

1. :func:`write_jsonl` / :func:`write_json` —— 临时文件 + ``os.replace`` 原子替换，
   并 fsync 文件与目录；
2. :func:`atomic_append_jsonl` —— **不会出现半截行**的追加（尾部截断自愈 +
   fsync），配合 :func:`repair_jsonl_tail` 处理历史残留；
3. :func:`file_signature` / :func:`prefix_sha256` —— 给 checkpoint 记录
   "写到哪儿了"的字节偏移与内容指纹，续跑时能**检测并修复**不一致。

``os.replace`` 在 POSIX 与 Windows 上都是原子的，因此"读者永远看到旧文件
或新文件，不会看到半个"。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

LOGGER = logging.getLogger("qboss_training.io")

#: 是否在写入后 fsync。默认开启（数据是花钱换来的，值得这点 IO 开销）。
#: 在 tmpfs / 网络盘上 fsync 可能很慢，可用环境变量关闭。
DEFAULT_FSYNC = os.environ.get("QBOSS_NO_FSYNC", "").strip() not in {"1", "true", "yes"}


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

def read_jsonl(path: str | Path, *, strict: bool = True) -> list[dict[str, Any]]:
    """读取 JSONL；空行与以 ``#`` 开头的行会被忽略。

    Args:
        strict: 为 True 时，遇到不完整/非法行直接报错。
            为 False 时，**跳过尾部的截断行**并告警 ——
            用于读取"上次崩溃留下的文件"。注意只容忍**最后一行**，
            中间出现坏行说明文件真的坏了，仍然报错。
    """
    records: list[dict[str, Any]] = []
    for line_no, line in _iter_lines(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            records.append(_parse_line(path, line_no, stripped))
        except ValueError:
            if not strict and _is_last_content_line(path, line_no):
                LOGGER.warning(
                    "%s:%d 是截断行（上次写入未完成），已跳过；"
                    "建议运行 repair_jsonl_tail() 正式修复",
                    path,
                    line_no,
                )
                break
            raise
    return records


def iter_jsonl(path: str | Path, *, repair_tail: bool = False) -> Iterator[dict[str, Any]]:
    """流式读取 JSONL（大文件友好）。

    Args:
        repair_tail: 为 True 时先修复尾部截断行（推荐用于续跑）。
    """
    source = Path(path)
    if repair_tail and source.exists():
        repair_jsonl_tail(source)
    for line_no, line in _iter_lines(source):
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


def _is_last_content_line(path: str | Path, line_no: int) -> bool:
    """``line_no`` 之后是否再没有非空行。"""
    total = 0
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        for index, line in enumerate(handle, start=1):
            if index > line_no and line.strip():
                return False
            total = index
    return line_no >= total


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------

def dumps_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def write_jsonl(
    path: str | Path,
    records: Iterable[dict[str, Any]],
    *,
    sort_by: str | None = None,
    fsync: bool | None = None,
) -> Path:
    """原子写入 JSONL（整文件替换）。``sort_by`` 指定字段时按键排序，保证可复现。"""
    target = Path(path)
    ensure_dir(target.parent if str(target.parent) else ".")
    items = list(records)
    if sort_by:
        items.sort(key=lambda item: str(item.get(sort_by, "")))
    buffer = io.StringIO()
    for item in items:
        buffer.write(dumps_line(item))
        buffer.write("\n")
    _atomic_write_text(target, buffer.getvalue(), fsync=fsync)
    return target


def atomic_append_jsonl(
    path: str | Path,
    records: Iterable[dict[str, Any]],
    *,
    fsync: bool | None = None,
    repair_tail: bool = True,
) -> Path:
    """**不会留下半截行**的追加写入。

    做法：
      1. 先把文件尾部可能存在的截断行修掉（截到最后一个换行处）；
      2. 以 ``"a"`` 打开，写入**完整**的若干行；
      3. ``flush`` + ``fsync`` 后才返回。

    为什么单次多行写入就足够：一次 ``write`` 调用把每条记录连同结尾换行
    一起提交，且返回前已 fsync。因此"已提交"的最小单位是一整行 ——
    崩溃只可能丢掉**尚未写入**的行，不会在文件中间留下半个 JSON。
    （若进程在写入过程中被强杀，仍可能留下尾部残行，
    这正是第 1 步与 :func:`repair_jsonl_tail` 存在的原因。）

    Returns:
        写入后的文件路径。
    """
    target = Path(path)
    ensure_dir(target.parent if str(target.parent) else ".")
    if repair_tail and target.exists():
        repair_jsonl_tail(target)

    payload = "".join(f"{dumps_line(item)}\n" for item in records)
    if not payload:
        return target

    with target.open("a", encoding="utf-8", newline="") as handle:
        handle.write(payload)
        handle.flush()
        if _should_fsync(fsync):
            os.fsync(handle.fileno())
    return target


@dataclass
class RepairResult:
    """尾部修复结果。"""

    path: str
    original_size: int
    repaired_size: int
    bytes_dropped: int
    dropped_fragment: str = ""

    @property
    def changed(self) -> bool:
        return self.bytes_dropped > 0


def repair_jsonl_tail(path: str | Path, *, fsync: bool | None = None) -> RepairResult:
    """把 JSONL 尾部的截断行截掉，只保留最后一个完整行。

    判断"完整"的标准很朴素但可靠：**文件必须以换行结尾**，
    且每个非空行都能解析为 JSON 对象。从后往前找到最后一个
    可解析且以换行结尾的位置，把其后的残余字节丢掉。

    Returns:
        :class:`RepairResult`，``changed`` 为 False 表示无需修复。
    """
    target = Path(path)
    if not target.exists():
        return RepairResult(str(target), 0, 0, 0)

    raw = target.read_bytes()
    original_size = len(raw)
    if original_size == 0:
        return RepairResult(str(target), 0, 0, 0)

    # 快速路径：以换行结尾且最后一行可解析 → 无需修复
    if raw.endswith(b"\n"):
        last = raw.rstrip(b"\n").split(b"\n")[-1]
        if _line_is_valid(last):
            return RepairResult(str(target), original_size, original_size, 0)

    # 从后往前找最后一个"干净"的行边界
    lines = raw.split(b"\n")
    # 最后一项是 "" 表示原文以换行结尾；否则它可能是残行
    trailing = lines.pop()
    cut = 0
    for index in range(len(lines) - 1, -1, -1):
        if _line_is_valid(lines[index]):
            cut = index + 1
            break
        trailing = lines[index] + b"\n" + trailing

    kept = b"".join(line + b"\n" for line in lines[:cut])
    dropped = original_size - len(kept)
    if dropped <= 0:
        return RepairResult(str(target), original_size, original_size, 0)

    _atomic_write_bytes(target, kept, fsync=fsync)
    LOGGER.warning(
        "%s 尾部有 %d 字节截断内容，已修复（%d → %d 字节）",
        target,
        dropped,
        original_size,
        len(kept),
    )
    return RepairResult(
        path=str(target),
        original_size=original_size,
        repaired_size=len(kept),
        bytes_dropped=dropped,
        dropped_fragment=trailing.decode("utf-8", errors="replace")[:200],
    )


def _line_is_valid(line: bytes) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith(b"#"):
        return True
    try:
        return isinstance(json.loads(stripped.decode("utf-8")), dict)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return False


def read_json(path: str | Path, default: Any = None) -> Any:
    source = Path(path)
    if not source.exists():
        return default
    with source.open("r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError:
            # 原子替换理论上不会留下半个 JSON，但外部手工编辑可能造成损坏。
            LOGGER.error("%s 不是合法 JSON，返回默认值（请检查该文件）", source)
            return default


def write_json(
    path: str | Path, payload: Any, *, indent: int = 2, fsync: bool | None = None
) -> Path:
    """原子写入 JSON。"""
    target = Path(path)
    ensure_dir(target.parent if str(target.parent) else ".")
    text = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False)
    _atomic_write_text(target, text + "\n", fsync=fsync)
    return target


def _should_fsync(fsync: bool | None) -> bool:
    return DEFAULT_FSYNC if fsync is None else fsync


def _atomic_write_text(target: Path, text: str, *, fsync: bool | None = None) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"), fsync=fsync)


def _atomic_write_bytes(target: Path, data: bytes, *, fsync: bool | None = None) -> None:
    """临时文件 + fsync + ``os.replace`` + 目录 fsync。

    目录 fsync 是必要的：``os.replace`` 只保证"替换动作"原子，
    但目录项本身的持久化在 POSIX 上需要额外 fsync 父目录；
    否则掉电后可能出现"文件内容已落盘但目录里没有这个名字"。
    Windows 不支持对目录 fsync（会抛 OSError），因此容错处理。
    """
    target = Path(target)
    parent = target.parent if str(target.parent) else Path(".")
    ensure_dir(parent)

    handle = tempfile.NamedTemporaryFile(
        "wb",
        delete=False,
        dir=str(parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            if _should_fsync(fsync):
                os.fsync(handle.fileno())
        os.replace(temp_path, target)
        if _should_fsync(fsync):
            _fsync_directory(parent)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """尽力 fsync 目录（Windows 上不支持，静默跳过）。"""
    if os.name == "nt":  # pragma: no cover - 平台相关
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# 文件签名：给断点提供"写到哪儿了"的依据
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FileSignature:
    """文件在某个时刻的大小与内容指纹。"""

    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"size": self.size, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: Any) -> "FileSignature | None":
        if not isinstance(payload, dict):
            return None
        size = payload.get("size")
        digest = payload.get("sha256")
        if not isinstance(size, int) or not isinstance(digest, str):
            return None
        return cls(size=size, sha256=digest)

    def matches(self, other: "FileSignature") -> bool:
        return self.size == other.size and self.sha256 == other.sha256


def file_signature(path: str | Path, *, limit: int | None = None) -> FileSignature:
    """计算文件（或前 ``limit`` 字节）的大小与 SHA-256。"""
    target = Path(path)
    if not target.exists():
        return FileSignature(size=0, sha256=hashlib.sha256(b"").hexdigest())

    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as handle:
        remaining = limit
        while True:
            chunk_size = 1 << 20 if remaining is None else min(1 << 20, remaining)
            if chunk_size <= 0:
                break
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return FileSignature(size=size, sha256=digest.hexdigest())


def prefix_sha256(path: str | Path, size: int) -> str:
    """只对文件前 ``size`` 字节求哈希（校验断点记录的前缀是否被改动）。"""
    return file_signature(path, limit=max(0, size)).sha256


def line_count(path: str | Path) -> int:
    """统计非空行数（用于与断点记录的行数比对）。"""
    total = 0
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                total += 1
    return total


# 兼容旧名（既有外部引用）
def append_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> Path:
    """向后兼容的追加入口。

    历史实现用裸 ``"a"`` 打开且不 fsync，可能留下半截行；
    现在统一走 :func:`atomic_append_jsonl`（含尾部修复 + fsync）。
    """
    return atomic_append_jsonl(path, records)


def count_lines(path: str | Path) -> int:
    """统计**所有**行（含空行），保留旧语义。"""
    total = 0
    with Path(path).open("r", encoding="utf-8") as handle:
        for _ in handle:
            total += 1
    return total
