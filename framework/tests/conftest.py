"""Shared test setup.

The framework is a plain package directory rather than an installed
distribution, so ``cf`` is imported from the repository root. Adding it here
means the suite works with any interpreter that has pytest, without an install
step -- which matters because the point of the framework is to be easy to point
at a program checkout and run.
"""

from __future__ import annotations

import sys
from pathlib import Path

FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
if str(FRAMEWORK_ROOT) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK_ROOT))

#: Where the program's source lives, relative to the framework.
PROGRAM_SRC = FRAMEWORK_ROOT.parent / "runtime" / "src"


def program_available() -> bool:
    """Return whether the program can be imported from its default location.

    The pure-logic tests must pass on a machine that has only the framework;
    only the end-to-end tests need the program.
    """
    if not (PROGRAM_SRC / "companion_runtime").is_dir():
        return False
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return True
