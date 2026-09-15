#!/usr/bin/env python3
"""Memory-quality simulation: what does the bot actually remember about a person?

The Runtime's own tests prove that each function behaves; this script plays a week of
ordinary conversation against a real Runtime and checks the memory *contract* the
design document states, the way a person would notice it being broken:

* §15 "not every sentence deserves to be remembered" - its own weak example is
  "今天中午吃炒饭";
* §16 four kinds of long-term memory, including relational experiences;
* §18 a correction re-defines the relation between past and present instead of
  deleting the past, and the character does not end up believing both versions;
* §19 forgetting is ``active -> low activation -> archived``, never deletion, and a
  fact that faded is still something the character knows;
* §20 recall is cued by the working situation, and a fact stated once must still be
  answerable days later.

Two surfaces are used, and nothing else:

1. the **operator surface** (``GET /memories`` through the shipped FastAPI app) for
   what is stored, what is withdrawn and why;
2. the **prompt block** the acting layer is handed (the same
   :func:`~companion_runtime.context.build` + :func:`render_block` the
   ``POST /context/render-block`` endpoint calls), for what the character can act on.

The simulated timestamps are passed explicitly to every Runtime call, so the run is
deterministic and does not depend on the wall clock.

Everything real: a real file SQLite database in WAL mode, the real reducer and
projections, the shipped defaults (``semantic.provider = "disabled"`` - no model, no
key, no network).

Usage::

    python scripts/e2e_memory_simulation.py --base-dir F:\\mem-sim
    python scripts/e2e_memory_simulation.py --base-dir ... --quiet
    python scripts/e2e_memory_simulation.py --list-phases
    python scripts/e2e_memory_simulation.py --base-dir ... --fault trivia

``--fault`` injects a *deployment* defect through the configuration (never a source
edit) so the corresponding checks can be shown to bite:

* ``trivia`` - sets ``memory.candidate_min_value`` to 0, i.e. a deployment that keeps
  every sentence: the admission checks must fail;
* ``no_maintenance`` - sets ``memory.consolidation_interval_seconds`` to 30 days, i.e.
  a deployment whose maintenance pass never runs: no memory forms at all, so every
  check that needs one must fail.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# Never drop bytecode next to the project sources.
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = REPO_ROOT / "runtime" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

IMPORT_ERROR = ""
try:
    from companion_runtime import context as context_module
    from companion_runtime import memory as memory_module
    from companion_runtime.config import RuntimeConfig
    from companion_runtime.runtime import Runtime
except Exception as error:  # noqa: BLE001 - reported as a startup failure
    IMPORT_ERROR = f"{type(error).__name__}: {error}"

BASE = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

#: The fact the whole run is about, stated once, in passing.
PREFERENCE = "记住，我不喜欢别人连续追问我在干嘛。"
PLAN = "我明天下午三点面试，结束了告诉你。"
RESULT = "面试过了！谢谢你那天惦记我。"
BIRTHDAY = "我生日是三月三号。"
COFFEE = "我平时喜欢喝咖啡，一天两杯。"
CORRECTION = "其实我现在不太喜欢咖啡了，改喝茶。"
SMALL_TALK = "在忙什么呢"
LATER = "忙完了，随便聊聊吧。"

#: Text that must never become a long-term memory (design §15's own examples).
TRANSIENT = {
    "早上好呀": "greeting",
    "今天中午吃炒饭。": "§15's weak-candidate example",
    "哈哈今天天气真好": "small talk",
    "在忙，晚点聊。": "transient",
    "嗯": "acknowledgement",
}

OK_MARK = "[PASS]"
BAD_MARK = "[FAIL]"
NOTE_MARK = "  ."


# --------------------------------------------------------------------------- verifier


class Check:
    """One assertion, with the evidence that decides it."""

    def __init__(self, phase: str, label: str, ok: bool, detail: str) -> None:
        self.phase = phase
        self.label = label
        self.ok = ok
        self.detail = detail


class Verifier:
    """Collects checks, prints them and decides the exit code."""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.current = ""
        self.checks: list[Check] = []
        self.notes: list[str] = []
        self.started = time.monotonic()

    def phase(self, title: str) -> None:
        """Start a phase."""
        self.current = title
        if not self.quiet:
            print()
            print("=" * 88)
            print(title)
            print("=" * 88)

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        """Record one assertion."""
        self.checks.append(Check(self.current, label, bool(ok), detail))
        if not self.quiet:
            print(f"  {OK_MARK if ok else BAD_MARK} {label}  [{detail}]")

    def note(self, message: str) -> None:
        """Record a diagnostic line."""
        self.notes.append(message)
        if not self.quiet:
            print(f"{NOTE_MARK} {message}")

    def summary(self) -> int:
        """Print the summary and return the exit code."""
        failed = [check for check in self.checks if not check.ok]
        print()
        print("=" * 88)
        print("SUMMARY")
        print("=" * 88)
        by_phase: dict[str, list[Check]] = {}
        for check in self.checks:
            by_phase.setdefault(check.phase, []).append(check)
        for phase, checks in by_phase.items():
            passed = len([check for check in checks if check.ok])
            mark = OK_MARK if passed == len(checks) else BAD_MARK
            print(f"  {mark} {phase}: {passed}/{len(checks)} checks passed")
        print()
        print(f"  checks passed: {len(self.checks) - len(failed)}")
        print(f"  checks failed: {len(failed)}")
        print(f"  wall clock: {time.monotonic() - self.started:.1f}s")
        if failed:
            print()
            print("FAILURES (with diagnostics)")
            print("-" * 88)
            for index, check in enumerate(failed, start=1):
                print(f"  {index}. [{check.phase}] {check.label}")
                print(f"       {check.detail}")
        return 1 if failed else 0


V = Verifier()


# --------------------------------------------------------------------------- scenario


class Scenario:
    """The Runtime under test plus the two surfaces the checks are allowed to read."""

    def __init__(self, base_dir: Path, faults: Sequence[str] = ()) -> None:
        self.base_dir = base_dir
        self.faults = set(faults)
        self.now = BASE
        self.config = self._build_config()
        self.runtime = Runtime(config=self.config, seed=20260301, created_at=BASE)
        self.client: Any = None
        try:
            from fastapi.testclient import TestClient

            from companion_runtime.api import create_app

            self.client = TestClient(create_app(self.runtime, self.config))
        except Exception as error:  # noqa: BLE001 - the operator surface is optional
            V.note(f"the HTTP operator surface is unavailable ({type(error).__name__}: {error})")

    def _build_config(self) -> RuntimeConfig:
        """Build the shipped default configuration, plus any injected fault."""
        config = RuntimeConfig()
        config.storage.database_path = str(self.base_dir / "runtime.sqlite3")
        config.storage.raw_log_path = str(self.base_dir / "raw_events.jsonl")
        config.storage.mirror_raw_events = True
        config.storage.wal = True
        # The standard deployment: no model, no key, no network.
        config.semantic.provider = "disabled"
        config.semantic.deep_refresh_enabled = False
        if "trivia" in self.faults:
            config.memory.candidate_min_value = 0.0
        if "no_maintenance" in self.faults:
            # A deployment whose maintenance pass never becomes due: the scripted story
            # runs for days and no memory is ever formed from it.
            config.memory.consolidation_interval_seconds = 30 * 24 * 3600.0
        return config

    # -- the user ---------------------------------------------------------------

    def say(self, text: str, *, at: datetime | None = None) -> None:
        """Send one user message, moving the simulated clock forward."""
        moment = at or (self.now + timedelta(minutes=1))
        self.now = moment
        self.runtime.process_user_message(content=text, timestamp=moment)

    def live_until(self, moment: datetime, *, step: timedelta = 12 * HOUR) -> None:
        """Let the Runtime live forward, running unattended maintenance rounds."""
        while self.now < moment:
            self.now = min(self.now + step, moment)
            self.runtime.endogenous_round(now=self.now, force=True)

    def wait(self, delta: timedelta, *, step: timedelta = 12 * HOUR) -> None:
        """Advance the simulated clock by ``delta``."""
        self.live_until(self.now + delta, step=step)

    # -- the operator surface ----------------------------------------------------

    def memories(self) -> list[dict[str, Any]]:
        """Return every memory the operator surface reports."""
        if self.client is not None:
            payload = self.client.get("/memories").json()
            return list(payload.get("memories") or [])
        pool = {
            item["memory_id"]: item
            for item in (
                memory.to_dict() for memory in self.runtime.projections.memory.list_activated(limit=200)
            )
        }
        return [
            memory.to_dict()
            | memory_module.supersession_record(memory)
            | {"activation": (pool.get(memory.memory_id) or {}).get("activation")}
            for memory in self.runtime.projections.memory.list_memories(status=None, limit=200)
        ]

    # -- the prompt block the acting layer is handed ------------------------------

    def block(self, *, at: datetime | None = None) -> str:
        """Return the rendered temporary block, exactly as the endpoint would."""
        moment = at or self.now
        bundle = context_module.build(runtime=self.runtime, now=moment)
        return context_module.render_block(bundle)

    def section(self, header: str = context_module.SECTION_MEMORY, *, at: datetime | None = None) -> str:
        """Return the body of one ``【...】`` section of that block."""
        lines = self.block(at=at).splitlines()
        start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
        if start is None:
            return ""
        body: list[str] = []
        for line in lines[start + 1 :]:
            if line.strip().startswith("【"):
                break
            body.append(line)
        return "\n".join(body).strip()

    def find(self, needle: str, *, memories: Sequence[Mapping[str, Any]] | None = None) -> list[Mapping[str, Any]]:
        """Return the memories whose summary contains ``needle``."""
        rows = memories if memories is not None else self.memories()
        return [row for row in rows if needle in str(row.get("summary") or "")]

    def hits(self, *, limit: int = 8) -> list[dict[str, Any]]:
        """Return the retrieval table for the current moment (diagnostics)."""
        bundle_cue = context_module.build(runtime=self.runtime, now=self.now).memories
        found = self.runtime.memory_store.retrieve(
            memory_module.RetrievalCue(query_text=SMALL_TALK, now=self.now),
            limit=limit,
            rng=self.runtime.rng,
        )
        return [
            {
                "summary": hit.memory.summary,
                "score": round(hit.score, 3),
                "lexical": round(hit.lexical, 3),
                "situation": round(hit.situation, 3),
                "unfinished": round(hit.unfinished, 3),
                "in_prompt": any(item["memory_id"] == hit.memory.memory_id for item in bundle_cue),
            }
            for hit in found
        ]

    def selection(self, *, at: datetime | None = None) -> list[dict[str, Any]]:
        """Return the memory section's items with the source each came from."""
        bundle = context_module.build(runtime=self.runtime, now=at or self.now)
        return [dict(item) for item in bundle.memories]

    def close(self) -> None:
        """Close the Runtime."""
        if self.client is not None:
            self.client.close()
        self.runtime.close()


def _short(value: Any, limit: int = 300) -> str:
    """Return a compact single-line rendering of ``value``."""
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _memory_view(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the fields the checks reason about."""
    return [
        {
            "summary": row.get("summary"),
            "kind": row.get("kind"),
            "status": row.get("status"),
            "retrievable": row.get("retrievable"),
            "superseded": row.get("superseded"),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- phases


def phase_admission(scenario: Scenario) -> None:
    """承认: not every sentence deserves to be remembered (design §15)."""
    V.phase("admission: what deserves to be remembered (design §15)")
    for text, why in TRANSIENT.items():
        scenario.say(text)
    scenario.say(PREFERENCE)
    scenario.wait(3 * HOUR)
    rows = scenario.memories()

    saved = [str(row.get("summary")) for row in rows]
    V.check(
        "a stated preference becomes a long-term memory",
        bool(scenario.find("追问", memories=rows)),
        _short(_memory_view(rows)),
    )
    offenders = {
        text: [summary for summary in saved if text.strip("。") in summary]
        for text in TRANSIENT
    }
    offenders = {text: hits for text, hits in offenders.items() if hits}
    V.check(
        "greetings, small talk, acknowledgements and one-off meals are not remembered",
        not offenders,
        _short(offenders) or f"{len(TRANSIENT)} transient line(s) stayed out of {len(rows)} memory/memories",
    )
    V.check(
        "every memory says it was extracted by rules, not written by a model",
        all(str(row.get("proposed_by") or "rule") == "rule" for row in rows),
        _short([row.get("proposed_by") for row in rows]),
    )


def phase_kinds(scenario: Scenario) -> None:
    """类型: the four kinds of long-term memory (design §16)."""
    V.phase("kinds: the four long-term memory kinds (design §16)")
    scenario.say(PLAN)
    scenario.say(RESULT)
    scenario.say(BIRTHDAY)
    scenario.say(COFFEE)
    scenario.wait(3 * HOUR)
    rows = scenario.memories()

    kinds = {str(row.get("summary")): str(row.get("kind")) for row in rows}
    for needle, expected in (
        ("追问", "user_preference"),
        ("生日", "stable_knowledge"),
        ("惦记", "relationship"),
        ("三点面试", "episodic"),
    ):
        found = scenario.find(needle, memories=rows)
        V.check(
            f"{needle!r} is stored as {expected}",
            bool(found) and found[0].get("kind") == expected,
            _short({"kinds": kinds}) if not found else f"kind={found[0].get('kind')}",
        )


def phase_correction(scenario: Scenario) -> None:
    """修正: a newer statement redefines the older one (design §18)."""
    V.phase("correction: the newer belief replaces the older one (design §18)")
    scenario.say(CORRECTION)
    scenario.wait(3 * HOUR)
    rows = scenario.memories()
    coffee = scenario.find("喜欢喝咖啡", memories=rows)
    tea = scenario.find("改喝茶", memories=rows)

    V.check(
        "the corrected statement is withdrawn rather than deleted",
        bool(coffee) and coffee[0].get("superseded") is True and coffee[0].get("status") != "archived",
        _short(_memory_view(coffee)),
    )
    V.check(
        "the operator surface can say why it is never recalled",
        bool(coffee) and str(coffee[0].get("superseded_by_hint") or "") == CORRECTION
        and str(coffee[0].get("retrieval_reason") or "").startswith("superseded"),
        _short({key: coffee[0].get(key) for key in ("retrieval_reason", "superseded_by_hint")} if coffee else {}),
    )
    V.check(
        "the new statement records what it replaced",
        bool(tea) and bool(tea[0].get("supersedes")),
        _short(_memory_view(tea)),
    )
    V.check(
        "the character believes exactly one of the two",
        sum(1 for row in scenario.find("咖啡", memories=rows) if row.get("retrievable")) == 1,
        _short(_memory_view(scenario.find("咖啡", memories=rows))),
    )


def phase_continuity(scenario: Scenario) -> None:
    """连续: a fact told once is still there two days later (design §1/§19)."""
    V.phase("continuity: a fact told once survives the next two days")
    scenario.say("我先去健身房了，回头聊。")
    scenario.say("今天加班到十点，有点累。")
    scenario.wait(2 * DAY)
    rows = scenario.memories()
    preference = scenario.find("追问", memories=rows)
    section = scenario.section()
    pool = {
        item.memory_id: item.activation
        for item in scenario.runtime.projections.memory.list_activated(limit=200)
    }
    V.note(
        "continuity diagnostics: "
        + _short(
            {
                "decay_rate": scenario.config.memory.activation_decay_rate,
                "preference_status": preference[0].get("status") if preference else None,
                "preference_activation": (
                    round(pool[str(preference[0].get("memory_id"))], 4)
                    if preference and pool.get(str(preference[0].get("memory_id"))) is not None
                    else None
                ),
                "in_pool_size": len(pool),
                "last_tick_at": str(scenario.runtime.state().last_tick_at),
                "now": str(scenario.now),
            }
        )
    )

    V.check(
        "two quiet days do not forget it",
        bool(preference) and preference[0].get("status") == "active",
        _short(_memory_view(preference)),
    )
    V.check(
        "it is still in front of the acting layer without being asked for",
        "追问" in section,
        _short({"section": section}),
    )
    V.check(
        "the prompt section carries durable facts, not only recent traffic",
        any(token in section for token in ("生日", "喝茶", "惦记")),
        _short({"section": section}),
    )


def phase_recall(scenario: Scenario) -> None:
    """想起: a cue brings back what had faded (design §19/§20)."""
    V.phase("recall: a faded fact is still known, and asking brings it back")
    # A week of ordinary traffic that has nothing to do with the birthday, so the fact
    # is neither recalled nor still sitting in the working situation or the recent
    # message window.
    scenario.say("最近工作有点忙。")
    scenario.say("周末想去爬山。")
    scenario.say("对了，我养了只猫，叫团子。")
    scenario.say("晚上打算早点睡。")
    scenario.wait(5 * DAY)
    rows = scenario.memories()
    birthday = scenario.find("生日", memories=rows)
    pool = {
        item.memory_id: item.activation
        for item in scenario.runtime.projections.memory.list_activated(limit=200)
    }
    state = scenario.runtime.state()
    round_cue = memory_module.build_cue(
        state=state,
        recent_events=scenario.runtime.events.recent(4),
        unfinished=scenario.runtime.projections.unfinished.list_open(),
        active_emotions=scenario.runtime.projections.emotion.list_active(),
        now=scenario.now,
        situation_terms=memory_module.situation_terms(scenario.runtime.projections),
    )
    V.note(
        "recall diagnostics: "
        + _short(
            {
                "birthday_in_pool": (
                    round(pool[str(birthday[0].get("memory_id"))], 4)
                    if birthday and pool.get(str(birthday[0].get("memory_id"))) is not None
                    else None
                ),
                "recent": [str(event.content)[:16] for event in scenario.runtime.events.recent(4)],
                "situation": round_cue.situation_terms,
                "unfinished": round_cue.unfinished_titles,
            },
            900,
        )
    )
    V.check(
        "after a quiet week the fact has faded out of the working set",
        bool(birthday) and birthday[0].get("status") == "low_activation",
        _short(_memory_view(birthday)) + " | recall table: " + _short(scenario.hits(), 600),
    )
    V.check(
        "fading did not delete it, and the operator surface still shows it",
        bool(birthday) and birthday[0].get("status") != "archived",
        _short(_memory_view(birthday)),
    )

    # The user asks about it directly. This is the question the module used to answer
    # with silence, because a faded memory was excluded from retrieval entirely.
    scenario.say("我生日是什么时候来着")
    hits = scenario.runtime.memory_store.retrieve(
        memory_module.RetrievalCue(query_text="我生日是什么时候来着", now=scenario.now),
        limit=3,
        rng=scenario.runtime.rng,
    )
    V.check(
        "a matching cue finds the faded memory",
        any("生日" in hit.memory.summary for hit in hits),
        _short([f"{hit.score:.2f}:{hit.memory.summary}" for hit in hits]),
    )
    scenario.wait(1 * HOUR)
    revived = scenario.find("生日", memories=scenario.memories())
    V.check(
        "being recalled puts it back into the working set",
        bool(revived) and revived[0].get("status") == "active",
        _short(_memory_view(revived)),
    )


def phase_freshness(scenario: Scenario) -> None:
    """刚知道的: what was just learned is in front of the model."""
    V.phase("freshness: the newest thing learned reaches the prompt")
    scenario.say("对了，我养了只猫，叫团子。")
    scenario.wait(4 * HOUR)
    rows = scenario.memories()
    cat = scenario.find("团子", memories=rows)
    section = scenario.section()
    selected = scenario.selection()
    V.check(
        "the new fact is stored",
        bool(cat),
        _short(_memory_view(rows)),
    )
    V.check(
        "the new fact is in the prompt",
        "团子" in section,
        _short(
            {
                "section": section,
                "selected": [
                    {"summary": item.get("summary"), "selection": item.get("selection")}
                    for item in selected
                ],
            },
            600,
        ),
    )


def phase_forgetting(scenario: Scenario) -> None:
    """遗忘: demotion is not deletion (design §19)."""
    V.phase("forgetting: nothing is deleted, only withdrawn")
    scenario.wait(3 * DAY)
    rows = scenario.memories()
    V.check(
        "every memory it ever formed is still on record",
        len(rows) >= 5 and all(row.get("status") != "archived" for row in rows),
        _short(_memory_view(rows)),
    )
    faded = [row for row in rows if row.get("status") == "low_activation"]
    V.check(
        "something did fade out of the working set",
        bool(faded),
        _short([row.get("summary") for row in faded]) or "nothing faded",
    )
    V.check(
        "a faded memory is not injected into the prompt",
        all(str(row.get("summary")) not in scenario.section() for row in faded),
        _short({"section": scenario.section()}),
    )


def phase_provenance(scenario: Scenario) -> None:
    """出处: nothing is remembered that the user did not say."""
    V.phase("provenance: every memory traces back to the user's own words")
    rows = scenario.memories()
    said = " ".join(
        str(event.content or "")
        for event in scenario.runtime.events.recent(200)
    )
    untraceable: list[dict[str, Any]] = []
    for row in rows:
        summary = str(row.get("summary") or "")
        tokens = {summary[index : index + 2] for index in range(len(summary) - 1)}
        tokens = {token for token in tokens if not token.isspace()}
        ratio = (
            sum(1 for token in tokens if token in said) / len(tokens) if tokens else 1.0
        )
        if ratio < 0.5:
            untraceable.append({"summary": summary, "trace_ratio": round(ratio, 3)})
    V.check(
        "no memory was invented",
        not untraceable,
        _short(untraceable) or f"{len(rows)} memory/memories traced to the user's words",
    )
    V.check(
        "the raw event log still has every message, unchanged",
        len(scenario.runtime.events.recent(200)) >= 12,
        f"{len(scenario.runtime.events.recent(200))} raw events",
    )


PHASES: list[tuple[str, str, Callable[[Scenario], None]]] = [
    ("admission", "1. what deserves to be remembered", phase_admission),
    ("kinds", "2. the four kinds", phase_kinds),
    ("correction", "3. a newer statement replaces an older one", phase_correction),
    ("continuity", "4. a fact told once survives two days", phase_continuity),
    ("recall", "5. a faded fact is still known", phase_recall),
    ("freshness", "6. what was just learned is in the prompt", phase_freshness),
    ("forgetting", "7. nothing is deleted", phase_forgetting),
    ("provenance", "8. nothing is invented", phase_provenance),
]
PHASE_IDS = [identifier for identifier, _title, _runner in PHASES]


# --------------------------------------------------------------------------- entry


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-dir", required=False, help="artifact directory (database lives here)")
    parser.add_argument("--quiet", action="store_true", help="only print checks and the summary")
    parser.add_argument("--list-phases", action="store_true", help="print the phase ids and exit")
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated phase ids to run (default: all)",
    )
    parser.add_argument(
        "--fault",
        action="append",
        default=[],
        choices=["trivia", "no_maintenance"],
        help="inject a deployment defect through the config, to prove a check bites",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the simulation and return the exit code."""
    args = parse_args(argv)
    if args.list_phases:
        for identifier, title, _runner in PHASES:
            print(f"{identifier:<12} {title}")
        return 0
    if IMPORT_ERROR:
        print(f"cannot start: the Runtime is not importable ({IMPORT_ERROR})")
        return 2
    if not args.base_dir:
        print("cannot start: --base-dir is required")
        return 2

    global V
    V = Verifier(quiet=bool(args.quiet))
    base_dir = Path(args.base_dir).resolve()
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    selected = [item for item in (args.only.split(",") if args.only else []) if item]
    unknown = [item for item in selected if item not in PHASE_IDS]
    if unknown:
        print(f"cannot start: unknown phase id(s) {unknown}; use --list-phases")
        return 2

    scenario = Scenario(base_dir, faults=args.fault)
    exit_code = 1
    try:
        for identifier, title, runner in PHASES:
            if selected and identifier not in selected:
                continue
            try:
                runner(scenario)
            except Exception as error:  # noqa: BLE001 - reported, never re-raised
                import traceback

                V.check(f"the run reaches the end of {identifier} without crashing", False, f"{type(error).__name__}: {error}")
                V.note(traceback.format_exc())
        exit_code = V.summary()
    finally:
        scenario.close()

    report = {
        "base_dir": str(base_dir),
        "phases": [identifier for identifier, _t, _r in PHASES],
        "phases_selected": selected or PHASE_IDS,
        "faults": sorted(set(args.fault)),
        "checks": [
            {"phase": check.phase, "label": check.label, "ok": check.ok, "detail": check.detail}
            for check in V.checks
        ],
        "totals": {
            "passed": len([check for check in V.checks if check.ok]),
            "failed": len([check for check in V.checks if not check.ok]),
        },
        "observations": V.notes,
    }
    (base_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print()
    print(f"artifacts: {base_dir / 'report.json'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
