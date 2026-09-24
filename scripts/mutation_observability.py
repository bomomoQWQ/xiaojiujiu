"""Mutation evidence for the beta's observability writes.

Each mutation removes one clause of the instrumentation and must turn a named test
red. Without that, "we record the decisions" is a claim, not a fact -- and the whole
point of the week is to trust the record.

Usage::

    python scripts/mutation_observability.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
TARGETS = {
    "runtime": RUNTIME / "src" / "companion_runtime" / "runtime.py",
    "api": RUNTIME / "src" / "companion_runtime" / "api_v1.py",
}
TEST_FILE = "tests/test_observability.py"

#: ``(file key, label, anchor, replacement, test that must go red)``.
MUTATIONS = [
    (
        "runtime",
        "M1: no verdict is written (the week has no game log)",
        """        if not self.config.observability.enabled:
            return
        # ``outcome`` is ``MotivationResult.to_dict()``: the verdict under""",
        """        if True:  # MUTATION M1
            return
        # ``outcome`` is ``MotivationResult.to_dict()``: the verdict under""",
        "test_a_silent_round_is_recorded_too",
    ),
    (
        "runtime",
        "M2: the state curve is not written (mood history is lost)",
        """            self.projections.observability.record_state_sample(
                connection,""",
        """            _ = None  # MUTATION M2
            if False:
              self.projections.observability.record_state_sample(
                connection,""",
        "test_the_state_curve_has_one_sample_per_round",
    ),
    (
        "runtime",
        "M3: renders are not recorded (no way to see what she was told)",
        """        if not self.config.observability.enabled:
            return
        stamp = ensure_aware(now) or utcnow()""",
        """        if True:  # MUTATION M3
            return
        stamp = ensure_aware(now) or utcnow()""",
        "test_the_context_render_is_recorded",
    ),
    (
        "runtime",
        "M4: the render text is always kept (the opt-in flag is ignored)",
        """        if self.config.observability.record_context_text:
            metadata["text"] = text""",
        """        metadata["text"] = text  # MUTATION M4""",
        "test_the_context_render_is_recorded",
    ),
]


def run_test(name: str) -> tuple[bool, str]:
    """Return whether the named test passed, plus pytest's last line."""
    environment = dict(os.environ)
    process = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "-k", name, "--no-header"],
        cwd=RUNTIME,
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
    originals = {key: path.read_text(encoding="utf-8") for key, path in TARGETS.items()}
    backups = {key: path.with_suffix(".py.mutation-backup") for key, path in TARGETS.items()}
    for key, path in TARGETS.items():
        shutil.copyfile(path, backups[key])

    survivors: list[str] = []
    try:
        for file_key, label, anchor, replacement, test_name in MUTATIONS:
            target = TARGETS[file_key]
            original = originals[file_key]
            if anchor not in original:
                print(f"{label}: ANCHOR MISSING (the code moved; update this script)")
                survivors.append(label)
                continue
            target.write_text(original.replace(anchor, replacement, 1), encoding="utf-8")
            try:
                passed, tail = run_test(test_name)
            finally:
                target.write_text(original, encoding="utf-8")
            print(f"{label}: {'GREEN (survived!)' if passed else 'RED (killed)'} -> {tail}")
            if passed:
                survivors.append(label)
    finally:
        for key, path in TARGETS.items():
            path.write_text(originals[key], encoding="utf-8")
            if path.read_text(encoding="utf-8") != originals[key]:
                print(f"restore failed for {path.name}; a backup sits next to it")
                return 2
            backups[key].unlink(missing_ok=True)

    if survivors:
        print(f"mutations survived: {survivors}")
        return 1
    print("all mutations killed; sources restored byte-identically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
