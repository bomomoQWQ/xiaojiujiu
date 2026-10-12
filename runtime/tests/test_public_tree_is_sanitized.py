"""The public tree must not regress to carrying the private beta deployment diary.

This deliberately scans *tracked* text only: local backups live under ignored
``.private-history/`` and are not a publication risk. The markers below are deployment/user
linkage, not generic secret regexes; a key scanner cannot catch a nickname joined to a health
history or a private LAN host joined to a real roster.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]

# Split the canaries so this test does not fail by containing the literal it bans.
FORBIDDEN_MARKERS = (
    "/home/" + "bom" + "omo",
    "/mnt/xz/" + "xiaojiujiu-beta",
    "192.168." + "1.15",
    "星河" + "Sarit",
    "硬邦" + "面包",
    "李司" + "淮",
    "博馍" + "馍",
    "qq" + "01",
    "qq" + "02",
    "qq" + "03",
    "qq" + "04",
    "qq" + "05",
    "qq" + "06",
    "qq" + "07",
    "qq" + "08",
    "qq" + "09",
    "qq" + "10",
)


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [ROOT / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]


def test_private_beta_diary_is_not_tracked() -> None:
    assert not (ROOT / "HANDOFF.md").is_file()


def test_tracked_text_has_no_private_deployment_or_user_markers() -> None:
    offenders: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in FORBIDDEN_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)}: {marker}")
    assert offenders == [], "private deployment/user data returned to the public tree:\n" + "\n".join(offenders)
