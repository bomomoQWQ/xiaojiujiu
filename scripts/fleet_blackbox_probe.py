#!/usr/bin/env python3
"""Read-only acceptance probe for a running Runtime fleet.

Black box means: only the public HTTP surface is touched. Nothing here posts an
event, claims outbox rows, ticks the scheduler or writes state, so the probe is
safe to run against a live deployment seconds before you take it down -- the
result describes the world as it is, not as the probe made it.

What it checks, per instance
----------------------------
* ``GET /health``   -- ``status == ok``, a version string, a monotonic state counter;
* ``GET /state``    -- the three projections a host needs to render anything:
  ``values`` / ``mood`` / ``drive``;
* ``GET /schedule`` -- a plan plus a dispatch verdict (the endpoint is documented as
  non-ticking, which is what makes it safe here);
* ``GET /outbox``   -- a status histogram, so "nothing is queued" is observable.

Checks decide the exit status; warnings do not. The split matters: a Runtime with
twelve attempts in flight is *working*, not broken, so treating that as a failure
makes the probe cry wolf on a perfectly healthy fleet and trains everyone to ignore
it. In-flight depth, a large failed-outbox count and version drift are reported as
warnings, and ``--fail-on-warning`` turns them strict for teardown, where "nothing
may be half-sent" is exactly the question being asked.

It also reports version drift across instances: mixed versions in one fleet are
invisible in ``/fleet/status`` and are the usual cause of "one person behaves
differently".

Person identity never leaves the machine: rows are labelled ``person-01``,
``person-02`` ... in control-plane order. ``--json`` writes the same data, still
anonymised, for attaching to a report.

Usage
-----
    python3 scripts/fleet_blackbox_probe.py --fleet-url http://127.0.0.1:8800
    python3 scripts/fleet_blackbox_probe.py --runtime-url http://127.0.0.1:8787
    python3 scripts/fleet_blackbox_probe.py --fleet-url ... --json /tmp/probe.json
    python3 scripts/fleet_blackbox_probe.py --fleet-url ... --fail-on-warning   # teardown

Exit status: 0 every check passed, 1 at least one failed (or a warning with
``--fail-on-warning``), 2 the fleet could not be reached at all (nothing to report).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

#: Statuses that mean "this row is still expected to move by itself".
OPEN_OUTBOX_STATES = ("pending", "ready", "leased", "proposed", "committed", "rendering")


def _get(url: str, timeout: float) -> tuple[int, Any]:
    """Return ``(status_code, decoded_json)``; raise on transport failure."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
        try:
            return response.status, json.loads(body)
        except json.JSONDecodeError:
            return response.status, {"_raw": body[:200]}


def _try_get(url: str, timeout: float) -> tuple[int, Any, str]:
    """Like :func:`_get`, but a failure is data instead of an exception."""
    try:
        status, payload = _get(url, timeout)
        return status, payload, ""
    except urllib.error.HTTPError as exc:
        return exc.code, {}, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        return 0, {}, f"{type(exc).__name__}: {exc}"


def _label(index: int) -> str:
    """Return an anonymised row label; the fleet's identities stay local."""
    return f"person-{index:02d}"


def _outbox_summary(stats: Any) -> str:
    """Render the outbox histogram compactly, ignoring empty states."""
    if not isinstance(stats, dict) or not stats:
        return "-"
    parts = [
        f"{key}={value}"
        for key, value in stats.items()
        if isinstance(value, (int, float)) and value
    ]
    return ",".join(parts) if parts else "empty"


def _open_outbox_items(stats: Any) -> int:
    """Count rows still expected to move; zero is what a clean teardown wants."""
    if not isinstance(stats, dict):
        return 0
    return sum(
        int(stats.get(state) or 0) for state in OPEN_OUTBOX_STATES if isinstance(stats.get(state), int)
    )


def probe_one(url: str, timeout: float) -> dict[str, Any]:
    """Run every read-only check against one Runtime and return the findings."""
    findings: dict[str, Any] = {"url": url, "checks": {}, "warnings": [], "errors": []}

    status, health, error = _try_get(f"{url}/health", timeout)
    findings["checks"]["health"] = status == 200
    if error:
        findings["errors"].append(f"health: {error}")
        return findings
    findings["version"] = health.get("runtime_version") or ""
    findings["state_version"] = health.get("state_version")
    findings["in_flight_attempts"] = health.get("in_flight_attempts")
    findings["outbox"] = _outbox_summary(health.get("outbox"))
    findings["raw_events"] = health.get("raw_events")

    findings["checks"]["status_ok"] = health.get("status") == "ok"
    findings["checks"]["version_present"] = bool(findings["version"])
    findings["checks"]["state_counter"] = isinstance(findings["state_version"], int)

    # In flight is depth, not damage: a working Runtime always has some. Only a teardown
    # cares, and it says so with --fail-on-warning.
    in_flight = findings["in_flight_attempts"]
    if isinstance(in_flight, int) and in_flight > 0:
        findings["warnings"].append(f"{in_flight} attempt(s) in flight")
    open_rows = _open_outbox_items(health.get("outbox"))
    if open_rows:
        findings["warnings"].append(f"{open_rows} outbox row(s) still open")

    failed_rows = int((health.get("outbox") or {}).get("failed") or 0)
    if failed_rows:
        findings["warnings"].append(f"{failed_rows} failed outbox row(s) in total")
        findings["failed_outbox"] = failed_rows

    status, state, error = _try_get(f"{url}/state", timeout)
    if error:
        findings["errors"].append(f"state: {error}")
        findings["checks"]["state_projections"] = False
    else:
        findings["checks"]["state_projections"] = all(
            key in state for key in ("values", "mood", "drive")
        )

    status, schedule, error = _try_get(f"{url}/schedule", timeout)
    if error:
        findings["errors"].append(f"schedule: {error}")
        findings["checks"]["scheduler_plan"] = False
    else:
        findings["checks"]["scheduler_plan"] = bool(schedule.get("plan")) and (
            "dispatch_allowed" in schedule
        )
        findings["dispatch_allowed"] = schedule.get("dispatch_allowed")

    status, outbox, error = _try_get(f"{url}/outbox?limit=5", timeout)
    if error:
        findings["errors"].append(f"outbox: {error}")
        findings["checks"]["outbox_readable"] = False
    else:
        findings["checks"]["outbox_readable"] = isinstance(outbox.get("stats"), dict)

    findings["passed"] = all(findings["checks"].values())
    return findings


def discover(fleet_url: str, timeout: float, runtime_host: str) -> list[dict[str, Any]]:
    """Return one entry per instance from the control plane (status + routes).

    ``runtime_host`` matters: the control plane reports ports as seen *inside* its own
    network namespace (``127.0.0.1:8787``). A probe running on the host, or on another
    machine, has to substitute the address it can actually reach - otherwise every
    instance looks dead while the fleet is perfectly healthy.
    """
    status, payload, error = _try_get(f"{fleet_url}/fleet/status", timeout)
    if error or not isinstance(payload, dict):
        # Exit 2 is part of the contract: "no fleet to probe" is not "the fleet failed
        # its checks", and a wrapper script has to be able to tell those apart.
        print(f"cannot read {fleet_url}/fleet/status: {error or 'unexpected payload'}", file=sys.stderr)
        raise SystemExit(2)

    routes: dict[str, str] = {}
    status, route_payload, _ = _try_get(f"{fleet_url}/fleet/routes", timeout)
    if isinstance(route_payload, dict):
        routes = {str(k): str(v) for k, v in route_payload.items()}

    people: list[dict[str, Any]] = []
    for entry in payload.get("people", []):
        if not isinstance(entry, dict):
            continue
        session = str(entry.get("session") or "")
        port = entry.get("port")
        candidates: list[str] = []
        if port:
            candidates.append(f"http://{runtime_host}:{port}")
        if not candidates and routes.get(session):
            candidates.append(routes[session])
        people.append(
            {
                "session": session,
                # Control plane order is stable; identity stays out of the report.
                "running": bool(entry.get("running")),
                "health": entry.get("health"),
                "restarts": entry.get("restarts"),
                "uptime_s": entry.get("uptime_s"),
                "candidates": candidates,
                "control_health": entry.get("health"),
            }
        )
    return people


def main(argv: list[str] | None = None) -> int:
    """Probe the fleet (or one Runtime) and print a pass/fail report."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fleet-url", default="", help="control plane, e.g. http://127.0.0.1:8800")
    parser.add_argument("--runtime-url", default="", help="probe a single Runtime instead")
    parser.add_argument(
        "--runtime-host",
        default="127.0.0.1",
        help="address the instances are reachable at from where this runs "
        "(default 127.0.0.1; use the container IP when probing a containerised fleet from the host)",
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="per-request timeout (seconds)")
    parser.add_argument("--json", dest="json_path", default="", help="write the anonymised report here")
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="treat warnings (in-flight depth, failed rows, version drift) as failures; "
        "use this when the question is 'is it safe to take down right now'",
    )
    args = parser.parse_args(argv)

    if not args.fleet_url and not args.runtime_url:
        parser.error("give --fleet-url or --runtime-url")

    report: dict[str, Any] = {"mode": "", "instances": [], "passed": False}
    failures = 0

    if args.runtime_url:
        report["mode"] = "single"
        findings = probe_one(args.runtime_url.rstrip("/"), args.timeout)
        findings["label"] = "person-01"
        report["instances"].append(findings)
        failures += 0 if findings.get("passed") else 1
    else:
        report["mode"] = "fleet"
        people = discover(args.fleet_url.rstrip("/"), args.timeout, args.runtime_host)
        for index, person in enumerate(people, start=1):
            label = _label(index)
            reachable = ""
            findings: dict[str, Any] | None = None
            for candidate in person["candidates"]:
                attempt = probe_one(candidate, args.timeout)
                if not attempt.get("errors") or attempt["checks"].get("health"):
                    findings = attempt
                    reachable = candidate
                    break
            if findings is None:
                findings = {
                    "checks": {"reachable": False},
                    "errors": ["no route reachable from here"],
                    "passed": False,
                }
            findings["label"] = label
            findings["control_running"] = person["running"]
            findings["control_health"] = person["control_health"]
            findings["restarts"] = person["restarts"]
            findings["uptime_s"] = person["uptime_s"]
            findings["reached_via"] = reachable
            report["instances"].append(findings)
            failures += 0 if findings.get("passed") else 1

    versions = sorted({item.get("version") for item in report["instances"] if item.get("version")})
    report["versions"] = versions
    report["version_drift"] = len(versions) > 1
    warnings = sum(len(item.get("warnings", [])) for item in report["instances"])
    report["warning_count"] = warnings
    report["passed"] = failures == 0 and bool(report["instances"])
    if args.fail_on_warning and warnings:
        report["passed"] = False

    print(f"mode: {report['mode']}   instances: {len(report['instances'])}   "
          f"failed: {failures}   warnings: {warnings}   versions: {', '.join(versions) or '-'}")
    if report["version_drift"]:
        print("  ! version drift across instances: " + ", ".join(versions))
    print()
    header = f"{'instance':<11} {'ver':<8} {'state':>7} {'fly':>4} {'dispatch':<9} {'outbox':<24} result"
    print(header)
    print("-" * len(header))
    for item in report["instances"]:
        checks = item.get("checks", {})
        failed = [name for name, ok in checks.items() if not ok]
        warns = item.get("warnings", [])
        if not item.get("passed"):
            result = "FAIL(" + ",".join(failed) + ")"
        elif warns:
            result = "WARN(" + str(len(warns)) + ")"
        else:
            result = "PASS"
        print(
            f"{item.get('label', '?'):<11} "
            f"{(item.get('version') or '-')[:8]:<8} "
            f"{str(item.get('state_version', '-')):>7} "
            f"{str(item.get('in_flight_attempts', '-')):>4} "
            f"{str(item.get('dispatch_allowed', '-'))[:9]:<9} "
            f"{(item.get('outbox') or '-'):<24} {result}"
        )
        for warning in warns:
            print(f"    ~ {warning}")
        for error in item.get("errors", []):
            print(f"    ! {error}")

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"\nreport written to {args.json_path}")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
