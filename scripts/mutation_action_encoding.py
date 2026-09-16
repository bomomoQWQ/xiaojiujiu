"""Mutation harness for the action-encoding parity fix (item ①).

Applies one mutation at a time to the changed files, runs the new acceptance tests, and
reports whether they died. A mutation that survives means the tests do not bite.

Usage (from the repository root, ``xiaojiujiu/``)::

    runtime/.venv/bin/python scripts/mutation_action_encoding.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]          # xiaojiujiu/
RUNTIME_DIR = ROOT / "runtime"
RUNTIME = RUNTIME_DIR / "src/companion_runtime/runtime.py"
USER_MODEL = RUNTIME_DIR / "src/companion_runtime/user_model.py"
API = RUNTIME_DIR / "src/companion_runtime/api.py"
TESTS = "tests/test_action_encoding_parity.py"

#: The canonical builder, quoted from the fixed source so an accidental edit is caught
#: rather than silently making every mutation a no-op.
OLD_ACTION_SPEC = """        if candidate is None:
            return user_model_module.describe_action(type="contact", proactive=True)
        return user_model_module.describe_action(
            type=candidate.type,
            proactive=candidate_module.is_candidate_proactive(candidate),
        )"""

THIN_ACTION_SPEC = """        return {
            "type": candidate.type if candidate else "contact",
            "proactive": True,
            "question": bool(candidate and "?" in (candidate.intent or "")),
        }"""

#: ``(label, [(path, old, new), ...])``. Multi-edit mutations are grouped so every
#: mutation is a *plausible* alternative implementation, not a syntax error.
MUTATIONS: list[tuple[str, list[tuple[pathlib.Path, str, str]]]] = [
    (
        "M1 observation reverts to the thin A (the original defect)",
        [(RUNTIME, OLD_ACTION_SPEC, THIN_ACTION_SPEC)],
    ),
    (
        "M2 emotional_expression only for `share` (ignore the sibling type)",
        [
            (
                USER_MODEL,
                '        "emotional_expression": kind in EMOTIONAL_EXPRESSION_TYPES,',
                '        "emotional_expression": kind == "share",',
            )
        ],
    ),
    (
        "M3 `question` type drops out of QUESTION_TYPES",
        [
            (
                USER_MODEL,
                '    {"follow_up", "check_in", "question", "curious_question"}',
                '    {"follow_up", "check_in", "curious_question"}',
            )
        ],
    ),
    (
        "M4 emotional_expression type drops out of EMOTIONAL_EXPRESSION_TYPES",
        [
            (
                USER_MODEL,
                'EMOTIONAL_EXPRESSION_TYPES: frozenset[str] = frozenset({"share", "emotional_expression"})',
                'EMOTIONAL_EXPRESSION_TYPES: frozenset[str] = frozenset({"share"})',
            )
        ],
    ),
    (
        "M5 topic_shift is never set",
        [
            (
                USER_MODEL,
                '        "topic_shift": kind in TOPIC_SHIFT_TYPES,',
                '        "topic_shift": False,',
            )
        ],
    ),
    (
        "M6 proactive is always 1",
        [(
            USER_MODEL,
            '        "proactive": bool(proactive),',
            '        "proactive": True,',
        )],
    ),
    (
        "M7 the naive fix: decide 是否追问 from punctuation",
        [
            (
                RUNTIME,
                OLD_ACTION_SPEC,
                """        if candidate is None:
            return user_model_module.describe_action(type="contact", proactive=True)
        spec = user_model_module.describe_action(
            type=candidate.type,
            proactive=candidate_module.is_candidate_proactive(candidate),
        )
        spec["question"] = spec["question"] or any(
            mark in (candidate.intent or "") for mark in ("?", "\uff1f")
        )
        return spec""",
            )
        ],
    ),
    (
        "M8 the public endpoint passes a supplied action through verbatim",
        [
            (
                API,
                "                action=user_model_module.describe_supplied_action(payload.get(\"action\")),",
                "                action=payload.get(\"action\") or {\"type\": \"contact\", \"proactive\": True},",
            )
        ],
    ),
]


def run_tests() -> tuple[bool, str]:
    """Run the acceptance tests; return (green?, summary line)."""
    proc = subprocess.run(
        [
            str(RUNTIME_DIR / ".venv/bin/python"),
            "-m",
            "pytest",
            TESTS,
            "-p",
            "no:randomly",
            "--tb=no",
        ],
        cwd=RUNTIME_DIR,
        capture_output=True,
        text=True,
    )
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    summary = lines[-1] if lines else "(no output)"
    return proc.returncode == 0, summary


def main() -> int:
    originals = {path: path.read_text(encoding="utf-8") for path in (RUNTIME, USER_MODEL, API)}
    green, summary = run_tests()
    print(f"baseline green={green}: {summary}")
    if not green:
        print("baseline is not green; refusing to mutate")
        return 2

    survivors: list[str] = []
    try:
        for label, edits in MUTATIONS:
            restore_needed: list[pathlib.Path] = []
            missing = False
            for path, old, new in edits:
                text = path.read_text(encoding="utf-8")
                if old not in text:
                    print(f"SKIP      {label}: anchor not found in {path.name}")
                    missing = True
                    break
                path.write_text(text.replace(old, new, 1), encoding="utf-8")
                restore_needed.append(path)
            if missing:
                for path in restore_needed:
                    path.write_text(originals[path], encoding="utf-8")
                survivors.append(label + " (anchor missing)")
                continue

            green, summary = run_tests()
            for path in restore_needed:
                path.write_text(originals[path], encoding="utf-8")
            verdict = "SURVIVED" if green else "KILLED"
            if green:
                survivors.append(label)
            print(f"{verdict:9} {label}  [{summary}]")
    finally:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    green, summary = run_tests()
    print(f"restored green={green}: {summary}")
    if survivors:
        print("\nSURVIVORS:")
        for item in survivors:
            print(" -", item)
        return 1
    print(f"\nall {len(MUTATIONS)} mutations killed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
