# Endogenous Companion Runtime

一个**内源主动型长期陪伴 Runtime sidecar**：独立于宿主框架（AstrBot 等）运行的 Python 进程，负责维护角色真正持续的**时间、情绪、记忆、用户认识、未尽之事、主动冲动与行为决策**。

主 LLM 只负责最后的语言表现，不是"脑子"。

```text
用户事件 → 不可变事件日志 → lazy_tick（时间推进）
        → 情绪引擎 / 记忆 / 未尽之事 / 用户交互模型
        → 记忆激活 → 候选意图池
        → 冲动 I / 节制 R / 压力 P → 动机博弈（沉默效用 · 危险率 · softmax）
        → action_attempt（committed ≠ sent）
        → 主 LLM 渲染 → 异步 outbox 投递 → 用户反馈 → 回到起点
```

- 版本：`0.1.0`（第一版可完整运行的 long-term companion Runtime）
- 依赖：Python **3.11+**、`fastapi`、`uvicorn`；存储与算法全部使用标准库（`sqlite3`、`math`、`dataclasses`）
- 本工程位于 `runtime/`，**不修改也不依赖 `AstrBot/`**；宿主通过 HTTP 接入
- **本工程不读取、不存储、不记录任何用户 API key。** 密钥属于宿主框架；Runtime 的配置输出经 `redact_tree()` 递归脱敏，即使有人把 `api_key` 塞进 `extras`，它也不会出现在 API 响应或日志里

---

## 1. 目录结构

```text
runtime/
├── pyproject.toml                 打包与 pytest 配置（src 布局）
├── README.md                      本文件
├── src/companion_runtime/
│   ├── __init__.py                版本号与 API 版本
│   ├── typing.py                  全部枚举与跨模块记录（RawEvent / CandidateIntent / ...）
│   ├── utility.py                 数学、时间、文本工具（sigmoid / softplus / softmax / decay）
│   ├── config.py                  分层 dataclass 配置 + TOML/JSON/环境变量 + 脱敏
│   ├── db.py                      SQLite 连接、事务/保存点、全部表结构、列迁移
│   ├── eventlog.py                append-only 原始事件日志（+ 可选 JSONL 镜像）
│   ├── projections.py             当前投影读写（runtime_state / 记忆 / 候选 / outbox ...）
│   ├── emotion.py                 事件评价 → 情绪动力学 → 情绪解释（模板 + 缓存）
│   ├── boundaries.py              边界状态机（语言检测、生命周期、硬约束裁决）
│   ├── unfinished.py              未尽之事状态机（检测、生命周期、唤醒锚点）
│   ├── memory.py                  记忆候选评分、巩固、检索、激活池
│   ├── user_model.py              用户交互模型（简化分层贝叶斯 + 证据权重）
│   ├── candidate.py               候选意图生成与池操作（ADD/UPDATE/RETIRE/REINTERPRET）
│   ├── pool.py                    候选池管理器（唯一应用写操作的地方）
│   ├── motivation.py              沉默效用、候选效用、危险率、softmax、I/R/P 动力学
│   ├── action.py                  action_attempt 状态机
│   ├── protocol.py                APPLY / REBASE / DISCARD + 并发重协调
│   ├── reducer.py                 唯一写者（提案应用、投递状态机、outbox 记账）
│   ├── runtime.py                 核心编排：lazy_tick、前台路径、内源主动路径
│   ├── scheduler.py               自适应内源唤醒调度
│   ├── context.py                 临时上下文组装 + 主 LLM prompt 块渲染
│   ├── authorize.py               边界/预算/文本硬门禁
│   ├── delivery.py                claim → render → send → observe 投递服务
│   ├── maintenance.py             WAL checkpoint、完整性校验、备份、恢复
│   ├── api.py                     FastAPI 应用
│   └── cli.py                     命令行入口
└── tests/                         单元 / 集成 / 耐久性测试
```

---

## 2. 快速开始

```powershell
cd runtime

# 安装（可编辑安装，含测试依赖）
python -m pip install -e ".[test]"

# 跑测试
python -m pytest -q

# 或不安装，直接把 src 放进 PYTHONPATH
$env:PYTHONPATH="src"; python -m pytest -q
```

### 启动 sidecar

```powershell
python -m companion_runtime.cli --base-dir ./data serve --host 127.0.0.1 --port 8787 `
    --maintenance-interval 3600 --backup-dir ./data/backups
```

`--maintenance-interval > 0` 时，服务运行期间会定期执行 checkpoint + 完整性校验（并可选写快照）；收到中断信号时会先做一次收尾 checkpoint 再退出。安装后也可直接用 `companion-runtime` 命令。

### 命令行

| 命令 | 作用 |
|---|---|
| `serve` | 运行 HTTP sidecar（可带自动维护） |
| `tick --now <ISO>` | 执行 `lazy_tick`，把 Runtime 推进到某时刻 |
| `endogenous --now <ISO> [--force] [--dry-run]` | 跑一次内源主动轮，打印完整决策 |
| `state --include all\|state\|candidates\|memories\|unfinished\|boundaries\|attempts` | 只读查看当前投影 |
| `verify [--json]` | 完整性 + 结构一致性检查（失败退出码 3） |
| `checkpoint --mode PASSIVE\|FULL\|RESTART\|TRUNCATE` | 把 WAL 合并回主库文件 |
| `backup [目标] [--keep N]` | 写一份一致快照（`VACUUM INTO`） |
| `restore <快照>` | 用快照覆盖数据库（先校验，拒绝覆盖运行中的库） |
| `recover [--backup-dir DIR] [--run]` | 打印恢复方案；`--run` 顺带执行一次维护 |
| `config` | 打印脱敏后的有效配置 |
| `health` | 打开数据库并打印健康摘要 |

全局参数：`--config <file.toml|file.json>`、`--base-dir DIR`、`--log-level LEVEL`。

---

## 3. 配置

三层覆盖，优先级由低到高：**dataclass 默认值 → `--config` 文件 → `CR_` 环境变量**。

```toml
# runtime.toml
runtime_id = "companion"
conversation_id = "default"

[values]                    # 价值观：把人格编译成动力学参数
autonomy = 0.72
boundary_respect = 0.88
emotional_expression = 0.46
relationship_maintenance = 0.79
user_care = 0.85
conflict_directness = 0.41
stability_commitment = 0.81
curiosity = 0.76

[server]
host = "127.0.0.1"
port = 8787
log_level = "INFO"

[storage]
database_path = "./data/runtime.sqlite3"
raw_log_path = "./data/raw_events.jsonl"
mirror_raw_events = true      # 额外写一份 append-only JSONL 便于离线查看
wal = true                    # 保持 true；见第 6 节
busy_timeout_ms = 5000

[drive]                       # I / R / P 动力学
tau_impulse_seconds = 5400
tau_restraint_seconds = 9000
beta = 4.0
kappa_plus = 0.00006
kappa_minus = 0.00004
impulse_release = 0.55        # 主动后冲动释放比例
pressure_release = 0.70
restraint_boost = 0.06
cooldown_seconds = 2400
max_contacts_per_day = 12
absence_saturation_hours = 36 # 完全无交流多久后"缺席项"饱和

[silence]
base = 0.28                   # 沉默的基本价值：没有理由就别开口
impulse_penalty = 0.10
pressure_penalty = 1.20

[utility]
temperature = 0.35            # softmax 温度
hazard_base = 0.00003         # 危险率基数
hazard_beta = 4.0
urgency_gain = 0.70           # "具体理由"的额外权重

[scheduler]
min_interval_seconds = 60
max_interval_seconds = 5400
quiet_hours_start = 23        # 安静时段内不主动（可选）
quiet_hours_end = 7

[user_model]
no_reply_weight = 0.06        # 「没回复」极弱证据
busy_attribution_floor = 0.10 # 用户很忙时归因权重下限
learning_rate = 0.35

[outbox]
lease_seconds = 45
max_attempts = 3
retry_backoff_seconds = 0   # 0 = nack 后立即可再领取（推荐；节流交给宿主重试队列）
```

环境变量（双下划线表示层级）：

```powershell
$env:CR_SERVER__PORT = "9000"
$env:CR_DRIVE__COOLDOWN_SECONDS = "600"
$env:CR_STORAGE__DATABASE_PATH = "D:\companion\runtime.sqlite3"
$env:CR_STORAGE__DATABASE_PATH = ":memory:"     # 内存库（测试/演示）
```

全部可用参数见 `config.py` 的 dataclass 定义（每个字段都有说明）。

---

## 4. 核心概念

### 4.1 不可变原始事件（append-only）

`raw_events` 只追加，从不 `UPDATE`/`DELETE`。`EventLog` 的公开 API 里根本没有修改方法（`test_event_log_is_append_only` 会检查）。事件结构：

```json
{
  "event_id": "evt_9f1c2ab34d5e",
  "event_type": "user_message",
  "timestamp": "2026-03-01T09:00:00+00:00",
  "actor": "user",
  "conversation_id": "default",
  "content": "明天下午面试，结束告诉你结果。",
  "metadata": {},
  "source_event_ids": [],
  "runtime_version": 12
}
```

事实、推断、解释严格分离：事实进 `working_situation_items(kind='fact')`，推断进 `kind='inference'` 并带 `confidence`，解释进 `interpretation_versions`（多版本 + `supersedes_id`），对现在有意义的新理解通过 `reappraisals` 追加，**从不回滚历史**。

### 4.2 版本与单写者

`runtime_state.version` 是全局单调计数器。所有写入必须经过 `Reducer`，且每次写入都带乐观并发检查：

```python
projections.runtime.write(state, conn, expect_version=state.version)   # 不匹配则 VersionConflict
```

后台模型不能写状态，只能提交 `Proposal`，由 `Reducer.process_proposal()` 分类：

| 分类 | 条件 | 处理 |
|---|---|---|
| **APPLY** | 源事件仍在，版本漂移在敏感度预算内 | 原样应用 |
| **REBASE** | 语义仍有效，但世界前进了 | 保留语义结果，**基于当前状态重算效应** |
| **DISCARD** | 源事件消失，或前提被新消息推翻 | 只入历史，不改状态 |

敏感度分级（`protocol.TASK_SENSITIVITY`）：浅层标签 `low`（预算 50 版）→ 事件评价 `medium`（6）→ 候选生成/情绪解释 `high`（2）→ 主动消息成品 `critical`（0，用户开口必须重协调）。

### 4.3 lazy_tick：唯一时间入口

**任何入口都必须先 `lazy_tick(now)`**（用户消息、内源唤醒、后台结果、发送回执）。它在一次事务里完成：

背景心境自然恢复 → 情绪事件衰减 → I/R/P 推进 → 冷却 → 记忆激活衰减 → 未尽之事时间状态 → 边界过期 → 候选期限 → 派生 `allow_proactive`。

因此"用户离开 8 小时"真的会被算成 8 小时。注意 `endogenous_round()` 自身会调用 `lazy_tick`，调用方**不要**先 tick 到同一时刻再唤醒 —— 那会让危险率积分区间为 0，永远不行动（集成测试专门覆盖了这一点）。

### 4.4 情绪：数值负责动力学，语言负责语义

```text
事件 → appraise_event()            规则化评价：方向/影响/激活/不确定性/关系信号/责任（不产出情绪值）
     → apply_new_emotion_events()  结合价值观、背景心境、用户模型、既有情绪事件计算数值变化
     → EmotionExplainer            把结构化状态翻译成第一人称心理语言（模板或本地小模型）
```

背景心境 = `valence / arousal / stability`；情绪影响事件 = 带 `decay_rate` 的衰减事件，`semantic_label = null` 是合法状态（知道"这是中等负向影响"，但暂时不知道叫什么）。

情绪解释带缓存：心理状态变化不足时复用旧解释，变化明显才重新解释（`should_re_explain`）。

### 4.5 I / R / P 动力学

```text
dI/dt = (Î - I) / τ_I          Î = σ(θ_Iᵀx + b_I)
dR/dt = (R̂ - R) / τ_R          R̂ = σ(θ_Rᵀx + b_R)
dP/dt = κ₊(1-P)·S_β(I-R) - κ₋·P·S_β(R-I),   S_β(x) = ln(1+e^{βx})/β
```

- 权重由价值观编译：边界观念高 → 节制高、冲动低；用户关怀高 → 未尽之事更推高冲动
- 时长项用**"距上次任何交流"**（`last_exchange_at`）而不是"距上次主动"：用户回一句就足以让缺席项归零
- 主动时刻的状态跃迁：`I ← (1-ρ_I)I`，`P ← (1-ρ_P)P`，`R ← min(1, R+ρ_R)`，并进入冷却

标定目标：无边界、无理由时，长时间沉默会**缓慢**推高冲动，但沉默效用仍然更高 —— 角色不会因为"只是孤独"就说话；一旦有具体理由（未尽之事到期等），才会跨过门槛。

### 4.6 动机博弈

```text
U_i = V_internal + V_user + V_relation − C_boundary − C_interrupt − C_repeat − C_risk
U_∅ = B₀ + aR + bB + cC − dI − eP²
D   = max_i U_i − U_∅
λ   = λ₀ · ln(1 + e^{βD})            ← 危险率，不是死阈值
P(主动 in Δt) = 1 − e^{−λ·Δt}
```

- **没有阈值悬崖**：0.799 / 0.801 不再决定发不发；概率不依赖心跳频率（`test_two_short_ticks_match_one_long_tick` 验证 `1-e^{-λ·2dt} = 1-(1-(1-e^{-λ·dt}))²`）
- 行动确定后用 **softmax(U_i / T)** 选候选，`T` 越低越理性
- `V_internal` 含 `urgency` 项：**具体理由**（未尽之事相关性 / 内在需要）才是"开口"的主要来源
- 高风险候选使用**保守下界**（近似分位）而非均值，数据越少越保守
- 硬边界**不参与博弈**：被阻止的候选 `total = -inf` 且 `blocked=True`，压力再高也没用（`force=True` 仅用于观测，不会真的行动）

### 4.7 边界状态机

边界是硬约束，不是博弈里的一项。类型：`temporal` / `topic` / `permanent` / `conditional`。

- 语言检测在入口屏障阶段就运行（高精度、可容忍漏检）；`以后/永远…` 生成永久边界，且**同一 scope 只保留最强的一条**（不会被广谱临时规则降级成 24 小时）
- 用户回话**只恢复"可以回复"**，不自动恢复"可以主动"
- `allow_proactive` 是**派生状态**，每次 tick 从生效边界重算 —— 时间窗过期后角色自动重新获得主动权
- 过期边界不删除，保留为历史证据

### 4.8 未尽之事

`open → waiting → due → resolved`，另有 `cancelled / muted / expired / invalidated`。

- 从用户语言里检测"需要后续跟进"的事项与预计结束时间（明天/后天/下午/晚上…），并做主题级去重（"等待面试结果" 与 "等待用户告知结果" 不会并存两条）
- 到 `due` 时成为**内源唤醒锚点**；用户主动告知结果会自动 `resolved`，并退役其衍生的候选
- `t_next = min(t_hazard, t_unfinished, t_boundary, t_cooldown, t_candidate, t_pause)`

### 4.9 记忆

```text
候选价值 M = 未来用途 + 重复性 + 用户强调 + 情绪显著性 + 未尽之事相关性 + 稳定性 − 短暂性
候选池 → 后台巩固 → 长期记忆（结构化字段 + 自然语言摘要双表示）
       → 检索（词面重合 + 局势 + 未尽之事 + 情绪 + 时间 − 刚想起惩罚 + ε）
       → 激活记忆池（激活值随时间衰减、Top-N 有界）
```

- **遗忘是归档不是删除**（`active → low_activation → archived`）
- **冲突不删旧记忆**：新信息降低旧记忆置信度并写 `superseded_by_hint`，可表达为"以前…后来…"
- 巩固是幂等的：同一内容重复出现会合并进已有记忆，而不是堆重复行
- 检索不依赖 embedding；`MemoryStore.retrieve()` 就是留给 embedding sidecar 的接缝（embedding 未就绪时本方案即为降级路径）
- **没有用户 query 也能"想起什么"**：`build_cue()` 用工作局势 + 未尽之事 + 当前情绪构造内部线索

### 4.10 用户交互模型

回答的是"**在某种情况下，如果角色这样做，这个用户大概率会怎么反应**"，不是好感度。

四个 logistic 模型（共享 13 维特征向量 `x = φ(A, C, Z)`）：`P_R`（回复）/ `P_+`（积极）/ `P_C`（愿意继续）/ `P_B`（触碰边界），外加 `U`（不确定性）。

- **分层收缩**：全局参数 `θ` + 每类行为的偏移 `δ_behaviour`，样本少时自动退回更一般的认识
- **证据权重** `w = w_source · w_attribution · w_semantic · w_recency`
- **观察 ≠ 归因**：`no_reply = true, reply_delay = 21600` 只以 `no_reply_weight`（默认 0.06）进入模型；用户很忙时 `w_attribution → floor`（默认 0.10）。**用户 6 小时没回几乎不改变"主动联系接受度"**
- **慢漂移** `Θ_t ~ N(Θ_{t-1}, Q·Δt)`：精度向先验回退，均值保留 —— 时间越久，旧认识不是消失而是置信度下降
- 行为相对**用户自己的基线**判断（回复延迟基线化），而不是绝对阈值
- 双视图：数值视图给心跳/情绪/动机，语义视图给强 API（冷启动时它会**诚实承认没有证据**，"只能依赖通用先验"）

### 4.11 候选意图与动机决策

候选生成器回答"我现在可能想做什么"（不是 RAG，也不是最终决策）。规则版（零模型，Level 0 降级）会产出：

1. 每件 live 未尽之事 → `follow_up` 候选（含 `invalidate_when`）
2. 每条高激活记忆 → `curious_question` 候选
3. **永久特殊候选**"没有具体事项，只是想和用户建立联系"，内在价值 `V_contact = σ(aI + bP − cR)`

强 API 只能提议 `ADD / UPDATE / RETIRE / REINTERPRET`，真正写池子的是**候选池管理器**（`pool.py`）；非法操作逐条拒绝而不中断批次。来源为空的候选会被拒绝（"念头不能凭空出现"）。

### 4.12 action_attempt：`committed` ≠ `sent`

```text
proposed → committed → rendering → ready_to_send → sent → resolved
异常：aborted / expired / failed
```

`committed` 表示"此刻确实已经决定想联系用户"，消息还不存在。状态跃迁全部校验并写入 append-only 的 `attempt_events`，被放弃的意图也留在历史里。

**并发重协调**：用户在 committed 之后、sent 之前开口时，不直接丢弃，而是进入 `KEEP / MERGE / RERENDER / RESOLVED / ABORT`：

- 用户已经回答了我们正要问的 → `RESOLVED`（"心有灵犀"）
- 用户处境重大变化（"家里出事了"）→ `ABORT`（原本想撒娇的表达不再合适，但历史保留）
- 中途出现显式边界 → `ABORT`（发送前最后一道门）
- 同主题但不满足 → `MERGE`（新消息并入原意图）

### 4.13 上下文注入是临时的

`context.build()` 产出**一次性** bundle：心理状态（自然语言）+ 工作局势 + 最终意图 + 少量激活记忆 + 表达边界 + 时间连续性。`render_block()` 渲染成主 LLM 的 prompt 块，**不包含裸浮点数**（模型对心理文本远比对 `anger = 0.72` 敏感）。

本轮结束后全部丢弃。永久对话历史只保存用户可见消息与 assistant 可见消息（`assert_ephemeral` 守住这个不变量）。

当角色在用户开口前几秒已 committed，block 会带上 `lead_seconds_before_user_message`，让主 LLM **可以**自然地说"你居然刚好发来了"，但不强迫。

---

## 5. HTTP API

所有端点都是薄壳：校验输入 → 委托 Runtime/Reducer → 返回 JSON。**没有任何端点直接写状态。**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活 + 紧凑活动摘要（版本、边界数、in-flight、outbox 统计） |
| POST | `/events` | 追加事件；`user_message` 走完整前台路径，其余原样追加 |
| GET | `/events` | 读取原始事件（`conversation_id` / `event_type` / `limit` / `newest_first`） |
| GET | `/events/{id}` | 单条事件 + 其解释版本 |
| GET | `/context` | 组装临时上下文 bundle |
| POST | `/context/render-block` | 只渲染 prompt 块 |
| POST | `/explain` | 当前第一人称心理状态 |
| GET | `/schedule` | 下一次内源唤醒计划 + 当前是否允许派发 |
| POST | `/tick` | 显式执行 `lazy_tick` |
| POST | `/endogenous` | 跑一次内源主动轮，返回完整决策 |
| POST | `/proposals` | 提交后台模型结果 → APPLY/REBASE/DISCARD |
| POST | `/tasks` | 派发前登记任务快照 |
| POST | `/reconcile` | 重协调 in-flight attempt |
| GET | `/outbox` | 列出投递队列 + 各状态统计 |
| POST | `/outbox/claim` | **claim/lease**：原子领取待办（`owner`/`limit`/`kinds`） |
| POST | `/outbox/{id}/ack` | 确认完成 |
| POST | `/outbox/{id}/nack` | 退回重试（默认**立即可再领取**；可传 `retry_delay_seconds` 节流，或 `terminal=true` 直接失败）。返回 `status`/`attempts`/`available_at` |
| POST | `/authorize` | 询问是否允许某行为（边界 + 预算 + attempt 完整性 + 文本检查）。返回 `allowed`（本次请求是否放行）与 `allow_proactive`（主动联系的总许可，与 `/boundaries` 同名字段） |
| POST | `/render` | 提交渲染结果（`outbox_id` + `text`）→ `ready_to_send` |
| POST | `/render/fail` | 上报渲染失败；返回 `attempt_state`（结果状态，重复上报幂等） |
| POST | `/rendered` | 直接路径：给 attempt 附文本并入队发送 |
| POST | `/delivery` | 上报发送结果（可附带 reaction） |
| POST | `/observations` | 记录一次用户反应 |
| GET | `/state` | 当前运行时投影 |
| GET | `/candidates` | 候选意图池 |
| POST | `/candidates/operations` | 通过池管理器应用 ADD/UPDATE/RETIRE/REINTERPRET |
| GET | `/memories` | 长期记忆 + 激活池 + 待巩固候选 |
| GET | `/user-model` | 数值视图 + 语义视图 |
| POST | `/user-model/predict` | 预测某候选行为的用户反应（含保守下界） |
| GET/POST | `/unfinished` | 列出 / 创建未尽之事 |
| POST | `/unfinished/{id}/resolve` | 结清未尽之事 |
| GET | `/boundaries` | 边界列表 + 当前许可裁决 |
| GET | `/attempts` | 行动尝试 + 状态跃迁日志。每个 attempt 同时给出 `state`（字符串，便于直接比较）与 `state_value`（同一取值的显式类型化字段） |
| GET | `/observations` | 交互观察历史 |
| GET | `/situation` | 工作局势（事实 / 推断 / 未尽之事） |
| GET | `/config` | 脱敏后的有效配置 |
| GET | `/maintenance/verify` | **完整性 + 结构一致性检查** |
| POST | `/maintenance/checkpoint` | **WAL checkpoint**（默认 TRUNCATE） |
| POST | `/maintenance/backup` | **写一致快照**（`VACUUM INTO`） |
| GET | `/maintenance/recovery-plan` | **恢复方案** |
| POST | `/maintenance/tick` | **例行维护**：checkpoint + verify（+ 可选备份与保留策略） |

OpenAPI 文档：`http://127.0.0.1:8787/docs`、`/openapi.json`。

### 宿主接入顺序（推荐）

```text
1. 收到平台消息      → POST /events {event_type: user_message, content}
2. 组装本轮 prompt   → GET  /context/render-block   （注入后即丢弃）
3. 主 LLM 生成回复   → 直接发给用户（Runtime 不阻塞前台）
4. 投递 worker 循环  → POST /outbox/claim（owner 固定）
                       kind=render → 用主 LLM 渲染 → POST /render
                       kind=send   → 真实发送       → POST /delivery
5. 用户后续反应      → POST /observations，或 /delivery 带 reaction
6. 后台模型结果      → POST /proposals（带 based_on_version 与 source_event_ids）
7. 定时维护（每小时）→ POST /maintenance/tick
```

---

## 6. 持久化、WAL、事务与蓝屏恢复

这一节是**硬约定**，不是建议。

### 6.1 运行参数

| 设置 | 值 | 原因 |
|---|---|---|
| `journal_mode` | **WAL** | 读不阻塞写、写不阻塞读 —— HTTP 服务与认知轮可同时进行 |
| `synchronous` | **NORMAL** | WAL 下使"已 COMMIT"的事务对**进程崩溃**持久，同时兼顾弱 VPS 的写入开销 |
| `busy_timeout` | 5000 ms | 短暂争用时等待而不是立刻报错 |
| 事务 | 每次变更 `BEGIN IMMEDIATE` | 单写者 + 写前取锁，避免升级死锁 |
| 嵌套 | `SAVEPOINT` | 一个维护操作可以与认知轮处在同一个原子单元里 |

### 6.2 崩溃与断电语义（明确区分）

实际只有两种情况，**都不是"数据库损坏"**：

| 场景 | 结果 |
|---|---|
| **进程被杀 / kill -9 / 服务崩溃**（无断电） | 已 `COMMIT` 的事务**全部保留**。WAL 中的提交记录已落盘，重连时自动回放 |
| **突然断电 / 蓝屏**（`synchronous=NORMAL`） | 可能丢失**最后若干个已提交事务**；但数据库**永不损坏**。SQLite 校验 WAL 帧校验和，回放到最后一个完整帧，丢弃"被撕开"的尾部 |
| 半途中断的写入 | 不可能产生"半轮认知结果"：整个认知轮在同一个事务里，要么全在，要么全不在 |

> 想要断电也零丢失，把 `synchronous` 改成 `FULL`（每次提交都 fsync），代价是写入延迟更高。第一版按架构文档选择 `NORMAL`：**可用性 > 推理速度**，且崩溃恢复总是安全的。

### 6.3 自动恢复流程

无需人工干预，下次连接时 SQLite 自动完成：

```text
1. 发现 <db>-wal
2. 校验每一帧 checksum，回放所有完整帧
3. 丢弃损坏的尾帧
4. 数据库停留在最后一个完整事务的状态
```

因为回放只在 WAL 存在时需要，运维风险点是 **WAL 无限增长**，所以需要定期 checkpoint。

### 6.4 checkpoint

```powershell
companion-runtime checkpoint --mode TRUNCATE
```

- `TRUNCATE`（默认）：合并并**把 WAL 截断到 0 字节**，推荐定期执行
- `PASSIVE` / `FULL` / `RESTART`：不同强度的合并；有并发读者时可能提前返回（`busy > 0`，无害，下次继续）
- 任何时候都可以安全执行，包括服务运行期间
- `serve --maintenance-interval 3600` 会把它作为后台任务自动运行
- 干净退出时也会自动做一次收尾 checkpoint

### 6.5 备份

```powershell
companion-runtime backup                       # → <数据目录>\backups\runtime-<UTC时间戳>.sqlite3
companion-runtime backup D:\snap\a.sqlite3 --keep 7
```

- 使用 **`VACUUM INTO`**：单条语句产出**事务一致**的快照，**不需要停止服务**
- 因为它是透过 WAL 读取的，**尚未 checkpoint 的已提交事务也会被包含**（有专门测试验证）
- 快照是**自包含**单文件：不含 `-wal`/`-shm` 依赖，可以直接用文件工具复制
- 默认拒绝覆盖已存在的文件，必须显式 `overwrite`
- `--keep N` 执行保留策略（`prune_backups()`，按 mtime 保留最新 N 份）
- 内存库（`:memory:`）无法备份，接口会明确返回 409

### 6.6 恢复演练（可直接照做）

```powershell
# 0) 先备份（服务运行中也行）
companion-runtime backup --keep 7

# 1) 看当前该做什么
companion-runtime recover
#   recommended_action: stop_runtime_first   ← 有 -wal/-shm，说明库在运行中
#   recommended_action: restore              ← 已停机，可以恢复
#   recommended_action: backup_now           ← 还没有任何快照

# 2) 停掉服务（干净退出会自动 checkpoint，-wal/-shm 随之消失）

# 3) 校验快照并安装（会先对快照跑 integrity_check）
companion-runtime restore D:\data\backups\runtime-20260301T090000Z.sqlite3
#   旧库会被保留为 <db>.replaced

# 4) 确认恢复结果
companion-runtime verify
companion-runtime health
```

`restore` 的两条安全护栏：

1. **拒绝覆盖运行中的数据库**：目标旁边存在 `-wal`/`-shm` 时直接报错（覆盖活动数据库会损坏它）
2. **先校验快照**：`integrity_check` 不过就拒绝安装

### 6.7 一致性校验

```powershell
companion-runtime verify          # 退出码 0 = 通过，3 = 有问题
```

检查项：

1. `PRAGMA integrity_check` == `ok`
2. `schema_meta` 中的 schema 版本存在
3. `runtime_state` 行存在且 `version >= 0`
4. 任何 `action_attempt.outbox_id` 都必须指向真实 outbox 行
5. `attempt_events` 引用的 attempt 必须存在
6. 不允许存在没有过期时间的 lease（那会让一行永远被占住）
7. 各表行数统计 + journal mode 断言

### 6.8 schema 迁移

`Database.migrate()` 是幂等的：先 `CREATE TABLE IF NOT EXISTS`，再对 `ADDED_COLUMNS` 声明的列用 `ALTER TABLE` 补齐（例如 `runtime_state.epoch_at`、`last_exchange_at`）。**已有数据库可以原地升级，不需要重建。**

---

## 7. 不变量（都有对应自动测试）

| # | 不变量 | 测试 |
|---|---|---|
| 1 | 原始事件不可修改 | `test_invariant_1_raw_events_are_never_modified` |
| 2 | 推断不能升级成事实 | `test_invariant_2_inference_cannot_become_fact` |
| 3 | 后台模型不能直接写 Runtime | `test_invariant_3_background_models_never_write_directly` |
| 4 | 所有入口先 `lazy_tick(now)` | `test_invariant_4_every_entry_calls_lazy_tick` |
| 5 | 显式边界高于动机算法（`P=1` 也无效） | `test_invariant_5_explicit_boundaries_outrank_the_game` |
| 6 | 主 LLM 不拥有情绪状态写权限 | `test_invariant_6_main_llm_has_no_state_write_authority` |
| 7 | 后验重解释不覆盖原始事件 | `test_invariant_7_reinterpretation_never_rewrites_the_past` |
| 8 | 用户不回复 ≠ 负反馈 | `test_invariant_8_no_reply_is_not_negative_feedback` |
| 9 | `committed != sent` | `test_invariant_9_committed_is_not_sent` |
| 10 | 隐藏心理上下文不进永久历史 | `test_invariant_10_hidden_context_never_enters_history` |

---

## 8. 测试

```powershell
python -m pytest -q                     # 全部
python -m pytest -q -m integration      # 只跑端到端场景
python -m pytest -q --cov=companion_runtime
```

覆盖范围：

- **基础设施**：数学工具数值性质、配置三层覆盖与脱敏、SQLite 事务/保存点/JSON 列、事件日志 append-only 与过滤
- **认知模块**：事件评价方向与不确定性、情绪衰减与心境恢复、解释器缓存与 provider 容错、边界检测（含假阳性防护）与生命周期、未尽之事全生命周期、记忆评分/巩固/冲突/去重/检索/激活、用户模型的特征/证据权重/冷启动/学习/漂移/双视图、候选生成与池管理器四种操作、沉默效用、效用分解、危险率（含频率无关性）、softmax 选择、I/R/P 动力学（惯性、饱和、释放）
- **协议与并发**：APPLY/REBASE/DISCARD 全部分支、敏感度表、rebase 辅助、五种重协调结果、outbox claim/lease/ack/nack/租约过期回收/优先级/kinds 过滤、action 状态机合法与非法跃迁
- **耐久性**（`test_durability.py`）：WAL 生效、认知轮原子性、**进程崩溃后已提交事务保留**、**截断 WAL 尾部后不损坏**、缺 sidecar 无害、四种 checkpoint 模式、verify 各类检查（含故意造坏）、备份一致性、备份包含未 checkpoint 事务、不能覆盖已有快照、停机后可读、**完整恢复演练（备份 → 毁库 → 确认不可读 → restore → verify → 继续可用）**、拒绝覆盖运行中的库、保留策略、CLI 全流程
- **HTTP**：全部端点契约、状态码（404/409/422）、OpenAPI 覆盖、outbox 全链路、边界/渲染/投递、维护端点
- **场景与不变量**：架构文档里的 7 个场景 + 完整"面试"故事 + 10 条不变量 + 降级模式 + 多进程并发写冲突

---

## 9. 降级模式（Level 0）

第一版默认就是**零模型可运行**：

- 事件评价：规则 + 双语信号词表
- 情绪解释：模板（可选接入本地 2B，通过 `EmotionSemanticProvider` 协议）
- 候选意图：规则生成器
- 记忆巩固：词面去重 + 结构化摘要
- 用户模型：先验 + 在线贝叶斯更新

需要更强语义时，通过 `POST /proposals` 把结果交回 Runtime：情绪解释、候选生成、记忆摘要、用户模型总结全部走 APPLY/REBASE/DISCARD。接口保持一致，Runtime 不被单一模型锁死。

---

## 10. 与宿主框架（AstrBot）的边界

| 归属 | 内容 |
|---|---|
| **宿主框架（不修改）** | 平台接入、账号/会话、主 LLM 调用、消息发送、角色卡/system prompt、API key 管理 |
| **本 Runtime sidecar** | 时间连续性、情绪、记忆、用户认识、未尽之事、候选意图、动机决策、边界与许可、投递协议 |
| **通信方式** | HTTP（`api.py`），异步 outbox claim/lease/ack |

Runtime 不 import `AstrBot` 的任何模块，也不修改其代码。宿主只需实现两个端口：

- `Renderer.render(payload) -> str`：主 LLM 把意图渲染成可见文本
- `Transport.send(text, conversation_id) -> Mapping`：真实发送

两者在测试里分别用 `EchoRenderer` 与 `NullTransport` 替代。

---

## 11. 已知边界与后续工作

第一版刻意简化但**不省略主要模块**：

- 检索是词面重合而非 embedding（`MemoryStore.retrieve` 是接缝；embedding 未就绪时本方案即为降级路径）
- 用户模型是"简化分层贝叶斯"：对角精度近似（Laplace），不是完整 MCMC / 变分推断
- 事件评价与边界检测是规则 + 词表，语义纠错依赖强 API 的 REINTERPRET
- `invalidate_when` 的匹配是关键词级，不是语义级
- 后台巩固目前由调用方驱动（`memory.consolidate()`），未内置常驻 worker 线程
- 尚无 Prometheus 指标导出（`/health`、`/maintenance/verify`、`/outbox` 已提供足够的数据结构）

---

## 12. 快速自检

```powershell
cd runtime
python -m pytest -q                                          # 期望：全部通过
python -m companion_runtime.cli --base-dir ./data health
python -m companion_runtime.cli --base-dir ./data backup --keep 7
python -m companion_runtime.cli --base-dir ./data verify
```
