"""用户模型的搬运：`user_model_params`（权重+精度+计数）与 `interaction_observations`（证据）。

为什么要**两张表一起搬**：参数里的精度（`precision`）与 `effective_count` 是从观测累积出来的
（`user_model._update`：`precision += w·x²`、`effective_count += w`）。只搬参数、不搬证据，
下一轮 `observe` 会从"有一堆把握、却没有任何证据"的状态继续 ✗；只搬证据、不搬参数，
那些把握得重新学一遍 ✗。所以两张表要么都搬，要么都不搬 ✓。

用户口径（2026-09-22）："用户模型也复用吧" ✓ —— 一个月建模实验正好需要这个起点
（停机前实测 `effective_count` 最高 0.855，9 人合计 44 条观测 ✓）。

用法（在舰队容器里跑，**只读**挂载数据卷）::

    docker exec xxj-runtime-fleet python3 /tmp/carryover_user_model.py export /tmp/out
    # 清库之后（舰队跑着新空库时）：
    docker exec xxj-runtime-fleet python3 /tmp/carryover_user_model.py import /tmp/out
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys

TABLES = ("user_model_params", "interaction_observations")


def rows_as_dicts(con: sqlite3.Connection, table: str) -> list[dict]:
    """Read one whole table as a list of mappings."""
    cols = [r[1] for r in con.execute("pragma table_info(%s)" % table)]
    return [dict(zip(cols, row)) for row in con.execute("select * from %s" % table)]


def dump(con: sqlite3.Connection, table: str, payload: list[dict]) -> None:
    """Insert rows back into one table, replacing whatever is there."""
    if not payload:
        return
    con.execute("delete from %s" % table)
    cols = [r[1] for r in con.execute("pragma table_info(%s)" % table)]
    keep = [c for c in cols if c in payload[0]]
    placeholders = ",".join("?" for _ in keep)
    con.executemany(
        "insert into %s (%s) values (%s)" % (table, ",".join(keep), placeholders),
        [[row.get(c) for c in keep] for row in payload],
    )


def export(out: str) -> int:
    """Write both tables of every instance to one JSON file per person."""
    os.makedirs(out, exist_ok=True)
    total_obs = 0
    for path in sorted(glob.glob("/data/*/companion.sqlite3")):
        tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            payload = {table: rows_as_dicts(con, table) for table in TABLES}
        finally:
            con.close()
        params = payload["user_model_params"][0] if payload["user_model_params"] else {}
        effective = 0.0
        for key in ("payload_json", "params_json", "value_json"):
            if params.get(key):
                try:
                    effective = float(json.loads(params[key]).get("effective_count") or 0.0)
                except Exception:
                    pass
        effective = params.get("effective_count") or effective
        observations = len(payload["interaction_observations"])
        total_obs += observations
        target = os.path.join(out, "%s-user-model.json" % tag)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print("  %-14s 观测 %-3d ｜ effective_count %-8s -> %s" % (
            tag, observations, round(float(effective), 3), os.path.basename(target)))
    print()
    print("共 %d 条观测；文件在 %s" % (total_obs, out))
    return 0


def install(source: str) -> int:
    """Install every exported table into the matching instance of a fresh fleet."""
    for name in sorted(os.listdir(source)):
        if not name.endswith("-user-model.json"):
            continue
        tag = name[: -len("-user-model.json")]
        path = "/data/default-friendmessage-%s/companion.sqlite3" % tag
        if not os.path.exists(path):
            print("  %-14s 没有库，跳过" % tag)
            continue
        payload = json.load(open(os.path.join(source, name), encoding="utf-8"))
        con = sqlite3.connect(path)
        try:
            with con:
                for table in TABLES:
                    dump(con, table, payload.get(table) or [])
        finally:
            con.close()
        print("  %-14s 装回 观测 %d ／ 参数 %d 行" % (
            tag, len(payload.get("interaction_observations") or []),
            len(payload.get("user_model_params") or [])))
    return 0


def main() -> int:
    """Dispatch export/install."""
    if len(sys.argv) < 3 or sys.argv[1] not in ("export", "import"):
        print(__doc__)
        return 2
    if sys.argv[1] == "export":
        return export(sys.argv[2])
    return install(sys.argv[2])


if __name__ == "__main__":
    raise SystemExit(main())
