"""Command line for the framework.

Two halves, deliberately separate processes:

* ``cf run`` boots a harness and stays in the foreground. It owns the clock, so
  it must keep running for time to be controllable.
* every other subcommand is a *client* that talks to a live harness over
  loopback HTTP. ``cf time advance 8h`` does not touch a clock directly; it asks
  the running harness to move its own.

That split is what makes "change the time while it is running" work at all.
The client finds the running harness either from an explicit ``--control`` URL or
by reading ``control_url`` out of the run directory's trace, so the common case is
just ``cf run`` in one terminal and ``cf time advance 8h --run-dir runs/xxx`` in
another.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .harness import DEFAULT_PROGRAM_SRC, Harness, HarnessConfig
from .config import (
    VALUE_AXES,
    ClientConfig,
    ConfigError,
    load_client_config,
    write_example,
)
from .host import DEFAULT_PLUGIN_ROOT
from .logbook import Logbook
from .mock_openai import MockReply, MockScript
from .onebot import OneBotFrontend
from .onebot_service import OneBotService, tail_transcript
from .program import ProgramClient, ProgramError
from .history import ChatHistory
from .tui import ChatTUI

DEFAULT_RUNS_DIR = Path("runs")


def _post(url: str, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """POST JSON to the control plane and decode the reply.

    Raises:
        SystemExit: With a readable message on any transport or protocol error.
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"control plane refused the request ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach the control plane at {url}: {exc.reason}") from exc


def _get(url: str, timeout: float = 15.0) -> dict[str, Any]:
    """GET JSON from the control plane."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {url}: {exc.reason}") from exc


def resolve_url(explicit: str, run_dir: str | None, field: str, flag: str) -> str:
    """Find one of a running harness's URLs, from the flag or from its trace.

    The harness records ``base_url``, ``control_url`` and ``mock_url`` in a single
    ``harness_ready`` record, so every client command can find its target the same
    way instead of each inventing its own discovery.

    Args:
        explicit: Value of the corresponding flag; used verbatim when non-empty.
        run_dir: A run directory whose trace names the surfaces.
        field: Which recorded field to read, e.g. ``control_url``.
        flag: Flag name to mention in the error, e.g. ``--control``.

    Returns:
        The base URL, without a trailing slash.

    Raises:
        SystemExit: When neither source yields a URL.
    """
    if explicit:
        return explicit.rstrip("/")
    if run_dir:
        trace = Path(run_dir) / "trace.jsonl"
        if not trace.exists():
            raise SystemExit(f"no trace at {trace}; pass {flag} http://127.0.0.1:PORT")
        found = ""
        with trace.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("kind") == "harness_ready" and record.get(field):
                    found = str(record[field])
        if not found:
            raise SystemExit(f"{trace} names no {field}; is the harness still starting?")
        return found.rstrip("/")
    raise SystemExit(f"pass {flag} http://127.0.0.1:PORT or --run-dir <dir>")


def resolve_control_url(explicit: str, run_dir: str | None) -> str:
    """Find the control URL of a running harness."""
    return resolve_url(explicit, run_dir, "control_url", "--control")


def resolve_runtime_url(explicit: str, run_dir: str | None) -> str:
    """Find the program's own HTTP base URL for a running harness."""
    return resolve_url(explicit, run_dir, "base_url", "--runtime")


# --------------------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace) -> int:
    """Boot a harness in the foreground until interrupted."""
    run_dir = Path(args.run_dir) if args.run_dir else DEFAULT_RUNS_DIR / time.strftime("%Y%m%d-%H%M%S")
    config = HarnessConfig(
        run_dir=run_dir,
        program_src=Path(args.program_src),
        base_dir=Path(args.base_dir) if args.base_dir else None,
        host=args.host,
        port=args.port,
        start_time=args.start_time,
        time_scale=args.time_scale,
        step=args.step,
        heartbeat_interval_s=args.heartbeat_interval,
        seed=args.seed,
        use_mock_semantics=not args.no_mock,
        config_path=args.config,
        echo_logs=not args.quiet,
    )
    harness = Harness(config)
    for spec in args.script or []:
        harness.logbook.event("script_note", {"spec": spec})
    try:
        harness.start()
    except RuntimeError as exc:
        print(f"framework: {exc}", file=sys.stderr)
        harness.logbook.close()
        return 2
    if args.script:
        _apply_script(harness, args.script)
    status = harness.status()
    print(f"run dir     : {status['run_dir']}")
    print(f"trace       : {status['trace']}")
    print(f"runtime     : {status['base_url']}")
    print(f"control     : {status['control_url']}")
    print(f"mock openai : {status['mock_url']}  (provider={status['provider']})")
    print(f"virtual now : {status['virtual_now']}")
    print("Ctrl-C to stop.")
    try:
        while not harness._stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\nstopping ...")
    summary = harness.stop()
    print(f"stopped: beats={summary['beats']} mock_calls={summary['mock_calls']}")
    return 0


def _apply_script(harness: Harness, specs: Sequence[str]) -> None:
    """Install a mock script given as ``--script`` strings.

    Each spec is either ``grounded`` (the default behaviour), ``500``, ``401``,
    ``timeout[:SECONDS]``, ``malformed[:TEXT]``, or ``json:<payload>``.
    """
    replies = []
    for spec in specs:
        lowered = spec.strip()
        if lowered in {"grounded", "auto"}:
            replies.append(MockReply())
        elif lowered.isdigit():
            replies.append(MockReply.http_error(int(lowered)))
        elif lowered.startswith("timeout"):
            _, _, seconds = lowered.partition(":")
            replies.append(MockReply.timeout(float(seconds or 60.0)))
        elif lowered.startswith("malformed"):
            _, _, text = lowered.partition(":")
            replies.append(MockReply.malformed(text or "这不是 JSON。"))
        elif lowered.startswith("json:"):
            replies.append(MockReply(payload=json.loads(lowered[5:])))
        else:
            raise SystemExit(f"unrecognised --script spec {spec!r}")
    harness.mock.script = MockScript(replies)
    harness.logbook.event("mock_script", {"replies": len(replies), "specs": list(specs)})


# ------------------------------------------------------------------ time / tick


def cmd_time(args: argparse.Namespace) -> int:
    """Change the running harness's clock."""
    base = resolve_control_url(args.control, args.run_dir)
    if args.time_action == "set":
        result = _post(f"{base}/control/time/set", {"when": args.value})
    elif args.time_action == "advance":
        result = _post(f"{base}/control/time/advance", {"by": args.value})
    elif args.time_action == "scale":
        result = _post(f"{base}/control/time/scale", {"scale": float(args.value)})
    elif args.time_action == "freeze":
        result = _post(f"{base}/control/time/freeze", {})
    elif args.time_action == "unfreeze":
        result = _post(f"{base}/control/time/unfreeze", {})
    else:  # pragma: no cover - argparse restricts the choices
        raise SystemExit(f"unknown time action {args.time_action!r}")
    _print_result(result)
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    """Force one heartbeat, optionally advancing the clock first."""
    base = resolve_control_url(args.control, args.run_dir)
    payload = {"by": args.by} if args.by else {}
    result = _post(f"{base}/control/tick", payload)
    body = result.get("result") or {}
    print(f"beat {body.get('beat')} at {body.get('virtual_now')}")
    _print_variables(body.get("variables") or {})
    return 0


def cmd_endogenous(args: argparse.Namespace) -> int:
    """Force one endogenous decision round."""
    base = resolve_control_url(args.control, args.run_dir)
    payload: dict[str, Any] = {"force": not args.respect_gate}
    if args.when:
        payload["when"] = args.when
    result = _post(f"{base}/control/endogenous", payload)
    print(json.dumps(result.get("result") or {}, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print the running harness's status and last-seen variables."""
    if args.run_dir or args.control:
        base = resolve_control_url(args.control, args.run_dir)
        result = _get(f"{base}/control/status")
        clock = result.get("clock") or {}
        print(f"virtual now : {clock.get('virtual_now')}")
        print(f"wall now    : {clock.get('wall_now')}")
        print(f"offset      : {clock.get('offset_seconds')}s   scale={clock.get('scale')}   frozen={clock.get('frozen')}")
        if args.full:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    raise SystemExit("pass --run-dir <dir> or --control http://127.0.0.1:PORT")


def cmd_shutdown(args: argparse.Namespace) -> int:
    """Ask a running harness to stop."""
    base = resolve_control_url(args.control, args.run_dir)
    _post(f"{base}/control/shutdown", {})
    print("asked the harness to stop")
    return 0


# ------------------------------------------------------------------ program IO


def _program(args: argparse.Namespace) -> ProgramClient:
    """Build a client for the running program."""
    return ProgramClient(resolve_runtime_url(args.runtime, args.run_dir))


def cmd_say(args: argparse.Namespace) -> int:
    """Append a user message, running the program's full foreground path."""
    client = _program(args)
    try:
        result = client.say(
            args.text,
            conversation_id=args.conversation,
            event_id=args.event_id,
            timestamp=args.at,
        )
    except ProgramError as exc:
        raise SystemExit(str(exc)) from exc
    outcome = result.get("outcome") or {}
    event = outcome.get("event") or {}
    print(f"event      : {event.get('event_id')}  at {event.get('timestamp')}")
    print(f"duplicate  : {outcome.get('duplicate', result.get('duplicate', False))}")
    if outcome.get("semantic_status"):
        print(f"settlement : {outcome.get('semantic_status')}")
    if args.full:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """Ask for a deep cognitive refresh -- the path that uses the mock endpoint."""
    client = _program(args)
    signals: dict[str, Any] = {}
    for name in (
        "major_event",
        "matter_due",
        "history_suspect",
        "user_evidence_overturns",
        "wants_proactive",
    ):
        if getattr(args, name.replace("-", "_"), False):
            signals[name] = True
    if args.candidate_pool_size is not None:
        signals["candidate_pool_size"] = args.candidate_pool_size
    if args.force:
        signals["force"] = True
    try:
        result = client.refresh(now=args.at or None, **signals)
    except ProgramError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"ran        : {result.get('ran')}   reason={result.get('reason')!r}")
    print(f"trigger    : {result.get('trigger', {}).get('reason')!r}   provider={result.get('provider')!r}")
    print(f"operations : {result.get('operations')}   applied={result.get('applied')}")
    print(f"violations : {result.get('violations')}")
    if args.full:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    """Show the events the program declined to guess about."""
    client = _program(args)
    try:
        backlog = client.backlog()
    except ProgramError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(backlog.get("stats", {}), ensure_ascii=False))
    for item in backlog.get("items", [])[: args.limit or 20]:
        print(f"  {item.get('event_id')}  relevance={item.get('potential_relevance')}")
    if args.full:
        print(json.dumps(backlog, ensure_ascii=False, indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- tail


def cmd_tail(args: argparse.Namespace) -> int:
    """Print the structured trace, optionally following it."""
    trace = Path(args.run_dir) / "trace.jsonl"
    if not trace.exists():
        raise SystemExit(f"no trace at {trace}")
    kinds = {item.strip() for item in (args.kind or "").split(",") if item.strip()}
    seen = 0
    with trace.open("r", encoding="utf-8") as handle:
        while True:
            line = handle.readline()
            if not line:
                if not args.follow:
                    return 0
                time.sleep(0.3)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kinds and record.get("kind") not in kinds:
                continue
            seen += 1
            if args.json:
                print(json.dumps(record, ensure_ascii=False))
            else:
                print(_format_record(record))
            if args.limit and seen >= args.limit:
                return 0


def _format_record(record: dict[str, Any]) -> str:
    """Render one trace record as a single readable line."""
    kind = record.get("kind", "?")
    moment = record.get("virtual_now") or record.get("wall_now") or ""
    scalars = []
    for key, value in record.items():
        if key in {"seq", "kind", "virtual_now", "wall_now", "detail", "tick", "request", "reply"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            scalars.append(f"{key}={value}")
    return f"{record.get('seq', 0):>6} {moment} [{kind}] " + " ".join(scalars)


def _print_result(result: dict[str, Any]) -> None:
    """Print a control result compactly."""
    if not result.get("ok", False):
        raise SystemExit(f"control plane refused: {result.get('error')}")
    print(json.dumps({key: value for key, value in result.items() if key != "ok"}, ensure_ascii=False, default=str))


def _print_variables(variables: dict[str, Any]) -> None:
    """Print the variable block of a heartbeat."""
    order = (
        "mood_valence",
        "mood_arousal",
        "mood_stability",
        "impulse",
        "restraint",
        "pressure",
        "allow_proactive",
        "contact_count_today",
        "unresolved",
        "unfinished_open",
        "candidates_active",
        "boundaries_effective",
        "outbox_pending",
        "outbox_delivered",
        "attempts_open",
        "next_wake_at",
        "next_wake_in_s",
        "next_wake_reasons",
        "quiet_hours",
    )
    for key in order:
        if key in variables:
            print(f"  {key:<20} {variables[key]}")



# --------------------------------------------------------------------------- chat


def cmd_chat(args: argparse.Namespace) -> int:
    """Open a chat window: you talk to the main LLM, the Runtime works behind it.

    Settings come from three layers, nearest first: the command line, the
    ``--config`` file, the built-in defaults. The value axes are **merged** rather
    than replaced, so ``--values user_care=0.9`` adjusts the active persona
    instead of silently discarding the other seven axes it configured.
    """
    try:
        file_config = load_client_config(args.config, persona=args.persona) if args.config else ClientConfig()
    except ConfigError as exc:
        print(f"配置有问题：{exc}", file=sys.stderr)
        return 2

    llm = file_config.llm
    clock = file_config.clock
    rig = file_config.harness
    persona = file_config.persona

    # The command line wins, per setting; the config file fills the rest.
    system_prompt = _pick(args.system_prompt, persona.system_prompt, llm.system_prompt, default="")
    values = {**persona.values, **(_parse_values(args.values, args.values_file) or {})}
    semantics = "mock" if args.mock_semantics else rig.semantics
    chosen_run_dir = _pick(args.run_dir, rig.run_dir)
    run_dir = Path(chosen_run_dir) if chosen_run_dir else DEFAULT_RUNS_DIR / time.strftime("%Y%m%d-%H%M%S")

    config = HarnessConfig(
        run_dir=run_dir,
        program_src=Path(_pick(args.program_src, rig.program_src, default=DEFAULT_PROGRAM_SRC)),
        base_dir=Path(args.base_dir) if args.base_dir else None,
        start_time=_pick(args.start_time, clock.start_time),
        time_scale=float(_pick(args.time_scale, clock.time_scale, default=1.0)),
        step=_pick(args.step, clock.step),
        heartbeat_interval_s=float(_pick(args.heartbeat_interval, clock.heartbeat_interval_s, default=1.0)),
        status_interval_s=float(_pick(args.status_interval, clock.status_interval_s, default=2.0)),
        seed=_pick(args.seed, rig.seed, default=20260915),
        use_mock_semantics=semantics != "disabled",
        echo_logs=False,
        llm_base_url=_pick(args.llm_base_url, llm.base_url, default=""),
        llm_model=_pick(args.llm_model, llm.model, default=""),
        llm_system_prompt=system_prompt,
        llm_temperature=float(_pick(args.temperature, llm.temperature, default=0.8)),
        llm_max_tokens=int(_pick(args.max_tokens, llm.max_tokens, default=800)),
        llm_timeout_s=float(_pick(args.timeout_s, llm.timeout_s, default=60.0)),
        llm_api_key_env=_pick(args.api_key_env, llm.api_key_env, default="CF_MAIN_LLM_API_KEY"),
        plugin_root=Path(_pick(args.plugin_root, rig.plugin_root, default=str(DEFAULT_PLUGIN_ROOT))),
        values=values,
        semantic_from_main_llm=semantics == "main_llm",
    )

    harness = Harness(config)
    if file_config.source_path is not None:
        harness.logbook.event(
            "config_loaded",
            {
                "path": str(file_config.source_path),
                "persona": persona.name,
                "persona_description": persona.description,
                "values_overridden": persona.values,
                "personas_available": sorted(file_config.personas),
                "semantics": semantics,
            },
            message=(
                f"[config] {file_config.source_path.name} persona={persona.name} "
                f"({len(persona.values)} 个轴被覆盖)"
            ),
        )
    try:
        harness.start()
    except RuntimeError as exc:
        print(f"framework: {exc}", file=sys.stderr)
        harness.logbook.close()
        return 2

    tui = ChatTUI(
        platform=harness.platform,
        clock=harness.clock,
        send=lambda text, umo, on_delta=None: harness.user_turn(text, umo, on_delta),
        status_provider=harness.last_variables,
        control={
            "beat": lambda _arg: "心跳完成",
            "endogenous": lambda _arg: json.dumps(harness.endogenous({}), ensure_ascii=False)[:400],
            "status": lambda _arg: json.dumps(harness.status(), ensure_ascii=False, indent=2, default=str)[:4000],
        },
        session_name=rig.session or "default",
        history=ChatHistory(
            _history_path(
                explicit=_pick(args.history, rig.history),
                config_path=file_config.source_path,
            )
        ),
        recap=int(_pick(args.recap, rig.recap, default=6)),
        tty=None if not args.no_tty else False,
    )
    tui.banner(
        llm=harness.llm.describe() if harness.llm is not None else {},
        runtime_url=harness.base_url,
        persona=persona,
        config_path=file_config.source_path,
    )
    if not getattr(harness.llm, "configured", False):
        print(
            "注意：没有配置主 LLM 端点，当前用确定性替身，回复不代表真模型。\n"
            "      设 CF_MAIN_LLM_BASE_URL / CF_MAIN_LLM_MODEL / CF_MAIN_LLM_API_KEY，\n"
            "      或传 --llm-base-url / --llm-model，或用 --config 指定配置文件。",
            file=sys.stderr,
        )
    try:
        return tui.run()
    except KeyboardInterrupt:
        return 0
    finally:
        summary = harness.stop()
        print(f"\n结束：{summary['beats']} 次心跳，LLM 调用 {summary.get('llm_stats', {}).get('calls', 0)} 次")
        print(f"日志：{run_dir}/framework.log  轨迹：{run_dir}/trace.jsonl")


def cmd_config_init(args: argparse.Namespace) -> int:
    """Write a commented example configuration."""
    try:
        written = write_example(Path(args.path), force=args.force)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"已写入 {written}")
    print(f"改完用 `cf chat --config {written}` 启动；`cf config show --config {written}` 看解析结果。")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    """Print the resolved configuration, so the layering is never a guess."""
    try:
        config = load_client_config(args.config, persona=args.persona)
    except ConfigError as exc:
        print(f"配置有问题：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
    prompt = config.persona.system_prompt or config.llm.system_prompt
    print(f"\n--- 生效的 system prompt（来自 {config.persona.source or '（空）'}）---")
    print(prompt if prompt else "（没有配置任何人格提示词，将使用框架内置的默认人格）")
    return 0


def _parse_values(inline: str, path: str) -> dict[str, float]:
    """Build the value overrides from ``k=v,k=v`` and/or a JSON file.

    Raises:
        SystemExit: On an unknown axis or an unparseable number, naming the
            offender. A silently ignored personality setting is the worst
            outcome here: the operator would believe they had configured it.
    """
    values: dict[str, float] = {}
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SystemExit(f"{path}: expected a JSON object of axis -> number")
        values.update({str(k): float(v) for k, v in raw.items()})
    for chunk in (inline or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, raw_value = chunk.partition("=")
        if not sep:
            raise SystemExit(f"价值观要写成 轴=数值，收到的是 {chunk!r}")
        values[key.strip()] = float(raw_value)
    unknown = sorted(set(values) - set(VALUE_AXES))
    if unknown:
        lines = "\n".join(
            f"  {name:<26} 默认 {default:<5} {doc}" for name, (default, doc) in VALUE_AXES.items()
        )
        raise SystemExit(f"未知的价值观轴：{', '.join(unknown)}\n可用的轴：\n{lines}")
    return values


def _history_path(*, explicit: str, config_path: Any) -> Path:
    """Return where the chat window's durable history should live.

    Three cases, in order: an explicit path (``--history``, or ``[harness].history``,
    which the config layer already resolved against the config file) wins; otherwise a
    loaded config file puts the history *beside itself*, so an experiment carries its own
    conversation; otherwise the cwd-relative default.

    It is deliberately not derived from ``run_dir``: that is stamped per run, and a
    history that dies with the run is not a history.
    """
    if explicit:
        return Path(explicit)
    if config_path is not None:
        return Path(config_path).parent / DEFAULT_RUNS_DIR / "chat_history.jsonl"
    return DEFAULT_RUNS_DIR / "chat_history.jsonl"


def _pick(*candidates: Any, default: Any = None) -> Any:
    """Return the first candidate that is neither ``None`` nor an empty string.

    Layers the three sources of a setting: the command line wins over the config
    file, which wins over the built-in default. The config-affected flags default
    to ``None`` precisely so "not given" is distinguishable from "given the same
    value as the default".
    """
    for value in candidates:
        if value is not None and value != "":
            return value
    return default


# --------------------------------------------------------------------------- cli


def cmd_onebot(args: argparse.Namespace) -> int:
    """Talk to a real AstrBot over OneBot v11, with a chat box and a live log.

    This is the piece that answers "does the real deployment behave the way we think":
    the framework's own host drives the adapter in process, while this connects to a
    running AstrBot as a OneBot implementation would (reverse WebSocket), so a message
    typed here travels the whole real path - adapter, plugin, Runtime - and every API
    call AstrBot makes on the way back is printed.
    """
    # A real model replies with emoji, and the Windows console defaults to GBK, where
    # printing one raises UnicodeEncodeError and takes the whole session down.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    frontend = OneBotFrontend(
        ws_url=args.ws_url,
        token=args.token or os.environ.get("CF_ONEBOT_TOKEN", ""),
        self_id=args.self_id,
        user_id=args.user_id,
        group_id=args.group_id,
        reconnect_interval=args.reconnect_interval,
    )
    service = OneBotService(
        frontend, host=args.http_host, port=args.http_port, log_path=args.log_file or None
    )
    service.start()
    print(f"onebot: {args.ws_url}  (self_id={args.self_id})")
    print(f"chat+log: http://{args.http_host}:{service.port}/")
    if not frontend.wait_connected(timeout=args.connect_timeout):
        print(f"连接 {args.ws_url} 超时（{args.connect_timeout}s）")
        service.stop()
        return 1
    print("connected")
    try:
        for text in args.say or []:
            frontend.send_user_message(text, group=args.group)
            if not args.wait:
                # With --wait the transcript below prints the same line already.
                print(f"我: {text}")
        if args.wait:
            for line in tail_transcript(service, seconds=args.wait, interval=0.4):
                print(line)
        if args.repl:
            print("输入消息回车发送，空行退出。")
            while True:
                try:
                    line = input("我> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    break
                if line.startswith("/"):
                    _onebot_debug_command(service, line)
                    continue
                try:
                    frontend.send_user_message(line, group=args.group)
                except Exception as exc:  # noqa: BLE001 - report, keep the session
                    print(f"发送失败: {type(exc).__name__}: {exc}")
            return 0
        if args.say or args.wait:
            return 0
        print("前台运行中，Ctrl+C 退出。")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        service.stop()


def _onebot_debug_command(service: OneBotService, line: str) -> None:
    """Handle a ``/`` command inside the REPL: debug views, no chat side effects."""
    parts = line.split(maxsplit=1)
    command = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if command == "/state":
        _print_result(service.frontend.snapshot())
    elif command == "/calls":
        for call in service.frontend.calls[-10:]:
            print(f"  {call.action} -> {call.status}/{call.retcode} {json.dumps(call.params, ensure_ascii=False)[:120]}")
    elif command == "/frames":
        for frame in service.frontend.frame_log(limit=20):
            arrow = "←" if frame["direction"] == "in" else "→"
            print(f"  {arrow} {frame['kind']} {json.dumps(frame['payload'], ensure_ascii=False)[:200]}")
    elif command == "/event":
        payload = json.loads(rest) if rest else {}
        service.frontend._send(payload, kind="event")
        print("  已注入事件")
    elif command == "/meta":
        print("  " + json.dumps(service.frontend.send_meta_event(rest or "connect"), ensure_ascii=False))
    else:
        print("  可用：/state /calls /frames /event <json> /meta [sub_type]")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="cf",
        description="外接测试框架：可控时间 + OpenAI 兼容 mock 端点 + 变量日志（不修改原程序）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="启动一个可外部调控的 harness（前台运行）")
    run.add_argument("--run-dir", default="", help="本次运行的产物目录（默认 runs/<时间戳>）")
    run.add_argument("--program-src", default=DEFAULT_PROGRAM_SRC, help="原程序 src 目录")
    run.add_argument("--base-dir", default="", help="原程序数据目录（默认 <run-dir>/program）")
    run.add_argument("--config", default=None, help="原程序的 TOML/JSON 配置")
    run.add_argument("--host", default="127.0.0.1", help="原程序 HTTP 绑定地址")
    run.add_argument("--port", type=int, default=0, help="原程序 HTTP 端口（0=自动）")
    run.add_argument("--start-time", default=None, help="虚拟起始时间（ISO-8601，缺省为真实当前时间）")
    run.add_argument("--time-scale", type=float, default=1.0, help="虚拟秒 / 真实秒")
    run.add_argument("--step", default=None, help="每个心跳推进的虚拟时长，如 1h（缺省不推进）")
    run.add_argument("--heartbeat-interval", type=float, default=1.0, help="心跳间隔（真实秒）")
    run.add_argument("--seed", type=int, default=20260915, help="原程序随机种子")
    run.add_argument("--no-mock", action="store_true", help="不接 mock 端点，用程序默认的 disabled provider")
    run.add_argument("--script", action="append", default=[], help="mock 脚本，如 500 / timeout:5 / malformed / json:{...}")
    run.add_argument("--quiet", action="store_true", help="不回显滚动日志到 stderr")
    run.set_defaults(func=cmd_run)

    t = sub.add_parser("time", help="调控运行中的 harness 时间")
    t.add_argument("time_action", choices=["set", "advance", "scale", "freeze", "unfreeze"])
    t.add_argument("value", nargs="?", default="", help="set=ISO 时间；advance=时长如 8h；scale=倍率")
    t.add_argument("--control", default="", help="控制面 URL")
    t.add_argument("--run-dir", default="", help="从该目录的 trace 里找控制面")
    t.set_defaults(func=cmd_time)

    tick = sub.add_parser("tick", help="立刻跑一次心跳")
    tick.add_argument("--by", default="", help="先推进的时长，如 2h")
    tick.add_argument("--control", default="")
    tick.add_argument("--run-dir", default="")
    tick.set_defaults(func=cmd_tick)

    endo = sub.add_parser("endogenous", help="强制一次内源决策回合")
    endo.add_argument("--when", default="", help="该回合使用的虚拟时刻")
    endo.add_argument("--respect-gate", action="store_true", help="不强制，尊重调度门")
    endo.add_argument("--control", default="")
    endo.add_argument("--run-dir", default="")
    endo.set_defaults(func=cmd_endogenous)

    st = sub.add_parser("status", help="查看运行中的 harness 状态")
    st.add_argument("--control", default="")
    st.add_argument("--run-dir", default="")
    st.add_argument("--full", action="store_true", help="打印完整 JSON")
    st.set_defaults(func=cmd_status)

    sd = sub.add_parser("shutdown", help="让运行中的 harness 停止")
    sd.add_argument("--control", default="")
    sd.add_argument("--run-dir", default="")
    sd.set_defaults(func=cmd_shutdown)

    tail = sub.add_parser("tail", help="查看结构化轨迹")
    tail.add_argument("--run-dir", required=True)
    tail.add_argument("--kind", default="", help="只看这些 kind，逗号分隔")
    tail.add_argument("--limit", type=int, default=0, help="最多打印多少条（0=全部）")
    tail.add_argument("--follow", action="store_true", help="持续跟踪")
    tail.add_argument("--json", action="store_true", help="原样打印 JSON 行")
    tail.set_defaults(func=cmd_tail)

    say = sub.add_parser("say", help="让用户说一句话（走原程序完整前台路径）")
    say.add_argument("text", help="用户说的话")
    say.add_argument("--conversation", default="", help="会话 id")
    say.add_argument("--at", default="", help="这句话发生的时间（ISO-8601）")
    say.add_argument("--event-id", default="", help="自带 event id 可让重复调用幂等")
    say.add_argument("--runtime", default="")
    say.add_argument("--run-dir", default="")
    say.add_argument("--full", action="store_true")
    say.set_defaults(func=cmd_say)

    refresh = sub.add_parser("refresh", help="请求一次深层认知刷新（这条才会打到 mock 端点）")
    refresh.add_argument("--at", default="", help="该次刷新使用的虚拟时刻")
    refresh.add_argument("--major-event", action="store_true", help="触发信号：发生了关系上的大事")
    refresh.add_argument("--matter-due", action="store_true", help="触发信号：有未尽之事到期")
    refresh.add_argument("--history-suspect", action="store_true", help="触发信号：早先的解释可能不对")
    refresh.add_argument("--user-evidence-overturns", action="store_true", help="触发信号：新证据推翻了旧读数")
    refresh.add_argument("--wants-proactive", action="store_true", help="触发信号：动机层想行动")
    refresh.add_argument("--candidate-pool-size", type=int, default=None, help="触发信号：当前候选池大小")
    refresh.add_argument("--force", action="store_true", help="跳过触发判定（诊断用）")
    refresh.add_argument("--runtime", default="")
    refresh.add_argument("--run-dir", default="")
    refresh.add_argument("--full", action="store_true")
    refresh.set_defaults(func=cmd_refresh)

    backlog = sub.add_parser("backlog", help="看还没被理解的事件")
    backlog.add_argument("--limit", type=int, default=20)
    backlog.add_argument("--runtime", default="")
    backlog.add_argument("--run-dir", default="")
    backlog.add_argument("--full", action="store_true")
    backlog.set_defaults(func=cmd_backlog)

    chat = sub.add_parser("chat", help="打开聊天窗口：你和主 LLM 对话，Runtime 在后台工作")
    chat.add_argument("--config", default="", help="客户端配置文件（.toml/.json）；命令行参数优先于它")
    chat.add_argument("--persona", default="", help="启用哪个命名人格档案（见配置文件）")
    chat.add_argument("--run-dir", default="", help="本次运行的产物目录（默认 runs/<时间戳>）")
    chat.add_argument("--program-src", default=None, help="原程序 src 目录（默认读配置或 ../runtime/src）")
    chat.add_argument("--plugin-root", default=None, help="AstrBot 插件仓库位置")
    chat.add_argument("--base-dir", default="")
    chat.add_argument("--start-time", default=None, help="虚拟起始时间（ISO-8601）")
    chat.add_argument("--time-scale", type=float, default=None, help="虚拟秒 / 真实秒")
    chat.add_argument("--step", default=None, help="每个心跳推进的时长，如 30m")
    chat.add_argument("--heartbeat-interval", type=float, default=None,
                      help="心跳间隔（真实秒）；0 = 关掉，由 /advance 手动推进")
    chat.add_argument("--status-interval", type=float, default=None,
                      help="状态栏变量刷新间隔（真实秒），与心跳无关")
    chat.add_argument("--seed", type=int, default=None)
    chat.add_argument(
        "--history",
        default="",
        help="持久对话记录的路径（默认放在配置文件旁边；见 [harness].history）",
    )
    chat.add_argument(
        "--recap", type=int, default=None, help="打开窗口时回显本会话最近几条（0 = 不回显）"
    )
    chat.add_argument("--llm-base-url", default="", help="主 LLM 端点（默认读 CF_MAIN_LLM_BASE_URL）")
    chat.add_argument("--llm-model", default="", help="主 LLM 模型名（默认读 CF_MAIN_LLM_MODEL）")
    chat.add_argument("--system-prompt", default="", help="角色设定（宿主人格，最高优先级）")
    chat.add_argument("--temperature", type=float, default=None)
    chat.add_argument("--values", default="",
                      help="覆盖人格价值观轴，如 user_care=0.95,emotional_expression=0.8")
    chat.add_argument("--values-file", default="", help="从 JSON 文件读取价值观轴")
    chat.add_argument("--mock-semantics", action="store_true",
                      help="强语义改用框架自带的 mock 端点（默认是与主 LLM 同一个端点）")
    chat.add_argument("--no-tty", action="store_true", help="关掉终端重绘（管道/重定向时用）")
    chat.add_argument("--max-tokens", type=int, default=None)
    chat.add_argument("--timeout-s", type=float, default=None, help="主 LLM 单次调用超时")
    chat.add_argument("--api-key-env", default=None, help="存放 key 的环境变量名（默认 CF_MAIN_LLM_API_KEY）")
    chat.set_defaults(func=cmd_chat)

    config_parser = sub.add_parser("config", help="客户端配置：生成示例 / 查看解析结果")
    config_sub = config_parser.add_subparsers(dest="config_action", required=True)
    config_init = config_sub.add_parser("init", help="写一份带注释的示例配置")
    config_init.add_argument("path", nargs="?", default="cf.toml")
    config_init.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    config_init.set_defaults(func=cmd_config_init)
    config_show = config_sub.add_parser("show", help="打印解析后的最终配置")
    config_show.add_argument("--config", required=True)
    config_show.add_argument("--persona", default="")
    config_show.set_defaults(func=cmd_config_show)

    onebot = sub.add_parser(
        "onebot", help="连到真实 AstrBot 的 OneBot v11 前端：聊天、日志、调试"
    )
    onebot.add_argument("--ws-url", required=True, help="AstrBot 的反向 WS 地址，如 ws://host:6299/ws")
    onebot.add_argument("--token", default="", help="access token（缺省读 CF_ONEBOT_TOKEN）")
    onebot.add_argument("--self-id", default="10001", help="机器人账号（X-Self-ID）")
    onebot.add_argument("--user-id", default="20001", help="默认私聊对象")
    onebot.add_argument("--group-id", default="30001", help="默认群号")
    onebot.add_argument("--group", action="store_true", help="以群聊身份发送")
    onebot.add_argument("--http-host", default="127.0.0.1", help="控制面绑定地址")
    onebot.add_argument("--http-port", type=int, default=6300, help="控制面端口（0 = 自动）")
    onebot.add_argument("--log-file", default="", help="把每条帧写进这个 JSONL")
    onebot.add_argument("--reconnect-interval", type=float, default=3.0, help="断线重连的起始间隔（秒）")
    onebot.add_argument("--connect-timeout", type=float, default=10.0, help="等待连接建立的秒数")
    onebot.add_argument("--say", action="append", default=[], help="发一条消息后退出（可重复）")
    onebot.add_argument("--wait", type=float, default=0.0, help="发送后收集回复的秒数")
    onebot.add_argument("--repl", action="store_true", help="交互式聊天（/state /calls /frames 可调试）")
    onebot.set_defaults(func=cmd_onebot)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
