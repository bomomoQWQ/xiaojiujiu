"""Mutation harness for the design-conformance fixes.

Each group owns one fix, the acceptance tests that pin it, and a set of mutations that a
plausible-but-wrong implementation would contain. A mutation that *survives* means the
tests do not bite, which is the only thing that makes an acceptance test worth having.

Applies one mutation at a time, runs the group's tests, restores every file, and reports.

Usage (from the repository root, ``xiaojiujiu/``)::

    runtime/.venv/bin/python scripts/mutation_design_conformance.py            # every group
    runtime/.venv/bin/python scripts/mutation_design_conformance.py priors     # one group

Groups:
    encoding         item ①, observation/prediction describe a behaviour the same way
    priors           item ⑤, what the cold-start priors claim
    declared_unused  item ⑦, nothing declared and never produced
    redelivery       崩溃窗口, a re-leased message must say so
    chat_history     backlog ③-1, the chat window's durable history
    boundary_synonyms 业务逻辑 B, a boundary must not be bypassable by renaming
    reply_length    业务逻辑 A, reply length judged against the user's own habit
    mood_classes    业务逻辑 C, mood matching decided by behaviour class
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]          # xiaojiujiu/
RUNTIME_DIR = ROOT / "runtime"
RUNTIME = RUNTIME_DIR / "src/companion_runtime/runtime.py"
USER_MODEL = RUNTIME_DIR / "src/companion_runtime/user_model.py"
API = RUNTIME_DIR / "src/companion_runtime/api.py"
REDUCER = RUNTIME_DIR / "src/companion_runtime/reducer.py"
PROJECTIONS = RUNTIME_DIR / "src/companion_runtime/projections.py"
TYPING = RUNTIME_DIR / "src/companion_runtime/typing.py"
API_V1 = RUNTIME_DIR / "src/companion_runtime/api_v1.py"
FRAMEWORK_DIR = ROOT / "framework"
CF_HISTORY = FRAMEWORK_DIR / "cf/history.py"
CF_TUI = FRAMEWORK_DIR / "cf/tui.py"
CANDIDATE = RUNTIME_DIR / "src/companion_runtime/candidate.py"
PROTOCOL = RUNTIME_DIR / "src/companion_runtime/protocol.py"

#: Test paths for the framework suite are written ``framework_tests/...`` and run from
#: ``framework/``; the Runtime's own suite runs from ``runtime/``. One harness, two roots.
FRAMEWORK_TESTS_PREFIX = "framework_tests/"

#: ``group -> (test paths, [(label, [(path, old, new), ...]), ...])``. Multi-edit mutations
#: are grouped so every mutation is a *plausible* alternative implementation, not a syntax
#: error.
GROUPS: dict[str, tuple[list[str], list[tuple[str, list[tuple[pathlib.Path, str, str]]]]]] = {
    # ------------------------------------------------------------------ item ①
    "encoding": (
        ["tests/test_action_encoding_parity.py"],
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
        ["tests/test_user_model_priors.py"],
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
    # ------------------------------------------------------------------ item ⑦
    "declared_unused": (
        ["tests/test_reappraisal_events.py", "tests/test_declared_but_unused.py"],
        [
            (
                "U1 the reappraisal event is not appended (the original gap)",
                [
                    (
                        REDUCER,
                        """        self._events.append(
            EventType.REAPPRAISAL,
            actor=Actor.RUNTIME,
            content=str(record["content"]),""",
                        """        self._events.append(
            EventType.SYSTEM,
            actor=Actor.RUNTIME,
            content=str(record["content"]),""",
                    )
                ],
            ),
            (
                "U2 the log entry and the projection row disagree",
                [
                    (
                        REDUCER,
                        '            content=str(record["content"]),\n            conversation_id=self._config.conversation_id,',
                        '            content="unrelated text",\n            conversation_id=self._config.conversation_id,',
                    )
                ],
            ),
            (
                "U3 a reappraisal calls itself a memory again",
                [
                    (
                        PROJECTIONS,
                        '        identifier = new_id("reappraisal")',
                        '        identifier = new_id("memory")',
                    )
                ],
            ),
            (
                "U4 the duplicated provenance comes back",
                [
                    (
                        REDUCER,
                        "        provenance = list(dict.fromkeys([target_event, *proposal.source_event_ids]))",
                        "        provenance = [target_event, *proposal.source_event_ids]",
                    )
                ],
            ),
            (
                "U5 a never-emitted EventType member is added back",
                [
                    (
                        TYPING,
                        '    SYSTEM = "system"',
                        '    TICK = "tick"\n    SYSTEM = "system"',
                    )
                ],
            ),
            (
                "U6 the reappraisal member is deleted to make the orphan check pass",
                [
                    (
                        TYPING,
                        '    REAPPRAISAL = "reappraisal"',
                        "",
                    ),
                    (
                        REDUCER,
                        "            EventType.REAPPRAISAL,",
                        "            EventType.SYSTEM,",
                    ),
                ],
            ),
        ],
    ),
    # ------------------------------------------------------------------ 崩溃窗口
    "redelivery": (
        ["tests/test_redelivery_visibility.py"],
        [
            (
                "R1 the redelivery flag is never set (the original gap)",
                [
                    (
                        API_V1,
                        '        "redelivery": attempts > 1,',
                        '        "redelivery": False,',
                    )
                ],
            ),
            (
                "R2 the claim counter is not surfaced at all",
                [
                    (
                        API_V1,
                        '        "attempts": attempts,\n        "redelivery": attempts > 1,\n',
                        "",
                    )
                ],
            ),
            (
                "R3 redelivery is hard-coded as if every lease were the first",
                [
                    (
                        API_V1,
                        "    attempts = int(item.attempts or 0)",
                        "    attempts = 1",
                    )
                ],
            ),
            (
                "R4 the flag off by one (first claim reads as a redelivery)",
                [
                    (
                        API_V1,
                        '        "redelivery": attempts > 1,',
                        '        "redelivery": attempts > 0,',
                    )
                ],
            ),
        ],
    ),
    # ------------------------------------------------------------------ ③-1 聊天历史
    "chat_history": (
        ["framework_tests/test_chat_history.py"],
        [
            (
                "H1 the recorder accepts any kind (hidden context could be persisted)",
                [
                    (
                        CF_HISTORY,
                        """        if kind not in VISIBLE_KINDS:
            raise ValueError(""",
                        """        if False:
            raise ValueError(""",
                    )
                ],
            ),
            (
                "H2 a delivered reply is recorded a second time",
                [
                    (
                        CF_TUI,
                        '        elif message.kind == "reply" and not self._reply_recorded:',
                        '        elif message.kind == "reply":',
                    )
                ],
            ),
            (
                "H3 reading ignores the session",
                [
                    (
                        CF_HISTORY,
                        "        for turn in self:\n            if turn.session == session:\n                kept.append(turn)",
                        "        for turn in self:\n            kept.append(turn)",
                    )
                ],
            ),
            (
                "H4 a blank line becomes a turn",
                [
                    (
                        CF_HISTORY,
                        "        if not body or not session:\n            return None",
                        "        if not session:\n            return None",
                    )
                ],
            ),
            (
                "H5 a damaged line takes the whole history down",
                [
                    (
                        CF_HISTORY,
                        """            except ValueError:
                self._malformed += 1
                continue""",
                        """            except ValueError:
                raise""",
                    )
                ],
            ),
            (
                "H6 the user's own turn is not written down",
                [
                    (
                        CF_TUI,
                        '        self._reply_recorded = False\n        self._remember(KIND_USER, text)',
                        '        self._reply_recorded = False',
                    )
                ],
            ),
        ],
    ),
    # ------------------------------------------------------------------ 业务逻辑 B
    "boundary_synonyms": (
        ["tests/test_boundary_synonyms.py"],
        [
            (
                "S1 the predicate keeps its own type list again (the original defect)",
                [
                    (
                        CANDIDATE,
                        '    return behaviour_class_of({"type": candidate.type}) not in NON_PROACTIVE_BEHAVIOUR_CLASSES',
                        """    return candidate.type in {
        "contact",
        "check_in",
        "follow_up",
        "curious_question",
        "share",
        "repair",
    }""",
                    )
                ],
            ),
            (
                "S2 an unknown type is waved through instead of failing closed",
                [
                    (
                        CANDIDATE,
                        '    return behaviour_class_of({"type": candidate.type}) not in NON_PROACTIVE_BEHAVIOUR_CLASSES',
                        '    return TYPE_TO_BEHAVIOUR.get(str(candidate.type or ""), "") not in NON_PROACTIVE_BEHAVIOUR_CLASSES and bool(TYPE_TO_BEHAVIOUR.get(str(candidate.type or "")))',
                    )
                ],
            ),
            (
                "S3 a reply is folded into the blocked set",
                [
                    (
                        CANDIDATE,
                        'NON_PROACTIVE_BEHAVIOUR_CLASSES: frozenset[str] = frozenset({"reply"})',
                        "NON_PROACTIVE_BEHAVIOUR_CLASSES: frozenset[str] = frozenset()",
                    )
                ],
            ),
            (
                "S4 the class rule is replaced by a hand-written class list",
                [
                    (
                        CANDIDATE,
                        '    return behaviour_class_of({"type": candidate.type}) not in NON_PROACTIVE_BEHAVIOUR_CLASSES',
                        '    return behaviour_class_of({"type": candidate.type}) in {"proactive_contact", "follow_up"}',
                    )
                ],
            ),
        ],
    ),
    # ------------------------------------------------------------------ 业务逻辑 A
    "reply_length": (
        ["tests/test_reply_length_baseline.py"],
        [
            (
                "L1 the absolute bars come back (the original defect)",
                [
                    (
                        USER_MODEL,
                        "            positive += self._relative_length_delta(reaction, positive)",
                        "            positive += 0.10 if reaction.reply_length >= 20 else (-0.05 if reaction.reply_length <= 4 else 0.0)",
                    )
                ],
            ),
            (
                "L2 the weight gate goes back to counting characters",
                [
                    (
                        USER_MODEL,
                        "        return self.relative_length_signal(int(length)) <= -REPLY_LENGTH_SHORT_SIGNAL",
                        "        return int(length) <= 4",
                    )
                ],
            ),
            (
                "L3 the baseline is never trusted",
                [
                    (
                        USER_MODEL,
                        "        if self._length_samples >= REPLY_LENGTH_BASELINE_MIN_SAMPLES:",
                        "        if False:",
                    )
                ],
            ),
            (
                "L4 observations never teach the baseline",
                [
                    (
                        USER_MODEL,
                        "    def _learn_reply_length(self, reaction: BehaviourReaction) -> None:",
                        "    def _learn_reply_length(self, reaction: BehaviourReaction) -> None:\n        return  # MUTATION",
                    )
                ],
            ),
            (
                "L5 the baseline is not persisted",
                [
                    (
                        USER_MODEL,
                        '        params["reply_length_baseline"] = {\n            "log_mean": self._length_log_mean,',
                        '        params["reply_length_baseline"] = {\n            "log_mean": 0.0,',
                    )
                ],
            ),
            (
                "L9 the new baselines are stored but never exposed to the operator",
                [
                    (
                        USER_MODEL,
                        '            "reply_length_baseline": self.reply_length_baseline_view(),\n            "reply_turns_baseline": self.reply_turns_baseline_view(),\n',
                        "",
                    )
                ],
            ),
            (
                "L7 the conversation term saturates at a hardcoded three again",
                [
                    (
                        USER_MODEL,
                        "            + CONVERSATION_TARGET_WEIGHT * self.conversation_bonus(reaction)",
                        "            + CONVERSATION_TARGET_WEIGHT * min(3, reaction.turns) / 3.0",
                    )
                ],
            ),
            (
                "L8 the conversation baseline is never consulted (always the fallback)",
                [
                    (
                        USER_MODEL,
                        "        if self._turns_samples >= CONVERSATION_BASELINE_MIN_SAMPLES:",
                        "        if False:",
                    )
                ],
            ),
            (
                "L6 a short reply is allowed to invert the sign",
                [
                    (
                        USER_MODEL,
                        "        delta = REPLY_LENGTH_TARGET_WEIGHT * self.relative_length_signal(int(reaction.reply_length))\n        if delta >= 0.0:\n            return delta\n        return -min(-delta, max(0.0, positive - 0.5))",
                        "        return REPLY_LENGTH_TARGET_WEIGHT * self.relative_length_signal(int(reaction.reply_length))",
                    )
                ],
            ),
        ],
    ),
    # ------------------------------------------------------------------ 业务逻辑 C
    "mood_classes": (
        ["tests/test_mood_matching_by_class.py"],
        [
            (
                "C1 the alignment keeps its own type sets again (the original defect)",
                [
                    (
                        RUNTIME,
                        '        behaviour = user_model_module.TYPE_TO_BEHAVIOUR.get(str(candidate.type or ""))\n        if top.direction == "-" and behaviour in user_model_module.MOOD_MATCH_NEGATIVE_CLASSES:\n            return clamp(0.4 + top.intensity)\n        if top.direction == "+" and behaviour in user_model_module.MOOD_MATCH_POSITIVE_CLASSES:',
                        '        behaviour = str(candidate.type or "")\n        if top.direction == "-" and behaviour in {"repair", "follow_up", "check_in"}:\n            return clamp(0.4 + top.intensity)\n        if top.direction == "+" and behaviour in {"share", "curious_question", "contact"}:',
                    )
                ],
            ),
            (
                "C2 an unknown type earns the bonus instead of the generic value",
                [
                    (
                        RUNTIME,
                        '        behaviour = user_model_module.TYPE_TO_BEHAVIOUR.get(str(candidate.type or ""))',
                        '        behaviour = user_model_module.behaviour_class_of({"type": candidate.type})',
                    )
                ],
            ),
            (
                "C3 a behaviour class is dropped from the negative table",
                [
                    (
                        USER_MODEL,
                        'MOOD_MATCH_NEGATIVE_CLASSES: frozenset[str] = frozenset({"repair", "follow_up", "proactive_contact"})',
                        'MOOD_MATCH_NEGATIVE_CLASSES: frozenset[str] = frozenset({"repair", "follow_up"})',
                    )
                ],
            ),
            (
                "C4 the protocol classifier goes back to its own shorter list",
                [
                    (
                        PROTOCOL,
                        "        if candidate_type in QUESTION_TYPES and event_tokens & intent_tokens:",
                        '        if candidate_type in {"follow_up", "curious_question", "check_in"} and event_tokens & intent_tokens:',
                    )
                ],
            ),
        ],
    ),
}


def _bytecode_paths(path: pathlib.Path) -> list[pathlib.Path]:
    """Return the ``__pycache__`` entries Python would load for ``path``."""
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return []
    return sorted(cache.glob(f"{path.stem}.*.pyc"))


def _invalidate_bytecode(paths) -> None:
    """Delete the cached bytecode of every touched source.

    Required, not hygiene. CPython validates a ``.pyc`` against the source's **mtime and
    size**, so a mutation that keeps the byte length and is reverted inside the same second
    leaves a cache entry whose header still "matches": the *restored* file then runs as the
    **mutant**. Measured here: ``-1.10`` → ``-0.10`` is the same five characters, and every
    prior that goes to ``0.00`` is the same four - so a whole class of mutations was able to
    leave the tree quietly testing the wrong code, and the harness's own "restored green"
    check could not see it. Bytecode is therefore never trusted: it is deleted around every
    apply and every restore, and the test subprocess runs with bytecode writing disabled.
    """
    for path in paths:
        for cached in _bytecode_paths(path):
            with contextlib.suppress(OSError):
                cached.unlink()


def _resolve_root(tests_paths: list[str]) -> tuple[list[str], pathlib.Path]:
    """Return the test paths and the directory they must run from."""
    if tests_paths and all(p.startswith(FRAMEWORK_TESTS_PREFIX) for p in tests_paths):
        # "framework_tests/foo.py" -> "tests/foo.py", run from framework/
        return ["tests/" + p[len(FRAMEWORK_TESTS_PREFIX) :] for p in tests_paths], FRAMEWORK_DIR
    return tests_paths, RUNTIME_DIR


def run_tests(tests_paths: list[str]) -> tuple[bool, str]:
    """Run one group's acceptance tests; return (green?, summary line)."""
    tests_paths, cwd = _resolve_root(tests_paths)
    env = dict(os.environ)
    # Belt and braces with _invalidate_bytecode: a test run must never *create* the stale
    # cache entry that a later restore would silently accept.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The interpreter that runs this harness is the one that can run the tests: it is
    # correct by construction and it is the only choice that works on both platforms.
    # This used to hardcode ``runtime/.venv/bin/python``, which does not exist on Windows
    # (the venv layout there is ``Scripts/python.exe``), so the whole harness died with
    # `WinError 2` before running a single mutation - the numbers it produced were only
    # ever reproducible on the machine that wrote it.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *tests_paths,
            "-p",
            "no:randomly",
            "--tb=no",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    summary = lines[-1] if lines else "(no output)"
    return proc.returncode == 0, summary


def _assert_restored(touched, originals: dict[pathlib.Path, str]) -> None:
    """Fail loudly if the tree is not exactly the original source, bytecode included."""
    for path in touched:
        if path.read_text(encoding="utf-8") != originals[path]:
            raise AssertionError(f"{path} was not restored to its original content")
        stale = _bytecode_paths(path)
        if stale:
            raise AssertionError(
                f"stale bytecode survives for {path.name}: {[p.name for p in stale]}"
            )


def run_group(name: str, tests_paths: list[str], mutations) -> list[str]:
    """Mutate, test, restore. Return the labels that survived."""
    touched = sorted({path for _label, edits in mutations for path, _old, _new in edits})
    _invalidate_bytecode(touched)
    originals = {path: path.read_text(encoding="utf-8") for path in touched}
    survivors: list[str] = []
    try:
        _assert_restored(touched, originals)
        green, summary = run_tests(tests_paths)
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
            _invalidate_bytecode(restore_needed)
            if missing:
                for path in restore_needed:
                    path.write_text(originals[path], encoding="utf-8")
                _invalidate_bytecode(restore_needed)
                survivors.append(f"{name}/{label} (anchor missing)")
                continue

            green, summary = run_tests(tests_paths)
            for path in restore_needed:
                path.write_text(originals[path], encoding="utf-8")
            _invalidate_bytecode(restore_needed)
            _assert_restored(restore_needed, originals)
            verdict = "SURVIVED" if green else "KILLED"
            if green:
                survivors.append(f"{name}/{label}")
            print(f"  {verdict:9} {label}  [{summary}]")
    finally:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")
        _invalidate_bytecode(touched)

    _assert_restored(touched, originals)
    green, summary = run_tests(tests_paths)
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
        tests_paths, mutations = GROUPS[name]
        survivors.extend(run_group(name, tests_paths, mutations))

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
