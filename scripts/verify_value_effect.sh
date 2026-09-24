#!/usr/bin/env bash
# 价值观改完之后，判决里的沉默效用有没有真的降下来。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, json, os, sqlite3

CUT = "2026-09-17T05:43"  # 改值时间

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = con.execute(
        "select decided_at, acted, reason, hazard, silence_utility, advantage from decisions"
        " order by decided_at desc limit 3"
    ).fetchall()
    print(f"  --- {person} ---")
    for decided_at, acted, reason, hazard, silence, advantage in rows:
        mark = "NEW " if str(decided_at) >= CUT else "old "
        print(f"    {mark}{str(decided_at)[11:19]} acted={acted} {reason:<24} "
              f"hazard={hazard:.4g} silence={silence:.4f} advantage={advantage:+.4f}")
    n_new = con.execute(
        "select count(*) from decisions where decided_at >= ?", (CUT,)
    ).fetchone()[0]
    if n_new:
        avg = con.execute(
            "select avg(silence_utility) from decisions where decided_at >= ?", (CUT,)
        ).fetchone()[0]
        print(f"    改后 {n_new} 条判决，沉默效用均值 {avg:.4f}")
PY
