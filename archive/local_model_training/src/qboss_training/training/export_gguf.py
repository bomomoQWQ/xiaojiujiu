"""llama.cpp GGUF 导出：HF 权重 → GGUF f16 → 量化（CPU 部署路径）。

链路与理由
----------
架构文档 §74 的部署目标是弱 VPS、Q4 优先、Q3 为内存极限、Q2 为最后生存模式。
llama.cpp 是这条链路唯一现实的运行时，因此导出必须可复现：

    merged HF 权重
      → convert_hf_to_gguf.py  →  f16 GGUF
      → llama-quantize          →  Q4_K_M / Q5_K_M / Q3_K_M / Q2_K

本模块做三件事：
  1. **工具定位**：兼容"源码仓库 + Python 脚本"与"发布包 + 编译好的二进制"
     两种 llama.cpp 安装形态；
  2. **命令构造**：把命令拼装与执行分离，纯函数 :func:`build_convert_command` /
     :func:`build_quantize_command` 可直接单测，不需要真的装 llama.cpp；
  3. **CPU 量化矩阵建议**：给出每种量化等级在弱 VPS 上的取舍，
     以及"2B 模型 + 短上下文"场景下的推荐默认值（Q4_K_M）。

注意：转换脚本与量化程序都在 llama.cpp 侧，本工程不 vendored 它们，
只负责调用与校验产物。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from ..errors import ToolUnavailable, TrainingError
from ..utils.io import write_json
from ..utils.secrets import utc_now_iso

LOGGER = logging.getLogger("qboss_training.gguf")

#: 推荐量化等级：名称 → (说明, 相对 f16 体积, 适用场景)
QUANT_MATRIX: dict[str, dict[str, Any]] = {
    "Q8_0": {
        "note": "几乎无损，体积约 f16 的 53%",
        "size_ratio": 0.53,
        "scene": "内存充裕，作为质量上界参考",
    },
    "Q6_K": {
        "note": "质量接近无损",
        "size_ratio": 0.41,
        "scene": "内存尚可，追求质量",
    },
    "Q5_K_M": {
        "note": "质量与体积平衡良好",
        "size_ratio": 0.36,
        "scene": "内存一般的 VPS",
    },
    "Q4_K_M": {
        "note": "架构文档 §74.1 的『优先 Q4』，本工程默认",
        "size_ratio": 0.30,
        "scene": "弱 VPS 默认选择",
    },
    "Q4_K_S": {
        "note": "比 Q4_K_M 更小",
        "size_ratio": 0.28,
        "scene": "内存紧张",
    },
    "Q3_K_M": {
        "note": "架构文档 §74.1 的『内存极限』",
        "size_ratio": 0.24,
        "scene": "内存极限；需重点复测 schema 合法率",
    },
    "Q2_K": {
        "note": "架构文档 §74.1 的『最后生存模式』",
        "size_ratio": 0.18,
        "scene": "只保可用性；结构化输出可能显著退化",
    },
}

DEFAULT_QUANT_TYPES: tuple[str, ...] = ("Q4_K_M",)
SAFE_QUANT_TYPES: tuple[str, ...] = ("Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q4_K_S", "Q3_K_M", "Q2_K")


@dataclass
class ExportConfig:
    """GGUF 导出配置。"""

    model_dir: str
    output_dir: str
    #: llama.cpp 仓库或安装根目录
    llama_cpp_dir: str | None = None
    #: 直接指定 convert_hf_to_gguf.py（llama_cpp_dir 之外的另一种定位方式）
    convert_script: str | None = None
    #: 直接指定 llama-quantize 二进制
    quantize_binary: str | None = None
    #: GGUF 文件基础名（不含扩展名）
    name: str = "qboss-2b"
    outtype: str = "f16"
    quant_types: tuple[str, ...] = DEFAULT_QUANT_TYPES
    threads: int = 0
    keep_f16: bool = True
    #: 只打印命令不执行（用于离线验证与文档生成）
    dry_run: bool = False
    timeout_s: float = 3600.0


@dataclass
class ExportReport:
    """导出结果。"""

    model_dir: str = ""
    output_dir: str = ""
    f16_path: str | None = None
    quantized: dict[str, str] = field(default_factory=dict)
    commands: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False
    finished_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_dir": self.model_dir,
            "output_dir": self.output_dir,
            "f16_path": self.f16_path,
            "quantized": dict(self.quantized),
            "commands": [list(item) for item in self.commands],
            "notes": list(self.notes),
            "dry_run": self.dry_run,
            "finished_at": self.finished_at,
        }


# --------------------------------------------------------------------------
# 工具定位
# --------------------------------------------------------------------------

def resolve_convert_script(llama_cpp_dir: str | Path | None) -> Path:
    """定位 ``convert_hf_to_gguf.py``。

    支持两种布局：
      * ``<root>/convert_hf_to_gguf.py``（llama.cpp 仓库根目录）
      * ``<root>/convert_hf_to_gguf.py`` 之外的常见嵌套（``scripts/`` 等）
    """
    if llama_cpp_dir is None:
        raise ToolUnavailable(
            "未提供 llama.cpp 目录。请用 --llama-cpp-dir 指向 llama.cpp 仓库根目录，"
            "或用 --convert-script 直接指定 convert_hf_to_gguf.py 的路径。"
        )
    root = Path(llama_cpp_dir)

    candidates = [
        root / "convert_hf_to_gguf.py",
        root / "scripts" / "convert_hf_to_gguf.py",
        root / "convert-hf-to-gguf.py",  # 旧版命名
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
        # 允许直接传脚本路径
    if root.is_file() and root.suffix == ".py":
        return root

    raise ToolUnavailable(
        f"在 {root} 下找不到 convert_hf_to_gguf.py。已尝试："
        + ", ".join(str(item) for item in candidates)
    )


def resolve_quantize_binary(llama_cpp_dir: str | Path | None) -> Path:
    """定位 ``llama-quantize``（或旧的 ``quantize``）。"""
    if llama_cpp_dir is None:
        found = shutil.which("llama-quantize") or shutil.which("quantize")
        if found:
            return Path(found)
        raise ToolUnavailable(
            "PATH 中找不到 llama-quantize。请用 --llama-cpp-dir 指定 llama.cpp 目录，"
            "或把编译产物加入 PATH。"
        )

    root = Path(llama_cpp_dir)
    names = (
        "llama-quantize.exe",
        "llama-quantize",
        "quantize.exe",
        "quantize",
    )
    search_dirs = [
        root / "build" / "bin",
        root / "build" / "bin" / "Release",
        root / "bin",
        root,
    ]
    for directory in search_dirs:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return candidate

    found = shutil.which("llama-quantize")
    if found:
        return Path(found)

    raise ToolUnavailable(
        "找不到 llama-quantize。请先编译 llama.cpp：\n"
        "  cmake -B build -DCMAKE_BUILD_TYPE=Release\n"
        "  cmake --build build --config Release -j"
    )


# --------------------------------------------------------------------------
# 命令构造（纯函数，可单测）
# --------------------------------------------------------------------------

def build_convert_command(
    convert_script: str | Path,
    model_dir: str | Path,
    outfile: str | Path,
    *,
    outtype: str = "f16",
    python_executable: str | None = None,
) -> list[str]:
    """构造 convert_hf_to_gguf.py 命令。"""
    return [
        python_executable or sys.executable,
        str(convert_script),
        str(model_dir),
        "--outfile",
        str(outfile),
        "--outtype",
        outtype,
    ]


def build_quantize_command(
    quantize_binary: str | Path,
    f16_path: str | Path,
    output_path: str | Path,
    quant_type: str,
    *,
    threads: int = 0,
) -> list[str]:
    """构造 llama-quantize 命令。"""
    command = [
        str(quantize_binary),
        str(f16_path),
        str(output_path),
        quant_type,
    ]
    if threads and threads > 0:
        command.extend(["--threads", str(threads)])
    return command


def _is_placeholder(path: Path) -> bool:
    """dry-run 用的占位路径含 ``<...>``，不应做存在性检查。"""
    return "<" in str(path)


def plan_export(config: ExportConfig) -> list[tuple[str, list[str]]]:
    """只规划命令，不执行。返回 ``[(标签, 命令), ...]``。"""
    output_dir = Path(config.output_dir)
    f16_path = output_dir / f"{config.name}-{config.outtype}.gguf"
    plan: list[tuple[str, list[str]]] = []

    if config.convert_script:
        convert_script = Path(config.convert_script)
        # dry-run 的占位路径（如 <llama.cpp>/convert_hf_to_gguf.py）不做存在性检查
        if not _is_placeholder(convert_script) and not convert_script.exists():
            raise TrainingError(f"指定的转换脚本不存在：{convert_script}")
    else:
        convert_script = resolve_convert_script(config.llama_cpp_dir)

    plan.append(
        (
            "convert",
            build_convert_command(
                convert_script, config.model_dir, f16_path, outtype=config.outtype
            ),
        )
    )

    if config.quant_types:
        if config.quantize_binary:
            quantize_binary = Path(config.quantize_binary)
            if not _is_placeholder(quantize_binary) and not quantize_binary.exists():
                raise TrainingError(f"指定的 llama-quantize 不存在：{quantize_binary}")
        else:
            quantize_binary = resolve_quantize_binary(config.llama_cpp_dir)
        for quant_type in config.quant_types:
            target = output_dir / f"{config.name}-{quant_type}.gguf"
            plan.append(
                (
                    f"quantize:{quant_type}",
                    build_quantize_command(
                        quantize_binary,
                        f16_path,
                        target,
                        quant_type,
                        threads=config.threads,
                    ),
                )
            )
    return plan


def run_export(config: ExportConfig) -> ExportReport:
    """执行导出。"""
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    unknown = [item for item in config.quant_types if item not in QUANT_MATRIX]
    if unknown:
        raise TrainingError(
            f"未知量化类型 {unknown}；可选：{', '.join(SAFE_QUANT_TYPES)}"
        )

    report = ExportReport(
        model_dir=str(config.model_dir),
        output_dir=str(output_dir),
        dry_run=config.dry_run,
    )

    f16_path = output_dir / f"{config.name}-{config.outtype}.gguf"

    try:
        plan = plan_export(config)
    except ToolUnavailable as exc:
        if not config.dry_run:
            raise
        # dry-run 且**工具未就绪**：这是可接受的，用占位路径展示命令形状。
        # 注意这里只吞 ToolUnavailable —— 若用户显式给了错误的
        # --convert-script 路径（TrainingError），那是输入错误，必须直接报错，
        # 不能因为 dry-run 就假装成功。
        report.notes.append(f"工具未就绪（dry-run 继续）：{exc}")
        placeholder = replace(
            config,
            convert_script=str(Path("<llama.cpp>") / "convert_hf_to_gguf.py"),
            quantize_binary=str(Path("<llama.cpp>") / "llama-quantize"),
            llama_cpp_dir=None,
        )
        plan = plan_export(placeholder)

    for label, command in plan:
        report.commands.append(list(command))
        if config.dry_run:
            LOGGER.info("[dry-run] %s: %s", label, " ".join(command))
            continue
        LOGGER.info("执行 %s：%s", label, " ".join(command))
        _run(command, timeout_s=config.timeout_s)
        if label == "convert":
            report.f16_path = str(f16_path)
        else:
            quant_type = label.split(":", 1)[1]
            report.quantized[quant_type] = str(
                output_dir / f"{config.name}-{quant_type}.gguf"
            )

    if not config.dry_run:
        _verify_outputs(report, f16_path, config)
        if not config.keep_f16 and f16_path.exists():
            LOGGER.info("删除中间 f16 文件：%s", f16_path)
            f16_path.unlink()
            report.f16_path = None

    for quant_type in config.quant_types:
        info = QUANT_MATRIX.get(quant_type)
        if info:
            report.notes.append(
                f"{quant_type}：{info['note']}；场景：{info['scene']}"
            )

    write_json(output_dir / "export_report.json", report.to_dict())
    return report


def _run(command: Sequence[str], *, timeout_s: float) -> None:
    """执行外部命令并把输出实时透传（便于在日志里看到转换进度）。"""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            timeout=timeout_s,
            env=env,
        )
    except FileNotFoundError as exc:
        raise TrainingError(
            f"命令不可执行：{command[0]}。请确认路径正确。原始错误：{exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TrainingError(
            f"命令超时（>{timeout_s}s）：{' '.join(str(item) for item in command)}"
        ) from exc

    if completed.returncode != 0:
        raise TrainingError(
            f"命令失败（退出码 {completed.returncode}）：{' '.join(str(item) for item in command)}\n"
            "常见原因：\n"
            "  * 模型目录不是合并后的 HF 权重（还是 adapter）\n"
            "  * transformers / gguf 版本不匹配（转换脚本需要特定 gguf 包版本）\n"
            "  * 磁盘空间不足或输出路径无写权限"
        )


def _verify_outputs(report: ExportReport, f16_path: Path, config: ExportConfig) -> None:
    if not f16_path.exists():
        raise TrainingError(
            f"转换结束但找不到 {f16_path}。请检查转换脚本输出。"
        )
    size_mb = f16_path.stat().st_size / (1024 * 1024)
    LOGGER.info("f16 GGUF 生成成功：%s（%.1f MB）", f16_path, size_mb)

    for quant_type, path_str in report.quantized.items():
        path = Path(path_str)
        if not path.exists():
            raise TrainingError(f"量化 {quant_type} 未产出文件：{path}")
        quant_mb = path.stat().st_size / (1024 * 1024)
        ratio = quant_mb / size_mb if size_mb else 0.0
        LOGGER.info(
            "量化 %s 生成成功：%s（%.1f MB，为 f16 的 %.1f%%）",
            quant_type,
            path,
            quant_mb,
            ratio * 100,
        )


def describe_matrix() -> str:
    """渲染量化矩阵说明（写进 README 或导出报告）。"""
    lines = [
        "| 类型 | 说明 | 约为 f16 体积 | 适用场景 |",
        "| --- | --- | --- | --- |",
    ]
    for name, info in QUANT_MATRIX.items():
        lines.append(
            f"| {name} | {info['note']} | {info['size_ratio'] * 100:.0f}% | {info['scene']} |"
        )
    return "\n".join(lines)


def estimate_ram_mb(param_count_billion: float, quant_type: str, context_tokens: int = 2048) -> float:
    """估算 llama.cpp CPU 推理所需内存（MB）。

    粗略模型：``权重 + KV cache + 运行时开销``。
    KV cache 按 2B 级模型每 token 约 0.12MB（f16 KV、GQA）估。
    """
    info = QUANT_MATRIX.get(quant_type)
    if info is None:
        raise TrainingError(f"未知量化类型 {quant_type}")
    # f16 约 2 bytes/参数 → 参数量(B) * 1000 (MB, 按 f16 计) * 量化体积比
    weights_mb = param_count_billion * 1000 * info["size_ratio"]
    kv_mb = context_tokens * 0.12
    overhead_mb = 200.0
    return weights_mb + kv_mb + overhead_mb
