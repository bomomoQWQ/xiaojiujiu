"""Stand up the Runtime fleet: one container, one Runtime process per person.

Generates two files and starts the service:

* ``fleet.yml`` -- a single ``runtime-fleet`` service. Its environment is copied
  from the hand-written base compose's ``runtime`` service, so the fleet cannot
  drift away from the single-instance configuration; only the values profile for
  this character is added on top.
* ``people.json`` -- the sessions the fleet serves, which the supervisor keeps as
  its source of truth (provision/deprovision rewrite it).

Per-person ports are 8787 upward and stay inside the compose network; the plugin
reaches them through the fleet's hostname. Only the control surface (8800) is
published by default. ``--publish-people`` additionally publishes the whole person
port range to the **LAN** (operator request, 2026-09-18): the per-person APIs have no
authentication of their own, and the person-to-port mapping is assigned by order, so
publishing one port would point at a different person after the next recreate -- the
range is the only honest form of "open that URL to the LAN".

Usage::

    python3 scripts/build_runtime_fleet.py --people 20001-20010 \\
        --compose /home/bomomo/astrbot_test/astrbot.yml \\
        --fleet /home/bomomo/astrbot_test/fleet.yml \\
        --data /home/bomomo/astrbot_test/fleet-data \\
        --checkout /home/bomomo/astrbot_test/src/xiaojiujiu
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SESSION_TEMPLATE = "default:FriendMessage:{person}"

#: The character's values profile. Values are seeded into a Runtime's state *once*,
#: when its database is created, so the fleet sets them here -- a person
#: provisioned later gets them too, and an existing instance is unaffected.
#: The character's values profile. Values are seeded into a Runtime's state *once*,
#: when its database is first created (``runtime.py`` -> ``ensure_defaults``); after
#: that the copy in the database is authoritative, so **changing these does nothing to
#: an instance that already exists** -- an existing person needs the stored
#: ``runtime_state.values_json`` edited (see ``scripts/set_yandere_values.sh``).
#:
#: The profile below is the "high attachment" one: it is tuned through the axes that
#: actually reach production code, and only those. Two of the eight are inert today --
#: ``emotional_expression`` and ``autonomy`` appear only in ``emotion.appraise_event``,
#: which has no production caller (the ingest path settles through
#: ``semantic.settlement_to_evaluation``, which carries no values). They are set anyway
#: so the intent is recorded and a future wiring picks it up.
#:
#: What each live axis does (file:line at the time of writing):
#:   boundary_respect      motivation.py:469 action cost against a boundary,
#:                         :666 pressure suppresses action, :673 silence utility +0.75x
#:   user_care             motivation.py:657 drive to resolve an unfinished matter
#:   relationship_maintenance  motivation.py:664 pull grows with absence,
#:                         memory.py:414 emotional salience of relationship memories
#:   stability_commitment  emotion.py:326 feelings decay slower, memory.py:404 long-term memory
#:   conflict_directness   motivation.py:675 silence utility -0.25x (speaks up instead)
#:   curiosity             motivation.py:665 approach drive +0.20x
VALUES = {
    "CR_VALUES__USER_CARE": "0.97",
    "CR_VALUES__RELATIONSHIP_MAINTENANCE": "0.97",
    "CR_VALUES__BOUNDARY_RESPECT": "0.35",
    "CR_VALUES__STABILITY_COMMITMENT": "0.95",
    "CR_VALUES__EMOTIONAL_EXPRESSION": "0.82",
    "CR_VALUES__CONFLICT_DIRECTNESS": "0.80",
    "CR_VALUES__AUTONOMY": "0.35",
    "CR_VALUES__CURIOSITY": "0.90",
}

#: Completion budget for one structured provider call.
#:
#: A ceiling, not a spend: only generated tokens are billed, so there is no reason to
#: run it close to the wire. The API accepts 1..384K and defaults to 8K in
#: non-thinking mode; 65536 sits far above anything a bounded refresh prompt can
#: produce (measured 481-1200 tokens) while staying well under the model's maximum,
#: so the budget can never be what truncates the reply. It mattered: at the code
#: default of 1024 the deep refresh ended mid-JSON, JSON Output only guarantees valid
#: JSON when the reply is complete, and the whole refresh silently degraded to empty.
SEMANTIC_ENV = {
    "CR_SEMANTIC_MAX_TOKENS": "65536",
}

#: Beta sampling cadence. A Runtime's own scheduler sleeps up to
#: ``max_interval_seconds`` (5400 by default) and a user message does *not* wake it,
#: so a week at the default would hold a handful of verdicts per person -- too thin
#: to read why the character did or did not act. Lowering the ceiling does not change
#: what she does: the hazard is integrated between decisions and is frequency
#: independent by design (two short intervals keep the survival probability of one
#: long one), so this only buys resolution. 900s = at least 96 verdicts per person
#: per day, still one or two orders of magnitude below the raw event volume.
SCHEDULER_ENV = {
    "CR_SCHEDULER__MIN_INTERVAL_SECONDS": "60",
    "CR_SCHEDULER__MAX_INTERVAL_SECONDS": "900",
}


def expand(people: str) -> list[str]:
    """Expand ``20001,20003-20010`` into a de-duplicated list of ids."""
    result: list[str] = []
    for chunk in people.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, end = chunk.split("-", 1)
            result.extend(str(number) for number in range(int(start), int(end) + 1))
        else:
            result.append(chunk)
    unique: dict[str, None] = {}
    for item in result:
        unique.setdefault(item, None)
    return list(unique)


def base_environment(compose: Path) -> tuple[str, list[str]]:
    """Return the base Runtime image and its environment lines (minus its session)."""
    lines = compose.read_text(encoding="utf-8").splitlines()
    image = ""
    env: list[str] = []
    in_runtime = False
    in_env = False
    for line in lines:
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            in_runtime = line.strip() == "runtime:"
            in_env = False
            continue
        if not in_runtime:
            continue
        if line.strip() == "environment:":
            in_env = True
            continue
        if in_env:
            if line.startswith("      ") and ":" in line:
                if not line.strip().startswith("CR_CONVERSATION_ID"):
                    env.append(line)
                continue
            if line.strip() and not line.startswith("      "):
                in_env = False
        if not image and line.strip().startswith("image:"):
            image = line.split("image:", 1)[1].strip()
    if not image or not env:
        sys.exit("could not read the base runtime service (image or environment)")
    return image, env


def render(image: str, env: list[str], compose: Path, checkout: Path, *, publish_people: bool = False) -> str:
    """Render the fleet compose file.

    Args:
        image: Runtime image tag.
        env: Environment lines copied from the base compose service.
        compose: The base compose file (its directory holds ``fleet-data``).
        checkout: Repository checkout mounted read-only at ``/fleet``.
        publish_people: Publish the per-person port range to the LAN. Off by default:
            those APIs have no authentication, so exposing them is an operator choice.
    """
    lines = [
        "# Generated by scripts/build_runtime_fleet.py -- re-run it instead of editing.",
        "# One container, one Runtime process per person: a Runtime holds one",
        "# character's memory and state, so isolation is per instance, not per container.",
        "services:",
        "  runtime-fleet:",
        f"    image: {image}",
        "    container_name: xxj-runtime-fleet",
        "    restart: unless-stopped",
        "    entrypoint:",
        "      - python",
        "      - /fleet/runtime_fleet.py",
        "      - --advertise-host",
        "      - runtime-fleet",
        "      - --people-file",
        "      - /fleet-data/people.json",
        "    environment:",
        *env,
        *[f"      {key}: {value}" for key, value in VALUES.items()],
        *[f"      {key}: {value}" for key, value in SCHEDULER_ENV.items()],
        *[f"      {key}: {value}" for key, value in SEMANTIC_ENV.items()],
        "    volumes:",
        f"      - {checkout}/scripts:/fleet:ro",
        f"      - {compose.parent}/fleet-data:/fleet-data",
        "      - runtime-fleet-data:/data",
        "      - /etc/localtime:/etc/localtime:ro",
        "    ports:",
        "      - \"8800:8800\"    # control surface",
        *(
            [
                "      # 用户要求（2026-09-18）：每个人的 Runtime HTTP API 也开给局域网。",
                "      # 为什么是一段而不是一个端口：person -> port 是**按顺序动态分配**的，",
                "      # 只开一个端口，下次重建后那个端口就是别人了。",
                "      # ⚠️ 这些 API **没有自己的鉴权**（runtime_token 为空），只适合家和内网，",
                "      # 不要把这一段映射到公网；不需要时删掉这几行并重建即可。",
                "      - \"8787-8799:8787-8799\"",
            ]
            if publish_people
            else ["      # per-person ports stay in the network (see --publish-people)"]
        ),
        "    # The image's own healthcheck probes 127.0.0.1:8787, which in this container",
        "    # is just whichever person happens to hold the first port. Nothing listens",
        "    # there when the roster does not include one (or when 8787 was retired), so",
        "    # the container reported unhealthy while it was serving perfectly -- a false",
        "    # alarm that would hide a real one. This container's job is the supervisor,",
        "    # so the control surface is what it is asked to answer for; per-person health",
        "    # is on /fleet/status and the dashboard.",
        "    healthcheck:",
        "      test:",
        "        - CMD",
        "        - python",
        "        - -c",
        "        - \"import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/fleet/status', timeout=4).status == 200 else 1)\"",
        "      interval: 30s",
        "      timeout: 5s",
        "      start_period: 30s",
        "      retries: 3",
        "    networks: [test_net]",
        "",
        "volumes:",
        "  runtime-fleet-data:",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--people", required=True, help="ids, e.g. 20001-20010")
    parser.add_argument("--compose", default="/home/bomomo/astrbot_test/astrbot.yml")
    parser.add_argument("--fleet", default="/home/bomomo/astrbot_test/fleet.yml")
    parser.add_argument("--data", default="/home/bomomo/astrbot_test/fleet-data")
    parser.add_argument("--checkout", default="/home/bomomo/astrbot_test/src/xiaojiujiu")
    parser.add_argument("--project", default="astrbot_test")
    parser.add_argument(
        "--publish-people",
        action="store_true",
        help="publish the per-person port range (8787-8799) to the LAN; unauthenticated APIs",
    )
    parser.add_argument("--no-start", action="store_true")
    args = parser.parse_args()

    compose = Path(args.compose)
    data = Path(args.data)
    image, env = base_environment(compose)
    people = [SESSION_TEMPLATE.format(person=item) for item in expand(args.people)]
    print(f"image={image} env_lines={len(env)} people={len(people)}")

    Path(args.fleet).write_text(
        render(image, env, compose, Path(args.checkout), publish_people=args.publish_people),
        encoding="utf-8",
        newline="\n",
    )
    print("wrote", args.fleet)

    data.mkdir(parents=True, exist_ok=True)
    people_file = data / "people.json"
    existing: list[str] = []
    if people_file.exists():
        try:
            existing = json.loads(people_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    merged = list(dict.fromkeys([*existing, *people]))
    payload = json.dumps(merged, ensure_ascii=False, indent=2)
    # The container runs as uid 10001 and owns this file, so the host user cannot
    # rewrite it: write through a root helper and hand ownership back afterwards.
    subprocess.run(
        ["docker", "run", "--rm", "-i", "-v", f"{data}:/d", "alpine", "sh", "-c",
         "cat > /d/people.json && chown -R 10001:10001 /d"],
        input=payload,
        check=True,
        capture_output=True,
        text=True,
    )
    print(f"wrote {people_file} ({len(merged)} people)")

    if args.no_start:
        return 0
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "-f", args.fleet, "-p", args.project,
         "up", "-d", "runtime-fleet"],
        capture_output=True,
        text=True,
    )
    print((result.stdout or "")[-300:] or (result.stderr or "")[-300:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
