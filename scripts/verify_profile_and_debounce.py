"""1) qq05 补档案后的变化（restraint 应朝新目标衰减，沉默效用应跌破候选）
   2) 防抖的确定性检验：连发的两条用户消息，是否只产生了一轮 assistant 回复
      （插件在 on_llm_response 上报，所以 1 条 assistant_message = 1 次 LLM 轮次）
"""
import datetime as dt
import sqlite3

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
CST = dt.timedelta(hours=8)
print("now = %s CST\n" % (NOW + CST).strftime("%m-%d %H:%M"))

print("=== 1) qq05 ===")
con = sqlite3.connect("file:/data/default-friendmessage-qq05/companion.sqlite3?mode=ro",
                      uri=True)
con.row_factory = sqlite3.Row
st = con.execute("select approach_impulse, restraint, pressure, updated_at from runtime_state").fetchone()
print("  I=%.4f R=%.4f P=%.4f  (state updated %s)" % (
    st["approach_impulse"], st["restraint"], st["pressure"], str(st["updated_at"])[11:19]))
print("  最近判决:")
for r in con.execute("select decided_at, reason, advantage, silence_utility from decisions"
                     " where delta_t < 2000 order by decided_at desc limit 3"):
    print("    %s %-26s adv=%+.5f sil=%.4f" % (
        r["decided_at"][11:19], r["reason"], r["advantage"], r["silence_utility"]))
print("  新的沉默效用目标（R 衰减到 ~0.53 时）: 0.28+0.45*0.53+0.42*I-1.2*P^2+0.125 ≈ %.3f" % (
    0.28 + 0.45 * 0.53 + 0.42 * st["approach_impulse"] - 1.2 * st["pressure"] ** 2 + 0.125))
con.close()

print()
print("=== 2) 防抖：连发是否只触发一轮 LLM ===")
for tag, lo, hi, label in (
    ("qq04", "2026-09-17T14:46:38", "2026-09-17T14:47:30", "两条间隔 2s（窗口内，应合并）"),
    ("qq03", "2026-09-17T14:45:40", "2026-09-17T14:46:30", "两条间隔 20s（窗口外，应各自回）"),
):
    con = sqlite3.connect("file:/data/default-friendmessage-%s/companion.sqlite3?mode=ro" % tag,
                          uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select timestamp, event_type, substr(content,1,30) c from raw_events"
        " where timestamp between ? and ? and event_type in ('user_message','assistant_message')"
        " order by timestamp", (lo, hi)).fetchall()
    users = [r for r in rows if r["event_type"] == "user_message"]
    replies = [r for r in rows if r["event_type"] == "assistant_message"]
    print("  %s  %s" % (tag, label))
    for r in rows:
        print("     %s %-18s %s" % (r["timestamp"][11:19], r["event_type"], r["c"]) )
    print("     -> 用户 %d 条，assistant 轮次 %d 次" % (len(users), len(replies)))
    con.close()
    print()
