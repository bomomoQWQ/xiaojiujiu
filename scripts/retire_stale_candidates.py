"""退休那些"依据的未完之事已被作废"的候选。

为什么需要手动做：plan_operations 只产出 ADD/UPDATE，**从不退休**（candidate.py:1188-1248），
所以候选只在两种情况下离开池子 —— TTL 到期，或用户发消息触发 _invalidate_candidates
（runtime.py:1326 是它唯一的调用点）。1670681411 那批的 TTL 到明天，等不起。

判定很精确，不需要人工清单：候选的 sources_json 里带 "unfinished:<matter_id>"，
只要它指向的 matter 现在是 invalidated，这条候选的依据就已经没了。
"""
import json
import sqlite3
import sys

sys.path.insert(0, "/app/runtime/src")
from companion_runtime.db import Database  # noqa: E402
from companion_runtime.projections import CandidateProjection  # noqa: E402

TARGETS = ("1670681411", "994959351")
NOTE = "依据的未完之事已作废（重复件合并）：%s"

for person in TARGETS:
    path = "/data/default-friendmessage-%s/companion.sqlite3" % person
    db = Database(path)
    projection = CandidateProjection(db)
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT candidate_id, status, sources_json, intent FROM candidate_intents"
            " WHERE status IN ('new','active')"
        ).fetchall()
        dead = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT unfinished_id, title FROM unfinished_matters WHERE status='invalidated'"
            )
        }
        before = len(rows)
        retired = 0
        for candidate_id, status, sources_json, intent in rows:
            try:
                sources = json.loads(sources_json or "[]")
            except ValueError:
                sources = []
            cited = [
                source.split(":", 1)[1]
                for source in sources
                if isinstance(source, str) and source.startswith("unfinished:")
            ]
            gone = [cid for cid in cited if cid in dead]
            if not gone:
                continue
            reason = NOTE % "；".join(dead[cid][:22] for cid in gone)
            projection.set_status(conn, candidate_id, "invalidated", reason=reason)
            retired += 1
            print("  %s 退休 %s" % (person, candidate_id))
            print("      %s" % intent[:60])
            print("      依据: %s" % "；".join(dead[cid][:34] for cid in gone))
        after = conn.execute(
            "SELECT count(*) FROM candidate_intents WHERE status IN ('new','active')"
        ).fetchone()[0]
        print("=== %s  活跃候选 %d -> %d（退休 %d）\n" % (person, before, after, retired))

print("=== 复核 ===")
import glob  # noqa: E402
import os  # noqa: E402
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    active = con.execute(
        "SELECT count(*) FROM candidate_intents WHERE status IN ('new','active')").fetchone()[0]
    live_matters = con.execute(
        "SELECT count(*) FROM unfinished_matters WHERE status IN ('open','waiting','due','muted')"
    ).fetchone()[0]
    con.close()
    print("  %-12s 活跃候选 %-3d  活跃未完之事 %-3d" % (tag, active, live_matters))
