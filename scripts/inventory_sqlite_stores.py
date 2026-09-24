#!/usr/bin/env python3
"""盘点两个 SQLite 里都存了什么。

- Runtime 认知库：读快照里的 8 份 companion.sqlite3（清理前的数据），各表行数汇总；
- AstrBot 宿主库：读快照里的 data_v4.db（清理前那份，含 20 个会话）。

注意：快照是只读挂载且库处于 WAL 模式，SQLite 无法在旁边建 -shm 文件，所以先把每份
复制到容器内的可写目录再以只读方式打开。
"""
import glob
import os
import shutil
import sqlite3

SNAP = "/snap"
WORK = "/tmp/inv"
os.makedirs(WORK, exist_ok=True)
PEOPLE = sorted(glob.glob(os.path.join(SNAP, "people", "*.sqlite3")))


def local_copy(path: str) -> str:
    """Return a copy of a snapshot database inside the writable temp dir."""
    target = os.path.join(WORK, os.path.basename(path))
    if not os.path.exists(target):
        shutil.copy2(path, target)
    return target


PURPOSE = {
    "raw_events": "原始事件（用户消息/她的消息/边界/工具结果…），append-only，一切认知的源头",
    "event_semantics": "事件的语义状态：resolved / unresolved + 方向、强度带、来源、理由",
    "runtime_state": "单行状态：心情、impulse、restraint、pressure、价值观、冷却与各类时间戳",
    "state_meta": "状态上的键值杂项（上次深刷新时间等）",
    "memories": "长期记忆（kind / 摘要 / 重要度 / 置信度 / 来源事件）",
    "memory_candidates": "记忆候选（pending → consolidated / merged / rejected）",
    "activated_memories": "工作集：当前被激活的记忆 + 激活度 / 召回次数",
    "unfinished_matters": "未尽之事（要等用户回答才能了结的事）",
    "candidate_intents": "候选意图池（想做的事 + 目的 + 约束 + 来源）",
    "active_emotion_events": "情绪冲击事件（方向 / 强度 / 激活 / 衰减率）",
    "emotion_explanations": "心理解释缓存（六段式 + 缓存键）",
    "decisions": "每一次开口决策的审计（trigger / acted / reason / hazard / advantage / 沉默效用）",
    "tasks": "协议任务快照（深刷新、候选生成、情绪评估…的状态与结果）",
    "proposals": "外部提案（含 rebase / discard 的审计）",
    "attempts": "一次「想说点什么」的尝试（committed → rendering → ready → sent/failed）",
    "attempt_events": "attempt 状态迁移的审计流水",
    "outbox": "待投递队列（render 行动 / send 行动及其载荷）",
    "boundaries": "用户声明的硬边界（授权层用它拦主动开口）",
    "situation_facts": "当前工作局势：事实与推断分开存",
    "topic_index": "话题索引（去重与路由用）",
    "interpretations": "对旧事件的重新解释（版本化，永不覆盖历史）",
    "reappraisals": "重新理解事件的审计事件",
    "observability": "可观测快照（回合 / 触发 / 计数的历史）",
    "user_model": "对用户的量化模型（响应率、忙碌概率…）",
    "user_model_observations": "用户模型的观测样本",
    "refresh_runs": "深层刷新的运行记录",
    "schema_version": "迁移版本",
}

print("=" * 80)
print("① Runtime 认知库（每人一个 companion.sqlite3）")
print("=" * 80)
if not PEOPLE:
    print("  快照里没有找到 companion.sqlite3")
else:
    print("  快照里有 %d 份，每份大小：" % len(PEOPLE))
    for path in PEOPLE:
        print("    %-52s %6.2f MB" % (os.path.basename(path), os.path.getsize(path) / 1e6))

    totals: dict[str, int] = {}
    for path in PEOPLE:
        con = sqlite3.connect("file:%s?mode=ro" % local_copy(path), uri=True)
        for (name,) in con.execute("select name from sqlite_master where type='table'"):
            try:
                count = con.execute("select count(*) from %s" % name).fetchone()[0]
            except sqlite3.Error:
                count = 0
            totals[name] = totals.get(name, 0) + count
        con.close()

    print()
    print("  各表行数（8 份合计，只列非空）：")
    for name, count in sorted(totals.items(), key=lambda kv: -kv[1]):
        if count == 0:
            continue
        print("    %-26s %8d   %s" % (name, count, PURPOSE.get(name, "")))
    empty = sorted(name for name, count in totals.items() if count == 0)
    if empty:
        print()
        print("  空表（结构在、暂时没数据）：%s" % ", ".join(empty))

    con = sqlite3.connect("file:%s?mode=ro" % local_copy(PEOPLE[1]), uri=True)
    print()
    print("  runtime_state 的列（单行状态，取自 %s）：" % os.path.basename(PEOPLE[1]))
    cols = [row[1] for row in con.execute("pragma table_info(runtime_state)")]
    print("    " + ", ".join(cols))
    con.close()

print()
print("=" * 80)
print("② append-only 的 JSONL 镜像（storage.mirror_raw_events=true 时的第二份事件账）")
print("=" * 80)
found = False
for pattern in ("people/*.jsonl", "*.jsonl"):
    for path in sorted(glob.glob(os.path.join(SNAP, pattern))):
        found = True
        print("    %-52s %6.2f MB" % (os.path.basename(path), os.path.getsize(path) / 1e6))
if not found:
    print("    快照里没有 JSONL（快照脚本只收了 .sqlite3 与 .log）")

print()
print("=" * 80)
print("③ AstrBot 宿主库 data_v4.db（人格、聊天记录、平台与插件配置都在这里）")
print("=" * 80)
host = os.path.join(SNAP, "astrbot_data_v4.db")
if not os.path.exists(host):
    print("  快照里没有 data_v4.db")
else:
    print("  大小 %.2f MB" % (os.path.getsize(host) / 1e6))
    con = sqlite3.connect("file:%s?mode=ro" % local_copy(host), uri=True)
    for (name,) in con.execute(
            "select name from sqlite_master where type='table' order by name"):
        count = con.execute("select count(*) from %s" % name).fetchone()[0]
        cols = [row[1] for row in con.execute("pragma table_info(%s)" % name)]
        print("    %-28s %6d 行   列: %s" % (name, count, ", ".join(cols[:8])))
    con.close()
