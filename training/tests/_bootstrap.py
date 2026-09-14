"""pytest 引导：把 ``src`` 与项目根目录加入 ``sys.path``。

pytest 会在收集测试前加载 ``conftest.py``，而它也只有在项目根目录已经
位于 ``sys.path`` 时才能被作为 ``conftest`` 模块导入。为了不依赖调用方式
（``pytest`` / ``python -m pytest`` / IDE 内运行），这里用更稳妥的
``tests/_bootstrap.py`` 承担路径设置，再由 ``tests/conftest.py`` 引入。

本文件是 pyproject 中 ``pytest`` 配置的 ``pythonpath`` 之外的**兜底**：
即使读到的是 ``confcutdir`` 之外的 conftest，import 也不会失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"

for candidate in (SRC, PROJECT_ROOT):
    text = str(candidate)
    if text not in sys.path:
        sys.path.insert(0, text)
