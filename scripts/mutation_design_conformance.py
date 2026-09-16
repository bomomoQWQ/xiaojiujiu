"""Mutation harness for the design-conformance fixes.

Each group owns one fix, the acceptance tests that pin it, and a set of mutations that a
plausible-but-wrong implementation would contain. A mutation that *survives* means the
tests do not bite, which is the only thing that makes an acceptance test worth having.

Applies one mutation at a time, runs the group's tests, restores every file, and reports.

Usage (from the repository root, ``xiaojiujiu/``)::

    runtime/.venv/bin/python scripts/mutation_design_conformance.py            # every group
    runtime/.venv/bin/python scripts/mutation_design_conformance.py priors     # one group

Groups:
    encoding  item ①, observation/prediction describe a behaviour the same way
    priors    item ⑤, what the cold-start priors claim
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

#: ``(label, tests_path, [(path, old, new), ...])``. Multi-edit mutations are grouped so
#: every mutation is a *plausible* alternative implementation, not a syntax error.
GROUPS: dict[str, tuple[str, list[tuple[str, list[tuple[pathlib.Path, str, str]]]]]] = {
    # ------------------------------------------------------------------ item ①
    "encoding": (
        "tests/test_action_encoding_parity.py",
        [
            (
                "E1 observation reverts to the thin A (the original defect)",
                [
                    (
                        RUNTIME,
                        """        if candidate is None:
            return user_model_module.describe_action(type="contact", proactive=True)
        return user_model_module.describe_action(
            type=candidate.type,
            proactive=candidate_module.is_candidate_proactive(candidate),
        )""",
                        """        return {
            "type": candidate.type if candidate else "contact",
            "proactive": True,
            "question": bool(candidate and "?" in (candidate.intent or "")),
        }""",
                    )
                ],
            ),
            (
                "E2 emotional_expression only for `share` (ignore the sibling type)",
                [
                    (
                        USER_MODEL,
                        '        "emotional_expression": kind in EMOTIONAL_EXPRESSION_TYPES,',
                        '        "emotional_expression": kind == "share",',
                    )
                ],
            ),
            (
                "E3 `question` type drops out of QUESTION_TYPES",
                [
                    (
                        USER_MODEL,
                        '    {"follow_up", "check_in", "question", "curious_question"}',
                        '    {"follow_up", "check_in", "curious_question"}',
                    )
                ],
            ),
            (
                "E4 emotional_expression type drops out of EMOTIONAL_EXPRESSION_TYPES",
                [
                    (
                        USER_MODEL,
                        'EMOTIONAL_EXPRESSION_TYPES: frozenset[str] = frozenset({"share", "emotional_expression"})',
                        'EMOTIONAL_EXPRESSION_TYPES: frozenset[str] = frozenset({"share"})',
                    )
                ],
            ),
            (
                "E5 topic_shift is never set",
                [
                    (
                        USER_MODEL,
                        '        "topic_shift": kind in TOPIC_SHIFT_TYPES,',
                        '        "topic_shift": False,',
                    )
                ],
            ),
            (
                "E6 proactive is always 1",
                [
                    (
                        USER_MODEL,
                        '        "proactive": bool(proactive),',
                        '        "proactive": True,',
                    )
                ],
            ),
            (
                "E7 the naive fix: decide 是否追问 from punctuation",
                [
                    (
                        RUNTIME,
                        """        if candidate is None:
            return user_model_module.describe_action(type="contact", proactive=True)
        return user_model_module.describe_action(
            type=candidate.type,
            proactive=candidate_module.is_candidate_proactive(candidate),
        )""",
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
                "E8 the public endpoint passes a supplied action through verbatim",
                [
                    (
                        API,
                        "                action=user_model_module.describe_supplied_action(payload.get(\"action\")),",
                        "                action=payload.get(\"action\") or {\"type\": \"contact\", \"proactive\": True},",
                    )
                ],
            ),
        ],
    ),
    # ------------------------------------------------------------------ item ⑤
    "priors": (
        "tests/test_user_model_priors.py",
        [
            (
                "P1 `novelty` is neutralised without updating its documented reason",
                [
                    (
                        USER_MODEL,
                        '"reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, -1.10, 0.25, -0.15, -0.30, 0.05, 0.80),',
                        '"reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, -1.10, 0.25, -0.15, -0.30, 0.00, 0.80),',
                    ),
                    (
                        USER_MODEL,
                        '"positive_probability": (0.35, 0.05, 0.10, -0.10, 0.05, -0.05, -0.45, -0.50, 0.05, -0.20, -0.55, 0.05, 0.60),',
                        '"positive_probability": (0.35, 0.05, 0.10, -0.10, 0.05, -0.05, -0.45, -0.50, 0.05, -0.20, -0.55, 0.00, 0.60),',
                    ),
                    (
                        USER_MODEL,
                        '"continue_probability": (0.20, 0.05, 0.20, -0.05, 0.15, -0.05, -0.40, -0.55, 0.10, -0.20, -0.35, 0.05, 0.45),',
                        '"continue_probability": (0.20, 0.05, 0.20, -0.05, 0.15, -0.05, -0.40, -0.55, 0.10, -0.20, -0.35, 0.00, 0.45),',
                    ),
                    (
                        USER_MODEL,
                        '"boundary_risk": (-2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.30, 0.75, 0.15, 0.35, 1.60, 0.20, -0.90),',
                        '"boundary_risk": (-2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.30, 0.75, 0.15, 0.35, 1.60, 0.00, -0.90),',
                    ),
                ],
            ),
            (
                "P2 cold start becomes suspicious of a first contact",
                [
                    (
                        USER_MODEL,
                        '"boundary_risk": (-2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.30, 0.75, 0.15, 0.35, 1.60, 0.20, -0.90),',
                        '"boundary_risk": (2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.30, 0.75, 0.15, 0.35, 1.60, 0.20, -0.90),',
                    )
                ],
            ),
            (
                "P3 a declared boundary no longer raises risk",
                [
                    (
                        USER_MODEL,
                        '"boundary_risk": (-2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.30, 0.75, 0.15, 0.35, 1.60, 0.20, -0.90),',
                        '"boundary_risk": (-2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.30, 0.75, 0.15, 0.35, 0.00, 0.20, -0.90),',
                    )
                ],
            ),
            (
                "P4 a provably busy user no longer raises risk",
                [
                    (
                        USER_MODEL,
                        '"boundary_risk": (-2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.30, 0.75, 0.15, 0.35, 1.60, 0.20, -0.90),',
                        '"boundary_risk": (-2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.00, 0.75, 0.15, 0.35, 1.60, 0.20, -0.90),',
                    )
                ],
            ),
            (
                "P5 stated permission stops being positive evidence",
                [
                    (
                        USER_MODEL,
                        '"reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, -1.10, 0.25, -0.15, -0.30, 0.05, 0.80),',
                        '"reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, -1.10, 0.25, -0.15, -0.30, 0.05, 0.00),',
                    )
                ],
            ),
            (
                "P6 contact fatigue stops being negative evidence",
                [
                    (
                        USER_MODEL,
                        '"reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, -1.10, 0.25, -0.15, -0.30, 0.05, 0.80),',
                        '"reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, 0.00, 0.25, -0.15, -0.30, 0.05, 0.80),',
                    )
                ],
            ),
            (
                "P7 fatigue is made weaker than permission (the ordering claim)",
                [
                    (
                        USER_MODEL,
                        '"reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, -1.10, 0.25, -0.15, -0.30, 0.05, 0.80),',
                        '"reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, -0.10, 0.25, -0.15, -0.30, 0.05, 0.80),',
                    )
                ],
            ),
        ],
    ),
}


def run_tests(tests_path: str) -> tuple[bool, str]:
    """Run one group's acceptance tests; return (green?, summary line)."""
    proc = subprocess.run(
        [
            str(RUNTIME_DIR / ".venv/bin/python"),
            "-m",
            "pytest",
            tests_path,
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


def run_group(name: str, tests_path: str, mutations) -> list[str]:
    """Mutate, test, restore. Return the labels that survived."""
    touched = sorted({path for _label, edits in mutations for path, _old, _new in edits})
    originals = {path: path.read_text(encoding="utf-8") for path in touched}
    survivors: list[str] = []
    try:
        green, summary = run_tests(tests_path)
        print(f"[{name}] baseline green={green}: {summary}")
        if not green:
            print(f"[{name}] baseline is not green; refusing to mutate")
            return ["<baseline not green>"]

        for label, edits in mutations:
            restore_needed: list[pathlib.Path] = []
            missing = False
            for path, old, new in edits:
                text = path.read_text(encoding="utf-8")
                if old not in text:
                    print(f"  SKIP      {label}: anchor not found in {path.name}")
                    missing = True
                    break
                path.write_text(text.replace(old, new, 1), encoding="utf-8")
                restore_needed.append(path)
            if missing:
                for path in restore_needed:
                    path.write_text(originals[path], encoding="utf-8")
                survivors.append(f"{name}/{label} (anchor missing)")
                continue

            green, summary = run_tests(tests_path)
            for path in restore_needed:
                path.write_text(originals[path], encoding="utf-8")
            verdict = "SURVIVED" if green else "KILLED"
            if green:
                survivors.append(f"{name}/{label}")
            print(f"  {verdict:9} {label}  [{summary}]")
    finally:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    green, summary = run_tests(tests_path)
    print(f"[{name}] restored green={green}: {summary}")
    return survivors


def main(argv: list[str]) -> int:
    requested = argv[1:] or list(GROUPS)
    unknown = [name for name in requested if name not in GROUPS]
    if unknown:
        print(f"unknown group(s): {', '.join(unknown)}; known: {', '.join(GROUPS)}")
        return 2

    survivors: list[str] = []
    for name in requested:
        tests_path, mutations = GROUPS[name]
        survivors.extend(run_group(name, tests_path, mutations))

    total = sum(len(GROUPS[name][1]) for name in requested)
    if survivors:
        print("\nSURVIVORS:")
        for item in survivors:
            print(" -", item)
        return 1
    print(f"\nall {total} mutations killed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
