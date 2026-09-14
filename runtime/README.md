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

## 0. v0.2：即时演出层与持久认知层分离

架构补丁 v0.2（`PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md`）把 Runtime 明确拆成两个**时间尺度**，而不是两个模型：

```text
【即时演出层】 当前用户原话 + 对话上下文 + 宿主人格 + 已有 Runtime 状态
             → 宿主主 LLM → 本轮即时反应

【持久认知层】 本轮交互沉淀
             → 记忆 / 情绪余波 / 用户模型 / 未尽之事 / 候选意图 / 后验重解释
             → 影响未来轮次（跨小时、跨天）
```

两条结论直接改变了部署形态：

1. **当前这一轮的即时理解与情感表现，本来就应该由主 LLM 完成。** 主 LLM 在当前轮已经能看到宿主设定、上下文、用户原话和 Runtime 状态，不需要 Runtime 先同步调用一个 2B 模型把"这句话是失落 0.46"算出来再喂给它。那条路径既昂贵，又重复了主 LLM 已有的能力。
2. **本地 2B 生成式模型不再是标准依赖。** 它已从标准架构移除，只作为可选 `SemanticProvider` 的一种实现保留（见第 7 节）。弱 VPS 上不再需要常驻权重、不再需要 llama.cpp 进程、不再需要推理队列。

> **Runtime 不依赖任何生成式模型也能完整运行。** 默认配置（`semantic.provider = "disabled"`）下：显式事件由规则表做粗粒度结算，模糊事件记为 `unresolved`，心理解释退化为确定性代码模板，记忆检索是词法重合，用户模型是在线贝叶斯。以上全部无需任何模型，且都有自动测试守着（`tests/test_acting_layer_independence.py` 会在 ingest 路径上直接拦断 `socket.connect`，证明这一轮不会拨出任何网络连接）。

主 LLM 与 Runtime 的分工一句话：

> **主 LLM 管"现在这一刻怎么活"；Runtime 管"活过以后留下什么"。**

章节映射（补丁 §0–§33 → 代码 → 测试 → 状态）另见 `docs/PATCH_V0.2_MAPPING.md`，其中"未实现"条目被逐条诚实标注。

---

## 1. 目录结构

```text
runtime/
├── pyproject.toml                 打包与 pytest 配置（src 布局）
├── README.md                      本文件
├── docs/
│   └── PATCH_V0.2_MAPPING.md      补丁 v0.2 章节 → 代码/测试/状态 映射表
├── src/companion_runtime/
│   ├── __init__.py                版本号与 API 版本
│   ├── typing.py                  全部枚举与跨模块记录（RawEvent / CandidateIntent / ...）
│   ├── utility.py                 数学、时间、文本工具（sigmoid / softplus / softmax / decay）
│   ├── config.py                  分层 dataclass 配置 + TOML/JSON/环境变量 + 脱敏（含 SemanticConfig）
│   ├── db.py                      SQLite 连接、事务/保存点、全部表结构、列迁移（SCHEMA_VERSION = 2）
│   ├── eventlog.py                append-only 原始事件日志（+ 可选 JSONL 镜像）
│   ├── projections.py             当前投影读写（runtime_state / 记忆 / 候选 / outbox / event_semantics ...）
│   ├── semantic.py                【v0.2】粗粒度语义结算：classify_event / 锚点表 / 歧义否决 / unresolved
│   ├── providers.py               【v0.2】可选 SemanticProvider 端口与四种实现 + 深层刷新契约
│   ├── deep_refresh.py            【v0.2】深层认知刷新：触发判定 / 请求组装 / 建议 grounding
│   ├── local_llm.py               本地小模型客户端（Qwen 系微调 + llama.cpp 端点，**仅被 providers.py 使用**）
│   ├── emotion.py                 事件评价 → 情绪动力学 → 心理解释（长期底色模板 + 缓存）
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
│   ├── context.py                 临时上下文组装 + 主 LLM prompt 块渲染（含 PRIORITY_PREAMBLE）
│   ├── authorize.py               边界/预算/文本硬门禁
│   ├── delivery.py                claim → render → send → observe 投递服务
│   ├── maintenance.py             WAL checkpoint、完整性校验、备份、恢复
│   ├── api.py                     FastAPI 应用
│   └── cli.py                     命令行入口
└── tests/                         单元 / 集成 / 耐久性测试
    ├── test_semantic.py                       【v0.2】锚点表、歧义否决、相关性、unresolved
    ├── test_providers.py                      【v0.2】四种 provider、契约校验、降级、密钥卫生
    ├── test_deep_refresh.py                   【v0.2】触发优先级、grounding、刷新编排、历史不被重写
    ├── test_cognition_api.py                  【v0.2】/cognition/refresh 与 /cognition/backlog 契约
    ├── test_acting_layer_independence.py      【v0.2】ingest 路径不依赖任何生成式模型（结构性证明）
    └── ...
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
| `refresh [--now ISO] [--force]` | **【v0.2】** 跑一次低频深层认知刷新，打印触发原因、grounding 违规与落地计数 |
| `backlog [--limit N]` | **【v0.2】** 列出 Runtime 有意没有解释的 unresolved 事件 |
| `verify [--json]` | 完整性 + 结构一致性检查（失败退出码 3） |
| `checkpoint --mode PASSIVE\|FULL\|RESTART\|TRUNCATE` | 把 WAL 合并回主库文件 |
| `backup [目标] [--keep N]` | 写一份一致快照（`VACUUM INTO`） |
| `restore <快照>` | 用快照覆盖数据库（先校验，拒绝覆盖运行中的库） |
| `recover [--backup-dir DIR] [--run]` | 打印恢复方案；`--run` 顺带执行一次维护 |
| `config` | 打印脱敏后的有效配置 |
| `health` | 打开数据库并打印健康摘要 |

全局参数：`--config <file.toml|file.json>`、`--base-dir DIR`、`--log-level LEVEL`。

> v0.2 新增两个命令：`refresh`（跑一次深层认知刷新）与 `backlog`（列出未解释的事件）。两者都不需要模型：没有配 provider 时 `refresh` 会以 `provider_unavailable` 正常退出。

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
wal = true                    # 保持 true；见第 10 节
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

[semantic]                    # 【v0.2】两个时间尺度的策略（全部有默认值）
provider = "disabled"         # disabled | local_cpu | local_gpu | remote_api
settle_on_ingest = true       # 入口处跑粗粒度规则结算（纯规则，无模型）
deep_refresh_enabled = true   # 允许低频深层认知刷新
unresolved_backlog_threshold = 8
unresolved_max_age_hours = 72.0
deep_refresh_min_interval_seconds = 3600.0
deep_refresh_idle_hours = 12.0
max_operations_per_refresh = 12
template_fallback = true
interpretation_max_age_seconds = 21600.0
```

环境变量（双下划线表示层级）：

```powershell
$env:CR_SERVER__PORT = "9000"
$env:CR_DRIVE__COOLDOWN_SECONDS = "600"
$env:CR_STORAGE__DATABASE_PATH = "D:\companion\runtime.sqlite3"
$env:CR_STORAGE__DATABASE_PATH = ":memory:"     # 内存库（测试/演示）

# 【v0.2】语义端口（默认关闭，不写这两行就是零模型运行）
$env:CR_SEMANTIC__PROVIDER = "disabled"          # 等价于 CR_SEMANTIC_PROVIDER
$env:CR_SEMANTIC__SETTLE_ON_INGEST = "true"
```

### 3.1 SemanticConfig 参考表（v0.2）

| 字段 | 默认值 | 含义 | 生产消费者 |
|---|---|---|---|
| `provider` | `"disabled"` | 要构建的 provider。`disabled` 是标准设置 | `Runtime.__init__` → `build_provider()` |
| `settle_on_ingest` | `true` | 入口路径上是否跑粗粒度规则结算 | `Runtime.process_user_message` |
| `deep_refresh_enabled` | `true` | 是否允许低频深层刷新 | `Runtime.deep_refresh`（总开关，关掉就立刻返回 `disabled`） |
| `unresolved_backlog_threshold` | `8` | 积压多少条 unresolved 才够触发一次刷新 | `deep_refresh.evaluate_triggers`（触发规则 `unresolved_backlog`） |
| `unresolved_max_age_hours` | `72.0` | 超过这个年龄的 unresolved 不再支撑刷新（原始事件仍然保留） | **预留**：只作为 `semantic.resolve_backlog()` 的参数默认值，而该函数目前无生产调用方 |
| `deep_refresh_min_interval_seconds` | `3600.0` | 两次刷新之间的最小间隔 | `deep_refresh.evaluate_triggers`（**优先级最高**：不满足就直接 `min_interval_not_elapsed`） |
| `deep_refresh_idle_hours` | `12.0` | 系统空闲多久才允许投机性刷新 | `deep_refresh.evaluate_triggers`（触发规则 `idle_refresh`） |
| `max_operations_per_refresh` | `12` | 单次刷新最多应用多少条 grounded 操作 | `Runtime.deep_refresh`：同时用作请求里 unresolved 的条数上限、`source_event_ids` 上限与 `ground_suggestions` 的操作上限 |
| `template_fallback` | `true` | 没有缓存解释时退回确定性模板。**不建议关掉** | **预留**：模板兜底当前无条件生效（不可关） |
| `interpretation_max_age_seconds` | `21600.0` | 一份心理解释多久后算 stale | **预留**：解释缓存的陈旧判定实际由 `task.explain_cache_ttl_seconds`（1800）承担 |

> 只有 `unresolved_max_age_hours`、`template_fallback`、`interpretation_max_age_seconds` 三项还处于"已进配置层、语义明确、但尚无生产代码读取"的状态；其余七项都在运行时真实生效。

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

v0.2 之后这条约束多了一层意义：**"当时没理解"不会让证据消失**。模糊事件被记为 `unresolved` 时，`raw_events` 里的原文一字不动，因此几小时后出现新证据时可以重新解释它（"追夫火葬场"路径）。

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

敏感度分级（`protocol.TASK_SENSITIVITY`）：浅层标签 `low`（预算 50 版）→ 事件评价 `medium`（6）→ 候选生成/情绪解释 `high`（2）→ 主动消息成品 `critical`（0，用户开口必须重协调）。**深层刷新 `deep_refresh` 也标为 `low`（预算 50）**：一次刷新推理的是"旧事件"，它的结论不该因为角色又活了几轮就被 REBASE 掉 —— 那正好会抹掉这次刷新刚发现的东西。

### 4.3 lazy_tick：唯一时间入口

**任何入口都必须先 `lazy_tick(now)`**（用户消息、内源唤醒、后台结果、发送回执）。它在一次事务里完成：

背景心境自然恢复 → 情绪事件衰减 → I/R/P 推进 → 冷却 → 记忆激活衰减 → 未尽之事时间状态 → 边界过期 → 候选期限 → 派生 `allow_proactive`。

因此"用户离开 8 小时"真的会被算成 8 小时。注意 `endogenous_round()` 自身会调用 `lazy_tick`，调用方**不要**先 tick 到同一时刻再唤醒 —— 那会让危险率积分区间为 0，永远不行动（集成测试专门覆盖了这一点）。

### 4.4 情绪：数值负责动力学，语言负责语义

v0.2 之后，前台路径的入口多了一层**粗粒度结算**（无模型，纯规则）：

```text
事件 → classify_event()              显式锚点 → 粗粒度结算（方向 / 强度带 / 置信度 / 来源）
                                     （无锚点或有歧义否决 → None，记为 unresolved）
     → settlement_to_evaluation()    强度带 → 数值（uncertainty = 1 − confidence，source = "coarse_rule"）
     → apply_new_emotion_events()    结合价值观、背景心境、用户模型、既有情绪事件计算数值变化
     → EmotionExplainer              把结构化状态翻译成"长期底色"（缓存或代码模板）
```

背景心境 = `valence / arousal / stability`；情绪影响事件 = 带 `decay_rate` 的衰减事件，`semantic_label = null` 是合法状态（知道"这是中等负向影响"，但暂时不知道叫什么）。

**规则层从不给情绪命名**：`CoarseSettlement.semantic_label` 恒为 `None`。命名（"嫉妒 / 委屈 / 懊恼"）属于低频深层刷新的职责，不属于每轮。

强度带的代表值（中点，不是测量值）：`negligible 0.05` / `low 0.20` / `medium 0.40` / `medium_high 0.62` / `high 0.85`。

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

### 4.13 上下文注入是临时的，而且是"背景"不是"指令"

`context.build()` 产出**一次性** bundle：长期心理状态（自然语言）+ 工作局势 + 最终意图 + 少量激活记忆 + 表达边界 + 时间连续性。`render_block()` 渲染成主 LLM 的 prompt 块，**不包含裸浮点数**（模型对心理文本远比对 `anger = 0.72` 敏感）。

v0.2 改了两件事：

- 心理区块标题从"当前心理状态"改成 **`【进入本轮前的长期状态（背景）】`**（`context.SECTION_PSYCH`）。它的语义是"我从哪来"，不是"这句话该怎么感觉"。
- 每个 prompt 块开头固定注入 `context.PRIORITY_PREAMBLE`，声明优先级顺序，并明确"如果当前用户原话与下面的长期状态不一致，以当前用户原话为准；你的即时反应由你自己根据当前语境完成"。

本轮结束后全部丢弃。永久对话历史只保存用户可见消息与 assistant 可见消息（`assert_ephemeral` 守住这个不变量）。

当角色在用户开口前几秒已 committed，block 会带上 `lead_seconds_before_user_message`，让主 LLM **可以**自然地说"你居然刚好发来了"，但不强迫。

### 4.14 心理解释：低频缓存 + 代码模板兜底

心理解释（`EmotionExplainer`）在 v0.2 里**不再是实时必需模块**，它是"持久心理状态的低频语义压缩器"：

```text
已有心理解释缓存？
├─ 有（且未 stale）→ 直接用
└─ 无 → 代码模板（确定性，永远可用）
```

- **情绪解释器可选，情绪状态本身不可选。**
- 模板只描述**长期底色**，不说当前这句话该怎么反应。例如：
  - `进入本轮之前，整体底色偏负向，还有没消化完的东西。`
  - `这段时间情绪基调偏低，表达会比平常收着。`
  - `长期表达风格偏向克制，不太主动施压。`
  - `底色是收着的，话不多但留有余地。`
- 模板按 `mood.valence`（±0.12 阈值）、`approach_impulse` vs `restraint`（+0.15）、`pressure`（0.45）、`restraint`（0.68 / 0.35）分支，每个分支有一组同义说法，随机选一条。
- 缓存带 key（`EmotionExplainer.cache_key`）：背景心境、冲动、节制、压力取一位小数拼成 `v..|a..|i..|r..|p..`。key 不变就复用，变了才重新算。
- provider 可用时，`_render()` 会先问 provider 要一段更细腻的语言；拿不到（或字段不全）就退回模板，**永远有输出**。

---

## 5. 两层职责与优先级（v0.2）

### 5.1 即时演出层 = 宿主主 LLM

负责回答：**"用户现在说了这句话，我这一刻怎么反应？"**

输入：

```text
宿主角色设定（人格、语气、世界设定）
+ 最近对话上下文
+ 当前用户原话
+ 当前确定事实
+ 已有 Runtime 持久状态（prompt 块注入，本轮结束即丢弃）
+ 必要的心理状态缓存（长期底色）
```

输出：**当前轮可见回复**。

关键点：主 LLM 当场表现出"有点舍不得"，**不等于** Runtime 必须立刻写入 `persistent_sadness = 0.7`。当场表现是演出，Runtime 状态是持续认知，**两者不是同一权限**。

### 5.2 持久认知层 = Runtime

负责回答：**"这件事结束以后，它在我身上留下了什么？"**

处理对象：情绪余波、背景心境、长期记忆、未尽之事、用户交互证据、用户模型、候选意图、主动动力、后验重解释。

Runtime 的任务**不是**"每条消息都准确判断这是失落 0.43 / 焦虑 0.27"，而是：

```text
这件事是否值得留下？
是否形成未尽之事？
是否改变背景心境？
是否留下用户偏好证据？
是否需要以后重新解释？
是否影响未来主动性？
```

它**不要求**每一轮都实时语义完备。

### 5.3 权威边界

| 谁 | 负责 | 不负责 |
|---|---|---|
| **主 LLM**（宿主） | 当前消息即时理解、当前情绪演出、当前语气、当前自然语言反应 | 直接写 Runtime 情绪状态；修改长期记忆事实；修改用户模型数值；绕过边界状态机；直接决定是否内源主动 |
| **Runtime**（本工程） | 跨轮心理连续性、背景心境、情绪余波、长期记忆、用户交互模型、未尽之事、主动动力、候选意图、动机决策、协议一致性、粗粒度语义结算 | 生成台词；替代主 LLM 理解当前这一句话；把"当场演出"当成状态写入 |

### 5.4 优先级（必须遵守）

```text
宿主最高层设定 / 安全约束
> 当前用户原话
> 当前确定事实
> 显式边界
> Runtime 持久心理状态
> 心理解释缓存
> 主 LLM 自然发挥
```

代码落点：`context.PRIORITY_PREAMBLE`，随每个 prompt 块注入。

- 最重要的一条：**当前用户原话与当前事实高于 Runtime 旧心理缓存。**
- 示例：Runtime 缓存写着"最近整体有些失落，表达偏克制"，用户突然说"其实我就是回来陪你的哈哈" —— 主 LLM 本轮可以立刻惊喜、放松、开心，**不能**因为旧缓存还写着"失落"就继续机械表现低落。这句新话是否让持久背景心境改变，由 Runtime 之后结算。
- 当前实现把第 5–6 级合并成一句"这里的长期状态"写进 preamble（`宿主设定与安全约束 > 当前用户原话 > 当前确定事实 > 显式边界 > 这里的长期状态`）。**逐级拆分尚未落到 prompt 文本里**，映射表中标注为部分实现。

---

## 6. 语义结算与 unresolved（v0.2）

### 6.1 为什么需要它

补丁 v0.2 取消了"每条用户消息都必须立刻得到准确事件评价"的要求，改成：

```text
高置信事件   → 粗粒度结算
低置信事件   → unresolved（保留原始事件）
重要事件     → 等低频深层刷新
普通低价值事件 → 可以永远不深挖
```

理由是**不对称的代价**：一个错误的结算会静默污染角色的长期状态，而一个 `unresolved` 只损失"早点结算"的机会，并且随时可以重来。所以 `semantic.classify_event()` 的契约是**单边的**：证据不明确就回答 `None`。

### 6.2 `classify_event()` 的判定规则

按顺序执行，任何一步不满足就返回 `None`（→ unresolved）：

1. **空文本**：`text.strip()` 为空 → `None`。
2. **非对话事件**：`event_type` 不在 `{user_message, assistant_message}` → `None`；`actor == "system"` → `None`。
3. **歧义否决**：在原文里按顺序找第一个命中的 `AMBIGUITY_MARKERS`（见 6.3）。命中即视为"这句话没有把自己承诺给某一种读法"。
4. **强锚点豁免**：只有当文本含极少数**明确陈述事实或感受**的强锚点时，命中否决标记仍可结算（当前豁免表：`去世 / 过世 / 被辞 / 被裁 / 失业 / 分手 / 离婚 / 确诊 / 手术 / 谢谢你 / 对不起 / 抱歉 / 我喜欢你 / 我很开心 / 我很高兴 / 我很难过 / 我很失望`）。否则**一旦命中否决标记就直接 `None`**。
5. **锚点表扫描**：按顺序遍历 `ANCHORS`（12 条），先对原文匹配，再对"剥掉中性填充词"后的文本匹配（`FILLER_TOKENS`：我今天/我现在/我刚刚/我刚才/今天/现在/刚刚/刚才/其实/真的/确实/感觉/觉得）。命中即产出 `CoarseSettlement`。
6. **钝性拒绝**：`BLUNT_REFUSALS`（`不行 / 不可以 / 我拒绝 / 不要这样`）且无否决标记 → 结算为 `explicit_refusal`（负向 / `medium` / 0.70）。
7. **其余**：`None` → unresolved，reason 记为 `no_explicit_anchor`。

锚点表覆盖的粗粒度类别（对应补丁 §10 的八类方向）：

| `settlement_source` | 方向 | 强度带 | 置信度 | 例 |
|---|---|---|---|---|
| `explicit_positive_feedback` | `+` | `medium_high` / `medium_high` | 0.85 / 0.80 | `谢谢你`、`被你安慰到`、`好多了` |
| `explicit_affection` | `+` | `high` | 0.85 | `我喜欢你`、`想你` |
| `explicit_good_news` | `+` | `high` | 0.80 / 0.78 | `面试过啦`、`考上了`、`拿到 offer` |
| `explicit_repair`（和解 / 道歉） | `+` | `medium` | 0.75 | `对不起`、`是我不好` |
| `explicit_joy` | `+` | `medium_high` | 0.80 | `很开心`、`好开心` |
| `explicit_need_for_space`（边界 / 需要空间） | `-` | `low` | 0.72 | `想自己待着`、`需要一点空间` |
| `major_loss`（重大事件） | `-` | `high` | 0.85 | `去世`、`过世`、`葬礼` |
| `major_setback`（重大事件） | `-` | `high` | 0.85 | `被裁`、`失业`、`分手`、`确诊`、`手术` |
| `explicit_conflict`（冲突） | `-` | `high` | 0.80 | `你根本不懂`、`我讨厌你` |
| `explicit_distress`（明确负向） | `-` | `medium_high` | 0.80 | `很难过`、`很失望`、`想哭` |
| `explicit_refusal`（拒绝） | `-` | `medium` | 0.75 / 0.70 | `我不想聊这个`、`不行` |

两点刻意设计：

- **剥离填充词只允许剥离中性词。** 剥离任何带评价的词（比如"其实"以外的程度词）都会把一句含糊的话变成确定的话 —— 那正是本模块要防止的失效模式。测试 `test_no_anchor_collides_with_an_ambiguity_marker` 守着"锚点不得与否决标记冲突"。
- **命名情绪不是规则层的事。** `CoarseSettlement.semantic_label` 恒为 `None`；`test_anchor_settles_with_the_expected_reading` 里有一条断言专门钉住这一点。

### 6.3 歧义否决表（`AMBIGUITY_MARKERS`）

| 标记 | 原因 |
|---|---|
| `算了` | `hedged_withdrawal` |
| `也没什么` / `没什么` | `minimising` |
| `随便` / `都行` / `无所谓` | `indifferent` |
| `可能` / `也许` / `大概` / `不知道` / `不清楚` | `uncertain` |
| `还好` / `一般` | `mild` |
| `再说吧` / `看情况` | `deferred` |

> **「算了，也没什么。」必须保持 unresolved。** 补丁 §11 与 §31 把这个字符串点名为典型例子：Runtime 不能瞎猜，只记录 `semantic_status = unresolved` + `potential_relevance`，原始事件照原样保留。测试 `test_exact_patch_example_is_unresolved` 直接断言 `classify_event("算了，也没什么。") is None`；`test_every_ambiguity_marker_is_covered_by_a_negative_case` 则保证"以后有人往否决表里加标记而不加测试"会立刻失败。

### 6.4 unresolved 的语义：可以晚点懂，但不能丢证据

一条 unresolved 事件会发生什么：

| 层面 | 行为 |
|---|---|
| 原始事件 | 照常写入 `raw_events`（append-only，一字不改） |
| 语义投影 | `event_semantics` 新增一行：`semantic_status='unresolved'`、`potential_relevance`、`unresolved_reason='no_explicit_anchor'` |
| 情绪 | **本轮不产生任何情绪余波**（`outcome.emotion_event_ids == []`）—— 还没理解的东西不该改变长期心境 |
| 工作局势 | 仍然写入 `kind='fact'`（"用户说：……"），但**不会**写成 `kind='inference'` 的关系信号 |
| 接口 | `POST /events` 的 `outcome` 里带 `semantic_status` / `potential_relevance` / `appraisal_source="deferred"` |
| 运维可见性 | `GET /health` 的 `semantics` 块给出 unresolved 计数（**积压在增长是正常运行，不是错误**） |

配套的 `MessageOutcome` 字段：

| 字段 | 取值 | 含义 |
|---|---|---|
| `appraisal_source` | `coarse_rule` | 由 Level 1 规则表结算 |
| | `deferred` | 记为 unresolved，等以后 |
| | `rule` | 旧词表路径（仅供仍要求逐轮读数的调用方），默认值 |
| `semantic_status` | `resolved` / `unresolved` | 这条事件是否已经有了持久的语义读法 |
| `potential_relevance` | `low` / `medium` / `high` | 刷新队列的优先级，**只影响排序，从不变更状态** |

### 6.5 `potential_relevance()`：便宜的排队优先级

```text
长度 ≤ 3                    → low
命中 HIGH_RELEVANCE_HINTS   → high   （关系/喜欢/讨厌/离开/分手/以后/永远/一直/为什么/是不是/你觉得）
有 live 未尽之事 且 距上次交流 ≥ 6h → medium
命中 MEDIUM_RELEVANCE_HINTS → medium （今天/明天/面试/工作/考试/答应/约/等/忙）
长度 ≥ 24                   → medium
否则                        → low
```

### 6.6 `settlement_to_evaluation()`：粗粒度 → 数值

```text
impact       = 强度带代表值（中点）
activation   = 强度带代表值 × 0.8
uncertainty  = 1 − 置信度          ← 低置信度的结算会自动阻尼自己在下游的影响
confidence   = 结算置信度
source       = "coarse_rule"
relation_signal = 由 settlement_source 映射（appreciation / closeness / good_news / repair / loss /
                  bad_news / distance / sorrow / neutral）
responsibility  = "unclear"
```

### 6.7 `resolve_backlog()`：把积压分成"还值得刷"与"该老了"

```python
resolve_backlog(unresolved, now=..., max_age_hours=72.0, limit=20) -> (live, stale)
```

按 `potential_relevance` 权重（high → medium → low）再按时间新→旧排序，取前 `limit` 条；超过 `max_age_hours` 的进 `stale`（**只是不再支撑一次刷新，原始事件仍然在库里**）。

> 诚实标注：`resolve_backlog()` 目前**只有测试覆盖，没有生产调用方**。第 8 节的深层刷新链已经落地，但它读积压用的是 `SemanticProjection.list_unresolved()`，没有走这个函数 —— 因此 `unresolved_max_age_hours`（"太老就不再支撑刷新"）这条策略**当前没有被执行**。

---

## 7. SemanticProvider：可选强语义端口（v0.2）

### 7.1 它是什么

`providers.SemanticProvider` 是一个 `runtime_checkable` Protocol，只做两件低频的事：

```python
available() -> bool
deep_refresh(request, *, timeout_s=None) -> DeepRefreshSuggestions | None
explain_state(payload, *, state_key="") -> dict[str, str] | None
health() -> dict[str, Any]
```

**它不是 Runtime 的必需依赖。** 三条契约对所有实现成立：

1. **只建议，不写状态。** provider 提议，Reducer 决定 `APPLY / REBASE / DISCARD`。
2. **Fail-open。** 不可用、超时、连不上、JSON 畸形 → 返回 `None` 或 `degraded=True` 的建议集，**从不抛异常**。
3. **Secret-safe。** 远端 key 只从 `CR_SEMANTIC_API_KEY` 读取，永不落盘、永不进日志、永不出现在 `repr()` 或 `health()` 里（只报 `configured` / `not configured`）。key 只存在于一个闭包单元里，连 `vars()` 都取不到。

### 7.2 四种实现

| 实现 | `name` | 用途 | 备注 |
|---|---|---|---|
| `DisabledProvider` | `disabled` | **默认**。没有模型、没有网络、零延迟。`deep_refresh()` / `explain_state()` 都返回 `None`，调用方走确定性模板 | 这是完整的实现，不是一个错误路径 |
| `LocalCPUProvider` | `local_cpu` | 已经跑着 `llama.cpp`（或任何 OpenAI 兼容端点）的部署**可选**接回本地强语义 | `local_llm.LocalModelClient` 的薄适配器：只读取和复用，不修改客户端。默认 `LocalModelConfig(enabled=True)` |
| `LocalGPUProvider` | `local_gpu` | 权重跑在 GPU 上，但**线格式与契约完全相同** | 与 `LocalCPUProvider` 是同一份代码，只有上报的名字不同 —— 让 health 和日志一眼看出权重跑在哪 |
| `RemoteAPIProvider` | `remote_api` | 任意 OpenAI 兼容远端 | key 只从 `CR_SEMANTIC_API_KEY` 读；`available()` 需要 base_url + model + key 三者齐全 |

### 7.3 如何启用

选择顺序（`resolve_provider_name()`）：

```text
1. config 里的 semantic.provider（或 extras['semantic']['provider']）
2. 环境变量 CR_SEMANTIC_PROVIDER
3. disabled
```

```powershell
# 默认：零模型
$env:CR_SEMANTIC_PROVIDER = "disabled"

# 本地 llama.cpp（CPU）—— 需要先自己把端点跑起来
$env:CR_SEMANTIC_PROVIDER   = "local_cpu"
$env:CR_SEMANTIC_BASE_URL   = "http://127.0.0.1:8080/v1"
$env:CR_SEMANTIC_MODEL      = "qboss-2b"

# 本地 GPU
$env:CR_SEMANTIC_PROVIDER = "local_gpu"

# 远端强语义（key 只放环境变量，绝不写进 config 文件）
$env:CR_SEMANTIC_PROVIDER = "remote_api"
$env:CR_SEMANTIC_BASE_URL = "https://api.example.com/v1"
$env:CR_SEMANTIC_MODEL    = "some-strong-model"
$env:CR_SEMANTIC_API_KEY  = "<放在部署环境的密钥管理里，不要提交进仓库>"
```

**未知或缺失的名字一律回落 `disabled` 并打一条 warning**：`build_provider()` 被设计为永不抛异常，因此"配置写错了"最坏的结果是"没有强语义"，而不是 Runtime 起不来。

### 7.4 环境变量一览

| 变量 | 作用 | 默认 / 生效范围 |
|---|---|---|
| `CR_SEMANTIC_PROVIDER` | 选择实现：`disabled` / `local_cpu` / `local_gpu` / `remote_api` | `disabled` |
| `CR_SEMANTIC_BASE_URL` | 覆盖端点 base_url | `local_*` → `LocalModelConfig.base_url`（`http://127.0.0.1:8080/v1`）；`remote_api` → 空（必须显式给） |
| `CR_SEMANTIC_MODEL` | 覆盖模型名 | `local_*` → `qboss-2b`；`remote_api` → 空（必须显式给） |
| `CR_SEMANTIC_TIMEOUT_S` | 覆盖超时 | `local_*` → 覆盖 `explain_timeout_s`（默认 6.0 s）；`remote_api` → 覆盖 `timeout_s`（默认 30.0 s） |
| `CR_SEMANTIC_MAX_TOKENS` | 补全上限 | **仅 `remote_api`**，默认 1024 |
| `CR_SEMANTIC_API_KEY` | **仅 `remote_api`** 的 bearer token。只从环境读，不落盘、不入库、不进日志、不进 health | 无 |
| `CR_LOCAL_MODEL_ENABLED` | 显式关掉已被选中的本地 provider | 当 `local_cpu` / `local_gpu` 被显式选中且此变量**未设置**时，视为 `enabled=True`；显式设成 `0/false` 可再关掉 |
| `CR_LOCAL_MODEL_BASE_URL` | `LocalModelConfig.from_env()` 读取 | `http://127.0.0.1:8080/v1` |
| `CR_LOCAL_MODEL_NAME` | 同上 | `qboss-2b` |
| `CR_LOCAL_MODEL_API_KEY` | 同上（受保护的自建端点用） | 无 |

本地 provider 复用的其余 `LocalModelConfig` 默认值：`appraise_timeout_s = 1.2`、`explain_timeout_s = 6.0`（同时作为 deep refresh 的超时）、`max_tokens = 256`、`temperature = 0.0`、`cache_ttl_s = 900`、`chat_template_kwargs = {"enable_thinking": false}`（微调模型屏蔽思考分支，否则 JSON 会掉进推理通道）。

> **安全约定：不要写任何 API key 到 `runtime.toml`。** `RemoteAPIProvider` 会**故意忽略**配置对象里的 key —— Runtime 的配置是可序列化的、会被 `/config` 打印、会进日志，因此它永远不允许携带凭据。

### 7.5 两种调用形态

`EmotionExplainer` 通过 `getattr` 兼容两种 provider 接口，所以 v0.1 的 `explain(payload)` 端口和 v0.2 的 `explain_state(payload, state_key=...)` 都能接上：

```text
有 explain_state(payload, state_key=...) → 优先用它（provider 侧缓存与 Runtime 侧缓存对齐）
否则有 explain(payload)                   → 用旧的
都没有 / 返回 None / 字段不全             → 退回代码模板
```

`state_key` 由 `EmotionExplainer.cache_key_from_payload(payload)` 生成（`v..|a..|i..|r..|p..`），provider 侧缓存 TTL 900 s、上限 128 条；`health()["stats"]` 会报告 `cache_hits` / `explain_ok` / `explain_degraded`。

### 7.6 当前接线状态（诚实标注）

- ✅ **`explain_state()` 已接线**：`context.runtime_explanation()` 与 `POST /explain` 现在会通过 `context._optional_explanation_provider(runtime)` 把 provider 交给 `EmotionExplainer`。这个辅助函数只在该 provider **真的可用**（`available()` 为真且不抛异常）时才返回它 —— 因为 explainer 把"有 provider"理解为"先问它"，把一个挂着但连不上的 provider 交进去只会凭空多一次失败调用。默认 `DisabledProvider` 下它返回 `None`，心理解释照旧走模板。
- ✅ **`deep_refresh()` 已接线**：`Runtime.deep_refresh()` 会在低频路径上调用 `self.semantic_provider.deep_refresh(request)`，整条链路（触发 → 组装 → provider → grounding → Reducer）已实现并有测试，见第 8 节。
- ✅ `Runtime.__init__` 构造 `runtime.semantic_provider`（`build_provider(self.config)`），`GET /health` 报告它的 `health()`。

到这一步，`SemanticProvider` 的两个能力（`deep_refresh` 与 `explain_state`）都有真实的生产调用方了。

---

## 8. 深层认知刷新（v0.2）

### 8.1 定位

强语义能力的**唯一**用途是低频的"后来想明白"，而不是每条消息的即时演出：

```text
✅ 复杂旧事件重解释        复杂未尽之事识别
✅ 心理状态深层语言化      记忆高级整理
✅ 候选意图生成            用户模型语义总结

❌ 每条消息的即时演出（那是主 LLM 的事）
```

### 8.2 输入：`DeepRefreshRequest`

```python
unresolved_events      # 还没被理解的事件（含原文预览）
situation              # 当前工作局势
mood                   # 长期背景心境
active_emotions        # 活跃情绪影响事件
memories               # 激活记忆
unfinished             # 未结清的未尽之事
user_model_summary     # 用户交互模型的散文摘要
candidates             # 候选池里已有的意图
key_quotes             # 必须逐字保留的关键原文
```

### 8.3 输出：只有建议权的建议集

```json
{
  "reinterpretations": [],
  "psychological_interpretation": {},
  "candidate_intent_operations": [],
  "memory_suggestions": [],
  "unfinished_matter_suggestions": [],
  "user_model_evidence_suggestions": []
}
```

（六个集合也允许嵌在 `{"suggestions": {...}}` 里。）

`parse_deep_refresh()` 把模型回复当作**不可信输入**逐字段校验：

| 情况 | 处理 |
|---|---|
| payload 不是对象 | 返回 `None`（调用方按"没有结果"处理） |
| 某个字段类型不对 | **丢弃该字段**并写进 `reason = "invalid_fields:<字段名>"`，`degraded = True`；不做猜谜式修复 |
| 某个字段缺失 | 允许 —— **缺失不等于损坏** |
| 列表里混进非对象元素 | 该字段整个丢弃并记名 |
| 全部合法 | `degraded = False` |

于是一次"部分畸形"的回复仍然能贡献它写对的那部分，"完全畸形"则表现为 `degraded=True + reason="invalid_json"` 或 `"error:<异常类型>"`，而不会污染状态。

### 8.4 什么时候触发：`deep_refresh.evaluate_triggers()`

补丁 §21 的八个条件在 `deep_refresh.evaluate_triggers()` 里按**优先级顺序**实现，第一个命中的胜出（同时满足多个时，`reason` 只报告最有分量的那一个）：

| 优先级 | `reason` | 判定 | 对应配置 |
|---|---|---|---|
| — | `min_interval_not_elapsed` | **前置否决**：距上次刷新不足 `deep_refresh_min_interval_seconds` 就直接拒绝，任何理由都不例外 | `deep_refresh_min_interval_seconds` |
| 1 | `unresolved_backlog` | unresolved 数量 ≥ 阈值 | `unresolved_backlog_threshold` |
| 2 | `major_event` | 刚发生重大关系事件 | 调用方传 `major_event` |
| 3 | `matter_due` | 未尽之事到期 | 调用方传 `matter_due` |
| 4 | `candidate_pool_empty` | 候选池为空（`candidate_pool_size <= 0`） | 调用方传 `candidate_pool_size` |
| 5 | `proactive_without_grounding` | 想主动但语义依据不足（`wants_proactive and not proactive_grounded`） | 调用方传两个标志 |
| 6 | `history_may_be_wrong` | 历史解释可能错误 | 调用方传 `history_suspect` |
| 7 | `user_evidence_overturns` | 用户新证据推翻旧理解 | 调用方传 `user_evidence_overturns` |
| 8 | `idle_refresh` | 长时间没刷新（`hours_since_last_refresh >= deep_refresh_idle_hours`） | `deep_refresh_idle_hours` |
| — | `not_needed` | 一条都不命中 —— **这是最常见的结果** | — |

也就是说：阈值与空闲时间这两类信号 Runtime 自己算得出来，其余六类（重大事件、到期、候选池、主动意图、历史可疑、用户反证）需要调用方在 `trigger_context` 里给出。没有给出就当作"不成立"，**不会**被猜成"成立"。

### 8.5 完整管线：五个阶段，每个阶段都可以拒绝

```text
① 开关          deep_refresh_enabled?          否 → reason="disabled"
② provider      provider.available()?          否 → reason="provider_unavailable"
③ 触发          evaluate_triggers(...)         否 → reason=<未命中的原因>（force=True 可跳过）
④ 组装          build_request(runtime, ...)    只读，不写任何状态
⑤ 调用          provider.deep_refresh(request) 异常/None/空 → 降级返回，绝不抛出
⑥ grounding     ground_suggestions(...)        全部不合法 → reason="all_suggestions_ungrounded"
⑦ 落地          一条 Proposal → Reducer        APPLY / REBASE / DISCARD
```

运维入口（两者都是幂等且安全的，`ran=false` 是正常结果而非错误）：

```powershell
# CLI
companion-runtime refresh --now 2026-03-01T09:00:00Z
companion-runtime refresh --force          # 跳过触发检查，仅用于诊断
companion-runtime backlog --limit 50       # 看看积压里到底是哪几条

# HTTP
POST /cognition/refresh                    # body 可带 now / force / trigger_context 的八个信号
GET  /cognition/backlog?limit=50
```

返回的 `DeepRefreshOutcome` 会如实报告每一步的结果，便于把"没什么可做的"和"provider 挂了"和"模型答了一堆但全部 grounding 失败"区分开：

```json
{
  "ran": true,
  "reason": "applied",
  "trigger": {"should_refresh": true, "reason": "unresolved_backlog", "priority": 1, "unresolved_count": 9},
  "provider": "remote_api",
  "degraded": false,
  "operations": 3,
  "applied": {"reinterpretation": 1, "memory": 2},
  "violations": [{"kind": "memory", "reason": "ungrounded_sources", "sources": ["evt_deadbeef"]}],
  "settled_events": 2,
  "latency_ms": 8421
}
```

### 8.6 为什么它仍然只是建议：grounding + Reducer

```text
provider 产出建议集
   ↓
deep_refresh.ground_suggestions(suggestions, resolvable=runtime._is_resolvable, ...)
   ↓ 只留下"引用得到真实实体"的操作
Runtime.deep_refresh 把它装成一条 Proposal
   ↓
POST /proposals 等价路径：Reducer.process_proposal → APPLY / REBASE / DISCARD
   ↓
Reducer._apply_deep_refresh：逐条应用，并记账
```

**grounding 是这条链路的承重墙**（`ground_suggestions`）：

| 规则 | 行为 |
|---|---|
| 未知 `kind` | 只接受六种已知操作类型（`OPERATION_KINDS`），其余一律丢弃，不做"先留着"的处理 |
| 必须有来源的 `kind` | `reinterpretation` / `candidate_intent` / `memory` / `user_model_evidence` 必须给出 `sources`，且**每一个**都能被解析到真实实体（`_is_resolvable` 认 `evt_*` / `mem_*` / `cnd_*` 前缀与未尽之事、情绪事件 id） |
| 允许无来源的 `kind` | 只有 `psychological_interpretation` 豁免 —— 它概括的是整体状态，不是某一条事件 |
| 缺来源 / 来源解析不到 | 丢弃该操作，并记一条结构化 `violation`（`missing_sources` / `ungrounded_sources` / `not_a_mapping` / `empty_payload`），可在 outcome 与测试里统计 |
| 操作数量超限 | 截断到 `max_operations_per_refresh`，并记一条 `exceeded_max_operations` |

于是"模型编造了一段从未发生过的对话"的表现是**一条 violation**，而不是一条假记忆。

Reducer 侧还有三条安全规则：

1. 每条操作必须**已经过 grounding**，因此"凭空的记忆"进不来。
2. 一批里的坏操作只记进 `result.notes` 并跳过，**不会让整批作废** —— 一次"大部分读懂了"的刷新仍然有价值（`test_a_partially_bad_bundle_still_applies_the_good_part`）。
3. **只有被成功应用的操作真正引用过的事件**才会被标为已结算（把已应用操作的 `sources` 收进 `touched`，再逐个 `SemanticProjection.settle_from_deep_refresh`），而且该事件必须原本就有语义记录。因此：什么都没应用成功的刷新**不许**清空积压；一次只谈了一个事件的刷新也**不会**顺手把整批 unresolved 一起关掉（`test_only_referenced_events_leave_the_backlog`）。

操作类型（`kind`）与落地：

| `kind` | 落地 | 效果 |
|---|---|---|
| `reinterpretation` | `interpretation_versions` + `reappraisals` | 追加新解释版本（带 `supersedes_id`）与重估事件，**从不回写旧事件**（`test_history_is_not_rewritten_by_a_reinterpretation`） |
| `psychological_interpretation` | `emotion_explanations` 缓存 | 存入心理解释缓存，`source = "deep_refresh"`，下一轮可直接复用（`test_the_interpretation_cache_is_updated_and_reused`） |
| `candidate_intent` | 候选池管理器 | 走 `ADD / UPDATE / RETIRE / REINTERPRET`，非法操作逐条拒绝 |
| `memory` | 长期记忆 | 建议的记忆写入/修改 |
| `unfinished_matter` | 未尽之事 | 建议的未尽之事变更 |
| `user_model_evidence` | 用户模型证据 | 建议的交互证据 |

### 8.7 当前接线状态（诚实标注）

- ✅ **已实现且已接线**：`SemanticProvider` 端口、`DeepRefreshRequest` / `DeepRefreshSuggestions` 契约、`parse_deep_refresh()` 校验、`DEEP_REFRESH_SYSTEM_PROMPT`、四种 provider 的 `deep_refresh()` 实现（含超时降级与统计）、触发判定（`evaluate_triggers`）、请求组装（`build_request`）、grounding（`ground_suggestions`）、编排（`Runtime.deep_refresh`）、落地端（`Reducer._apply_deep_refresh`）、HTTP 与 CLI 入口，以及 `tests/test_deep_refresh.py` 与 `tests/test_cognition_api.py`。
- ⚠️ **没有内置自动调度**：`scheduler.py` 不引用深层刷新，也没有任何代码会自动调用 `Runtime.deep_refresh()`。触发**判定**是自动的，触发**调用**目前必须由宿主或运维发起（`POST /cognition/refresh` 或 `companion-runtime refresh`，例如放进宿主的每小时定时任务）。按补丁 §21 的字面要求，"何时触发"的条件表已经实现，但"由谁按定时器去问"这一环留给了宿主。
- ⚠️ **`semantic.resolve_backlog()` 仍无生产调用方**：刷新路径直接用 `SemanticProjection.list_unresolved()`；`resolve_backlog()` 目前只有 `test_semantic.py` 覆盖，`unresolved_max_age_hours` 也只作为它的参数默认值存在 —— 也就是说"超过 72 小时的 unresolved 不再支撑刷新"这条策略**当前没有被刷新路径执行**。
- ✅ **两个能力都已接线**：`deep_refresh()`（第 8 节）与 `explain_state()`（第 7.6 节）都有真实生产调用方。默认 `disabled` 时两者都安全地不做事。
- ⚠️ **`template_fallback` / `interpretation_max_age_seconds` 无消费者**（见第 3.1 节）。

---

## 9. HTTP API

所有端点都是薄壳：校验输入 → 委托 Runtime/Reducer → 返回 JSON。**没有任何端点直接写状态。**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活 + 紧凑活动摘要（版本、边界数、in-flight、outbox 统计、**语义结算统计**、**provider 快照**） |
| POST | `/events` | 追加事件；`user_message` 走完整前台路径，其余原样追加 |
| GET | `/events` | 读取原始事件（`conversation_id` / `event_type` / `limit` / `newest_first`） |
| GET | `/events/{id}` | 单条事件 + 其解释版本 |
| GET | `/context` | 组装临时上下文 bundle |
| POST | `/context/render-block` | 只渲染 prompt 块 |
| POST | `/explain` | 当前第一人称心理状态 |
| POST | `/cognition/refresh` | **【v0.2】** 跑一次低频深层认知刷新。可带 `now` / `force` / 八个触发信号；返回 `DeepRefreshOutcome`（`ran=false` 是正常结果） |
| GET | `/cognition/backlog` | **【v0.2】** 列出被有意留在 unresolved 的事件 + 统计（`limit`） |
| GET | `/schedule` | 下一次内源唤醒计划 + 当前是否允许派发 |
| POST | `/tick` | 显式执行 `lazy_tick` |
| POST | `/endogenous` | 跑一次内源主动轮，返回完整决策 |
| POST | `/proposals` | 提交后台模型结果 → APPLY/REBASE/DISCARD（`task_type="deep_refresh"` 走深层刷新处理器） |
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

> v0.2 新增两个端点：`POST /cognition/refresh`（触发一次深层刷新）与 `GET /cognition/backlog`（查看积压）。此外 `GET /health` 的 `semantics` 块给出计数。

OpenAPI 文档：`http://127.0.0.1:8787/docs`、`/openapi.json`。

### 9.1 `GET /health` 响应示例

零模型部署（默认配置）：

```json
{
  "status": "ok",
  "runtime_version": "0.1.0",
  "state_version": 42,
  "runtime_id": "companion",
  "now": "2026-03-01T09:00:00+00:00",
  "last_tick_at": "2026-03-01T08:59:58+00:00",
  "allow_proactive": true,
  "active_boundaries": 1,
  "open_unfinished": 2,
  "active_candidates": 3,
  "in_flight_attempts": 0,
  "outbox": {"pending": 0, "leased": 0, "sent": 5, "failed": 0},
  "semantic_provider": {
    "provider": "disabled",
    "available": false,
    "enabled": false,
    "reason": "disabled"
  },
  "semantics": {
    "by_status": {"resolved": 12, "unresolved": 3},
    "by_relevance": {"low": 9, "medium": 4, "high": 2},
    "unresolved": 3
  },
  "raw_events": 15
}
```

接了远端强语义时，`semantic_provider` 块变成（注意 **key 只报"配没配"，永远不回显**）：

```json
{
  "provider": "remote_api",
  "available": true,
  "base_url": "https://api.example.com/v1",
  "model": "some-strong-model",
  "api_key": "configured",
  "stats": {
    "deep_refresh_calls": 0,
    "deep_refresh_ok": 0,
    "deep_refresh_degraded": 0,
    "explain_calls": 4,
    "explain_ok": 4,
    "explain_degraded": 0,
    "cache_hits": 11
  },
  "cache_entries": 2
}
```

怎么读这两个块：

- `semantics.unresolved` 增长是**正常运行**：模糊事件本来就该先挂着。
- `by_relevance` 里的 `high` 是"以后更值得回头看一眼"的那些。
- `semantic_provider.available = false` 配上 `provider = "disabled"` 是默认状态，不是故障。
- 本地 provider 的 health 里还会多一个 `client` 子块（被包裹的 `LocalModelClient` 自己的快照）。

### 9.2 宿主接入顺序（推荐）

```text
1. 收到平台消息      → POST /events {event_type: user_message, content}
                       响应里的 outcome.semantic_status 告诉你这条是否已结算
2. 组装本轮 prompt   → GET  /context/render-block   （注入后即丢弃）
3. 主 LLM 生成回复   → 直接发给用户（Runtime 不阻塞前台）
4. 投递 worker 循环  → POST /outbox/claim（owner 固定）
                       kind=render → 用主 LLM 渲染 → POST /render
                       kind=send   → 真实发送       → POST /delivery
5. 用户后续反应      → POST /observations，或 /delivery 带 reaction
6. 后台模型结果      → POST /proposals（带 based_on_version 与 source_event_ids）
7. 定时维护（每小时）→ POST /maintenance/tick
```

第 1 步与第 3 步之间**没有任何模型调用**：Runtime 的入口路径只做 `lazy_tick`、原始事件落库、硬边界规则、机械性工作局势更新、粗粒度结算。这就是补丁 §28 说的"关键路径预算"。

---

## 10. 持久化、WAL、事务与蓝屏恢复

这一节是**硬约定**，不是建议。

### 10.1 运行参数

| 设置 | 值 | 原因 |
|---|---|---|
| `journal_mode` | **WAL** | 读不阻塞写、写不阻塞读 —— HTTP 服务与认知轮可同时进行 |
| `synchronous` | **NORMAL** | WAL 下使"已 COMMIT"的事务对**进程崩溃**持久，同时兼顾弱 VPS 的写入开销 |
| `busy_timeout` | 5000 ms | 短暂争用时等待而不是立刻报错 |
| 事务 | 每次变更 `BEGIN IMMEDIATE` | 单写者 + 写前取锁，避免升级死锁 |
| 嵌套 | `SAVEPOINT` | 一个维护操作可以与认知轮处在同一个原子单元里 |

### 10.2 崩溃与断电语义（明确区分）

实际只有两种情况，**都不是"数据库损坏"**：

| 场景 | 结果 |
|---|---|
| **进程被杀 / kill -9 / 服务崩溃**（无断电） | 已 `COMMIT` 的事务**全部保留**。WAL 中的提交记录已落盘，重连时自动回放 |
| **突然断电 / 蓝屏**（`synchronous=NORMAL`） | 可能丢失**最后若干个已提交事务**；但数据库**永不损坏**。SQLite 校验 WAL 帧校验和，回放到最后一个完整帧，丢弃"被撕开"的尾部 |
| 半途中断的写入 | 不可能产生"半轮认知结果"：整个认知轮在同一个事务里，要么全在，要么全不在 |

> 想要断电也零丢失，把 `synchronous` 改成 `FULL`（每次提交都 fsync），代价是写入延迟更高。第一版按架构文档选择 `NORMAL`：**可用性 > 推理速度**，且崩溃恢复总是安全的。

### 10.3 自动恢复流程

无需人工干预，下次连接时 SQLite 自动完成：

```text
1. 发现 <db>-wal
2. 校验每一帧 checksum，回放所有完整帧
3. 丢弃损坏的尾帧
4. 数据库停留在最后一个完整事务的状态
```

因为回放只在 WAL 存在时需要，运维风险点是 **WAL 无限增长**，所以需要定期 checkpoint。

### 10.4 checkpoint

```powershell
companion-runtime checkpoint --mode TRUNCATE
```

- `TRUNCATE`（默认）：合并并**把 WAL 截断到 0 字节**，推荐定期执行
- `PASSIVE` / `FULL` / `RESTART`：不同强度的合并；有并发读者时可能提前返回（`busy > 0`，无害，下次继续）
- 任何时候都可以安全执行，包括服务运行期间
- `serve --maintenance-interval 3600` 会把它作为后台任务自动运行
- 干净退出时也会自动做一次收尾 checkpoint

### 10.5 备份

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

### 10.6 恢复演练（可直接照做）

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

### 10.7 一致性校验

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

### 10.8 schema 迁移（v0.2：SCHEMA_VERSION = 2）

`Database.migrate()` 是幂等的：先 `CREATE TABLE IF NOT EXISTS`，再对 `ADDED_COLUMNS` 声明的列用 `ALTER TABLE` 补齐（例如 `runtime_state.epoch_at`、`last_exchange_at`）。**已有数据库可以原地升级，不需要重建。**

v0.2 只新增了一张表：

```sql
CREATE TABLE IF NOT EXISTS event_semantics (
    event_id            TEXT PRIMARY KEY,
    semantic_status     TEXT NOT NULL DEFAULT 'unresolved',   -- resolved | unresolved
    direction           TEXT,                                 -- + / - / 0 / +-
    intensity_band      TEXT,                                 -- negligible..high
    confidence          REAL,
    settlement_source   TEXT,                                 -- explicit_positive_feedback ...
    evidence            TEXT,                                 -- 命中的表面形式，留作审计
    potential_relevance TEXT NOT NULL DEFAULT 'low',
    unresolved_reason   TEXT,
    settled_at          TEXT,
    deep_refresh_id     TEXT,                                 -- 哪次刷新结算了它
    version             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_semantics_status
    ON event_semantics(semantic_status, potential_relevance);
```

这张表是**派生投影**，不是权威：缺行表示"还没看过"，有行也只是记录"持久层当前相信什么、或者决定暂时什么都不相信"。它可以从 `raw_events` + 规则表重建，所以它自己从不承担权威。**`raw_events` 仍然是唯一不可变的事实来源** —— unresolved 事件永远不会因为"没人理解它"而消失。

---

## 11. 不变量（都有对应自动测试）

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

v0.2 新增一组**结构性保证**（不是编号不变量，因为它们是"架构性质"而不是"状态性质"），全部在 `tests/test_acting_layer_independence.py`：

| 保证 | 测试 |
|---|---|
| 没有配置 provider 时 ingest 完全可用 | `test_ingest_works_with_no_provider_configured` |
| ingest 期间**不会拨出任何网络连接**（直接拦 `socket.connect`） | `test_no_outbound_socket_is_opened_during_ingest` |
| 模糊事件被推迟而不是被猜（且不制造情绪余波） | `test_ambiguous_events_are_deferred_not_guessed` |
| 被推迟的原始事件一字不丢 | `test_raw_event_survives_being_unresolved` |
| 「算了，也没什么。」必须保持 unresolved | `test_exact_patch_example_is_unresolved`（`tests/test_semantic.py`） |
| 规则层从不给情绪命名 | `test_anchor_settles_with_the_expected_reading`（断言 `semantic_label is None`） |
| 注入的 prompt 块自我声明是"背景"且服从当前轮 | `test_context_block_is_marked_as_background` |
| unresolved 不会以"已理解的情绪状态"形式泄漏进 prompt 块 | `test_unresolved_events_do_not_leak_into_the_block_as_facts` |
| ingest 自己**不会**触发一次深层刷新（低频就是低频） | `test_deep_refresh.py::test_ingest_does_not_trigger_a_refresh` |
| provider 抛异常不会让刷新失败 | `test_a_raising_provider_is_not_fatal` |
| 一条 grounding 不过的操作都不许改状态 | `test_all_ungrounded_suggestions_apply_nothing` |
| 重解释**从不**重写历史事件 | `test_history_is_not_rewritten_by_a_reinterpretation` |
| 只有真的应用了操作才把事件标为已结算 | `test_a_grounded_reinterpretation_settles_the_backlog` |
| 只有被已应用操作引用过的事件才离开积压 | `test_only_referenced_events_leave_the_backlog` |

---

## 12. 测试

```powershell
python -m pytest -q                     # 全部
python -m pytest -q -m integration      # 只跑端到端场景
python -m pytest -q --cov=companion_runtime
```

规模：本文撰写时实测 **约 500+ 项**（`python -m pytest -q` 收集 608 项，全部通过）。测试数量随模块演进持续增长（本文撰写过程中就从 559 涨到 608），请以你自己那次运行的输出为准，不要以本文数字为准。

覆盖范围：

- **基础设施**：数学工具数值性质、配置三层覆盖与脱敏、SQLite 事务/保存点/JSON 列、事件日志 append-only 与过滤
- **认知模块**：事件评价方向与不确定性、情绪衰减与心境恢复、解释器缓存与 provider 容错、边界检测（含假阳性防护）与生命周期、未尽之事全生命周期、记忆评分/巩固/冲突/去重/检索/激活、用户模型的特征/证据权重/冷启动/学习/漂移/双视图、候选生成与池管理器四种操作、沉默效用、效用分解、危险率（含频率无关性）、softmax 选择、I/R/P 动力学（惯性、饱和、释放）
- **v0.2 语义结算**（`test_semantic.py`，53 项）：锚点表逐条方向与来源、歧义否决表逐条（含"每个否决标记都必须有负例"）、补丁点名例句必须 unresolved、填充词剥离、强锚点豁免、钝性拒绝 vs 含糊拒绝、强度带与置信度范围、`semantic_label` 恒为 `None`、锚点与否决标记不得冲突、`potential_relevance` 分档、`resolve_backlog` 的 live/stale 切分与排序
- **v0.2 provider**（`test_providers.py`，73 项）：四种实现的选择与回落、未知名字回落 disabled、构造失败回落 disabled、六个建议字段的类型校验与部分畸形处理、`suggestions` 包装键、超时/连不上/畸形 JSON 全部 fail-open（`None` 或 `degraded`）、本地 client 复用、解释缓存与 `state_key` 契约、provider 统计、**密钥永不出现在 `repr`/`health`/错误信息里**
- **v0.2 两层独立性**（`test_acting_layer_independence.py`，11 项）：见第 11 节右表
- **v0.2 深层认知刷新**（`test_deep_refresh.py`，约 40 项）：八条触发规则各自的优先级与"最紧急者胜出"、阈值来自配置、最小间隔压过所有理由、优先级表与补丁顺序一致；grounding 的六种操作类型可达、编造的来源被拒、缺来源被拒、解释缓存豁免来源、畸形条目被记录而不崩溃、操作数被截断、未知字段被忽略；请求组装的九个段落齐备、**组装请求不写库**、`key_quotes` 来自真实事件；编排层的开关关闭/provider 不可用/无触发/超时抛异常/空建议/全部 grounding 失败/部分坏批次/`force` 只跳过触发不跳过可用性/outcome 可 JSON 序列化；以及"ingest 不触发刷新"与"积压对 health 可见"
- **v0.2 认知 HTTP 契约**（`test_cognition_api.py`，9 项）：`POST /cognition/refresh` 与 `GET /cognition/backlog` 的响应形状与状态码
- **协议与并发**：APPLY/REBASE/DISCARD 全部分支、敏感度表、rebase 辅助、五种重协调结果、outbox claim/lease/ack/nack/租约过期回收/优先级/kinds 过滤、action 状态机合法与非法跃迁
- **耐久性**（`test_durability.py`）：WAL 生效、认知轮原子性、**进程崩溃后已提交事务保留**、**截断 WAL 尾部后不损坏**、缺 sidecar 无害、四种 checkpoint 模式、verify 各类检查（含故意造坏）、备份一致性、备份包含未 checkpoint 事务、不能覆盖已有快照、停机后可读、**完整恢复演练（备份 → 毁库 → 确认不可读 → restore → verify → 继续可用）**、拒绝覆盖运行中的库、保留策略、CLI 全流程
- **HTTP**：全部端点契约、状态码（404/409/422）、OpenAPI 覆盖、outbox 全链路、边界/渲染/投递、维护端点
- **场景与不变量**：架构文档里的 7 个场景 + 完整"面试"故事 + 10 条不变量 + 降级模式 + 多进程并发写冲突

---

## 13. 降级模式（Level 0）

**默认就是零模型可运行**（v0.2 之后这不只是"降级"，而是标准形态）：

- 事件评价（粗粒度结算）：规则 + 双语锚点表 + 歧义否决表（`semantic.py`）
- 事件评价（旧词表路径）：规则 + 双语信号词表（`emotion.appraise_event`）
- 情绪解释：确定性代码模板（长期底色）；可选接入 `SemanticProvider.explain_state()`
- 候选意图：规则生成器
- 记忆巩固：词面去重 + 结构化摘要
- 用户模型：先验 + 在线贝叶斯更新

需要更强语义时，通过 `POST /proposals` 把结果交回 Runtime：情绪解释、候选生成、记忆摘要、用户模型总结全部走 APPLY/REBASE/DISCARD。接口保持一致，Runtime 不被单一模型锁死。

三级结构（补丁 §22）在代码里的对应：

```text
Level 2  低频深层语义      providers.SemanticProvider + deep_refresh 编排（可选；默认 disabled，未配 provider 时整条链安全跳过）
Level 1  廉价认知          规则 / 统计 / 词法检索 / 缓存 / 高置信事件提取
                          → semantic.py、emotion.py、memory.py、user_model.py
Level 0  确定性 Runtime    时间 / 状态机 / 情绪余波 / I-R-P / 边界 / 未尽之事 / 协议 / 动机决策
                          → runtime.py、motivation.py、boundaries.py、unfinished.py、protocol.py

（另一条轴）主 LLM       当前轮即时演出，不属于 Level 0/1/2
```

---

## 14. 与宿主框架（AstrBot）的边界

| 归属 | 内容 |
|---|---|
| **宿主框架（不修改）** | 平台接入、账号/会话、主 LLM 调用、消息发送、角色卡/system prompt、API key 管理 |
| **本 Runtime sidecar** | 时间连续性、情绪、记忆、用户认识、未尽之事、候选意图、动机决策、边界与许可、投递协议、语义结算与 unresolved |
| **通信方式** | HTTP（`api.py`），异步 outbox claim/lease/ack |

Runtime 不 import `AstrBot` 的任何模块，也不修改其代码。宿主只需实现两个端口：

- `Renderer.render(payload) -> str`：主 LLM 把意图渲染成可见文本
- `Transport.send(text, conversation_id) -> Mapping`：真实发送

两者在测试里分别用 `EchoRenderer` 与 `NullTransport` 替代。

v0.2 之后这条边界多了一句更硬的表述：**主 LLM 就是即时演出层**。它不是 Level 0/1/2 里的"深层认知模块"，也不需要 Runtime 先把当前这句话解析成数值再交给它。

---

## 15. 弱 VPS 部署建议（v0.2）

### 15.1 最小常驻集合

移除本地 2B 之后，弱 VPS 上**只需要跑三件东西**：

```text
1. Runtime sidecar（本工程：Python + fastapi/uvicorn + SQLite）
2. Bot / 宿主框架（平台接入 + 主 LLM 调用，例如 AstrBot）
3. 数据库（SQLite 单文件即可；要更稳就上 PostgreSQL）

可选：轻量词法检索（已内置）、可选轻量 embedding（未实现，见第 16 节）
```

**不再需要**常驻：

```text
❌ 1GB+ 生成模型权重
❌ llama.cpp 推理进程
❌ 本地模型 warmup
❌ 推理任务队列
❌ 2B 微调与量化版本维护
❌ 为模型准备的 swap / 大页配置
```

这直接换来了"低成本、长期稳定、可维护"：Runtime 常驻内存以 Python 解释器 + SQLite 页缓存为主，磁盘只有数据库文件、WAL 和备份快照。

### 15.2 如果一定要跑本地模型：实测成本量级

以下是实测的量级参考（约 2B 模型，`Q4_K_M` 量化）：

| 配置 | 生成速度 | 备注 |
|---|---|---|
| 8 线程（8 个性能核） | **≈ 20 tok/s** | 满打满算的并行度 |
| 单核 | **≈ 8.9 tok/s** | 只给一个核时的真实速度 |
| 半核（约半数的核可用） | **≈ 3.7 tok/s** | 与宿主、数据库抢 CPU 时更接近这个数 |
| 常驻内存 RSS | **≈ 2 GB** | 权重 + KV cache + 运行时 |

补丁 §2 记录的同机型（i7-13700H，8 个性能核，Q4_K_M）更完整的一次评价开销：

```text
生成速度      ≈ 18 token/s
单次评价输出  ≈ 67 token      ⇒ 纯生成 ≈ 3.7 s
输入长度      ≈ 250～600 token，提示处理 ≈ 157 token/s ⇒ ≈ 1.6～3.8 s
理论总耗时    ≈ 5.3～7.5 s
实测 p50      ≈ 6.65 s
```

结论很直接：**光是把 250 token 的提示塞进去（≈ 1.59 s）就已经超过约 1.2 s 的同步前处理预算**，更不用说生成。

### 15.3 因此：本地模型只能是低频异步能力

如果确实需要本地强语义，请把它当成**低频异步能力**而不是实时组件：

- ❌ 不要放进关键路径（`process_user_message` 里**不允许**出现模型调用；`test_no_outbound_socket_is_opened_during_ingest` 会直接拦断网络连接）
- ✅ 只用于低频的深层刷新与心理解释缓存填充（第 7、8 节）
- ✅ 保持 `semantic.settle_on_ingest = true`：入口处走纯规则结算，零延迟
- ✅ 半核 3.7 tok/s 意味着一次刷新可能要跑几分钟 —— 这正是 `deep_refresh_min_interval_seconds = 3600`、`deep_refresh_idle_hours = 12` 这类旋钮存在的理由：这类工作**本来就该慢**
- ✅ 如果本地跑不动，`RemoteAPIProvider` 是更省 VPS 的选择（把算力放到远端，VPS 只留 Runtime）

### 15.4 运维要点

| 事项 | 建议 |
|---|---|
| 数据库目录 | 放在持久化盘；`restore` 会拒绝覆盖运行中的库 |
| WAL | 保持 `wal = true`，用 `serve --maintenance-interval 3600` 或 cron 定时 checkpoint |
| 备份 | `backup --keep 7`，快照是自包含单文件，可直接拷走 |
| 内存 | 零模型部署下 Runtime 内存以 SQLite 页缓存为主；本地模型会额外占 ≈ 2 GB RSS |
| 启动 | `--host 127.0.0.1`（前面套反代），不要裸奔在公网 |
| 密钥 | 只放环境变量（`CR_SEMANTIC_API_KEY` 等），**绝不写进 `runtime.toml`**；Runtime 从设计上就拒绝从配置文件读 key |
| 观测 | `GET /health`（含 `semantics` 与 `semantic_provider`）、`GET /outbox`、`GET /maintenance/verify` 已够用；尚无 Prometheus 导出 |

---

## 16. 已知边界与后续工作

v0.2 之后仍然刻意简化但**不省略主要模块**，并且把"还没接上的部分"明确列出：

**v0.2 相关（仍未接通）**

- **深层刷新没有内置自动调度**：触发**判定**已实现（`deep_refresh.evaluate_triggers`，含补丁 §21 的八条规则与最小间隔否决），但没有定时器会自动调用它 —— `scheduler.py` 不引用深层刷新，`Runtime.deep_refresh()` 目前只被 `POST /cognition/refresh` 与 `companion-runtime refresh` 调用。**"由谁按节奏去问"这一环留给宿主**（例如放进宿主的每小时任务）
- **`semantic.resolve_backlog()` 无生产调用方**：刷新路径直接读 `SemanticProjection.list_unresolved()`，因此"超过 `unresolved_max_age_hours` 的 unresolved 不再支撑刷新"这条策略当前**没有被执行**（原始事件当然仍然保留）
- **`explain_state()` 的调用是有条件的**：`context._optional_explanation_provider()` 只在 provider `available()` 为真时才把它交给 `EmotionExplainer`。因此"provider 配置了但当前连不上"时，心理解释会安静地退回模板（这是刻意的：不给 context 路径增加一次注定失败的调用），但在 `/health` 里仍会看到 `semantic_provider.available = false`
- **`template_fallback` 与 `interpretation_max_age_seconds` 无消费者**：模板兜底当前无条件生效（不可关）；解释缓存陈旧判定实际由 `task.explain_cache_ttl_seconds` 承担
- **优先级不是逐级落进 prompt 的**：preamble 把"Runtime 持久心理状态 / 心理解释缓存 / 主 LLM 自然发挥"合并成一句"这里的长期状态"
- **六类触发信号依赖调用方提供**：`major_event` / `matter_due` / `candidate_pool_size` / `wants_proactive` + `proactive_grounded` / `history_suspect` / `user_evidence_overturns` 需要宿主在调用时给出；没给出就按"不成立"处理（不会被猜成成立）

**长期存在的**

- 检索是词面重合而非 embedding（`MemoryStore.retrieve` 是接缝；embedding 未就绪时本方案即为降级路径）
- 用户模型是"简化分层贝叶斯"：对角精度近似（Laplace），不是完整 MCMC / 变分推断
- 事件评价与边界检测是规则 + 词表，语义纠错依赖强 API 的 REINTERPRET
- 粗粒度结算的锚点表是**词法**的：它覆盖明确的表达，不覆盖反讽、隐喻与长距离指代 —— 这些正是要留给深层刷新和主 LLM 的部分
- `invalidate_when` 的匹配是关键词级，不是语义级
- 后台巩固目前由调用方驱动（`memory.consolidate()`），未内置常驻 worker 线程
- 尚无 Prometheus 指标导出（`/health`、`/maintenance/verify`、`/outbox` 已提供足够的数据结构）

`docs/PATCH_V0.2_MAPPING.md` 里给出了逐章节（§0–§33）的代码位置、测试文件与状态标注。

---

## 17. 快速自检

```powershell
cd runtime
python -m pytest -q                                          # 期望：全部通过
python -m companion_runtime.cli --base-dir ./data health
python -m companion_runtime.cli --base-dir ./data backup --keep 7
python -m companion_runtime.cli --base-dir ./data verify
```

v0.2 补充的自检（全部应在**零模型**状态下给出正常结果）：

```powershell
# 1) 确认默认配置就是 disabled，且 Runtime 照常工作
python -m companion_runtime.cli --base-dir ./data config | Select-String "semantic" -Context 0,12

# 2) 发一句模糊的话，确认它被诚实记为 unresolved 而不是被猜
curl -s http://127.0.0.1:8787/events -H "content-type: application/json" `
  -d '{"event_type":"user_message","content":"算了，也没什么。"}'
#   → outcome.semantic_status = "unresolved"
#     outcome.appraisal_source = "deferred"
#     outcome.emotion_event_ids  = []

# 3) 看看积压里到底是哪几条
python -m companion_runtime.cli --base-dir ./data backlog --limit 20

# 4) 试着刷新一次 —— 没配 provider 时应当礼貌地拒绝，而不是报错
python -m companion_runtime.cli --base-dir ./data refresh
#   → {"ran": false, "reason": "provider_unavailable", "provider": "disabled", ...}

curl -s http://127.0.0.1:8787/health
#   → semantics.unresolved 增加 1；semantic_provider.provider = "disabled"
```
