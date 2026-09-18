#!/usr/bin/env bash
# 按用户要求：停 NapCat（切断 QQ 通道）。顺便查清那个陌生会话 3309892640 是谁。
set -u

echo "=== 1) 停 napcat-test ==="
docker stop xxj-napcat-test >/dev/null && echo "  已停"

echo
echo "=== 2) 全部相关容器状态 ==="
docker ps -a --format '{{.Names}}\t{{.Status}}' \
  | grep -Ei "astrbot-test|xxj-runtime|xxj-napcat-test|xxj-onebot|napcat|astrbot$" || true

echo
echo "=== 3) 还有谁能发消息吗 ==="
for probe in "astrbot-test 6186" "xxj-napcat-test 6098"; do
  set -- $probe
  printf "  %-18s " "$1:$2"
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 4 "http://127.0.0.1:$2/" || echo "无响应"
done

echo
echo "=== 4) 陌生会话 3309892640 的来头（读 AstrBot 库，只读）==="
python3 - <<'PY'
import sqlite3

DB = "/home/bomomo/astrbot_test/data/data_v4.db"
con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
for table in tables:
    cols = [r[1] for r in con.execute("pragma table_info(%s)" % table)]
    text_cols = [c for c in cols if c in
                 ("unified_msg_origin", "umo", "session_id", "conversation_id", "user_id",
                  "sender_id", "content", "message", "text", "created_at", "timestamp")]
    if not text_cols:
        continue
    for col in ("unified_msg_origin", "umo", "session_id", "conversation_id", "sender_id"):
        if col not in cols:
            continue
        try:
            hits = con.execute(
                "select count(*) from %s where cast(%s as text) like '%%3309892640%%'"
                % (table, col)).fetchone()[0]
        except sqlite3.Error:
            continue
        if hits:
            print("  %s.%s 命中 %d 行" % (table, col, hits))
            order = "created_at" if "created_at" in cols else cols[0]
            for row in con.execute(
                    "select %s from %s where cast(%s as text) like '%%3309892640%%'"
                    " order by %s desc limit 5" % (",".join(text_cols), table, col, order)):
                print("     ", [str(v)[:60] for v in row])
con.close()
PY
