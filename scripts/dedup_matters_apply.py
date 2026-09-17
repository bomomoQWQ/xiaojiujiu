"""手动去重：把确认重复的未完之事标成 invalidated，并写明它并入了哪一件。

人工判定（逐条看过，见 HANDOFF 那一节），保留规则：同一话题里留"措辞最全"或"最早"的一件。
作废件写 resolution_note，把用户提过几次的信号留在记录里，不随合并丢失。
预期：1670681411 活跃 11 -> 7；994959351 活跃 7 -> 3；其余 5 个实例不动。

用投影自己的 set_status（同一套 SQL），在每实例一个事务里完成；不构造 Runtime，
避免 load_config() 顺手生成杂散文件（这个坑踩过）。
"""
import sqlite3
import sys

sys.path.insert(0, "/app/runtime/src")
from companion_runtime.db import Database  # noqa: E402
from companion_runtime.projections import UnfinishedProjection  # noqa: E402

LIVE = ("open", "waiting", "due", "muted")

#: (实例后缀, 要作废的 id 后缀, 保留的 id 后缀, 原因)
PLAN = [
    ("1670681411", "127bca", "6d0a6f", "奶茶邀约同一件事的最早版本（3 次提及）"),
    ("1670681411", "1113e0", "6d0a6f", "同上：无糖奶茶是同一邀约的另一种说法"),
    ("1670681411", "627e5b", "5a93ed", "「我试试什么（）」同一问题的重复建档"),
    ("1670681411", "03e558", "4d5e74", "夜羊社作品同一问题（哪部/为何喜欢）"),
    ("994959351", "d2f2ac", "9cb17d", "句号偏好同一问题的最早版本（3 次提及）"),
    ("994959351", "ee4a2f", "9cb17d", "同上：回应形式（换行/句号）偏好"),
    ("994959351", "42ceb9", "46d686", "「只收到自动回复」同一诉求的第三次提及"),
    ("994959351", "3e55d2", "46d686", "同上：第一次提及"),
]


def resolve_one(con: sqlite3.Connection, suffix: str, statuses: tuple[str, ...]) -> str:
    """把 id 后缀解析成完整 id；必须唯一命中，否则报错。"""
    rows = con.execute(
        "SELECT unfinished_id, title, status FROM unfinished_matters"
        " WHERE unfinished_id LIKE ? AND status IN (%s)" % ",".join("?" * len(statuses)),
        ("%" + suffix, *statuses),
    ).fetchall()
    if len(rows) != 1:
        raise SystemExit("后缀 %s 命中 %d 条（应为 1），中止" % (suffix, len(rows)))
    return rows[0][0], rows[0][1]


by_person: dict[str, list] = {}
for person, drop, keep, why in PLAN:
    by_person.setdefault(person, []).append((drop, keep, why))

for person, items in by_person.items():
    path = "/data/default-friendmessage-%s/companion.sqlite3" % person
    db = Database(path)
    projection = UnfinishedProjection(db)
    with db.transaction() as conn:
        before = conn.execute(
            "SELECT count(*) FROM unfinished_matters WHERE status IN ('open','waiting','due','muted')"
        ).fetchone()[0]
        print("=== %s  活跃 %d 件 ===" % (person, before))
        for drop, keep, why in items:
            drop_id, drop_title = resolve_one(conn, drop, LIVE)
            keep_id, keep_title = resolve_one(conn, keep, LIVE)
            note = "重复件：并入 %s（%s）；原因：%s" % (keep_id, keep_title[:26], why)
            projection.set_status(conn, drop_id, "invalidated", note=note)
            print("  invalidated %s" % drop_id)
            print("      %s" % drop_title[:70])
            print("      -> 并入 %s  %s" % (keep_id, keep_title[:40]))
        after = conn.execute(
            "SELECT count(*) FROM unfinished_matters WHERE status IN ('open','waiting','due','muted')"
        ).fetchone()[0]
        print("  活跃 %d -> %d" % (before, after))
        print()

print("=== 复核：所有实例的活跃数与状态分布 ===")
import glob  # noqa: E402
import os  # noqa: E402
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    rows = con.execute(
        "SELECT status, count(*) FROM unfinished_matters GROUP BY status ORDER BY status"
    ).fetchall()
    live = sum(n for s, n in rows if s in LIVE)
    con.close()
    print("  %-12s 活跃 %-3d  %s" % (tag, live, dict(rows)))
