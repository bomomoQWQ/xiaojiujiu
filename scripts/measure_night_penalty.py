"""量：夜间惩罚按"边界观念"缩放后，夜里会多开口几次。

背景：`night_penalty` 现在只加在**沉默效用**上（软限制），用户的设计意图是
"晚上也发消息才算病娇，偶尔来点算情绪/情趣"。但实测真实账号在 00-05 点被放行过 **0 次**。
本脚本量"把有效夜间惩罚降下来"会变成几次，好据此定系数。

为什么能信：**不重写任何公式** —— 用的是线上同一份决策代码
（`motivation.decide` / `target_drives` / `step_drives` / `hazard_rate` / `action_probability`
/ `release_after_contact`）。"发出去"这一步严格照 `reducer.py:1863-1869` 的顺序记账：
`rollover_contact_day` → `last_contact_at` → `last_exchange_at` → `contact_count_today += 1`，
再调 `release_after_contact`（冲量/压力衰减、节制提升、上冷却）✓

四档（她 `boundary_respect=0.05` 时的有效惩罚 = `night_penalty × (FLOOR + (1-FLOOR)×br)`）：
    FLOOR 1.0（现状）→ 0.3000
    FLOOR 0.7        → 0.2145
    FLOOR 0.5        → 0.1575
    FLOOR 0.3        → 0.1005

全程只读：把每个人的 `companion.sqlite3` 连同 `-wal`/`-shm` **同名**拷进临时目录再跑
（改名会丢 WAL，踩过）。跑在 fleet 容器里（需要 `/app/runtime/src` 与 `/data`）::

    docker cp scripts/measure_night_penalty.py xxj-runtime-fleet:/tmp/measure.py
    docker exec xxj-runtime-fleet python3 /tmp/measure.py
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

STEP = 300.0
DAYS = 7.0
CST = timedelta(hours=8)
FLOORS = ((1.0, "现状(仅②)"), (0.7, "FLOOR 0.7"), (0.5, "FLOOR 0.5"), (0.3, "FLOOR 0.3"))
#: 先跑一档"真·现状"（惩罚原样 0.30、不加②）当**校验档**：它必须复现实测
#: （0-5 点真实账号 0 次）——复现不了就说明仿真本身不可信，后面几档也别看。
BASELINE_TIER = (None, "校验：现状 0.30(无②)")
#: 第②项：用户关怀。**方向要先定语义**：写成 `(1 − KAPPA×用户关怀)` 是"越在意越忍不住联系"
#: （占有欲那种在意，病娇读法），和"怕他难受所以不吵他"（关怀那种在意）**正好相反**。
#: 她两个轴都是 1.0（用户关怀、关系维护），取相反方向会互相抵消 —— 所以用 ② 之前必须挑一个语义，
#: 别两边都想要。①（边界观念）没有这个歧义，优先用它。
KAPPA = 0.3
#: ③"夜里打扰代价(他)"还没有数据支撑（全舰队夜里观测 2 条），一律返回中性值 1.0。
NIGHT_INTRUSION = 1.0


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
    folder = tempfile.mkdtemp(prefix="measure-")
    copy = os.path.join(folder, os.path.basename(path))
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, copy + suffix)
    return Runtime(config, database=Database(copy), created_at=utcnow())


def simulate(runtime: Runtime, config, now0: datetime, seed: int) -> list[datetime]:
    """Roll the real decision code forward, firing contacts, and return when she spoke."""
    state = runtime.state()
    pending = runtime.projections.candidates.list_active(limit=config.candidate.max_active)
    verdict = B.evaluate(
        runtime.projections.boundaries.active(now0), now=now0, state=state, is_proactive=True
    )
    pending, _blocked = runtime._partition_by_boundaries(pending, now=now0)
    if not pending:
        return []
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
    tolerance = max(1, config.utility.repeat_contact_tolerance)
    unfinished = runtime.projections.unfinished.list_open()
    memory_activation = runtime.memory_store.activation_strength()
    window = config.utility.repeat_window_seconds

    rng = random.Random(seed)
    fires: list[datetime] = []
    moment = now0
    end = now0 + timedelta(days=DAYS)
    while moment <= end:
        reference = parse(state.last_contact_at) or parse(state.epoch_at) or now0
        hours_contact = max(0.0, (moment - reference).total_seconds() / 3600.0)
        busy = runtime.user_model.busy_probability(
            hours_since_contact=(moment - last_user).total_seconds() / 3600.0 if last_user else 99.0,
            replied_recently=bool(last_user and (moment - last_user).total_seconds() < 1800.0),
            context={},
        )
        recent = sum(1 for fired in fires if (moment - fired).total_seconds() <= window)
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
            # 必须把**同一个** rng 贯穿整场仿真：`decide` 内部用它掷骰
            # （`motivation.py:954`），每轮新建 `Random(0)` 会让骰子冻在第一颗数上，
            # 于是 `outcome.acted` 恒为假、开口数恒为 0（踩过）。
            rng=rng,
            situation_text=context.get("summary") or "",
            emotion_alignment=alignments,
        )
        outcome = result.outcome
        advantage, reason = outcome.advantage, outcome.reason
        # 注意：`decide` **自己**已经掷过骰子（motivation.py:954 `source.random() > probability`
        # → `hazard_not_triggered`），所以这里只能看 `outcome.acted`，不能再掷一次 ——
        # 第一版就是又掷了一遍，导致开口数是真实的两倍左右（假数据）。
        if outcome.acted:
            # 照 reducer.py:1863-1869 的顺序记账，再走线上同一段释放函数。
            M.rollover_contact_day(state, now=moment)
            state.last_contact_at = moment
            state.last_exchange_at = moment
            state.contact_count_today += 1
            M.release_after_contact(state, config=config, now=moment)
            fires.append(moment)

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
    return fires


def main() -> int:
    """Measure every instance under every night-penalty floor."""
    base = load_config()
    now0 = utcnow()
    print("now = %s UTC = %s CST   步长 %.0fs  滚动 %.0f 天  night_penalty 基线 %.2f"
          % (now0.strftime("%m-%d %H:%M"), cst(now0), STEP, DAYS, base.scheduler.night_penalty))
    print()
    paths = sorted(glob.glob("/data/*/companion.sqlite3"))
    summary: dict[float, dict] = {}
    for floor, label in (BASELINE_TIER, *FLOORS):
        config = load_config()
        print("=" * 96)
        print("### %s" % label)
        total = night = 0
        penalties: list[float] = []
        per_person = []
        for index, path in enumerate(paths):
            tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
            runtime = runtime_on_copy(path, config)
            try:
                values = runtime.state().values
                # ① 边界观念：越不顾忌，夜里的惩罚越轻（"敢说"）—— 无歧义，优先用它
                # ② 用户关怀：按"占有欲"语义降低惩罚（越在意越忍不住联系）；
                #    若按"怕他难受"语义就该**提高**惩罚 —— 两者相反，用前先定语义
                # ③ 夜里打扰代价(他)：暂时恒为中性 1.0（观测样本不够）
                if floor is None:
                    # 校验档：惩罚原样，不加②——用来对齐实测（0-5 点真实账号 0 次）。
                    config.scheduler.night_penalty = base.scheduler.night_penalty
                else:
                    config.scheduler.night_penalty = (
                        base.scheduler.night_penalty
                        * (floor + (1.0 - floor) * values.boundary_respect)
                        * (1.0 - KAPPA * values.user_care)
                        * NIGHT_INTRUSION
                    )
                penalties.append(config.scheduler.night_penalty)
                fires = simulate(runtime, config, now0, seed=1000 + index)
            finally:
                runtime.close()
            night_fires = [f for f in fires if 0 <= (f + CST).hour < 6]
            total += len(fires)
            night += len(night_fires)
            cadence = ("%.1f 天一次" % (DAYS / len(night_fires))) if night_fires else "7 天 0 次"
            per_person.append((tag, len(fires), len(night_fires), cadence))
        print("   本档生效惩罚：%s" % " ".join("%.4f" % p for p in sorted(set(round(p, 6) for p in penalties))))
        for tag, count, night_count, cadence in sorted(per_person, key=lambda row: -row[2]):
            print("   %-14s 7 天开口 %-3d ｜ 夜里(0-5) %-2d ｜ 夜里频率 %s" % (
                tag, count, night_count, cadence))
        print("   —— 合计：7 天 %d 次，其中夜里 %d 次（%.1f%%）→ 每天 %.2f 次、夜里 %.2f 次/天"
              % (total, night, 100.0 * night / total if total else 0.0, total / DAYS, night / DAYS))
        summary[floor] = {"label": label, "total": total, "night": night,
                          "penalty": penalties[0] if penalties else 0.0}
    print()
    print("=" * 96)
    print("%-14s %10s %12s %12s %10s" % ("档位", "生效惩罚", "7天开口", "夜里开口", "夜里频率"))
    for floor, label in (BASELINE_TIER, *FLOORS):
        row = summary[floor]
        cadence = ("%.1f 天一次" % (DAYS / row["night"])) if row["night"] else "从不"
        print("%-18s %10.4f %12d %12d %10s" % (label, row["penalty"], row["total"], row["night"], cadence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
