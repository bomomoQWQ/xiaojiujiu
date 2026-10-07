"""The tools a deployment actually runs must not point at the author's machine.

Every one of these scripts started life on one laptop, with that laptop's paths baked in:
``/srv/companion/astrbot_test`` for the stack, ``F:\\...`` for the checkout, ``E:\\`` for the
backup drive. A second deployment that copies such a script does not fail loudly -- it either
backs nothing up, or writes its compose file into a directory nothing will ever read, and both
look like success from the outside.

So the rule is checked rather than remembered. The list below is the "current tools" table in
``scripts/README.md``; everything else under ``scripts/`` is the historical record of how one
incident was diagnosed on one machine, documented as such, and deliberately left alone.

The Windows pattern is deliberately narrow. A first attempt matched any drive letter
(``[A-Za-z]:\\\\``) and immediately failed on its own material, because inside a Python string
literal the text ``would:\\n`` contains ``d:\\`` and a regex like ``\\d\\d:\\d\\d`` contains
``d:\\`` too. Only a real-looking drive path is a finding.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS = Path(__file__).parents[2] / "scripts"

#: The shipped tooling, as listed in scripts/README.md.
CURRENT_TOOLS = (
    "fleet_admin.py",
    "fleet_roster.py",
    "fleet_naming.py",
    "dispatch_authority.py",
    "rolling_restart.py",
    "runtime_fleet.py",
    "build_runtime_fleet.py",
    "check_versions.py",
    "relaunch.sh",
    "fleet_runtime_usage.sh",
    "backup.ps1",
)

#: Paths that can only mean "this ran on the author's machine".
AUTHOR_PATTERNS = (
    re.compile(r"/srv/companion\b"),
    re.compile(r"\b[CDEFG]:\\{1,2}[A-Za-z0-9_.]"),
    re.compile(r"/Users/[a-z]"),
)


def test_the_listed_tools_all_exist() -> None:
    """Guard the guard: a stale list would make the check below pass by doing nothing."""
    missing = [name for name in CURRENT_TOOLS if not (SCRIPTS / name).is_file()]
    assert missing == [], f"scripts/README.md lists tools that are not there: {missing}"


def test_no_current_tool_hard_codes_an_author_machine_path() -> None:
    """None of the shipped tooling may carry the author's own paths."""
    offenders: list[str] = []
    for name in CURRENT_TOOLS:
        text = (SCRIPTS / name).read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern in AUTHOR_PATTERNS:
                if pattern.search(line):
                    offenders.append(f"scripts/{name}:{number}: {line.strip()}")
    assert offenders == [], (
        "these tools point at the author's machine; make the value a parameter, an "
        "environment override, or derive it from the script's own location:\n"
        + "\n".join(offenders)
    )


def test_compose_generator_defaults_are_relative() -> None:
    """The generator is the tool a new deployment runs first, so its defaults matter.

    They used to be absolute paths into the author's stack, which meant a new deployment
    either failed obscurely or wrote its compose somewhere it would never look.
    """
    source = (SCRIPTS / "build_runtime_fleet.py").read_text(encoding="utf-8")
    assert 'default="astrbot.yml"' in source
    assert 'default="fleet-v2.yml"' in source
    # Guessing a compose project name would attach the new stack to somebody else's.
    assert 'parser.add_argument("--project", required=True' in source
