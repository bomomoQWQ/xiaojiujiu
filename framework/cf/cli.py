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
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .harness import DEFAULT_PROGRAM_SRC, Harness, HarnessConfig
from .logbook import Logbook
from .mock_openai import MockReply, MockScript
from .program import ProgramClient, ProgramError

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


# --------------------------------------------------------------------------- cli


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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
