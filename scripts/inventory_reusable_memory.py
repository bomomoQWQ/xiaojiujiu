#!/usr/bin/env python3
"""盘点快照里"可以复用到上线后"的东西：
  1) boundaries —— 用户声明的硬边界（这是指令，不是聊天记忆，理应复用）
  2) user_model_params —— 从行为里学到的量化用户模型
  3) 非 episodic 的记忆（durable：偏好/身份/关系）
  4) unfinished_matters —— 还没了结的事（连续性）
"""
import glob
import json
import os
import shutil
import sqlite3

SNAP = "/snap"
WORK = "/tmp/inv"
os.makedirs(WORK, exist_ok=True)


def copy(path):
    target = os.path.join(WORK, os.path.basename(path))
    if not os.path.exists(target):
        shutil.copy2(path, target)
    return target


def rows(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        return [("ERROR", str(exc))]


for path in sorted(glob.glob(os.path.join(SNAP, "people", "*.sqlite3"))):
    tag = os.path.basename(path).replace(".sqlite3", "").replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % copy(path), uri=True)
    print("=" * 76)
    print("用户 %s" % tag)
    print("=" * 76)

    print("  [边界]（指令性质，理应复用）")
    got = rows(con, "select * from boundaries")
    if not got or got[0][0] == "ERROR":
        print("    无")
    else:
        cols = [r[1] for r in con.execute("pragma table_info(boundaries)")]
        for row in got:
            data = dict(zip(cols, row))
            print("    %s kind=%s scope=%s topic=%s" % (
                data.get("boundary_id"), data.get("kind"), data.get("scope"), data.get("topic")))

    print("  [用户量化模型]（从行为学到的，非聊天内容）")
    cols = [r[1] for r in con.execute("pragma table_info(user_model_params)")]
    got = rows(con, "select * from user_model_params")
    if not got:
        print("    无")
    else:
        for row in got:
            data = dict(zip(cols, row))
            keep = {k: v for k, v in data.items()
                    if k not in ("updated_at",) and not k.endswith("_json")}
            print("    %s" % json.dumps(keep, ensure_ascii=False)[:150])

    print("  [durable 记忆]（偏好 / 身份 / 关系；episodic 不算）")
    got = rows(con, "select kind, round(importance,2), substr(summary,1,60) from memories"
                    " where kind != 'episodic' order by kind, importance desc")
    if not got:
        print("    无")
    else:
        for kind, importance, summary in got:
            print("    %-16s imp=%.2f  %s" % (kind, importance, summary))

    print("  [未完之事]（连续性）")
    got = rows(con, "select round(priority,2), status, substr(title,1,50) from unfinished_matters"
                    " where status not in ('resolved','cancelled','expired','invalidated')"
                    " order by priority desc limit 6")
    if not got:
        print("    无未了结的")
    else:
        for priority, status, title in got:
            print("    p=%.2f %-10s %s" % (priority, status, title))

    con.close()
    print()
