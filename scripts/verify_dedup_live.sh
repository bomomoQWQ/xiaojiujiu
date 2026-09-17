#!/usr/bin/env bash
# 去重修复在生产里成立吗：有没有哪个来源事件又攒出多条在用条目。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import collections, glob, json, os, sqlite3

LIVE = ("open", "waiting", "due", "muted")

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = con.execute(
        "select unfinished_id, status, source_event_ids, created_at, title"
        " from unfinished_matters order by created_at"
    ).fetchall()
    live = [r for r in rows if r[1] in LIVE]
    by_event: dict[str, list] = collections.defaultdict(list)
    for r in live:
        try:
            ids = json.loads(r[2] or "[]")
        except Exception:
            ids = []
        key = ",".join(sorted(str(x) for x in ids)) or "__none__"
        by_event[key].append(r)
    dupes = {k: v for k, v in by_event.items() if len(v) > 1}

    print(f"  {person}: 总 {len(rows)} 条，在用 {len(live)} 条，"
          f"占用的事件组 {len(by_event)} 个，重复组 {len(dupes)} 个")
    if dupes:
        for key, items in dupes.items():
            print(f"    !! 事件 {key} 有 {len(items)} 条:")
            for r in items:
                print(f"       {r[0]} | {r[3][:19]} | {str(r[4])[:46]}")
    else:
        for r in live:
            print(f"     {r[0]} | {r[3][:19]} | {str(r[4])[:52]}")
PY
