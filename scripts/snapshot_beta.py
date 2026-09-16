"""Freeze the whole fleet before you start changing it.

The week's data is the only baseline there is, so taking it before an optimization
is not optional: a changed parameter with no "before" to compare against produces a
feeling, not a result. Each person's database and log are copied out (SQLite's own
backup, not a file copy, so a running writer cannot hand over a half-written page),
plus the fleet's config and routes.

Usage (from the host)::

    python3 scripts/snapshot_beta.py --note "before tuning hazard" \\
        --root /mnt/xz/xiaojiujiu-beta/snapshots

Runs inside a container with the fleet volume mounted read-write and the export
disk mounted, exactly like ``export_beta_data.py``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def snapshot_one(db_path: Path, target: Path) -> str:
    """Copy one database with SQLite's backup API; returns its size."""
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()
    return f"{target.stat().st_size} bytes"


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", default="/data")
    parser.add_argument("--root", required=True, help="snapshot directory")
    parser.add_argument("--fleet", default="http://runtime-fleet:8800")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    data_root = Path(args.data_root)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    target_root = Path(args.root) / stamp
    (target_root / "people").mkdir(parents=True, exist_ok=True)

    manifest = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "note": args.note,
        "people": {},
    }
    for db_path in sorted(data_root.glob("*/companion.sqlite3")):
        person = db_path.parent.name
        try:
            size = snapshot_one(db_path, target_root / "people" / f"{person}.sqlite3")
        except sqlite3.Error as error:
            print(f"  {person}: FAILED {error}")
            continue
        log_path = data_root / "logs" / f"{person}.log"
        if log_path.exists():
            shutil.copyfile(log_path, target_root / "people" / f"{person}.log")
        manifest["people"][person] = {"database": size}
        print(f"  {person}: {size}")

    for name, url in (("fleet_status", "/fleet/status"), ("fleet_routes", "/fleet/routes")):
        try:
            with urllib.request.urlopen(f"{args.fleet.rstrip('/')}{url}", timeout=15) as response:
                payload = response.read().decode("utf-8")
            (target_root / f"{name}.json").write_text(payload, encoding="utf-8")
        except (urllib.error.URLError, OSError):
            pass

    config_path = data_root.parent / "astrbot.yml"
    if config_path.exists():
        shutil.copyfile(config_path, target_root / "compose.yml")

    (target_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"snapshot: {target_root} ({len(manifest['people'])} people)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
