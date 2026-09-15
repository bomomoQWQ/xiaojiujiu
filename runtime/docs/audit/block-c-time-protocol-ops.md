# 设计文档 §54–§97 对照实现（时间模型 / 协议层 / 部署与流程）

| 项目 | 值 |
|---|---|
| 审计日期 | 2026-09-15 18:15 (+08:00) |
| 审计基准提交 | `8564327`（`git rev-parse --short HEAD` = `8564327`，`docs: 把插件市场发布与加 CI 标记为已冻结`，2026-09-15） |
| 被审计文档 | `内源主动型长期陪伴AI_Runtime_完整架构设计.md` §54–§97（第 2311–3964 行） |
| 先行阅读的补丁 | `PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md`（§0–§33） |
| 审计方式 | **只读代码**：本文件是本次审计唯一创建/修改的文件；未改动任何源码、测试或文档，未执行 `git commit` |
| 工作区状态 | **审计期间工作区在被并发修改**（另一路审计/开发正在写入）。审计对象 = 提交 `8564327` + 审计时段的工作区内容；下表给出我实际核对过的文件 SHA-256 前 12 位，用于固定证据 |

审计期间被并发改动、且与本文结论相关的文件哈希（SHA-256 前 12 位 / 行数）：

| 文件 | 行数 | sha256[:12] |
|---|---|---|
| `runtime/src/companion_runtime/db.py` | 611 | `FE415E8AF615` |
| `runtime/src/companion_runtime/db_base.py`（未跟踪） | 311 | `C76A1A66302B` |
| `runtime/src/companion_runtime/db_postgres.py`（未跟踪） | 645 | `E77958C127C8` |
| `runtime/src/companion_runtime/runtime.py` | 2225 | `DB5C3E286C08` |
| `runtime/src/companion_runtime/projections.py` | 2226 | `7434C0B16BFE` |
| `runtime/src/companion_runtime/reducer.py` | 1760 | `8F2CDA613D11` |
| `runtime/src/companion_runtime/scheduler.py` | 531 | `ED55A6AFD057` |
| `runtime/src/companion_runtime/protocol.py` | 548 | `85F1F56F8E6C` |
| `runtime/src/companion_runtime/action.py` | 521 | `6A2DAB348937` |
| `runtime/src/companion_runtime/context.py` | 579 | `1FE4D142ECFE` |
| `runtime/src/companion_runtime/memory.py` | 872 | `2B5E37CAD484` |
| `runtime/src/companion_runtime/maintenance.py` | 695 | `D76C9950C757` |

> 行号均为 1-based，符号写作 `file.py::symbol:line`。下文"未跟踪"指审计时 `git status` 为 `??`（`db_base.py`、`db_postgres.py`、`tests/test_sql_portability.py`、`tests/test_db_postgres.py`）。

---

## 0. 先决判定：哪些要求已被补丁取代

补丁 v0.2 §16 正式删除"本地 2B 作为标准事件评价 / 情绪解释核心组件"，§29 明确弱 VPS 的常驻集合不再包含模型权重、llama.cpp、warmup 与推理队列，§17 只保留 `SemanticProvider` 抽象且把实现收敛为 `disabled` / `remote_api`（`providers.py::KNOWN_PROVIDER_NAMES`，`config.py::SemanticConfig.provider` 默认 `"disabled"`）。因此本区块中被取代的是：

| 被取代对象 | 取代依据 | 现状 |
|---|---|---|
| §61 2B 单 Worker（单并发、队列优先级、未开始的分析合并成一次） | 补丁 §16 / §29 | 被补丁取代。另：遗留字段 `config.py::TaskConfig.merge_window_seconds:332` 无任何消费者，"合并成一次分析"没有实现 |
| §74 弱 VPS 部署策略（2B 量化档位、上下文长度、单线程/nice/cgroup、常驻、不靠 swap） | 补丁 §16 / §29 | 被补丁取代（§74.3–§74.5 全部以"本地跑模型"为前提） |
| §88 Level 1 / Level 2（1B~1.5B、约 2B Q4） | 补丁 §16 / §17 | 被补丁取代；Level 0 是唯一交付形态（见下表 §88） |
| §89 Phase 3（2B 模型接入：事件评价 / 情绪解释 / 单 Worker / 缓存） | 补丁 §16 / §17 | 被补丁取代；缓存与 provider 端口保留（`emotion.py::EmotionExplainer.explain_and_store:588`） |

未被补丁取代、仍需按文档核对的部分：§54–§60、§62–§73、§75–§87、§88 Level 0、§89 其余 Phase、§90–§97。

---

## 1. 主表：§54–§97 逐节对照

| 章节 | 要求要点 | 代码位置 | 判定 | 差在哪 / 证据 |
|---|---|---|---|---|
| §54 调度系统 | 事件驱动 + 惰性时间推进 + 自适应内部唤醒 + **优先级队列** | `scheduler.py::plan:126`、`::collect_signals:69`、`::should_dispatch:219`、`::Scheduler:251`；`runtime.py::lazy_tick:461` | 部分实现 | 前三项都在位且落到真实部署（`cli.py::cmd_serve:230` 起 `Scheduler`）；**没有优先级队列**：`Scheduler` 只计算下一次唤醒时刻，不排队工作。`projections.py::OutboxProjection.claim:1357` 的 `ORDER BY priority ASC`（:1394）只服务 render/send 行 |
| §55 优先级 | P0 前台 / P1 近实时后台 / P2 内源主动 / P3 后台维护 | `typing.py::Priority:242`；`reducer.py:306`；`api.py:654`；`projections.py:1394` | 部分实现 | 四类工作确实走不同代码路径，但 `Priority` 只当标签用：生产代码只赋过 `P1_NEAR_REALTIME`（`reducer.py:306`、`api.py:654` 默认值），`P0_FOREGROUND`/`P2_ENDOGENOUS`/`P3_MAINTENANCE` 全仓零赋值（仅定义处命中），也没有任何队列按这四类排序 |
| §56 `lazy_tick(now)` 统一时间入口 | 拿写锁→`lazy_tick`→处理事件→`state_version+1`→释放；列出 8 类入口 | `runtime.py::lazy_tick:461`（写锁 :480、单调时钟 :501、事务 :513-522）；`projections.py::RuntimeProjection.write:166`（:193-194 每次都 +1） | 部分实现 | (a) 不是每个入口都先 tick：`POST /proposals`（`api.py:643`）、非 `user_message` 的 `POST /events`（`api.py:240`）、`/outbox/claim|ack|nack`（`api.py:426/449/467`）、`POST /observations`（`api.py:732`）、`POST /candidates/operations`（`api.py:765`）、`/unfinished` 建/结（`api.py:813/833`）、v1 投递回执（`api_v1.py:1556`）都直接写库；(b) `process_user_message` 的 tick（`runtime.py:709`）与写事务（:711）是两个锁域，不是文档画的单一临界区 |
| §57 `lazy_tick()` 更新什么 | 10 项一次性更新 | `runtime.py::_apply_time_passage:579` | 已实现 | 逐项落点：背景心境恢复 :602、情绪事件衰减 :593、接近冲动/节制/压力 :642-656、主动冷却 :542（`motivation.py::cooldown_remaining:542` 按截止时刻判定，不做衰减）、激活记忆衰减 :622、未尽之事时间状态 :606、边界过期 :616、候选期限 :619 |
| §58 自主唤醒时间 | `t_next = min(t_hazard, t_unfinished, t_boundary, t_cooldown, t_candidate)` | `scheduler.py::plan:144-193`（模块 docstring :7） | 已实现 | 五个锚点齐全，另加 `foreground_pause`、`maintenance`（:159-160）；带 ±5% 抖动（:178-182）与静默时段（:184-192）；平静时睡到 `max_interval_seconds=5400`（`config.py:321`） |
| §59 前台回复与后台心理消化 | 本轮即时反应由主 LLM 按原话产生；持久影响由后台结算 | `runtime.py:825-905`（ingest 结算段）；插件 `astrbot_plugin_companion_runtime\main.py::_inject_context:444`；`context.py::PRIORITY_PREAMBLE:71` | 已实现 | 补丁已删掉 2B 前置；粗粒度结算在 ingest 内同步完成，语义结算才低频（`:874-893` 记 `unresolved`）。比文档"心理状态滞后一轮"更强 |
| §60 入口屏障 | 用户消息一到立即暂停新的内源派发；确认安全后恢复 | `runtime.py:755-761`（`max` 延长，不缩短）、`runtime.py:1113-1116`、`scheduler.py::should_dispatch:239-240` | 已实现 | 恢复条件是定时（`config.py::SchedulerConfig.foreground_pause_seconds:325`=60s）而非"等后台分析确认"；但边界/撤回规则在同一事务内同步执行（`runtime.py:767-787`），"规则漏之前不会主动发"这一性质成立 |
| §61 2B 单 Worker | 2B 单并发 + 队列 + 未开始的分析合并 | — | 被补丁取代 | 见 §0。可用证据：`config.py::TaskConfig.merge_window_seconds:332` 无消费者（全仓 grep 仅定义处命中） |
| §62.1 单写者原则 | 模型与后台任务只能提交建议 | `reducer.py:1-16`、`:197`（"The only writer of Runtime state"）、`:231`；深层刷新经 `runtime.py:1519-1527` 走 `Reducer.process_proposal` | 已实现 | 后备模型路径全部经 Proposal；前台 Runtime 自身直写投影属设计内（它不是模型） |
| §62.2 协议流程 | Runtime Queue → 唯一 Reducer → `lazy_tick` → 版本/依赖检查 → APPLY/REBASE/DISCARD → PostgreSQL | `reducer.py::process_proposal:231`（分类 :259、版本冲突 `projections.py:190`、重复 task :275、三分支 :289-313） | 部分实现 | 缺「Runtime Queue」：proposal 由 HTTP 直接进 Reducer（`api.py:643`），没有中间队列；`process_proposal` 内也没有 `lazy_tick` |
| §63 异步任务快照 | 任务开始记 `task_id/task_type/based_on_version/source_event_ids/created_at`，结果回来先判依赖 | `db.py:158-169`（`background_tasks`）、`projections.py::TaskProjection.register:1688`、`reducer.py:300`、`protocol.py::classify:179` | 部分实现 | 表、快照写入、版本/依赖判定都在位；但生产侧只派发 `DEEP_REFRESH` 一种任务（`runtime.py:1519`），其余 6 类 `TaskKind`（`emotion_eval`/`candidate_gen`/`shallow_tag`/`memory_summary`/`user_model_summary`/`emotion_explain`/`proactive_draft`）只出现在测试与 reducer 分派表里，仓库内没有派发方（全仓 grep `TaskKind.*` 命中：runtime.py 仅 DEEP_REFRESH） |
| §64 APPLY / REBASE / DISCARD | 三分法 + REBASE 重算示例 + DISCARD 撤回示例 | `protocol.py::classify:179`（预算表 :71）、`::rebase_emotion_evaluation:461`、`::rebase_candidate_payload:493`、`RETRACTION_PATTERNS:78`；`reducer.py::_rebase:374` | 已实现 | 撤回→DISCARD 与主题重叠判定在 `protocol.py:236-242` + `::_retracts:261`；测试 `tests/test_protocol.py`、`tests/test_protocol_hardening.py` |
| §65 不同结果的过期敏感度（7 类） | 每类结果在版本变化后的处理方式 | `protocol.py::TASK_SENSITIVITY:49`、`STALENESS_BUDGET:71`、`classify:248`、`should_discard_explanation:536` | 部分实现 | 七类都有条目（浅层标签 low、事件评价 medium、心理解释 high+丢弃、候选 high+重挂接地、记忆摘要 low、用户模型总结 medium、主动成品 critical）；但"用户模型总结→**检查证据集合版本**"不存在证据集合版本概念，只用版本差；且如 §63，这些类型的实际派发方缺失，敏感度只在外部主动提交 proposal 时才生效 |
| §66 当前投影与历史日志 | 不可变/追加型与当前投影两类分开；投影坏了可从历史重建 | `db.py:1-16`（两类清单）、各表定义；`maintenance.py::verify:300` | 部分实现 | 分类与表都在位；**没有"从历史重建投影"的代码**；`verify` 只做 `PRAGMA integrity_check` + 行数 + 三条引用检查（:326-418），且在 PG 上会因 PRAGMA 失败（`db_postgres.py:39-42` 自述） |
| §67 重解释协议 | 新增 `interpretation_v2`、`supersedes=v1`，不覆盖原文；生成 `reappraisal_event`；可触发情绪更新/未尽之事/候选检查/用户模型重归因 | `projections.py::InterpretationProjection.add_version:1919`、`::add_reappraisal:1988`；`reducer.py::_apply_reinterpretation:684` | 部分实现 | 版本追加、`supersedes_id`、重估事件、grounding 都在位且不回写原文；**没有任何代码消费 `reappraisals`**（全仓：写入仅 `reducer.py:716`，读取仅 `projections.py::list_reappraisals:2017` 及 API/测试/脚本）——重估本身不派生情绪事件、未尽之事或候选，只能靠同一条 deep-refresh bundle 里的兄弟操作 |
| §68 行动尝试状态机 | `proposed→committed→rendering→ready_to_send→sent→resolved` + `aborted/expired/failed` | `action.py::TRANSITIONS:34`、`::transition:183`、`::expire_stale:371`、`TERMINAL_STATES:70`；日志 `projections.py::AttemptProjection.record_transition:1147` | 已实现 | 非法迁移抛 `action.py::IllegalTransition:102`；`attempt_events` 追加式留痕 |
| §69 `committed` 不等于 `sent` | 已提交≠已发出 | `action.py::mark_sent:276`（:290 先判 `ready_to_send`）；`runtime.py:1352-1360`（提交**不**记日程预算）；`reducer.py::mark_delivered:1586`（:1711-1717 发送才计费） | 已实现 | 测试 `tests/test_integration_scenarios.py::test_invariant_9_committed_is_not_sent:693`、`tests/test_action_outbox.py::test_committed_is_not_sent:197` |
| §70 并发重协调 | KEEP / MERGE / RERENDER / RESOLVED / ABORT | `protocol.py::reconcile:303`、`action.py::apply_reconcile_outcome:451`、`reducer.py::reconcile_attempt:843`/`reconcile_pending_attempts:943`；自动调用 `runtime.py:1059` | 已实现 | 五种结果的判定顺序见 `protocol.py:341-404`（含危机→ABORT、边界→ABORT）；测试 `test_scenario_6:324`、`6b:369`、`7:400`、`test_action_outbox.py::test_reconcile_outcomes_terminate_or_preserve:254` |
| §71 "心有灵犀" | 告知主 LLM"用户开口前你已提交"，但不强迫表达 | `context.py::describe_intent:229`（`lead_seconds_before_user_message:255`）、`render_block:507-534`（:533 写出提前秒数）、`_recently_closed_attempt:294`（120s 窗口 :89） | 已实现 | 渲染文本明确写"是否提起由当前语境决定，不要硬凹"（:531）；测试 `tests/test_delivery_scheduler_context.py::test_intent_description_includes_the_lead_time:433` |
| §72 用户反馈归因协议 | 观察 ≠ 归因；归因需结合基线/忙碌概率/行为类型/近期状态/显式反馈 | `user_model.py::compute_weight:240`、`::observe:571`；`db.py:142-155`（`attribution_confidence`/`source_weight` 分列）；`runtime.py::_attribute_user_reply:1844` | 已实现 | 忙碌概率（`user_model.py::busy_probability`）、来源权重、`no_reply_weight` 各自独立；回复只归给同会话最新一条 `sent`（`runtime.py::_newest_sent_attempt:1802`），且归因即结算，不能二次归因 |
| §73 原始数据优先，解释后置 | 先存 `no_reply = true`，不写"用户讨厌主动联系" | `runtime.py:874-893`、`projections.py::SemanticProjection.record_unresolved:2048`、`user_model.py:845`（`no_reply=true, reply_delay=…`）、`config.py:258`（`no_reply_weight=0.06`） | 已实现 | 未解析事件不产生任何情绪余波（`runtime.py:891-893`）；测试 `tests/test_semantic.py::test_exact_patch_example_is_unresolved:55`、`tests/test_user_model.py::test_no_reply_is_a_very_weak_signal:85` |
| §74 弱 VPS 部署策略 | 2B 量化/短上下文/单线程低优先级/常驻/不靠 swap | — | 被补丁取代 | 见 §0；`README.md:320` 只保留"弱 VPS 上不需要跑模型"的结论，实测数据归档在 `archive/README.md` |
| §75 Embedding / RAG 部署 | sidecar、单线程、低优先级、异步、持久化 embedding；未就绪用 FTS/BM25 兜底 | `memory.py::MemoryStore.retrieve:602`（词法打分 :647-652；模块 docstring :10-11 自述"用词法重合代替 embedding，留了接缝"） | 部分实现 | 只有文档所称的**兜底路径本身**：没有 embedding 模型/sidecar、没有持久化向量、没有 `memory_embeddings` 表；"启动时不重新 embedding 全部历史"因此无对象。`README.md:434` 记为已知降级；补丁 §29 把 embedding 降为可选 |
| §76 后台任务原则 | P3 可暂停/可重试/可丢弃/可恢复；从快照/证据版本开始；回来经协议重检 | `projections.py::OutboxProjection:1212`（claim :1357 / nack :1505 / requeue :1559 / cancel :1620）、`reducer.py:300`、`protocol.py::classify:179` | 部分实现 | 重试、丢弃、恢复、快照（`based_on_version`）、回来重检都在位；**"可暂停"没有实现**：没有任何暂停状态或开关，`background_tasks.status` 只有 `in_flight`/`settled`/`discarded`（`projections.py::TaskProjection:1681-1730`） |
| §77 PostgreSQL 建议表（18 张） | 见下方 §3 差异表 | `db.py::SCHEMA_STATEMENTS:36`、`db_postgres.py::PostgresDatabase:308` | 部分实现 | 15/18 同名或近名；缺 `memory_embeddings`；`user_model_global`+`user_model_contextual` 合并为 `user_model_params(scope)` 且只写过 `global`；另有 8 张文档未列的表（详见 §3） |
| §78 表设计原则 | 高频排序/过滤/时间字段用普通列；JSONB 只放扩展 metadata / 低频 schema / 模型原始输出 | `db.py:36-372`（普通列 + 索引 :87-89/:114/:130/:188/:247/:263/:295/:340/:359/:371）、`JSON_COLUMNS:388-408` 白名单 | 已实现 | 高频字段全部是普通列并建索引；JSON 只出现在 `*_json` 白名单列里（扩展、模型原始输出、不透明列表）。PG 侧**完全不用 JSONB**——JSON 存 TEXT 由 Python 解析，理由写在 `db_postgres.py:12-19`（有意偏离文档的 PG 建议，见 §3） |
| §79 版本字段建议 | 核心：`runtime_version`/`source_version`/`source_event_ids`/`updated_at`；解释：`interpretation_version`/`supersedes_id`/`confidence` | `db.py:73-85`（`raw_events.runtime_version`）、`:117-129`（`interpretation_versions`）、`:361-369`（`attempt_events.runtime_version`） | 已实现 | 七个字段名都有落点，但**不统一**：`runtime_version` 只在 `raw_events`/`attempt_events`（`event_semantics` 叫 `version`，`db.py:109`），`updated_at` 在追加型表上普遍缺失（`reappraisals`/`interpretation_versions`/`interaction_observations` 只有 `created_at`） |
| §80 主 LLM 的临时注入格式 | 五段固定区块，不 dump 数据库 | `context.py:55-61`（区块名）、`render_block:460`、`ContextBundle.ephemeral:106`、`assert_ephemeral:571` | 已实现 | 心理段按补丁 §4.1/§14.1 改名为「进入本轮前的长期状态（背景）」，另加「刚刚差点要说的话」「时间连续性」「注意」；插件以临时 part 注入（`main.py:476` `mark_as_temp()`） |
| §81 主 LLM 无权修改内部状态 | 语言输出不是内部事实 | `api_v1.py:559-579`（assistant_message 只作事实落库）、`runtime.py:895-905`（情绪只由规则/提案产生）、`reducer.py::_apply_emotion_evaluation:450` | 已实现 | 测试 `test_invariant_6_main_llm_has_no_state_write_authority:619`、`tests/test_emotion_boundaries_unfinished.py::test_own_message_is_not_evidence_about_the_world:122` |
| §82 最终权威归属（11 条） | 每条权威各自归属 | 见下方 §4 逐条映射 | 已实现 | 逐条都有唯一落点；"情绪解释权威"按补丁从 2B 解释器变为可选 provider，但仍只写解释缓存（`reducer.py::_apply_explanation:537`），不改状态 |
| §83 完整实时对话流程 | 时序图：消息→tick→落事件→屏障→主 LLM→异步评价→APPLY/REBASE/DISCARD→情绪→解释→观察/记忆候选 | `runtime.py::process_user_message:677`（tick :709 → 事件 :733 → 屏障 :758 → 边界 :767 → 结算 :850 → 情绪 :895 → 工作局势 :908 → 观察/记忆候选 :990-1023） | 部分实现 | 顺序与语义都在，但两处与图不一致：Runtime **不调用主 LLM**（由宿主插件承担，插件 `main.py:430-486`），"2B 异步事件评价 + 情绪解释"被补丁替换为 ingest 时的粗粒度规则结算 + 可选深层刷新，因此图中参与者 M 与"异步"性质没有对应实现 |
| §84 完整内源主动流程 | 唤醒→tick→许可→未尽之事/激活记忆→候选(不足则强 API 刷新)→效用博弈→committed→渲染→并发重协调→发送→释放 | `runtime.py::endogenous_round:1071`（许可 :1113、激活 :1131-1140、候选刷新 :1122-1127、博弈 :1166-1188、commit :1204）、`reducer.py::mark_delivered:1586` | 部分实现 | 唯一断支是"**候选池不够 → 强 API 刷新候选**"：刷新是本地规则生成（`candidate.py::generate:178`，docstring 自称 degradation Level 0；`::plan_operations:306`），仓库内没有向 provider 派发 `candidate_gen` 的代码 |
| §85 完整"面试"示例 | 端到端闭环示例 | `tests/test_integration_scenarios.py::test_complete_interview_story:447`；抢先满足 `::test_scenario_6:324` | 已实现 | 断言覆盖：`等待面试结果` 事项与状态（:480-481）、due→acted（:490-494）、投递→sent（:499-502）、回复→resolved（:517）、心境转正（:518）、记忆候选（:519） |
| §86 系统不变量（10 条） | "建议把这些写成自动测试" | 见下方 §2 专表 | 部分实现 | 10/10 都有同名测试（`tests/test_integration_scenarios.py:531-724`）；但 86.4 的测试只覆盖 2 个入口，代码里另有 9 个写库入口不 tick（详见 §2） |
| §87 情景测试集建议（7 个场景） | 见下方 §5 专表 | `tests/test_integration_scenarios.py`、`scripts/*.py` | 已实现 | 7/7 都有 pytest 对位；只有场景 5 的"产生现在的愧疚/修复候选"没有对位（详见 §5） |
| §88 资源降级模式 | Level 0/1/2 三级且接口一致 | `config.py::SemanticConfig.provider:364`（`"disabled"`）、`providers.py::DisabledProvider:374`、`emotion.py::EmotionExplainer._render_template:739`、`semantic.py::classify_event:515`、`providers.py::SemanticProvider:242` | 部分实现 | Level 0 是默认且唯一交付形态，有测试（`test_integration_scenarios.py::test_degradation_level_0_runs_with_no_models_at_all:732`）；Level 1/2 随本地 2B 被补丁取代；"接口保持一致"由 `SemanticProvider` 端口保证 |
| §89 推荐实现阶段（Phase 1–8） | 八个阶段 | 各模块；`memory.py::consolidate:283`（无自动调用方）、`memory.py::needs_consolidation:853`（无调用方）；无 embedding / 无 CPU 优先级 | 部分实现 | Phase 1/2/4/5/6/7 已实现；Phase 3 被补丁取代；Phase 8 只做了记忆合并逻辑与用户模型摘要（两者都需要外部派发 proposal），embedding sidecar 与 CPU 优先级未实现，且没有任何常驻触发（详见 §6 缺陷 1） |
| §90 第一版不要做什么（10 条禁令） | 关系模拟器/多 Agent/每轮反思/固定心跳/重复 embedding/一功能一模型/几十种人格数值/爱情值/主 LLM 决定内部事实/重解释回滚历史 | 全仓核对：无多 Agent 调度；`scheduler.py::plan:144` 事件驱动（`max_interval_seconds` 是上限不是心跳）；无 embedding；单一 provider 端口；`runtime.py:891-893` 不回滚；`api_v1.py:559-579` 主 LLM 只落事实 | 已实现 | 10 条禁令逐条无违反。唯一可讨论项：`config.py:321` 的 5400s 是"最长睡眠"而非固定心跳（`:168` base_interval 分支） |
| §91 可扩展：未来关系模型 | 关系模型（共同经历/冲突修复/互动惯例） | — | 未实现 | 没有关系模型；最接近的是 `user_model_params` 与 `interpretation_versions(target_kind='user_model_evidence')`（`reducer.py::_apply_user_model_evidence:810`）。文档自己标注"不属于 MVP 阻塞项" |
| §92 一句话概括每个模块（12 条） | 模块一句话摘要 | 各模块文件（`config.py`/`projections.py`/`emotion.py`/`memory.py`/`unfinished.py`/`user_model.py`/`memory.py::retrieve`/`candidate.py`/`motivation.py`/`motivation.py::decide`/宿主主 LLM/`reducer.py`+`protocol.py`） | 文档·约定 | 纯摘要，没有可验收物；12 条与实现的对应关系在上列文件 |
| §93 最终完整数据流 | 事件日志→tick→工作局势→四路→候选→I/R/P→博弈→attempt→主 LLM→反馈 | 各模块（同上）；断链在 `memory.py::consolidate:283` | 部分实现 | 唯一断链是"记忆候选 → 长期记忆"：`consolidate` 只被 `reducer.py:511`（需要外部提交 `memory_summary` proposal）调用，仓库内没有任何派发方，CLI 也没有巩固命令（`cli.py:82-188` 子命令清单），`memory.needs_consolidation:853` 无调用方 → 默认部署下 `memories` 表永远为空（详见 §6 缺陷 1） |
| §94 人格连续性来自哪里（8 项） | 稳定价值观 + 长期记忆 + 持续情绪 + 未尽之事 + 用户模型 + 主动动力学 + 候选意图 + 时间连续性 | `config.py::ValueProfile`（`typing.py:277`）、`memory.py`、`db.py:47-69`（`runtime_state`）、`unfinished.py`、`user_model.py`、`motivation.py`、`candidate.py`、`runtime.py::lazy_tick:461` | 部分实现 | 8 项中 7 项名副其实；"长期记忆"在默认部署下不成立（同 §93：候选不会巩固，激活池 `activated_memories` 因此恒空） |
| §95 最终设计哲学 | 成本/可靠性/可维护性/拟人感的工程模型 | — | 文档·约定 | 纯哲学；对应落点在 `semantic.py`/`providers.py`/`deep_refresh.py` 的模块 docstring 与 `README.md` |
| §96 当前架构完成度（17 项 ✓ + 剩余清单） | 自评"当前认知层可以视为…✓" | 逐项见 §7 说明 | 部分实现 | 17 项中 15 项名副其实；**"长期记忆 ✓"与"RAG / 激活记忆 ✓"在默认部署下不成立**（候选无巩固调用方）；"协议层 ✓（概念封箱）"成立。"剩余"清单里的"情景测试"现已完成（§87 7/7），"任务队列实现"部分完成（outbox 有队列，6 类任务无派发方） |
| §97 最后一句 | 一句话概括项目 | — | 文档·约定 | 纯表述 |

---

## 2. §86 不变量 → 测试对照表

`runtime/tests/test_integration_scenarios.py:531-724` 有名为 `Invariants` 的区块，含 `test_invariant_1..10`，**10/10 都有专测，没有一条"无测试"**。但强度不均：86.4 是最弱的一环，86.2 的"升级"动作本身没有测试。

| 不变量 | 测试（`file::test` @def 行） | 断言强度 / 缺什么 |
|---|---|---|
| 86.1 原始事件不可修改 | `tests/test_integration_scenarios.py::test_invariant_1_raw_events_are_never_modified:536`；`tests/test_core_infrastructure.py::test_event_log_is_append_only:514` | 弱：只做"读回原文不变"+ `dir(EventLog)` 里没有 `update/delete/edit/replace`（:547）。**没有任何测试对 `raw_events` 执行 SQL `UPDATE`/`DELETE` 并断言被拒**（`db.py` 无 TRIGGER，全仓 grep `TRIGGER|BEFORE UPDATE|RAISE(` 零命中）——不可变性靠"不提供写路径"实现，DB 层不强制 |
| 86.2 推断不能升级成事实 | `tests/test_integration_scenarios.py::test_invariant_2_inference_cannot_become_fact:550`；`tests/test_delivery_scheduler_context.py::test_context_separates_facts_from_inferences:388`；`tests/test_acting_layer_independence.py::test_unresolved_events_do_not_leak_into_the_block_as_facts:175`；"新证据"侧 `tests/test_deep_refresh.py::test_an_invented_source_is_rejected:149` | **专测最弱**：只断言 `fact`/`inference` 两种 `kind` 共存且 inference 的 `confidence < 1.0`（:555-559），**从不尝试"把 inference 提升为 fact"并断言被拒**。代码侧确实没有这种路径（`runtime.py:908-928` 是两次独立 upsert），但没有测试钉住 |
| 86.3 后台模型不能直接写 Runtime | `tests/test_integration_scenarios.py::test_invariant_3_background_models_never_write_directly:562`；`tests/test_deep_refresh.py::test_building_a_request_does_not_write:240`、`::test_all_ungrounded_suggestions_apply_nothing:312`；`tests/test_cognition_api.py::test_an_ungrounded_bundle_changes_nothing:138` | 行为面为主（前后 version / 候选数不变）+ 一条 `hasattr` 检查（:579）。**没有对语义 provider 做写权限内省**：若 provider 将来直接拿 `runtime.db` 写，现有测试不会失败 |
| 86.4 所有入口先 `lazy_tick(now)` | `tests/test_integration_scenarios.py::test_invariant_4_every_entry_calls_lazy_tick:582`；辅助 `tests/test_runtime_lifecycle_fixes.py::test_lazy_tick_keeps_the_clock_monotone:90`、`tests/test_api.py::test_tick_endpoint_advances_time:157` | **只有 2 个入口被断言**（`process_user_message` :594、`endogenous_round` :597）。§56 列出的其余入口（proposal 返回、投递回执、观察写入…）没有任何测试断言 tick；而且这些入口在代码里**确实不 tick**（见 §56 行）。不变量与实现同时不足 |
| 86.5 显式边界高于动机算法 | `tests/test_integration_scenarios.py::test_invariant_5_explicit_boundaries_outrank_the_game:602`；`tests/test_emotion_boundaries_unfinished.py::test_boundary_blocks_endogenous_action_even_under_extreme_pressure:460`、`::test_permanent_boundary_never_lapses_at_any_time:502`、`::test_boundary_outranks_every_drive_configuration:528`；`tests/test_candidate_motivation.py::test_blocked_candidate_has_negative_infinite_utility:493`；`tests/test_api_v1.py::test_v1_authorize_denies_a_send_under_a_boundary:541`；脚本 `scripts/blackbox_user_simulation.py::phase_boundary:2141` | **覆盖最好的一条**：效用 `-inf`（:494/:616）、参数化 I/R/P 含 (1.0,1.0,0.0)、跨 365 天时间扫描、授权闸门、v1 发送闸门、黑盒"窗口内零未经请求消息"（scripts:2173-2198） |
| 86.6 主 LLM 不拥有情绪状态写权限 | `tests/test_integration_scenarios.py::test_invariant_6_main_llm_has_no_state_write_authority:619`；`tests/test_emotion_boundaries_unfinished.py::test_explainer_has_no_write_access:251`、`::test_own_message_is_not_evidence_about_the_world:122`、`::test_appraise_never_outputs_final_emotion_values:77` | 两半各一条：`dir(EmotionExplainer)` 无写方法（:624）+ 追加 assistant_message 后 `mood_valence == 0.0`（:633）。**只查了心境一个轴**（未查 arousal/I/R/P）；`test_own_message…:122` 是最强的机制断言（自己说的话 impact=0） |
| 86.7 后验重解释不覆盖原始事件 | `tests/test_integration_scenarios.py::test_invariant_7_reinterpretation_never_rewrites_the_past:636`；`tests/test_deep_refresh.py::test_history_is_not_rewritten_by_a_reinterpretation:356`（:374 `after.to_dict() == original.to_dict()` 整行相等）；`tests/test_integration_scenarios.py::test_scenario_5_reappraisal_does_not_rewrite_history:257`（:301 版本 `[1,2]`）、`::test_scenario_5b_reappraisal_events_are_append_only:309` | 扎实：走真实 `deep_refresh` 路径做整行比对，且版本累积而非覆盖 |
| 86.8 用户不回复不等于负反馈 | `tests/test_integration_scenarios.py::test_invariant_8_no_reply_is_not_negative_feedback:653`（:681 `<0.05`、:688 `weight.total<0.05`）；`tests/test_user_model.py::test_no_reply_is_a_very_weak_signal:85`（:91 `== config.no_reply_weight`）、`::test_absent_reply_barely_moves_the_model:272`（6 小时无回复，:290 `<0.02`）、`::test_absent_reply_description_is_neutral:321`、`::test_busy_attribution_collapses_the_weight:94` | 扎实（权重机制 + 行为面）。脚本侧只有行为面：`scripts/blackbox_user_simulation.py::phase_silence:2249`（cap/cooldown/无指责措辞），不触碰权重 |
| 86.9 `committed != sent` | `tests/test_integration_scenarios.py::test_invariant_9_committed_is_not_sent:693`；`tests/test_action_outbox.py::test_committed_is_not_sent:197`、`::test_legal_transitions:74`、`::test_illegal_transitions_are_rejected:91`（参数化含 `(COMMITTED, SENT)`）、`::test_terminal_states_have_no_exits:96`、`::test_full_lifecycle_persists_and_logs_every_transition:107`；`tests/test_api_v1.py::test_v1_send_report_without_sent_is_not_a_delivery:439` | **覆盖最完整的一条**：状态机 + 非法迁移 + 投递契约表 + 脚本（`e2e_resilience_simulation.py` 未提交 attempt 被判 409） |
| 86.10 隐藏心理上下文不进入永久对话历史 | `tests/test_integration_scenarios.py::test_invariant_10_hidden_context_never_enters_history:711`；`tests/test_delivery_scheduler_context.py::test_context_bundle_is_marked_ephemeral:376`；**插件** `astrbot_plugin_companion_runtime/tests/test_plugin_integration.py::PluginIntegrationTests::test_context_is_injected_as_a_temporary_part:369`（:384 `part._no_save`）；脚本 `scripts/blackbox_user_simulation.py:1555`（`_no_save` 断言）、:2710（泄漏扫描） | Runtime 侧专测只是字符串扫描（:721-724，防 `__RUNTIME_STATE__` 与 `- 感受` 行）；**唯一机制级断言在插件套件**（`:384`）。插件套件对 86.1–86.9 没有任何断言（grep `AttemptState`/`committed`/`boundary`/`inference`/`no_reply` 全 0 命中） |

**§86 结论**：不变量测试存在且成体系，但"有测试"≠"被强制"——86.1（无 DB 层强制、无篡改尝试测试）、86.2（升级路径无测试）、86.4（只覆盖 2/11 个入口且实现本身不合规）、86.10（Runtime 侧仅字符串扫描）这四条的强制力明显低于表面印象。

---

## 3. §87 建议情景 → 现有覆盖对照表

§87 的 7 个场景**在 `runtime/tests/test_integration_scenarios.py` 里有逐条对位**（模块 docstring :1-3 自述"mirror the scenario set in the architecture document"），这是两个测试树里唯一带场景编号的测试文件。三个脚本**都没有被任何自动化入口调用**：`runtime/pyproject.toml` 只有 `testpaths = ["tests"]`，仓库根没有 `.github`、没有 CI/Makefile/tox/nox（`HANDOFF.md` 把"加 CI"记为已冻结）。

| 建议场景 | 现状 | 对位证据（`file::test` @def 行 / 脚本 phase 行） | 缺口 |
|---|---|---|---|
| 场景 1 明确边界 | 已覆盖 | `test_integration_scenarios.py::test_scenario_1_explicit_boundary:72`（原话 :74，P=1 段 :85-93）；`::test_invariant_5…:602`；`test_emotion_boundaries_unfinished.py::test_boundary_outranks_every_drive_configuration:528`（参数化含 1.0/1.0/0.0）、`::test_boundary_blocks_endogenous_action_even_under_extreme_pressure:460`、`::test_permanent_boundary_never_lapses_at_any_time:502`；`test_delivery_scheduler_context.py::test_authorize_denies_proactive_under_a_boundary:274`；脚本 `blackbox_user_simulation.py::phase_boundary:2141`（:2173-2198 四条 check） | 脚本侧无法验证"即使 P=1 也不能绕过"（黑盒不调用 `/endogenous`）；脚本用的是永久边界措辞（`TEXT_CONTACT_BAN`），"今天"这种临时窗口只在 pytest 侧 |
| 场景 2 长时间未联系 | 已覆盖（一处口径偏差） | `::test_scenario_2_long_absence_raises_drive_gradually:115`（2→48h 采样，:125-126 单调、:129 无跳变 `<0.35`）；`::2b:133`、`::2c:173`、`::2d:144`；`test_candidate_motivation.py::test_pressure_accumulates_when_impulse_exceeds_restraint:698`；框架 `framework/tests/test_host.py::TestLiveHost::test_the_runtime_decides_to_speak_on_its_own:242` | "主动概率逐渐增加"是钉在 I/P 与步长上，不是显式主动概率曲线；前置"用户模型接受主动"没有单独断言；脚本侧只设置了长时间缺席，没有 I/P 轨迹断言 |
| 场景 3 刚主动过 | 已覆盖 | `::test_scenario_3_recent_contact_suppresses_a_second_message:196`（:212-213 冷却/压力，:217-221 十分钟后不可能再发）；`test_candidate_motivation.py::test_release_after_contact_transitions_state:741`（:751 `restraint > 0.2`）、`::test_cooldown_suppresses_action:816`；`test_runtime_lifecycle_fixes.py::test_a_delayed_delivery_report_cannot_shorten_the_cooldown:122` | 脚本最小模拟间隔是 30 分钟，"10 分钟前刚主动过"没有脚本对位 |
| 场景 4 用户忙 | **名义覆盖，专测实为空断言** | 声称对位：`::test_scenario_4_busy_user_does_not_lower_acceptance:229`。真实覆盖：`test_user_model.py::test_absent_reply_barely_moves_the_model:272`（恰好 21600s=6h，:290 `<0.02`）、`::test_busy_attribution_collapses_the_weight:94`、`::test_busy_probability_estimator:444`（`stated_busy` ≥0.85，经 `runtime.py:82` 的 `BUSY_MARKERS` 含"工作很多"接线）、`test_integration_scenarios.py::test_invariant_8…:653` | `test_scenario_4:229` **不可能失败**：该流程从未发出主动消息，`_attribute_user_reply` 在无 sent attempt 且无显式 reaction 时返回 `(None, None)`（`runtime.py:1880-1882`），因此从没有观察进入用户模型。我按该测试原样复跑（内存库、`PYTHONDONTWRITEBYTECODE=1`）：`count_in_flight=0`、`sent=0`、`effective_count=0.0`、`before == after == 0.4937503255004896`，`abs_diff = 0.0`。另外刺激语是"这几天工作很多，我可能回得慢"（:231）而非"今天工作很多"，6 小时点是**回复**（:239-241）而非静默。**没有任何测试把"忙碌语 → 6 小时静默 → 主动接受度"跑成一条流程**；三个脚本均未覆盖 |
| 场景 5 后验重解释 | 部分覆盖 | 追加式版本+重估：`test_deep_refresh.py::test_a_grounded_reinterpretation_settles_the_backlog:329`（:349 `reinterpretation==1`、:352 `list_reappraisals`）、`::test_history_is_not_rewritten_by_a_reinterpretation:356`；`test_integration_scenarios.py::test_scenario_5…:257`、`::5b:309`；`test_semantic.py::test_exact_patch_example_is_unresolved:55`；脚本 `scripts/e2e_patch_v02.py` 步骤 6-7（:143、:201-206） | 两处缺口：(a) "你那时候果然没发现"**从不构成触发**——它只出现在 `test_integration_scenarios.py:267`，且下一行 :269 是恒真的 `assert second.relation_signal if hasattr(second, "relation_signal") else True`（`runtime.py::MessageOutcome:133-170` 无该字段，已核对）；`test_deep_refresh.py::test_ingest_does_not_trigger_a_refresh:533` 明确 ingest 不触发刷新，触发点是 `POST /cognition/refresh` 或内源轮。(b) **"产生现在的愧疚/修复候选"没有实现也没有测试**：`reappraisals` 只被 `reducer.py:716` 写入、只被 `projections.py:2017` 读出，`candidate.py`/`motivation.py`/`context.py` 均不消费 |
| 场景 6 并发心有灵犀 | 已覆盖 | `::test_scenario_6_user_speaks_three_seconds_after_commitment:324`（:342 用户消息 +3s，:348 resolved/abort，:351 committed 历史留存，:357-358 committed=1/sent=0）；`::6b_topic_overlap_merges_instead_of_aborting:369`（:389 `merge`）；五种结果全量 `test_action_outbox.py::test_reconcile_outcomes_terminate_or_preserve:254`；`test_protocol.py::test_user_beats_the_intent:276` 等；ingest 自动重协调 `test_runtime_lifecycle_fixes.py::test_ingest_re_coordinates_an_undelivered_intention:346`；提前量提示 `test_delivery_scheduler_context.py::test_intent_description_includes_the_lead_time:433`（:453 ≈3.0s） | 脚本侧结构性不可能覆盖（黑盒每轮后清空队列，`blackbox_user_simulation.py::_drain_outbox:1673`）；`e2e_resilience_simulation.py` 的 `p13_concurrent` 是并发重复**上报**，不是重协调 |
| 场景 7 主动生成时用户发生重大负面事件 | 部分覆盖 | `::test_scenario_7_severe_news_aborts_a_light_message:400`（:407 撒娇候选，:421 "家里出事了，我很难受"，:429-430 abort + reason，:434 原意图留存，:435 outbox 取消，:439 未发送）；`test_protocol.py::test_severe_news_aborts_a_light_intent:284`；`test_action_outbox.py::test_abort_preserves_history:177` | (a) **RERENDER 分支没有对位**：`protocol.py:355-361` 遇到危机标记一律 ABORT，没有任何测试覆盖"危机下改为重渲染"；(b) **"最终语气转换为关心"不可断言**：唯一痕迹是 `protocol.py:360` 的 note 字符串与 `runtime/README.md:433` 的散文声明，实际措辞由宿主主 LLM 在 Runtime 之外产生 |

---

## 4. §77–§79 表 / 字段 / 版本规则差异

### 4.1 §77 的 18 张建议表 vs `db.py` 实际 schema

| 文档表名 | 实现 | 状态 |
|---|---|---|
| `raw_events` | `raw_events`（`db.py:73`） | 一致 |
| `runtime_state` | `runtime_state`（:47） | 一致 |
| `working_situation_items` | `working_situation_items`（:249） | 一致 |
| `interpretation_versions` | `interpretation_versions`（:117） | 一致 |
| `emotion_events` | `active_emotion_events`（:191） | 改名（投影语义更准确） |
| `emotion_explanations` | `emotion_explanations`（:205） | 一致 |
| `unfinished_matters` | `unfinished_matters`（:232） | 一致 |
| `boundaries` | `boundaries`（:216） | 一致 |
| `memory_candidates` | `memory_candidates`（:265） | 一致 |
| `memories` | `memories`（:280） | 一致 |
| **`memory_embeddings`** | **不存在** | **缺表**（与 §75 一致：没有 embedding） |
| `activated_memories` | `activated_memories`（:297） | 一致 |
| `interaction_observations` | `interaction_observations`（:142） | 一致 |
| `user_model_global` | `user_model_params`（:307），`scope` 主键 | 合并为一张表 |
| `user_model_contextual` | 无独立表；只能靠 `scope` 值表达，生产代码只写过 `global`（`projections.py::UserModelProjection.GLOBAL_SCOPE:1753`，无 contextual 写入方） | **名存实无** |
| `candidate_intents` | `candidate_intents`（:318） | 一致 |
| `action_attempts` | `action_attempts`（:342） | 一致 |
| `background_tasks` | `background_tasks`（:158） | 一致 |

反向（实现有、文档 §77 未列）8 张：`schema_meta`（:39）、`event_semantics`（:97，补丁 v0.2 的 `unresolved` 承载表）、`reappraisals`（:132，文档在 §66.1 以 `reappraisal_events` 之名提到）、`outbox`（:171，投递队列）、`attempt_events`（:361，文档在 §66.1 以 `action_attempt_events` 之名提到）。另有 §66.2 的名称差异：`working_situation`→`working_situation_items`、`active_boundaries`→`boundaries`、`user_model_current`→`user_model_params`。

### 4.2 §78 表设计原则

- 高频排序/过滤/时间列全部是普通列并建索引（`db.py:87-89`、`:114`、`:130`、`:188`、`:247`、`:263`、`:295`、`:340`、`:359`、`:371`），**符合**文档要求。
- JSON 只出现在白名单列（`db.py::JSON_COLUMNS:388-408`），承载扩展 metadata、模型原始输出与不透明列表（`source_event_ids`、`*_json`），**符合**"不要所有东西都塞 JSONB"的意图。
- **PG 侧有意偏离**：`db_postgres.py:12-19` 明确所有 JSON 存 `TEXT` 由 Python 解析、时间存 ISO-8601 文本、类型沿用共享 DDL（不用 `jsonb`/`timestamptz`），理由是"两个后端不应在别处看不见的地方分叉"。因此"JSONB 适合…"这条 PG 专属建议**未被采纳**（属于有意设计，不是遗漏）。

### 4.3 §79 版本字段

| 字段 | 落点 | 覆盖情况 |
|---|---|---|
| `runtime_version` | `raw_events.runtime_version`（`db.py:83`，默认 0）、`attempt_events.runtime_version`（:367） | 覆盖；`event_semantics` 用 `version`（:109）命名不同 |
| `source_version` | `interpretation_versions.source_version`（:125） | 覆盖（仅解释表） |
| `source_event_ids` | `raw_events`/`interpretation_versions`/`reappraisals`/`interaction_observations`/`background_tasks`/`memory_candidates`/`memories`/`unfinished_matters` | 覆盖面最广 |
| `updated_at` | 投影表普遍有（`runtime_state`/`working_situation_items`/`memories`/`memory_candidates`/`candidate_intents`/`action_attempts`/`user_model_params`/`activated_memories`/`event_semantics`） | **追加型表没有**：`raw_events`、`reappraisals`、`interpretation_versions`、`interaction_observations`、`attempt_events`、`background_tasks` 只有 `created_at`（追加型可接受，但不是文档"核心可带"的统一口径） |
| `interpretation_version` | `interpretation_versions.interpretation_version`（:121） | 覆盖 |
| `supersedes_id` | `interpretation_versions.supersedes_id`（:122） | 覆盖 |
| `confidence` | `interpretation_versions.confidence`（:124），另见 `memories`、`memory_candidates`、`candidate_intents`、`interaction_observations.semantic_confidence` | 覆盖 |

**§77–§79 结论**：表名以"近名/合并"为主（15/18），真正缺的是 `memory_embeddings`；`user_model_contextual` 只有列可表达、没有生产写入方；§79 的七个字段名都有落点但缺少统一规则（`runtime_version`/`updated_at` 的分布不均）。

### 4.4 PostgreSQL 后端（`db_postgres.py`）对 §77–§79 的落实

- 后端存在且被真实选中：`db.py::open_database:543`（:557-562）在 `storage.dsn` 非空时返回 `PostgresDatabase`；`config.py::StorageConfig.dsn:101` 与 `::is_postgres:108` 提供开关；`pyproject.toml` 新增 `postgres` 可选依赖（工作区改动）。
- 文档的 PG 建议（表清单、版本字段）在 PG 上**逐字复用 SQLite 的 DDL**：`db_postgres.py::PostgresDatabase.migrate:581` 直接执行 `db.SCHEMA_STATEMENTS`，`ADDED_COLUMNS` 也复用（:329）。因此 §77/§79 的差异在 PG 上完全一致（包括缺 `memory_embeddings`）。
- 单写者语义在 PG 上用事务级 advisory lock 实现：`WRITER_LOCK_KEY:99`、`_begin:497`（:517 `pg_advisory_xact_lock`）；这与 §62.1 的单写者原则在 PG 上等价成立。
- **未移植的 SQLite-only SQL 会让 PG 部署在半路失败**（模块 docstring `db_postgres.py:38-42` 自述，我在当前工作区复核仍存在）：`eventlog.py:399` 的 `ORDER BY … rowid`、`projections.py:341` 的 `LIMIT -1 OFFSET`、`maintenance.py` 的 `PRAGMA journal_mode:203`/`PRAGMA synchronous:210`/`PRAGMA wal_checkpoint:265`/`PRAGMA integrity_check:329`；另有 `runtime.py:744` 只捕获 `sqlite3.IntegrityError`（PG 的重复键是 psycopg 的 `UniqueViolation`），该分支是"并发写者抢先插入同一 `event_id`"的幂等兜底，在 PG 上会直接抛出。`projections.py:1317` 的注释显示那里依赖 `rowid` 的 tie-break 已被去掉（并发修复进行中），说明这类移植工作仍在推进。

---

## 5. §82 权威归属逐条映射（补充证据）

| 文档归属 | 实现落点 |
|---|---|
| 原始事件权威 → 不可变事件日志 | `eventlog.py::EventLog`（只 append；`raw_events` 表 `db.py:73`） |
| 工作局势权威 → 当前工作投影 | `projections.py::SituationProjection:250`（fact / inference 两类，`runtime.py:908-928`） |
| 人格动力学权威 → 价值观参数 | `typing.py::ValueProfile:277`（`config.py::RuntimeConfig.values:382`），由 `motivation.py::target_drives:386` 消费 |
| 情绪状态权威 → Runtime 情绪引擎 | `emotion.py`（`tick_emotions`、`mood_relax`；`runtime.py::_apply_time_passage:579`） |
| 情绪解释权威 → 2B 解释器（仅解释不改状态） | `emotion.py::EmotionExplainer:451`（provider 可选，模板兜底 `_render_template:739`）；写入只到解释缓存 `reducer.py::_apply_explanation:537` |
| 用户认识权威 → 用户交互模型 + 证据 | `user_model.py::UserInteractionModel`（`observe:571`）、`interaction_observations`（`db.py:142`） |
| 历史索引权威 → 长期记忆 | `memory.py::MemoryProjection`/`MemoryStore`（⚠ 默认部署下无巩固调用方，见 §6 缺陷 1） |
| 想做什么 → 候选意图池 | `candidate.py` + `pool.py`（`candidate_intents` `db.py:318`） |
| 做不做 → 动机博弈层 | `motivation.py::decide:560`（危险率 :269、softmax :690、沉默效用 :771） |
| 边界与许可 → 边界状态机 | `boundaries.py` + `authorize.py`（`runtime.py:767-804` 每轮重算） |
| 怎么说 → 主 LLM | 宿主插件 `astrbot_plugin_companion_runtime\main.py:430-486`（Runtime 不生成文本） |

## 6. §96 完成度逐项核对（补充证据）

| 文档 ✓ 项 | 核对结果 |
|---|---|
| 价值观参数 / 工作局势 / 情绪评价 / 情绪动力学 / 情绪解释 / 未尽之事 / 记忆形成 / 用户交互模型 / 候选意图生成 / 冲动节制压力 / 动机博弈 / 内源主动 / 调度思想 / 并发重协调 / 协议层 | 成立（落点见 §1、§5） |
| **长期记忆** | **不成立**：`memories` 只能由 `memory.py::consolidate:283` 写入，而它只被 `reducer.py:511` 调用，前提是外部提交 `memory_summary` proposal——仓库内没有派发方，CLI 也没有巩固命令 |
| **RAG / 激活记忆** | **不成立（默认部署）**：`memory.py::MemoryStore.retrieve:602`/`activate:714` 读的是已巩固记忆，没有巩固就没有召回，激活池恒空 |
| 剩余清单："数据库 Schema 细化 / 接口 Schema 定义 / 任务队列实现 / 参数标定 / RAG 模型选型 / 性能压测 / 情景测试 / 真实部署" | "情景测试"已完成（§87 7/7）；"任务队列实现"部分完成（outbox 队列可用，6 类后台任务无派发方）；"RAG 模型选型"未做（词法）；"性能压测"有 `scripts/runtime_bench.py`；"真实部署"有 `Dockerfile`/`docker-compose.yml` + `framework/` 外部实验台 |

---

## 7. 必须补的（按影响排序，每条给最小下一步）

1. **记忆巩固没有任何调用方**（影响：长期记忆/激活池/RAG/§94"人格连续性"的"长期记忆"一项全部空转）
   最小下一步：在 `runtime.py::endogenous_round` 的 P2 段（`:1129` 事务内）加一个 `if memory_module.needs_consolidation(...)` 分支调用 `memory_module.consolidate(...)` 并把结果写进 `EndogenousOutcome`；或退一步，给 `cli.py` 加 `companion-runtime consolidate` 并在 `cmd_serve:259` 的 `maintenance_loop` 里按 `config.memory.consolidation_interval_seconds` 调用它。
2. **§86.4：9 个写库入口不 `lazy_tick`**（影响：这些入口的决策读到的是滞后整段 elapsed 的 drive）
   最小下一步：在 `api.py:240/426/449/467/643/732/765/813/833` 与 `api_v1.py:1556` 的处理函数首行加 `runtime.lazy_tick(...)`（更稳的做法是把 tick 收进 `Reducer.process_proposal` 与 `Reducer.mark_delivered` 的写事务开头）；把 `test_invariant_4:582` 改成参数化，对每个入口断言 tick 被调用。
3. **6/7 类后台任务没有派发方**（影响：§63/§65/§89 Phase 5/6/8 与"强 API"名义存在、实际不动）
   最小下一步：二选一并写进契约——(a) 在文档与 README 明确"这些任务由外部适配器派发"；(b) 给 `EMOTION_EXPLAIN`/`USER_MODEL_SUMMARY`/`MEMORY_SUMMARY` 接一个真实派发点（例如 `endogenous_round` 的 P3 段）。
4. **§84 的"候选池不足→强 API 刷新"是规则生成**（影响：候选池永远是规则产物，`CANDIDATE_GEN` 的敏感度/重挂接地策略无处生效）
   最小下一步：在 `candidate.py::should_refresh:405` 命中且 `semantic_provider.available()` 时派发一条 `CANDIDATE_GEN` proposal；若决定不做，就把 §84 与 README 的口径改成"候选由本地规则生成"。
5. **§67 重估不派生下游状态**（影响：`reappraisals` 只是日志，"后来想明白→愧疚/修复"链路缺一环）
   最小下一步：在 `reducer.py::_apply_reinterpretation:684` 之后，把重估的方向/强度交给 `emotion.apply_new_emotion_events` 生成一条粗粒度情绪事件；或明确写下"重估只记历史、下游靠同一 bundle 的兄弟操作"的约定。
6. **§60 屏障恢复是定时而非"确认安全"**（影响：文档口径与实现不一致）
   最小下一步：要么把 `foreground_pause_until` 与"本轮是否新增边界"绑定（有新增则暂停至边界到期），要么把这句文档改成"定时屏障 + 同步边界规则"。
7. **PG 后端在 4 类 SQLite-only SQL 上不可用，且开关无守卫**（影响：`storage.dsn` 一开就会在健康检查/维护/事件读取上失败）
   最小下一步：逐个替换 `eventlog.py:399` 的 `rowid`、`projections.py:341` 的 `LIMIT -1 OFFSET`、`maintenance.py` 的 PRAGMA 系列（按后端分支），并把 `runtime.py:744` 的 `except sqlite3.IntegrityError` 换成后端无关的冲突异常（由 `db_base` 暴露）；在 `open_database:543` 加一条启动期"该后端尚未完整移植"的显式告警或在文档标注实验性。
8. **§77 表名/缺表无对照文档**（影响：运维照文档建表会对不上）
   最小下一步：在 `db.py` 模块 docstring 或 `runtime/README.md` 加一张"文档名 → 实现名"对照（含 `memory_embeddings` 未实现、`user_model_params.scope` 取代 global/contextual）。
9. **遗留无消费者字段 `TaskConfig.merge_window_seconds:332`**（影响：误导后来者以为 §61 的合并已实现）
   最小下一步：删除该字段，或实现"未开始的同类分析合并"。
10. **§55 优先级枚举零赋值**（影响：读代码的人以为存在 P0/P2/P3 调度）
    最小下一步：要么在真实派发点赋 `P0_FOREGROUND`/`P2_ENDOGENOUS`/`P3_MAINTENANCE`，要么把枚举缩到实际使用的值并在 docstring 说明四类是"代码路径"而非"队列优先级"。

## 8. 可以永远不做的

- §74 全部（量化档位、上下文长度、单线程/nice/cgroup、常驻、swap）——补丁已废；弱 VPS 现在只需 Runtime + 存储 + 检索（`README.md:294-320`）。
- §88 Level 1 / Level 2 的本地 1B~2B 档位——同上；`SemanticProvider` 端口已保证"接口一致"这一真正的要求。
- §61 的 2B 单 Worker 队列与合并——除"合并"这一想法外，其余随本地路线废弃。
- §75 的 embedding sidecar 与持久化向量——词法兜底是当前唯一路径且已在 `README.md:434`、`runtime/docs/PATCH_V0.2_MAPPING.md`（第二节 #7）记录为有意降级；补丁 §29 也只说"可选"。
- §91 未来关系模型——文档自己标注不属于 MVP 阻塞项。
- §89 Phase 8 的 CPU 优先级 / 常驻巩固 worker——前者随本地模型废弃，后者与 §76 的"P3 可暂停"一样属于长期演进；prometheus 指标同理。
- §90 明确禁止的东西（多 Agent 自我讨论、每轮大模型反思、一功能一模型、"爱情值"核心状态）——不做才是对的。

## 9. 审计中发现的实际缺陷（只报不改）

1. **记忆永远无法形成（默认部署）**：`memories` 的唯一写入者是 `memory.py::consolidate:283`，其唯一调用者是 `reducer.py:511`（需外部 `memory_summary` proposal）；`memory.py::needs_consolidation:853` 无任何调用方，CLI 无巩固命令，插件不提交 proposal。后果：`/memories` 长期为空、`activated_memories` 恒空、候选的 `memory:` 来源永不出现、`runtime/README.md:387` 描述的数据流断在"后台巩固"一步。`runtime/README.md:1373` 承认"没有常驻 worker"，但没有承认"仓库内也没有任何调用方"。
2. **调度器承诺了一次不会发生的唤醒**：`scheduler.py::_maintenance_due:115`（:117-123）用待巩固候选算出 maintenance 锚点并参与 `min(...)`，但被它唤醒的 `endogenous_round`（`runtime.py:1071-1210`）从不做巩固——该锚点目前只改变睡眠时长，不触发任何维护工作。
3. **`test_scenario_4_busy_user_does_not_lower_acceptance:229` 是恒真断言**：该流程没有主动消息被发出 → 没有观察进入用户模型 → 前后两次 `predict` 完全相同（我在内存库复跑：`abs_diff = 0.0`，`observations = 0`）。它给人一种"场景 4 已覆盖"的错觉，实际该场景在 Runtime 层没有流程级测试。
4. **`test_scenario_5:269` 是恒真表达式**：`assert second.relation_signal if hasattr(second, "relation_signal") else True`，而 `runtime.py::MessageOutcome:133-170` 没有 `relation_signal` 字段，因此永远为真——"这句话是否被识别为关系信号"实际未被断言。
5. **§86.4 的 9 个写库入口不推进时间**：`api.py:240`（非 user_message 事件）、`api.py:426/449/467`（outbox 领取/确认/退回）、`api.py:643`（proposal）、`api.py:732`（观察）、`api.py:765`（候选操作）、`api.py:813/833`（未尽之事）、`api_v1.py:1556`（投递回执）。典型后果：长时间静默后到达的投递回执会在 `mark_delivered:1586` 里计费并刷新 `last_contact_at`，但 `approach_impulse/pressure` 仍是旧的；紧随其后的 `/schedule`、`/authorize`、`/proposals` 都会基于过期 drive 判定（时间最终不会被吞掉——`lazy_tick` 是单调的，下一次 tick 会补算整段 elapsed——但那一刻的决策是旧的）。
6. **PG 开关会把人带进已知不完整的分支**：`db.py::open_database:557` 只要 `dsn` 非空就选 `PostgresDatabase`，而该后端自述有 4 类未移植 SQL（`db_postgres.py:38-42`），实际后果是 `GET /health`（`maintenance.journal_mode:203`）、`verify`（:329）、`checkpoint`（:265）、`eventlog.read`（`eventlog.py:399` 的 rowid 排序）、`working_situation.prune`（`projections.py:341`）在 PG 上失败，且 `runtime.py:744` 捕获的是 `sqlite3.IntegrityError`（PG 重复键异常类型不同，会向上抛）。没有任何启动期检查会阻止这条路。
7. **`raw_events` 的不可修改性没有 DB 层强制**：`db.py` 无 TRIGGER、无只读视图，`EventLog` 只是"不提供 update/delete"（`tests/test_integration_scenarios.py:547` 断言的是方法名不存在）。任何拿到 `db` 句柄的代码都能 `UPDATE raw_events`；审计中未发现有人这么做，但机制上不设防（`scripts/e2e_resilience_simulation.py` 用重复 `event_id` + 不同文本重放来验证幂等，属于最接近篡改尝试的检查）。
8. **测试强度的其他缺口（非代码缺陷）**：`test_invariant_2:550` 不验证"inference→fact 的升级被拒"；`test_invariant_6:619` 只查 `mood_valence` 一个轴；`test_invariant_10:711` 只是字符串扫描（真正的机制断言在插件套件 `test_plugin_integration.py:384` 的 `_no_save`）；语义 provider 没有写权限内省（`test_invariant_3:562` 只做行为对比）。

---

## 10. 判定统计（共 45 行；§54–§97 为 44 节，其中 §62 拆成 §62.1/§62.2 两行）

| 判定 | 行数 | 章节 |
|---|---|---|
| 已实现 | 20 | §57 §58 §59 §60 §62.1 §64 §68 §69 §70 §71 §72 §73 §78 §79 §80 §81 §82 §85 §87 §90 |
| 部分实现 | 19 | §54 §55 §56 §62.2 §63 §65 §66 §67 §75 §76 §77 §83 §84 §86 §88 §89 §93 §94 §96 |
| 未实现 | 1 | §91 |
| 被补丁取代 | 2 | §61 §74 |
| 文档·约定 | 3 | §92 §95 §97 |
| **合计** | **45** | §54–§97（44 节，§62 拆为 §62.1/§62.2 两行） |
