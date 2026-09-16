"""Mutation evidence for the plugin's assistant-report and whitelist wiring.

A fix is only believable if reverting it turns a *named* test red. This script
reverts exactly one clause at a time in ``astrbot_plugin_companion_runtime/main.py``,
runs the named test, restores the file, and fails loudly if a mutation survives.

It exists because the two failures it guards were both silent: the plugin logged
that it started and polled the Runtime while none of its hooks ever ran.

Usage::

    python scripts/mutation_assistant_report.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "astrbot_plugin_companion_runtime" / "main.py"
BACKUP = TARGET.with_suffix(".py.mutation-backup")
PLUGIN_DIR = ROOT / "astrbot_plugin_companion_runtime"
TEST_FILE = "tests/test_plugin_integration.py"

#: ``(label, anchor, replacement, test that must go red)``.
MUTATIONS = [
    (
        "M1: on_llm_response reports nothing (a streamed turn is lost)",
        """            text = as_str(getattr(response, "completion_text", "")).strip()
            if text and self._report_assistant(event, text):
                self._mark_assistant_reported(event)""",
        """            text = as_str(getattr(response, "completion_text", "")).strip()
            text = ""  # MUTATION M1""",
        "test_streamed_turn_is_reported_from_the_llm_response",
    ),
    (
        "M2: after_message_sent ignores the already-reported marker",
        """            if self._assistant_reported(event):
                return
            text = self._result_text(event)""",
        """            text = self._result_text(event)  # MUTATION M2""",
        "test_a_reported_turn_is_not_reported_twice",
    ),
    (
        "M3: the whitelist self-check never runs",
        """        self._start()
        self._warn_if_whitelisted_out()""",
        """        self._start()  # MUTATION M3""",
        "test_an_unwhitelisted_plugin_warns_at_startup",
    ),
    (
        "M4: the whitelist check does not compare plugin names",
        """        if entries == ["*"] or name in entries:
            return""",
        """        if True:  # MUTATION M4
            return""",
        "test_an_unwhitelisted_plugin_warns_at_startup",
    ),
]


def run_test(name: str) -> tuple[bool, str]:
    """Return whether the named test passed, plus pytest's last line."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        ["tests/stubs", ".", environment.get("PYTHONPATH", "")],
    ).rstrip(os.pathsep)
    process = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "-k", name, "--no-header"],
        cwd=PLUGIN_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    tail = (process.stdout or process.stderr).strip().splitlines()[-1:]
    return process.returncode == 0, tail[0] if tail else ""


def main() -> int:
    """Run every mutation and report survivors."""
    original = TARGET.read_text(encoding="utf-8")
    shutil.copyfile(TARGET, BACKUP)
    survivors: list[str] = []
    try:
        for label, anchor, replacement, test_name in MUTATIONS:
            if anchor not in original:
                print(f"{label}: ANCHOR MISSING (the code moved; update this script)")
                survivors.append(label)
                continue
            TARGET.write_text(original.replace(anchor, replacement, 1), encoding="utf-8")
            try:
                passed, tail = run_test(test_name)
            finally:
                TARGET.write_text(original, encoding="utf-8")
            print(f"{label}: {'GREEN (survived!)' if passed else 'RED (killed)'} -> {tail}")
            if passed:
                survivors.append(label)
    finally:
        TARGET.write_text(original, encoding="utf-8")
        if TARGET.read_text(encoding="utf-8") != original:
            print("restore failed; the backup is next to the file")
            return 2
        BACKUP.unlink(missing_ok=True)

    if survivors:
        print(f"mutations survived: {survivors}")
        return 1
    print("all mutations killed; source restored byte-identically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
