"""核实三件事：
1) 测试栈配了哪些平台（aiocqhttp / webchat），platform id 是什么；
2) 测试者到底从哪个平台发消息（Runtime 的 raw_events 里有插件上报的平台名）；
3) astrbot-test 能不能上外网。
"""
import json
import sqlite3

print("=== 1) astrbot-test 配了哪些平台 ===")
with open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
platforms = cfg.get("platform") or []
for item in platforms:
    if isinstance(item, dict):
        print("  id=%-14s type=%-18s enable=%s" % (
            item.get("id"), item.get("type"), item.get("enable")))
    else:
        print("  %r" % (item,))

print()
print("=== 2) Runtime 侧：最近 25 条用户消息来自哪个平台 ===")
con = sqlite3.connect(
    "file:/data/default-friendmessage-1670681411/companion.sqlite3?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(
    "select timestamp, actor, content, metadata_json, conversation_id from raw_events"
    " where event_type='user_message' order by timestamp desc limit 25").fetchall()
platforms_seen = {}
for r in rows:
    meta = {}
    try:
        meta = json.loads(r["metadata_json"] or "{}")
    except ValueError:
        pass
    plat = meta.get("platform") or meta.get("platform_name") or "?"
    platforms_seen[plat] = platforms_seen.get(plat, 0) + 1
print("  平台分布（近 25 条）:", platforms_seen)
print("  conversation_id 样例:", sorted({r["conversation_id"] for r in rows})[:5])
print()
print("  最近 10 条：")
for r in rows[:10]:
    meta = {}
    try:
        meta = json.loads(r["metadata_json"] or "{}")
    except ValueError:
        pass
    text = " ".join((r["content"] or "").split())[:42]
    print("    %s plat=%-12s type=%-14s sender=%-12s %s" % (
        str(r["timestamp"])[:19], meta.get("platform"),
        meta.get("message_type") or meta.get("chat_type"),
        meta.get("sender_id") or meta.get("sender_name"), text))

print()
print("=== 全部 raw_events 的平台分布（按 event_type）===")
for r in con.execute(
        "select event_type, count(*) n from raw_events group by event_type order by n desc"):
    print("  %-22s %d" % (r["event_type"], r["n"]))
con.close()
