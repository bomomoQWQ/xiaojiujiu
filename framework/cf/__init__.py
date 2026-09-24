"""外接测试框架（companion framework）。

四个能力，都在框架里，不改原程序：

* **可控时钟** -- :class:`~cf.clock.ControllableClock`，运行中可 set / advance /
  scale / freeze，通过重绑原程序模块里的 ``utcnow`` 让整个进程跟着虚拟时间走。
* **OpenAI 兼容端点** -- :class:`~cf.mock_openai.MockOpenAIServer`，作为原程序
  「强语义理解」（``remote_api`` provider）的输入源，默认给出**有据可依**的建议集。
* **跑马日志** -- :class:`~cf.logbook.Logbook`，轮转文本日志 + 结构化 JSONL 轨迹，
  每条心跳都带上心境、I/R/P、未尽之事、候选、调度锚点等变量。
* **组装与命令行** -- :class:`~cf.harness.Harness` 与 ``cf`` 命令。
"""

from __future__ import annotations

__all__ = [
    "ControllableClock",
    "Harness",
    "HarnessConfig",
    "Logbook",
    "MockOpenAIServer",
    "MockReply",
    "MockScript",
    "VariableProbe",
    "install_process_clock",
]

__version__ = "0.1.0"


def __getattr__(name: str) -> object:
    """Resolve the public names lazily.

    Importing :mod:`cf` must not pull in the program or uvicorn: ``cf --help``
    and the pure-logic unit tests both need to work on a machine where the
    program is not importable.
    """
    if name in {"ControllableClock", "install_process_clock"}:
        from . import clock

        return getattr(clock, name)
    if name in {"Logbook"}:
        from .logbook import Logbook

        return Logbook
    if name in {"MockOpenAIServer", "MockReply", "MockScript"}:
        from . import mock_openai

        return getattr(mock_openai, name)
    if name == "VariableProbe":
        from .variables import VariableProbe

        return VariableProbe
    if name in {"Harness", "HarnessConfig"}:
        from . import harness

        return getattr(harness, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
