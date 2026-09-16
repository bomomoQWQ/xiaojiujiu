#!/usr/bin/env bash
# aiocqhttp 的 ApiNotAvailable 是什么语义（瞬时 vs 永久），以及账本修好没有。
set -u
echo "=== aiocqhttp.exceptions ==="
docker exec astrbot-test sh -c 'sed -n "1,50p" /usr/local/lib/python3.12/site-packages/aiocqhttp/exceptions.py'
echo
echo "=== 抛出点 ==="
docker exec astrbot-test sh -c 'grep -n "ApiNotAvailable" -B 6 -A 3 /usr/local/lib/python3.12/site-packages/aiocqhttp/api_impl.py | head -40'
echo
echo "=== 账本：degraded 现在怎么记的 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

path = "/data/default-friendmessage-1670681411/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print("  ran_at | trigger | ran | reason | ops | settled | degraded")
for row in con.execute(
    "select ran_at, trigger, ran, reason, operations, settled_events, degraded from refresh_runs"
    " order by ran_at"
):
    print("  ", str(row[0])[:19], "|", row[1], "|", row[2], "|", row[3], "|", row[4], "|", row[5], "|", row[6])
PY
