#!/usr/bin/env bash
# 10 条未尽之事是正常的还是只进不出。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, sqlite3

path = glob.glob("/data/default-friendmessage-qq01/companion.sqlite3")[0]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
cols = [r[1] for r in con.execute("pragma table_info(unfinished_matters)")]
print("  columns:", ", ".join(cols))
print()
print("  == 状态分布 ==")
for row in con.execute("select status, count(*) from unfinished_matters group by 1"):
    print("   ", row)
print()
print("  == 全部条目 ==")
key = "title" if "title" in cols else cols[1]
extra = [c for c in ("priority", "due_at", "expires_at", "updated_at", "created_at") if c in cols]
select = ", ".join([key, "status"] + extra)
for row in con.execute(f"select {select} from unfinished_matters order by created_at desc limit 15"):
    print("   ", " | ".join(str(x)[:46] for x in row))
print()
print("  == 有没有被解决过的痕迹（resolved/closed 等）==")
for row in con.execute("select count(*) from unfinished_matters where status != 'open'"):
    print("   非 open 条数:", row[0])
PY
