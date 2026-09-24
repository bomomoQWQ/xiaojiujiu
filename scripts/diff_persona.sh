#!/usr/bin/env bash
# 对比库里的人格与仓库里的 人格设定.md，并把差异打出来（只读）。
set -u

docker cp /home/bomomo/astrbot_test/src/xiaojiujiu/人格设定.md astrbot-test:/tmp/persona_file.md 2>/dev/null
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import difflib
import sqlite3

con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
row = con.execute("select system_prompt, skills, updated_at from personas").fetchone()
con.close()
db_prompt = row[0] or ""
with open("/tmp/persona_file.md", encoding="utf-8") as handle:
    file_prompt = handle.read()

print("  库里: %d 字（skills=%r, updated_at=%s）" % (len(db_prompt), row[1], row[2]))
print("  文件: %d 字" % len(file_prompt))
print("  完全一致: %s" % (db_prompt.strip() == file_prompt.strip()))

if db_prompt.strip() != file_prompt.strip():
    print()
    print("  === 差异（- 库里 / + 文件）===")
    diff = difflib.unified_diff(
        db_prompt.splitlines(), file_prompt.splitlines(),
        fromfile="DB", tofile="file", lineterm="", n=1)
    for line in list(diff)[:80]:
        print("  " + line)
PY
