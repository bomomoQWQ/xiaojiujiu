"""预测"下一次主动发送消息"的时间分布 —— 用**真实决策代码**在库的只读副本上滚时间。

和仓库里其它四个预测脚本的区别：那些把公式抄了一遍（`hazard_base`、utility 权重、
busy 台阶……都得手抄，抄错了没人发现）。这个脚本**不重写任何公式**：它 import 运行期包，
用 `motivation.decide` / `step_drives` / `target_drives` / `hazard_rate`，也就是线上真正跑的
那几段代码，所以"预测"和"实际会怎么判"用的是同一份实现。

做法（全程只读，不碰线上实例）：
1. 把每个人的 `companion.sqlite3` 连同 `-wal`/`-shm` **同名**拷进临时目录
   （改名会丢 WAL —— 我们已经在验证记忆搬运时踩过一次）；
2. 在副本上建一个 `Runtime`，按 `endogenous_round` 的装配取当前 state / 候选 / 边界裁决 /
   情绪 / 预测 / alignment；
3. 每 `STEP` 秒推进一次：先按 `lazy_tick` 的方式演进冲动/节制/压力，再用 `decide` 算这一轮的
   advantage（内部含夜间惩罚、冷却、日上限、硬边界），把 hazard `λ = 3e-5·softplus(4·adv)`
   按轮积成生存曲线；
4. 输出 25% / 中位 / 75% / 90% 分位数 —— 是"到某时刻为止至少开口一次"的概率，
   **不是**一次随机掷骰的结果。

必须在 fleet 容器里跑（需要 `/app/runtime/src` 与 `/data`）::

    docker cp scripts/predict_next_contact.py xxj-runtime-fleet:/tmp/predict.py
    docker exec xxj-runtime-fleet python3 /tmp/predict.py

前提：**没有人再给她们发消息**。任何一条用户消息都会重写前台暂停、冷却与缺席时钟，
预测随即作废。
"""
from __future__ import annotations

import glob
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/runtime/src")

from companion_runtime import boundaries as B          # noqa: E402
from companion_runtime import motivation as M          # noqa: E402
from companion_runtime.config import load_config       # noqa: E402
from companion_runtime.db import Database              # noqa: E402
from companion_runtime.runtime import Runtime          # noqa: E402
from companion_runtime.utility import clamp, utcnow    # noqa: E402

#: 决策轮步长（秒）。线上调度器上限是 `scheduler.max_interval_seconds`（900s），
#: 取更小只会让积分更细，不改变模型。
STEP = 300.0
#: 向前滚多久。
HORIZON_H = 72.0
CST = timedelta(hours=8)


def cst(moment: datetime | None) -> str:
    """Render an instant in the operator's timezone."""
    return (moment + CST).strftime("%m-%d %H:%M") if moment else "-"


def parse(value) -> datetime | None:
    """Parse a stored timestamp into an aware datetime."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def runtime_on_copy(path: str, config) -> Runtime:
    """Open a Runtime on a same-name copy of an instance database."""
    folder = tempfile.mkdtemp(prefix="predict-")
    copy = os.path.join(folder, os.path.basename(path))
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, copy + suffix)
    return Runtime(config, database=Database(copy), created_at=utcnow())


def simulate(runtime: Runtime, config, now0: datetime) -> dict:
    """Roll the real decision code forward and integrate the hazard."""
    state = runtime.state()
    pending = runtime.projections.candidates.list_active(limit=config.candidate.max_active)
    verdict = B.evaluate(
        runtime.projections.boundaries.active(now0), now=now0, state=state, is_proactive=True
    )
    pending, blocked = runtime._partition_by_boundaries(pending, now=now0)
    emotions = runtime.projections.emotion.list_active()
    context = runtime._situation_context(now0)
    predictions = {
        item.candidate_id: runtime.user_model.predict(
            action=runtime._action_spec(item), context=context
        )
        for item in pending
    }
    alignments = {item.candidate_id: runtime._emotion_alignment(item, emotions) for item in pending}

    last_user = parse(state.last_user_message_at)
    reference = parse(state.last_contact_at) or parse(state.epoch_at) or now0
    tolerance = max(1, config.utility.repeat_contact_tolerance)
    recent = runtime._recent_contact_count(now0)
    unfinished = runtime.projections.unfinished.list_open()
    memory_activation = runtime.memory_store.activation_strength()

    marks: dict[str, datetime | None] = {"25%": None, "中位": None, "75%": None, "90%": None}
    survive, first, rows = 1.0, None, []
    moment = now0
    while moment <= now0 + timedelta(hours=HORIZON_H):
        hours_contact = max(0.0, (moment - reference).total_seconds() / 3600.0)
        busy = runtime.user_model.busy_probability(
            hours_since_contact=(moment - last_user).total_seconds() / 3600.0 if last_user else 99.0,
            replied_recently=bool(last_user and (moment - last_user).total_seconds() < 1800.0),
            context={},
        )
        inputs = M.MotivationInputs(
            state=state,
            candidates=pending,
            predictions=predictions,
            boundary_allow_proactive=verdict.allow_proactive,
            boundary_ids=verdict.blocking_ids,
            boundary_risk_baseline=1.0 if verdict.allow_proactive is False else 0.0,
            active_emotions=emotions,
            recent_contacts=recent,
            hours_since_contact=hours_contact,
            cooldown_active=bool(state.cooldown_until and parse(state.cooldown_until) > moment),
            now=moment,
            elapsed_seconds=STEP,
            force_allow=False,
        )
        result = M.decide(
            inputs,
            config=config,
            rng=random.Random(0),
            situation_text=context.get("summary") or "",
            emotion_alignment=alignments,
        )
        advantage, reason = result.outcome.advantage, result.outcome.reason
        if first is None:
            first = (advantage, reason)
        # 只有真正掷骰的轮次才计入生存曲线：冷却中/额度用完/前台暂停都不掷骰。
        if reason not in ("cooldown_active", "daily_contact_budget_exhausted", "foreground_pause"):
            if advantage is not None and advantage > -50:
                hazard = M.hazard_rate(advantage, config=config)
                survive *= max(0.0, 1.0 - M.action_probability(hazard, STEP))
                hit = 1.0 - survive
                for label, quantile in (("25%", 0.25), ("中位", 0.5), ("75%", 0.75), ("90%", 0.9)):
                    if marks[label] is None and hit >= quantile:
                        marks[label] = moment
        if len(rows) < 3 and moment - now0 <= timedelta(hours=1.0):
            rows.append((cst(moment), advantage, reason,
                         "夜间" if 0 <= (moment + CST).hour < 6 else "白天"))

        drive_inputs = M.DriveInputs(
            emotion_tendency=runtime._emotion_tendency(state, emotions),
            unfinished=1.0 if unfinished else 0.0,
            memory_activation=memory_activation,
            hours_since_contact=hours_contact,
            recent_contact_ratio=clamp(recent / tolerance),
            boundary_pressure=0.0 if verdict.allow_proactive else 1.0,
            user_busy=busy,
            uncertainty=0.35 if runtime.user_model.numeric_view()["effective_count"] < 3 else 0.15,
            mood_valence=state.mood_valence,
        )
        targets = M.target_drives(drive_inputs, state=state, config=config)
        M.step_drives(state=state, targets=targets, config=config, dt_seconds=STEP)
        moment += timedelta(seconds=STEP)

    return {
        "marks": marks,
        "first": first,
        "candidates": [item.intent for item in pending],
        "blocked": blocked,
        "samples": rows,
        "survive72": survive,
    }


def main() -> int:
    """Predict every instance and print a summary table."""
    config = load_config()
    now0 = utcnow()
    print("now = %s UTC = %s CST   步长 %.0fs  上限 %.0fh" % (
        now0.strftime("%m-%d %H:%M"), cst(now0), STEP, HORIZON_H))
    print("模型：adv → λ = %.1e·softplus(%.1f·adv)；冷却 %.0fs；日上限 %d；夜间(00-06 CST) 沉默效用 +%.2f"
          % (config.utility.hazard_base, config.utility.hazard_beta,
             config.drive.cooldown_seconds, config.drive.max_contacts_per_day,
             config.scheduler.night_penalty))
    print()
    rows = []
    for path in sorted(glob.glob("/data/*/companion.sqlite3")):
        tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
        runtime = runtime_on_copy(path, config)
        try:
            outcome = simulate(runtime, config, now0)
        finally:
            runtime.close()
        advantage, reason = outcome["first"] or (None, None)
        marks = outcome["marks"]
        rows.append((tag, advantage, marks, outcome))
        print("### %s" % tag)
        print("  当前 advantage=%s (%s)  候选=%s%s" % (
            "%.4f" % advantage if isinstance(advantage, (int, float)) else "-", reason,
            " / ".join(outcome["candidates"])[:150] or "无",
            "  被边界挡=%s" % outcome["blocked"] if outcome["blocked"] else ""))
        for when, adv, why, daypart in outcome["samples"]:
            print("   %s  adv=%s  %s（%s）" % (
                when, "%.4f" % adv if isinstance(adv, (int, float)) else "-", why, daypart))
        print("   预测：25%% %s | **中位 %s** | 75%% %s | 90%% %s   72h 内不开口 %.1f%%" % (
            cst(marks["25%"]), cst(marks["中位"]), cst(marks["75%"]), cst(marks["90%"]),
            outcome["survive72"] * 100))
        print()

    print("=" * 92)
    print("%-14s %10s  %-14s %-14s %s" % ("人", "adv(现在)", "中位", "90%", "备注"))
    for tag, advantage, marks, outcome in sorted(
        rows, key=lambda item: (item[2]["中位"] is None, item[2]["中位"] or now0)
    ):
        note = "候选 %d 条" % len(outcome["candidates"])
        if outcome["blocked"]:
            note += "，边界挡下 %d" % len(outcome["blocked"])
        print("%-14s %10s  %-14s %-14s %s" % (
            tag, "%.4f" % advantage if isinstance(advantage, (int, float)) else "-",
            cst(marks["中位"]), cst(marks["90%"]), note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
