"""One version number, three files -- and a way to prove they still agree.

`CHANGELOG.md` states the convention: the release version lives in
`runtime/pyproject.toml`, in `runtime/src/companion_runtime/__init__.py` (that is what
`/health` reports as `runtime_version`) and in the plugin repository's `metadata.yaml`.

The convention had quietly stopped being true in **all three places at once** -- the
changelog said 0.4.1, the code said 0.4.0, the plugin said 0.1.0 -- which is what always
happens to a stated-but-unchecked convention. So check it, and run it before tagging:

    python3 scripts/check_versions.py
    python3 scripts/check_versions.py --plugin-repo ../astrbot_plugin_companion_runtime

The plugin is a *separate repository* and may simply not be beside this one; in that case
the check says so and verifies the literals it can see instead of pretending everything
agrees. Exit status is 1 on any mismatch, so this works as a release gate rather than as
a report nobody reads.

Note what this does **not** check: compatibility. That is `RUNTIME_API_VERSION` on the
Runtime side and `PROTOCOL_VERSION` plus `astrbot_version` on the plugin side. Matching
numbers are a labelling convenience, not a contract.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

#: Repository root -- this file lives in ``<root>/scripts/``.
STACK_ROOT = Path(__file__).resolve().parent.parent

#: Directory name of the plugin repository, in every layout we have seen. It is a separate
#: git repository that is sometimes a sibling of the stack and sometimes nested inside the
#: checkout (``src/<repo>/``), so look for the name rather than assuming a depth.
PLUGIN_DIR_NAME = "astrbot_plugin_companion_runtime"

VERSION_PATTERN = re.compile(r"^\s*version\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
DUNDER_VERSION_PATTERN = re.compile(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
CHANGELOG_PATTERN = re.compile(r"^##\s+(\d+\.\d+\.\d+)", re.MULTILINE)
METADATA_VERSION_PATTERN = re.compile(r"^version:\s*(\S+)", re.MULTILINE)


def _first(pattern: re.Pattern[str], path: Path) -> str | None:
    """Return the first capture of ``pattern`` in ``path``, or None if absent.

    Read as ``utf-8-sig``: a byte-order mark would otherwise sit in front of the first line
    and make ``^version`` fail to match, which reports a perfectly good file as missing.
    """
    if not path.is_file():
        return None
    match = pattern.search(path.read_text(encoding="utf-8-sig"))
    return match.group(1) if match else None


def find_plugin_repo(explicit: str | None) -> Path | None:
    """Locate the plugin repository, or return None when it is not on this machine.

    An **explicit** path (``--plugin-repo`` or ``$PLUGIN_REPO``) is honoured strictly: if it
    does not contain a ``metadata.yaml`` this raises rather than quietly falling back to a
    guess. Silently checking a different repository than the one that was asked for is worse
    than not checking at all -- it reports "ok" about something nobody asked about.

    Without an explicit path, try the two layouts we actually have: a sibling of the stack,
    or nested inside the checkout (``src/<repo>/``).
    """
    for raw in (explicit, os.environ.get("PLUGIN_REPO")):
        if raw:
            candidate = Path(raw).expanduser()
            if (candidate / "metadata.yaml").is_file():
                return candidate.resolve()
            raise ValueError(
                f"{raw} is not the plugin repository: no metadata.yaml in it",
            )
    guesses = (
        STACK_ROOT / PLUGIN_DIR_NAME,
        STACK_ROOT / "src" / "xiaojiujiu" / PLUGIN_DIR_NAME,
        STACK_ROOT.parent / PLUGIN_DIR_NAME,
    )
    for candidate in guesses:
        if (candidate / "metadata.yaml").is_file():
            return candidate.resolve()
    return None


def collect(plugin_repo: Path | None) -> list[tuple[str, str | None]]:
    """Return ``(where, version)`` for every place the number is supposed to appear."""
    found: list[tuple[str, str | None]] = [
        ("CHANGELOG.md (top entry)", _first(CHANGELOG_PATTERN, STACK_ROOT / "CHANGELOG.md")),
        ("runtime/pyproject.toml", _first(VERSION_PATTERN, STACK_ROOT / "runtime" / "pyproject.toml")),
        (
            "runtime/src/companion_runtime/__init__.py  (/health -> runtime_version)",
            _first(DUNDER_VERSION_PATTERN, STACK_ROOT / "runtime" / "src" / "companion_runtime" / "__init__.py"),
        ),
    ]
    if plugin_repo is not None:
        found.append(
            (
                f"{PLUGIN_DIR_NAME}/metadata.yaml",
                _first(METADATA_VERSION_PATTERN, plugin_repo / "metadata.yaml"),
            )
        )
    return found


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 when every version agrees, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--plugin-repo",
        default=None,
        help=f"path to the {PLUGIN_DIR_NAME} checkout (default: look beside/near the stack)",
    )
    args = parser.parse_args(argv)

    try:
        plugin_repo = find_plugin_repo(args.plugin_repo)
    except ValueError as error:
        print(f"\n{error}", file=sys.stderr)
        return 1
    entries = collect(plugin_repo)

    width = max(len(where) for where, _ in entries)
    for where, version in entries:
        print(f"{where:<{width}}  {version or '<MISSING>'}")

    if plugin_repo is None:
        print(
            "\nthe plugin repository is not on this machine, so its metadata.yaml was not "
            "checked;\npass --plugin-repo or set PLUGIN_REPO if it lives somewhere else.",
            file=sys.stderr,
        )

    missing = [where for where, version in entries if not version]
    distinct = {version for _, version in entries if version}
    if missing:
        print(f"\nno version found in: {', '.join(missing)}", file=sys.stderr)
        return 1
    if len(distinct) > 1:
        print(
            "\nthese disagree: "
            + ", ".join(f"{where} = {version}" for where, version in entries if version),
            file=sys.stderr,
        )
        return 1

    print(f"\nok: every place says {distinct.pop()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
