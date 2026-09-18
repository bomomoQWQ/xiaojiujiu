#!/usr/bin/env bash
# 情绪层的持久证据：settled 事件的来源/强度带、各实例的情绪事件与解释缓存、以及强度带的数值映射。
set -u

echo "=== 1) 强度带 -> 数值 的映射（决定能不能过 min_event_impact=0.06）==="
docker exec xxj-runtime-fleet python3 -c "
import sys; sys.path.insert(0, '/app/runtime/src')
from companion_runtime.semantic import band_to_intensity
for band in ('none', 'low', 'medium', 'high', 'unknown', ''):
    try:
        print('  band=%-8r -> %s' % (band, band_to_intensity(band)))
    except Exception as exc:
        print('  band=%-8r -> %s' % (band, exc))
"

echo
echo "=== 2) 每个实例：settled 事件的来源与强度带分布 + 情绪事件/解释缓存 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import os
import sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    print("  --- %s ---" % tag)
    if "event_semantics" in tables:
        rows = con.execute(
            "select settlement_source, intensity_band, count(*) from event_semantics"
            " group by settlement_source, intensity_band order by 3 desc").fetchall()
        if rows:
            for source, band, count in rows:
                print("      settled: source=%-24s band=%-8s x%d" % (source, band, count))
        else:
            print("      event_semantics 空")
        unresolved = con.execute(
            "select count(*) from event_semantics where semantic_status != 'resolved'").fetchone()[0]
        print("      未 settled: %d" % unresolved)
    for name, label in (("active_emotion_events", "情绪事件"), ("emotion_explanations", "解释缓存")):
        if name in tables:
            print("      %s: %d 条" % (label, con.execute("select count(*) from %s" % name).fetchone()[0]))
    if "runtime_state" in tables:
        cols = [row[1] for row in con.execute("pragma table_info(runtime_state)")]
        wanted = [c for c in ("mood_valence", "mood_arousal", "restraint", "approach_impulse", "pressure") if c in cols]
        if wanted:
            row = con.execute("select %s from runtime_state" % ", ".join(wanted)).fetchone()
            print("      状态: %s" % ", ".join("%s=%.4f" % (c, v or 0.0) for c, v in zip(wanted, row)))
    con.close()
PY

echo
echo "=== 3) 注入块里的情绪相关段落（看她的"感受"从哪来）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:8787/context/render-block', data=b'{}',
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=60) as r:
    block = (json.loads(r.read().decode()).get('block') or '')
for line in block.splitlines():
    if any(token in line for token in ('长期感受', '在意', '拉扯', '倾向', '克制', '表达底色', '情绪', '记忆')):
        print('  ' + line)
"
